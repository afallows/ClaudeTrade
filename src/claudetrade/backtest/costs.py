"""Transaction-cost model shared by the backtester and (eventually) paper trading.

Every dollar figure returned here is a *modelling assumption*, not a fact. The
defaults are conservative-but-plausible for a mid/large-cap US equity swing
strategy; they will be wrong for illiquid names, and they will drift as real
fee schedules change. Concretely:

* SEC Section 31 fees and the FINRA Trading Activity Fee are statutory rates
  that are revised periodically (SEC's rate is set annually; FINRA's is set by
  FINRA rule). The constants in ``CostModelConfig`` must be refreshed against
  the current published schedule -- do not assume the shipped defaults are
  current.
* The slippage model uses a **square-root market-impact form**
  (``impact ~ sqrt(participation)``) rather than a linear one. This follows the
  empirical microstructure literature (e.g. Almgren et al., "Direct Estimation
  of Equity Market Impact"): impact grows sub-linearly in order size relative
  to available liquidity, so a linear model understates the cost of small
  orders relative to large ones and overstates it for very large ones. Getting
  this wrong in the *optimistic* direction (underestimating costs) is exactly
  the kind of assumption that manufactures a flattering backtest, so the
  sub-linear, not-too-forgiving form is used deliberately.
* This module holds no backtest-specific state (no calendar, no portfolio, no
  clock) precisely so the same ``CostModel`` instance can be reused unchanged
  by a paper-trading broker adapter -- a cost model that is only honest in the
  backtest is not honest at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from claudetrade.config import CostModelConfig

#: Which side of the tape an order sits on. Regulatory fees (SEC 31, FINRA TAF)
#: apply only to *sales* -- which includes both a long position's closing sell
#: and a short position's opening sell-short. "buy" covers a long's opening
#: purchase and a short's closing buy-to-cover.
OrderSide = Literal["buy", "sell"]


@dataclass(slots=True)
class CostBreakdown:
    """Itemised cost of one fill, in dollars (not per-share)."""

    commission: float = 0.0
    regulatory_fees: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0
    borrow: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.regulatory_fees + self.spread + self.slippage + self.borrow


@dataclass(slots=True)
class CostModel:
    """Pure functions of price/size/volume -- no mutable state.

    Constructed once from ``CostModelConfig`` and shared by every consumer
    (backtest engine, execution simulator, and any future paper/live broker
    adapter) so cost assumptions cannot silently diverge between contexts.
    """

    config: CostModelConfig

    # --- individual cost components ---------------------------------------

    def commission(self, shares: int, price: float) -> float:
        """Per-share commission plus a flat per-trade fee, floored at a minimum.

        Many discount brokers charge $0 today; the knobs exist for brokers,
        share classes or account tiers that still charge per-share/per-ticket.
        """
        if shares <= 0:
            return 0.0
        raw = shares * self.config.commission_per_share + self.config.commission_per_trade
        return max(raw, self.config.commission_min) if raw > 0 else 0.0

    def regulatory_fees(self, side: OrderSide, shares: int, principal: float) -> float:
        """SEC Section 31 fee + FINRA TAF, sales only.

        Both rates are revised periodically by their respective regulators and
        are NOT indexed automatically here -- ``CostModelConfig.sec_fee_rate``
        and ``taf_per_share`` must be updated by hand against the current
        published schedule, or reported costs will silently drift stale.
        """
        if side != "sell" or shares <= 0:
            return 0.0
        sec_fee = max(0.0, principal) * self.config.sec_fee_rate
        taf = min(shares * self.config.taf_per_share, self.config.taf_max_per_trade)
        return sec_fee + taf

    def spread_cost(self, price: float) -> float:
        """Half-spread, in dollars per share, paid on both entry and exit.

        Modelled as a fixed number of basis points of price rather than a
        quoted bid/ask (which the daily-bar data layer does not supply).
        """
        return price * (self.config.half_spread_bps / 10_000.0)

    def slippage(
        self,
        price: float,
        shares: int,
        bar_volume: float,
        is_gap: bool = False,
    ) -> float:
        """Execution slippage in dollars per share.

        ``base_slippage_bps`` models the ordinary cost of walking the book;
        the size-impact term scales with the square root of participation
        (shares filled / bar volume) -- see the module docstring for why a
        square-root form is used instead of a linear one. ``is_gap`` adds a
        further fixed penalty for orders that fill through a gap, where the
        realised price is inherently less predictable than a same-direction
        continuous move.
        """
        if price <= 0 or shares <= 0:
            return 0.0
        base = price * (self.config.base_slippage_bps / 10_000.0)
        participation = 0.0
        if bar_volume and bar_volume > 0:
            participation = min(1.0, shares / bar_volume)
        impact = price * (self.config.impact_coefficient_bps / 10_000.0) * math.sqrt(participation)
        gap_extra = price * (self.config.gap_slippage_bps / 10_000.0) if is_gap else 0.0
        return base + impact + gap_extra

    def borrow_cost(self, notional: float, days: float) -> float:
        """Stock-borrow cost for a short position, accrued over ``days``.

        A flat annualised rate applied to calendar days on the *notional at
        risk*; real borrow rates vary by name (hard-to-borrow names can cost
        many multiples of this), so this is a floor, not a ceiling, on the true
        cost of holding a short.
        """
        if notional <= 0 or days <= 0:
            return 0.0
        return abs(notional) * (self.config.short_borrow_annual_pct / 100.0) * (days / 365.0)

    # --- combined helpers ---------------------------------------------------

    def total_entry_cost(
        self,
        *,
        side: OrderSide,
        shares: int,
        price: float,
        bar_volume: float,
        is_gap: bool = False,
    ) -> CostBreakdown:
        """Itemised cost of opening a position with this fill.

        Borrow cost is intentionally excluded here: it accrues over the
        *holding period*, which is unknown at entry, and is charged instead by
        the portfolio on a daily basis (see ``backtest.portfolio``).
        """
        principal = shares * price
        return CostBreakdown(
            commission=self.commission(shares, price),
            regulatory_fees=self.regulatory_fees(side, shares, principal),
            spread=self.spread_cost(price) * shares,
            slippage=self.slippage(price, shares, bar_volume, is_gap) * shares,
        )

    def total_exit_cost(
        self,
        *,
        side: OrderSide,
        shares: int,
        price: float,
        bar_volume: float,
        is_gap: bool = False,
    ) -> CostBreakdown:
        """Itemised cost of closing (all or part of) a position with this fill."""
        principal = shares * price
        return CostBreakdown(
            commission=self.commission(shares, price),
            regulatory_fees=self.regulatory_fees(side, shares, principal),
            spread=self.spread_cost(price) * shares,
            slippage=self.slippage(price, shares, bar_volume, is_gap) * shares,
        )


def side_for(direction_sign: int, *, is_entry: bool) -> OrderSide:
    """Map a position direction/leg to a regulatory order side.

    A long's entry is a buy and its exit is a sell. A short's entry is a
    sell-short (a "sell" for fee purposes) and its exit is a buy-to-cover.
    """
    is_sell = (direction_sign > 0) != is_entry
    return "sell" if is_sell else "buy"
