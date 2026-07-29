"""Portfolio-level risk limits and the kill switch.

Sizing answers "how big?"; this module answers "at all?". Checks run against a
``PortfolioState`` snapshot and return a ``LimitCheck`` explaining every breach
rather than a bare boolean, so the operator can see which constraint bound.

The same code path is used by the backtester, the paper broker and (were one
configured) a live broker adapter. A limit that only exists in the backtest is
not a limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from claudetrade.config import AppConfig
from claudetrade.domain import Direction


class RiskLimitError(RuntimeError):
    """A hard risk limit refused an action."""


@dataclass(slots=True)
class OpenPosition:
    """Minimal view of an open position needed for limit arithmetic."""

    symbol: str
    direction: Direction
    shares: int
    entry_price: float
    stop_price: float
    sector: str = ""
    correlation_group: str = ""

    @property
    def notional(self) -> float:
        return abs(self.shares * self.entry_price)

    @property
    def open_risk(self) -> float:
        """Dollars still at risk to the current stop.

        A position whose stop has been trailed past entry has negative risk;
        it is floored at zero so a locked-in winner frees heat but never
        creates a negative-risk allowance.
        """
        per_share = (self.entry_price - self.stop_price) * self.direction.sign
        return max(0.0, per_share * self.shares)


@dataclass(slots=True)
class PortfolioState:
    """Snapshot used for limit checks."""

    equity: float
    cash: float
    positions: list[OpenPosition] = field(default_factory=list)
    realised_pnl_today: float = 0.0
    realised_pnl_week: float = 0.0
    kill_switch_engaged: bool = False

    @property
    def open_heat_pct(self) -> float:
        """Total open risk as a percentage of equity."""
        if self.equity <= 0:
            return 100.0
        return 100.0 * sum(p.open_risk for p in self.positions) / self.equity

    @property
    def gross_exposure_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        return 100.0 * sum(p.notional for p in self.positions) / self.equity

    def sector_exposure_pct(self, sector: str) -> float:
        if self.equity <= 0 or not sector:
            return 0.0
        total = sum(p.notional for p in self.positions if p.sector == sector)
        return 100.0 * total / self.equity

    def group_exposure_pct(self, group: str) -> float:
        if self.equity <= 0 or not group:
            return 0.0
        total = sum(p.notional for p in self.positions if p.correlation_group == group)
        return 100.0 * total / self.equity

    def holds(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)


@dataclass(slots=True)
class LimitCheck:
    """Result of vetting a prospective position."""

    allowed: bool
    breaches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Multiplier the caller should apply to size (1.0 = unrestricted).
    size_multiplier: float = 1.0

    def reason(self) -> str:
        return "; ".join(self.breaches) if self.breaches else ""


def check_new_position(
    *,
    config: AppConfig,
    state: PortfolioState,
    symbol: str,
    direction: Direction,
    notional: float,
    dollar_risk: float,
    sector: str = "",
    correlation_group: str = "",
) -> LimitCheck:
    """Vet a prospective position against every portfolio limit.

    Returns:
        ``LimitCheck.allowed`` is False when *any* hard limit is breached; the
        breach list names all of them, not just the first.
    """
    risk = config.risk
    breaches: list[str] = []
    warnings: list[str] = []

    if state.kill_switch_engaged or risk.kill_switch_engaged or config.trading.kill_switch_engaged:
        breaches.append("kill switch is engaged: no new positions are permitted")

    if state.equity <= 0:
        breaches.append("account equity is zero or negative")
        return LimitCheck(allowed=False, breaches=breaches)

    if direction is Direction.SHORT and not config.signals.allow_shorts:
        breaches.append("short selling is disabled in configuration")

    if state.holds(symbol):
        breaches.append(f"already holding {symbol}: pyramiding is not enabled")

    if len(state.positions) >= risk.max_concurrent_positions:
        breaches.append(
            f"concurrent position limit reached ({len(state.positions)}/"
            f"{risk.max_concurrent_positions})"
        )

    projected_heat = state.open_heat_pct + 100.0 * dollar_risk / state.equity
    if projected_heat > risk.max_portfolio_heat_pct + 1e-9:
        breaches.append(
            f"portfolio heat would reach {projected_heat:.2f}%, above the "
            f"{risk.max_portfolio_heat_pct:.2f}% limit"
        )

    position_pct = 100.0 * notional / state.equity
    if position_pct > risk.max_position_size_pct + 1e-9:
        breaches.append(
            f"position would be {position_pct:.1f}% of equity, above the "
            f"{risk.max_position_size_pct:.1f}% limit"
        )

    if sector:
        projected_sector = state.sector_exposure_pct(sector) + position_pct
        if projected_sector > risk.max_sector_exposure_pct + 1e-9:
            breaches.append(
                f"{sector} exposure would reach {projected_sector:.1f}%, above the "
                f"{risk.max_sector_exposure_pct:.1f}% limit"
            )

    if correlation_group:
        projected_group = state.group_exposure_pct(correlation_group) + position_pct
        if projected_group > risk.max_correlated_exposure_pct + 1e-9:
            breaches.append(
                f"correlated exposure ({correlation_group}) would reach "
                f"{projected_group:.1f}%, above the {risk.max_correlated_exposure_pct:.1f}% limit"
            )

    daily_loss_pct = -100.0 * state.realised_pnl_today / state.equity
    if daily_loss_pct >= risk.max_daily_loss_pct:
        breaches.append(
            f"daily loss limit hit ({daily_loss_pct:.2f}% >= {risk.max_daily_loss_pct:.2f}%): "
            "trading is halted for the session"
        )
    elif daily_loss_pct >= risk.max_daily_loss_pct * 0.75:
        warnings.append(f"approaching the daily loss limit ({daily_loss_pct:.2f}%)")

    weekly_loss_pct = -100.0 * state.realised_pnl_week / state.equity
    if weekly_loss_pct >= risk.max_weekly_loss_pct:
        breaches.append(
            f"weekly loss limit hit ({weekly_loss_pct:.2f}% >= {risk.max_weekly_loss_pct:.2f}%)"
        )

    if direction is Direction.LONG and notional > state.cash:
        breaches.append(f"insufficient cash: need ${notional:,.0f}, have ${state.cash:,.0f}")

    return LimitCheck(allowed=not breaches, breaches=breaches, warnings=warnings)


def remaining_heat_pct(config: AppConfig, state: PortfolioState) -> float:
    """Unused portfolio heat budget, in percentage points."""
    return max(0.0, config.risk.max_portfolio_heat_pct - state.open_heat_pct)


def engage_kill_switch(state: PortfolioState) -> PortfolioState:
    """Halt new entries immediately.

    Deliberately does *not* liquidate: force-selling into an unknown market is
    itself a risk decision and belongs to the operator, not to an automated
    guard. Existing stops remain active.
    """
    state.kill_switch_engaged = True
    return state
