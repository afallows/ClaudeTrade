"""Backtest account state: cash, open positions, exposure and risk bookkeeping.

``BacktestPortfolio`` is the single place that turns fills into cash-flow and
open positions into ``domain.Trade`` rows. It reuses ``risk.sizing`` and
``risk.limits`` unchanged -- the same functions a paper or live broker adapter
would call -- so a limit that only exists in the backtest is not a limit.

Multi-price exits (partial profit-taking, or a stop that unwinds over several
bars because of the participation cap) are reconciled into the single
``entry_price`` / ``exit_price`` / ``shares`` triple that ``domain.Trade``
exposes by using the *volume-weighted average exit price* across every exit
fill. That average is chosen deliberately: with ``shares`` set to the
position's original size, ``(avg_exit_price - entry_price) * shares * sign``
reproduces the exact sum of each partial fill's own P&L, so ``Trade.gross_pnl``
(a computed property we cannot override) stays correct even though the
position may have closed in several pieces at several prices.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from claudetrade.backtest.costs import CostModel, side_for
from claudetrade.backtest.execution import (
    EntryOrder,
    ExecutionSimulator,
    ExitDecision,
    FillResult,
    update_trailing_stop,
)
from claudetrade.config import AppConfig
from claudetrade.domain import Bar, Direction, ExitReason, Fill, MarketRegime, Trade
from claudetrade.risk.limits import LimitCheck, OpenPosition, PortfolioState, check_new_position
from claudetrade.risk.sizing import SizingResult, size_position

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Position:
    """A live open position and everything needed to manage and grade it.

    Implements ``execution.PositionView`` structurally (duck-typed; see that
    module for why it is a ``Protocol`` rather than a shared base class).
    """

    trade_id: str
    signal_id: str
    symbol: str
    strategy: str
    direction: Direction
    entry_session: dt.date
    entry_price: float
    initial_shares: int
    shares: int
    initial_stop_loss: float
    stop_loss: float
    targets: list[float]
    target_fractions: list[float]
    targets_hit: list[bool]
    trailing_stop_atr: float | None
    time_stop_session: dt.date
    initial_risk_per_share: float
    moving_average_exit_period: int | None = None
    pre_earnings_exit_days: int | None = None
    next_earnings_date: dt.date | None = None
    highest_close_since_entry: float = 0.0
    lowest_close_since_entry: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    sector: str = ""
    market_cap_bucket: str = ""
    days_to_earnings_at_entry: int | None = None
    confidence_at_entry: float = 0.0
    sentiment_source: str = "none"
    regime_at_entry: MarketRegime = MarketRegime.UNKNOWN
    fills: list[Fill] = field(default_factory=list)
    commission_total: float = 0.0
    fees_total: float = 0.0
    slippage_total: float = 0.0
    borrow_cost_total: float = 0.0
    #: Running volume-weighted exit accumulator; see module docstring.
    exit_notional_accum: float = 0.0
    exit_shares_accum: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return abs(self.shares * self.entry_price)

    @property
    def open_risk(self) -> float:
        per_share = (self.entry_price - self.stop_loss) * self.direction.sign
        return max(0.0, per_share * self.shares)

    def as_open_position(self) -> OpenPosition:
        """View used by ``risk.limits`` heat/exposure calculations."""
        return OpenPosition(
            symbol=self.symbol,
            direction=self.direction,
            shares=self.shares,
            entry_price=self.entry_price,
            stop_price=self.stop_loss,
            sector=self.sector,
        )


@dataclass(slots=True)
class EquityPoint:
    """One session's mark-to-market snapshot."""

    session: dt.date
    equity: float
    cash: float
    open_positions: int
    exposure_pct: float
    portfolio_heat_pct: float
    drawdown_pct: float
    sector_exposure: dict[str, float] = field(default_factory=dict)


def _normalise_target_fractions(targets: list[float], fractions: list[float]) -> list[float]:
    """Default to an equal split across targets when the plan left it blank."""
    if fractions:
        return list(fractions)
    if not targets:
        return []
    return [1.0 / len(targets)] * len(targets)


@dataclass(slots=True)
class BacktestPortfolio:
    """Cash, positions, exposure and risk state for one backtest run."""

    config: AppConfig
    cost_model: CostModel
    execution: ExecutionSimulator
    cash: float
    equity: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[Trade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    high_water_mark: float = 0.0
    kill_switch_engaged: bool = False
    #: Realised P&L keyed by calendar session, used for the daily/weekly loss
    #: limits in ``risk.limits.check_new_position``.
    _realised_pnl_by_session: dict[dt.date, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.equity <= 0:
            self.equity = self.cash
        if self.high_water_mark <= 0:
            self.high_water_mark = self.equity

    # --- state views --------------------------------------------------------

    def _realised_pnl_since(self, as_of: dt.date, lookback_days: int) -> float:
        start = as_of - dt.timedelta(days=lookback_days)
        return sum(
            pnl for session, pnl in self._realised_pnl_by_session.items() if start < session <= as_of
        )

    def portfolio_state(self, as_of_session: dt.date) -> PortfolioState:
        """Snapshot for ``risk.limits.check_new_position`` and sizing."""
        return PortfolioState(
            equity=self.equity,
            cash=self.cash,
            positions=[p.as_open_position() for p in self.positions.values()],
            realised_pnl_today=self._realised_pnl_by_session.get(as_of_session, 0.0),
            realised_pnl_week=self._realised_pnl_since(as_of_session, 7),
            kill_switch_engaged=self.kill_switch_engaged,
        )

    def open_heat_pct(self) -> float:
        if self.equity <= 0:
            return 100.0
        return 100.0 * sum(p.open_risk for p in self.positions.values()) / self.equity

    # --- sizing / risk integration -------------------------------------------

    def size_and_vet(
        self,
        *,
        symbol: str,
        direction: Direction,
        entry_price: float,
        stop_price: float,
        as_of_session: dt.date,
        avg_dollar_volume: float | None = None,
        sector: str = "",
        risk_multiplier: float = 1.0,
    ) -> tuple[SizingResult, LimitCheck]:
        """Size a prospective position and vet it against portfolio limits.

        Both calls use the *current* portfolio state, so ordering matters:
        callers evaluating several candidates in one session must call this
        (and then ``open_position``) one at a time, in rank order, so each
        later candidate sees the heat/exposure already committed by earlier
        ones.
        """
        state = self.portfolio_state(as_of_session)
        sizing = size_position(
            config=self.config,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            account_equity=self.equity,
            available_cash=self.cash,
            avg_dollar_volume=avg_dollar_volume,
            open_heat_pct=state.open_heat_pct,
            risk_multiplier=risk_multiplier,
        )
        if not sizing.is_tradable:
            return sizing, LimitCheck(allowed=False, breaches=[sizing.rejection_reason])
        limit_check = check_new_position(
            config=self.config,
            state=state,
            symbol=symbol,
            direction=direction,
            notional=sizing.notional_usd,
            dollar_risk=sizing.dollar_risk,
            sector=sector,
        )
        return sizing, limit_check

    # --- position lifecycle ---------------------------------------------

    def open_position(
        self,
        *,
        trade_id: str,
        signal_id: str,
        strategy: str,
        entry_order: EntryOrder,
        fill: FillResult,
        bar: Bar,
        stop_loss: float,
        targets: list[float],
        target_fractions: list[float],
        trailing_stop_atr: float | None,
        time_stop_session: dt.date,
        moving_average_exit_period: int | None = None,
        pre_earnings_exit_days: int | None = None,
        next_earnings_date: dt.date | None = None,
        sector: str = "",
        market_cap_bucket: str = "",
        days_to_earnings_at_entry: int | None = None,
        confidence_at_entry: float = 0.0,
        sentiment_source: str = "none",
        regime_at_entry: MarketRegime = MarketRegime.UNKNOWN,
    ) -> Position:
        """Record a filled entry as a new open position and move cash.

        ``fill`` is the ``ExecutionSimulator.try_fill_entry`` result; the
        caller (the engine) is responsible for having decided the order
        should be placed at all (sizing, risk limits already passed).

        Raises:
            ValueError: if ``entry_order.symbol`` already has an open
                position. ``self.positions`` is a plain dict keyed by symbol,
                so a second open would silently overwrite (not merge with)
                the first: the overwritten ``Position`` is never closed, so
                its entry cash debit/credit is never reversed and the ledger
                permanently diverges from the trade log by that position's
                notional. This must never happen with correct callers (the
                engine tracks working orders per symbol precisely so it never
                asks for a second concurrent entry on the same symbol) -- see
                ``BacktestEngine.run``'s ``pending_symbols`` guard -- so this
                is a fail-fast integrity check, not a normal control-flow
                path.
        """
        if entry_order.symbol in self.positions:
            existing = self.positions[entry_order.symbol]
            raise ValueError(
                f"cannot open {entry_order.symbol}: a position is already open "
                f"(trade_id={existing.trade_id}, entered {existing.entry_session}); "
                "opening a second one would silently overwrite it and orphan its "
                "entry cash in the ledger"
            )
        side = side_for(entry_order.direction.sign, is_entry=True)
        principal = fill.shares * fill.price
        commission = self.cost_model.commission(fill.shares, fill.price)
        fees = self.cost_model.regulatory_fees(side, fill.shares, principal)

        entry_fill = Fill(
            session=bar.session,
            price=fill.price,
            shares=fill.shares,
            commission=commission,
            fees=fees,
            slippage=fill.slippage_per_share * fill.shares,
            is_partial=fill.is_partial,
            note=fill.note,
        )

        # Cash moves opposite to direction: buying (long entry) consumes
        # cash; selling short *raises* cash (less fees/commission), which is
        # a simplification -- real short sales post the proceeds to margin,
        # not free cash, but the sizing/limit checks already treat short
        # notional the same as long notional, so this keeps the ledger
        # internally consistent without modelling margin explicitly.
        cash_delta = -entry_order.direction.sign * principal - commission - fees
        self.cash += cash_delta

        risk_per_share = abs(fill.price - stop_loss)
        position = Position(
            trade_id=trade_id,
            signal_id=signal_id,
            symbol=entry_order.symbol,
            strategy=strategy,
            direction=entry_order.direction,
            entry_session=bar.session,
            entry_price=fill.price,
            initial_shares=fill.shares,
            shares=fill.shares,
            initial_stop_loss=stop_loss,
            stop_loss=stop_loss,
            targets=list(targets),
            target_fractions=_normalise_target_fractions(targets, target_fractions),
            targets_hit=[False] * len(targets),
            trailing_stop_atr=trailing_stop_atr,
            time_stop_session=time_stop_session,
            initial_risk_per_share=risk_per_share,
            moving_average_exit_period=moving_average_exit_period,
            pre_earnings_exit_days=pre_earnings_exit_days,
            next_earnings_date=next_earnings_date,
            highest_close_since_entry=fill.price,
            lowest_close_since_entry=fill.price,
            sector=sector,
            market_cap_bucket=market_cap_bucket,
            days_to_earnings_at_entry=days_to_earnings_at_entry,
            confidence_at_entry=confidence_at_entry,
            sentiment_source=sentiment_source,
            regime_at_entry=regime_at_entry,
            fills=[entry_fill],
            commission_total=commission,
            fees_total=fees,
            slippage_total=entry_fill.slippage,
        )
        self.positions[entry_order.symbol] = position
        return position

    def process_bar_for_position(
        self,
        symbol: str,
        bar: Bar,
        *,
        atr_value: float | None = None,
        moving_average_value: float | None = None,
        force_close: bool = False,
        force_close_reason: ExitReason = ExitReason.END_OF_BACKTEST,
        force_close_price: float | None = None,
    ) -> Trade | None:
        """Advance one open position by one bar.

        Updates MFE/MAE, asks the execution simulator for an exit decision,
        applies it (cash + bookkeeping), and -- if the position remains open
        -- ratchets the ATR trailing stop from this now-completed bar. Returns
        the completed ``Trade`` if this bar fully closed the position, else
        ``None`` (including when the position only partially exited).
        """
        position = self.positions.get(symbol)
        if position is None:
            return None

        self._update_mfe_mae(position, bar)

        decision = self.execution.simulate_exit(
            position,
            bar,
            force_close=force_close,
            force_close_reason=force_close_reason,
            force_close_price=force_close_price,
            moving_average_value=moving_average_value,
        )

        trade: Trade | None = None
        if decision is not None:
            trade = self._apply_exit_decision(position, bar, decision)

        if trade is None and symbol in self.positions:
            # Position survived the bar (or only partially exited): ratchet
            # the trailing stop using this bar's now-completed close, ready
            # to be tested against tomorrow's bar. Never uses tomorrow's data.
            position.highest_close_since_entry = max(position.highest_close_since_entry, bar.close)
            position.lowest_close_since_entry = min(position.lowest_close_since_entry, bar.close)
            if position.trailing_stop_atr is not None and atr_value is not None:
                position.stop_loss = update_trailing_stop(position, atr_value)

        return trade

    def _update_mfe_mae(self, position: Position, bar: Bar) -> None:
        """Track maximum favourable/adverse excursion, in % and in R.

        Uses the bar's high/low so a position that touched a favourable or
        adverse extreme intrabar (without necessarily closing there) is still
        graded honestly -- MFE/MAE answer "how good/bad did it get", not "how
        did it close".
        """
        sign = position.direction.sign
        entry = position.entry_price
        if entry <= 0:
            return
        if sign > 0:
            favourable_px, adverse_px = bar.high, bar.low
        else:
            favourable_px, adverse_px = bar.low, bar.high
        favourable_pct = (favourable_px - entry) / entry * 100.0 * sign
        adverse_pct = (entry - adverse_px) / entry * 100.0 * sign
        position.mfe_pct = max(position.mfe_pct, favourable_pct)
        position.mae_pct = max(position.mae_pct, adverse_pct)
        if position.initial_risk_per_share > 0:
            favourable_r = (favourable_px - entry) * sign / position.initial_risk_per_share
            adverse_r = (entry - adverse_px) * sign / position.initial_risk_per_share
            position.mfe_r = max(position.mfe_r, favourable_r)
            position.mae_r = max(position.mae_r, adverse_r)

    def _apply_exit_decision(
        self, position: Position, bar: Bar, decision: ExitDecision
    ) -> Trade | None:
        """Apply one (possibly partial) exit fill: cash, fees, bookkeeping."""
        side = side_for(position.direction.sign, is_entry=False)
        principal = decision.shares * decision.price
        commission = self.cost_model.commission(decision.shares, decision.price)
        fees = self.cost_model.regulatory_fees(side, decision.shares, principal)

        exit_fill = Fill(
            session=bar.session,
            price=decision.price,
            shares=decision.shares,
            commission=commission,
            fees=fees,
            slippage=decision.slippage_per_share * decision.shares,
            is_partial=decision.remaining_shares > 0,
            note=f"{decision.reason.value}: {decision.note}".strip(": "),
        )
        position.fills.append(exit_fill)
        position.commission_total += commission
        position.fees_total += fees
        position.slippage_total += exit_fill.slippage
        position.exit_notional_accum += principal
        position.exit_shares_accum += decision.shares
        if decision.target_index is not None and decision.reason in (
            ExitReason.TARGET,
            ExitReason.PARTIAL_TARGET,
        ):
            position.targets_hit[decision.target_index] = True

        # Cash: closing a long is a sale (+cash); covering a short is a
        # purchase (-cash). Direction.sign is +1 long / -1 short.
        cash_delta = position.direction.sign * principal - commission - fees
        self.cash += cash_delta
        position.shares -= decision.shares

        if position.shares > 0:
            return None  # partial exit only; position remains open

        # Fully closed: accrue any outstanding short borrow cost, then build
        # the completed Trade using the volume-weighted average exit price
        # (see module docstring) so domain.Trade's computed P&L is exact even
        # though this position may have closed across several fills/bars.
        if position.direction is Direction.SHORT:
            holding_days = max(1, (bar.session - position.entry_session).days)
            borrow = self.cost_model.borrow_cost(position.notional, holding_days)
            position.borrow_cost_total += borrow
            self.cash -= borrow

        avg_exit_price = position.exit_notional_accum / max(1, position.exit_shares_accum)
        trade = Trade(
            trade_id=position.trade_id,
            signal_id=position.signal_id,
            symbol=position.symbol,
            strategy=position.strategy,
            direction=position.direction,
            entry_session=position.entry_session,
            entry_price=position.entry_price,
            shares=position.initial_shares,
            stop_loss=position.initial_stop_loss,
            targets=position.targets,
            exit_session=bar.session,
            exit_price=avg_exit_price,
            exit_reason=decision.reason,
            fills=position.fills,
            commission_total=position.commission_total,
            fees_total=position.fees_total,
            slippage_total=position.slippage_total,
            borrow_cost_total=position.borrow_cost_total,
            mfe_pct=position.mfe_pct,
            mae_pct=position.mae_pct,
            mfe_r=position.mfe_r,
            mae_r=position.mae_r,
            initial_risk_per_share=position.initial_risk_per_share,
            regime_at_entry=position.regime_at_entry,
            sector=position.sector,
            market_cap_bucket=position.market_cap_bucket,
            days_to_earnings_at_entry=position.days_to_earnings_at_entry,
            confidence_at_entry=position.confidence_at_entry,
            sentiment_source=position.sentiment_source,
            notes=position.notes,
        )
        self.closed_trades.append(trade)
        del self.positions[position.symbol]
        self._realised_pnl_by_session[bar.session] = (
            self._realised_pnl_by_session.get(bar.session, 0.0) + trade.net_pnl
        )
        return trade

    def accrue_daily_borrow_costs(self, as_of_session: dt.date) -> None:
        """Charge one day of stock-borrow cost against every open short.

        Called once per session by the engine for positions still open at
        end of day; the cost at final close additionally covers whatever
        fraction of the last day was not yet charged (see
        ``_apply_exit_decision``), so shorts are never under-charged simply
        because they closed intraday of their last session.
        """
        for position in self.positions.values():
            if position.direction is not Direction.SHORT:
                continue
            cost = self.cost_model.borrow_cost(position.notional, days=1)
            position.borrow_cost_total += cost
            self.cash -= cost

    # --- marking ------------------------------------------------------------

    def mark_to_market(self, session: dt.date, last_prices: dict[str, float]) -> EquityPoint:
        """Recompute equity from cash + open positions' mark and record it.

        ``last_prices`` should hold, for every open symbol, the latest
        observable close as of ``session`` (never a later one).
        """
        mark_value = 0.0
        sector_notional: dict[str, float] = {}
        for symbol, position in self.positions.items():
            price = last_prices.get(symbol, position.entry_price)
            # Liquidation value of this position at the current mark: selling
            # a long returns +price*shares; covering a short costs
            # -price*shares. ``direction.sign`` is +1/-1, so this one term
            # is correct for both sides -- see the accounting note below.
            mark_value += position.direction.sign * price * position.shares
            sector_notional[position.sector] = (
                sector_notional.get(position.sector, 0.0) + position.notional
            )
        # Equity = cash still held + the current liquidation value of every
        # open position.
        #
        # A long entry *reduced* cash by its entry notional, so a long's
        # ``mark_value`` (+price*shares) both returns that notional and adds
        # the unrealised P&L when added to cash -- consistent with the old
        # "add back entry basis, then add unrealised P&L" formulation this
        # replaced.
        #
        # A short entry *raised* cash by its entry notional (see
        # ``open_position``: proceeds are credited, not held as margin), so a
        # short's mark value must be the *negative* of the full current
        # notional (-price*shares), not just its unrealised P&L -- the prior
        # formula added only unrealised P&L here, which left the entry
        # proceeds sitting in cash uncancelled and overstated equity by
        # exactly one short's entry notional for as long as it stayed open
        # (self-correcting only once the short was covered, since covering
        # cash flow is itself correct -- so it never showed up in the final
        # cash/trades reconciliation, only in the equity curve while the
        # short was live).
        self.equity = self.cash + mark_value
        if self.equity <= 0:
            # Never let equity go underwater/negative silently: a real broker
            # would issue a margin call long before this, so a backtest that
            # reaches it either found a genuinely account-destroying strategy
            # or -- as with the ledger-orphaning bug this check was added
            # alongside -- has an accounting defect. Either way it must be
            # loud, not a number a reader has to notice is implausible on
            # their own.
            log.warning(
                "Portfolio equity non-positive on %s: equity=$%.2f (cash=$%.2f, "
                "%d open position(s)); treat this run as underwater/margin-called.",
                session,
                self.equity,
                self.cash,
                len(self.positions),
            )
        self.high_water_mark = max(self.high_water_mark, self.equity)
        drawdown_pct = (
            0.0
            if self.high_water_mark <= 0
            else 100.0 * (self.high_water_mark - self.equity) / self.high_water_mark
        )
        exposure_pct = (
            0.0
            if self.equity <= 0
            else 100.0 * sum(p.notional for p in self.positions.values()) / self.equity
        )
        point = EquityPoint(
            session=session,
            equity=self.equity,
            cash=self.cash,
            open_positions=len(self.positions),
            exposure_pct=exposure_pct,
            portfolio_heat_pct=self.open_heat_pct(),
            drawdown_pct=drawdown_pct,
            sector_exposure={
                sector: (100.0 * notional / self.equity if self.equity > 0 else 0.0)
                for sector, notional in sector_notional.items()
            },
        )
        self.equity_curve.append(point)
        return point


__all__ = ["BacktestPortfolio", "EquityPoint", "Position"]
