"""Risk-based position sizing.

The base rule is the standard one::

    shares = allowed_dollar_risk / |entry - stop|

That figure is then reduced -- never increased -- by a series of caps:

* **Position value cap** -- no single name exceeds ``max_position_size_pct``.
* **Liquidity cap** -- the order may not exceed ``max_pct_of_adv`` of the name's
  average daily dollar volume. A position that cannot be exited in a day is not
  a position, it is a hostage.
* **Buying-power cap** -- long notional cannot exceed available cash.
* **Portfolio-heat cap** -- the *incremental* risk must fit inside the remaining
  heat budget, which is what stops eight simultaneous "small" trades adding up
  to an account-threatening loss.

Every reduction is recorded in ``SizingResult.constraints`` so the UI can
explain why a signal is smaller than the headline risk setting implies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from claudetrade.config import AppConfig
from claudetrade.domain import Direction


@dataclass(slots=True)
class SizingResult:
    """Outcome of sizing one prospective position."""

    shares: int
    notional_usd: float
    dollar_risk: float
    risk_per_share: float
    risk_pct_of_account: float
    binding_constraint: str = "risk_budget"
    constraints: list[str] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str = ""

    @property
    def is_tradable(self) -> bool:
        return self.shares > 0 and not self.rejected

    def reason(self) -> str:
        """Human-readable explanation of why the size is what it is.

        Joins the rejection reason (when the request was structurally invalid or
        every cap reduced it to zero) with each cap that reduced the position.
        This is what the UI shows beneath a signal so a user is never left
        guessing why a trade is smaller than the headline risk setting implies.
        """
        parts = [p for p in (self.rejection_reason, *self.constraints) if p]
        return "; ".join(parts) if parts else f"sized by {self.binding_constraint}"


def size_position(
    *,
    config: AppConfig,
    direction: Direction,
    entry_price: float,
    stop_price: float,
    account_equity: float | None = None,
    available_cash: float | None = None,
    avg_dollar_volume: float | None = None,
    open_heat_pct: float = 0.0,
    risk_multiplier: float = 1.0,
) -> SizingResult:
    """Compute a position size that satisfies every configured limit.

    Args:
        config: Application configuration supplying the risk limits.
        direction: Long or short. ``FLAT`` is rejected.
        entry_price: Reference entry price (mid of the entry zone).
        stop_price: Initial protective stop.
        account_equity: Current equity; defaults to the configured account size.
        available_cash: Cash available for a long purchase; defaults to equity.
        avg_dollar_volume: 20-day average dollar volume, used for the liquidity
            cap. When ``None`` the liquidity cap is skipped and noted.
        open_heat_pct: Risk already committed across open positions, as a
            percentage of equity.
        risk_multiplier: Regime or strategy adjustment in ``(0, 1]``. Values
            above 1 are clamped -- nothing may size *up* beyond the configured
            per-trade risk.

    Returns:
        A ``SizingResult``. A zero-share result is normal (not an error) when
        the caps bite; ``rejected`` marks a structurally invalid request.
    """
    risk_cfg = config.risk
    constraints: list[str] = []

    if direction is Direction.FLAT:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, rejected=True, rejection_reason="direction=flat")
    if entry_price <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, rejected=True, rejection_reason="entry<=0")

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return SizingResult(
            0, 0.0, 0.0, 0.0, 0.0, rejected=True, rejection_reason="stop equals entry (zero risk)"
        )
    # A stop on the wrong side would make risk_per_share look fine while the
    # trade is actually unprotected.
    if direction is Direction.LONG and stop_price >= entry_price:
        return SizingResult(
            0, 0.0, 0.0, 0.0, risk_per_share, rejected=True,
            rejection_reason="long stop is not below entry",
        )
    if direction is Direction.SHORT and stop_price <= entry_price:
        return SizingResult(
            0, 0.0, 0.0, 0.0, risk_per_share, rejected=True,
            rejection_reason="short stop is not above entry",
        )

    equity = account_equity if account_equity is not None else risk_cfg.account_size_usd
    if equity <= 0:
        return SizingResult(
            0, 0.0, 0.0, risk_per_share, 0.0, rejected=True, rejection_reason="equity<=0"
        )
    cash = available_cash if available_cash is not None else equity

    multiplier = min(max(risk_multiplier, 0.0), 1.0)
    if risk_multiplier > 1.0:
        constraints.append("risk_multiplier clamped to 1.0")

    # 1. Base risk budget.
    risk_budget = equity * (risk_cfg.max_risk_per_trade_pct / 100.0) * multiplier

    # 2. Remaining portfolio heat.
    remaining_heat_pct = max(0.0, risk_cfg.max_portfolio_heat_pct - open_heat_pct)
    heat_budget = equity * (remaining_heat_pct / 100.0)
    if heat_budget < risk_budget:
        constraints.append(
            f"portfolio heat: {remaining_heat_pct:.2f}% of {risk_cfg.max_portfolio_heat_pct:.2f}% "
            "budget remaining"
        )
    allowed_risk = min(risk_budget, heat_budget)
    if allowed_risk <= 0:
        return SizingResult(
            0, 0.0, 0.0, risk_per_share, 0.0,
            binding_constraint="portfolio_heat",
            constraints=constraints,
            rejected=True,
            rejection_reason="portfolio heat budget is exhausted",
        )

    shares = math.floor(allowed_risk / risk_per_share)
    binding = "risk_budget" if allowed_risk == risk_budget else "portfolio_heat"

    # 3. Maximum position value.
    max_notional = equity * (risk_cfg.max_position_size_pct / 100.0)
    cap = math.floor(max_notional / entry_price)
    if cap < shares:
        shares, binding = cap, "max_position_size"
        constraints.append(f"position value capped at {risk_cfg.max_position_size_pct:.1f}% of equity")

    # 4. Liquidity: keep the order a small share of daily turnover.
    if avg_dollar_volume is not None and avg_dollar_volume > 0:
        max_liquidity_notional = avg_dollar_volume * (risk_cfg.max_pct_of_adv / 100.0)
        cap = math.floor(max_liquidity_notional / entry_price)
        if cap < shares:
            shares, binding = cap, "liquidity"
            constraints.append(
                f"liquidity capped at {risk_cfg.max_pct_of_adv:.1f}% of average daily dollar volume"
            )
    else:
        constraints.append("liquidity cap skipped: average dollar volume unknown")

    # 5. Buying power. Shorts consume margin rather than cash; the conservative
    #    approximation here charges the same notional against available funds.
    if cash > 0:
        cap = math.floor(cash / entry_price)
        if cap < shares:
            shares, binding = cap, "buying_power"
            constraints.append("capped by available cash")

    shares = max(0, shares)
    dollar_risk = shares * risk_per_share
    return SizingResult(
        shares=shares,
        notional_usd=shares * entry_price,
        dollar_risk=dollar_risk,
        risk_per_share=risk_per_share,
        risk_pct_of_account=100.0 * dollar_risk / equity if equity else 0.0,
        binding_constraint=binding,
        constraints=constraints,
        rejected=False,
        rejection_reason="" if shares > 0 else f"all caps reduced size to zero ({binding})",
    )
