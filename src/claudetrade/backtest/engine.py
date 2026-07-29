"""Event-driven bar-by-bar backtest engine with deterministic trade lifecycle.

The engine walks through every trading session in chronological order, generates
signals through the same SignalEngine.scan() used in live trading, queues orders
with configurable delay, and manages position lifecycle through execution
simulation and accounting. No trade is left open: forced closes and delisting
handling ensure every position is classified as a win, loss, or breakeven.

See BacktestConfig for execution assumptions and walkforward.py for validation
logic (parameter sensitivity, out-of-sample metrics aggregation).
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from claudetrade.backtest.costs import CostModel
from claudetrade.backtest.execution import EntryOrder, ExecutionSimulator
from claudetrade.backtest.metrics import compute_metrics, segment_metrics
from claudetrade.backtest.portfolio import BacktestPortfolio, EquityPoint
from claudetrade.config import AppConfig, BacktestConfig
from claudetrade.domain import Bar, ExitReason, RegimeState, SecurityInfo, Trade
from claudetrade.signals.engine import SignalEngine
from claudetrade.strategies.base import StrategyContext
from claudetrade.version import CODE_VERSION

log = logging.getLogger(__name__)


class ContextProvider(Protocol):
    """Abstract interface a coordinator implements to hand data to the engine.

    Separates the engine from the data layer so backtests and live trading use
    identical strategy evaluation code (see signals/engine.py module docstring).
    """

    def sessions(self) -> list[dt.date]:
        """All trading sessions in chronological order."""
        ...

    def symbols_for(self, session: dt.date) -> list[str]:
        """Symbols eligible to be evaluated on ``session``."""
        ...

    def build_context(self, symbol: str, session: dt.date) -> StrategyContext | None:
        """Point-in-time context for one symbol on one session.

        Returns ``None`` if the symbol is inactive (not yet listed, delisted, or
        no data). The engine asserts no lookahead on every context.
        """
        ...

    def bar(self, symbol: str, session: dt.date) -> Bar | None:
        """The OHLCV bar for ``symbol`` on ``session``, or ``None`` if unavailable."""
        ...

    def security(self, symbol: str) -> SecurityInfo:
        """Reference data (sector, cap bucket, delisted_date, etc.)."""
        ...

    def regime(self, session: dt.date) -> RegimeState:
        """Market regime state for ``session``."""
        ...


@dataclass(slots=True)
class DictContextProvider:
    """In-memory test double implementing ContextProvider."""

    # Mapping of (symbol, session) -> Bar
    _bars: dict[tuple[str, dt.date], Bar] = field(default_factory=dict)
    # Mapping of symbol -> SecurityInfo
    _securities: dict[str, SecurityInfo] = field(default_factory=dict)
    # Mapping of session -> RegimeState
    _regimes: dict[dt.date, RegimeState] = field(default_factory=dict)
    # Mapping of (symbol, session) -> StrategyContext
    _contexts: dict[tuple[str, dt.date], StrategyContext] = field(default_factory=dict)

    def add_bar(self, bar: Bar) -> None:
        """Register a bar for later retrieval."""
        self._bars[(bar.symbol, bar.session)] = bar

    def add_security(self, security: SecurityInfo) -> None:
        """Register security reference data."""
        self._securities[security.symbol] = security

    def add_regime(self, regime: RegimeState) -> None:
        """Register a regime state."""
        self._regimes[regime.session] = regime

    def add_context(self, ctx: StrategyContext) -> None:
        """Register a pre-built context."""
        self._contexts[(ctx.symbol, ctx.session)] = ctx

    def sessions(self) -> list[dt.date]:
        """Extract sorted unique sessions from registered bars."""
        sessions = set()
        for _symbol, session in self._bars:
            sessions.add(session)
        return sorted(sessions)

    def symbols_for(self, session: dt.date) -> list[str]:
        """Extract symbols active on ``session`` from registered bars."""
        symbols = set()
        for symbol, s in self._bars:
            if s == session:
                symbols.add(symbol)
        return sorted(symbols)

    def build_context(self, symbol: str, session: dt.date) -> StrategyContext | None:
        """Return a pre-registered context or None."""
        return self._contexts.get((symbol, session))

    def bar(self, symbol: str, session: dt.date) -> Bar | None:
        """Return a registered bar or None."""
        return self._bars.get((symbol, session))

    def security(self, symbol: str) -> SecurityInfo:
        """Return registered security or a default stub."""
        return self._securities.get(symbol, SecurityInfo(symbol=symbol))

    def regime(self, session: dt.date) -> RegimeState:
        """Return a registered regime or a default unknown."""
        if session not in self._regimes:
            from claudetrade.domain import MarketRegime
            return RegimeState(session=session, regime=MarketRegime.UNKNOWN)
        return self._regimes[session]


@dataclass(slots=True)
class RejectionFunnel:
    """Per-stage counts of why candidates fell out of a backtest run.

    ADR-0007 Decision 3(b): every scan/backtest reports this so "0 trades" is
    always attributable rather than indistinguishable from a silently broken
    pipeline (trading-bot's ``backtest_order_gateway.py`` bug class -- pattern
    only, GPL/MIT conflict, not copied). Every (symbol, session) candidate the
    provider offers enters at ``universe_candidates`` and is accounted for by
    exactly one of the buckets below (a symbol can of course recur across
    multiple sessions and contribute to several buckets over the run).

    Field groups, in pipeline order:

    * ``universe_candidates`` / ``universe_filtered_symbols`` / ``no_context``
      -- data-layer attrition, before any strategy runs.
    * ``strategy_declined`` / ``strategy_errors`` -- a strategy looked and
      passed (declined) or blew up (an unhandled exception or an invalid
      proposal; see ``signals.engine.RejectedCandidate`` stage "strategy").
    * ``gate_rejected`` / ``score_rejected`` -- ``signals.scoring`` hard gates
      and the overall-score/confidence thresholds.
    * ``sizing_zero`` / ``limits_rejected`` -- risk sizing produced zero
      shares, or portfolio-level limits (heat, concentration, kill switch)
      blocked an otherwise-sized position.
    * ``signals_generated`` -- cleared every stage above; an order was queued.
    * ``entries_filled`` / ``entries_expired_unfilled`` / ``entries_carried_to_end``
      -- what happened to a queued order: filled against a later bar, never
      filled within ``SignalConfig.signal_expiry_days`` sessions and dropped,
      or still queued (unfilled, unexpired) when the run's last session ended.
    """

    universe_candidates: int = 0
    #: Symbols the provider offered at least once but for which
    #: ``build_context`` returned None on *every* session in the run --
    #: structurally excluded from the universe, as opposed to a transient
    #: per-session gap (see ``no_context`` below).
    universe_filtered_symbols: int = 0
    #: (symbol, session) pairs where ``build_context`` returned None.
    no_context: int = 0
    #: strategy name -> decline reason -> count, drained from each
    #: ``Strategy.decline()`` call via ``drain_rejections()``.
    strategy_declined: dict[str, dict[str, int]] = field(default_factory=dict)
    strategy_errors: int = 0
    gate_rejected: int = 0
    score_rejected: int = 0
    sizing_zero: int = 0
    limits_rejected: int = 0
    signals_generated: int = 0
    orders_queued: int = 0
    entries_filled: int = 0
    entries_expired_unfilled: int = 0
    entries_carried_to_end: int = 0

    def strategy_decline_total(self) -> int:
        return sum(sum(reasons.values()) for reasons in self.strategy_declined.values())

    def summary_lines(self) -> list[str]:
        """Human-readable funnel, most-upstream stage first.

        Used by both the CLI's plain-text output and the markdown report --
        kept here rather than duplicated in each renderer.
        """
        lines = [
            f"Universe candidates (symbol x session): {self.universe_candidates}",
            f"  Symbols never producing a usable context: {self.universe_filtered_symbols}",
            f"  (symbol, session) pairs with no context: {self.no_context}",
            f"Strategy declines: {self.strategy_decline_total()} "
            f"(errors/invalid proposals: {self.strategy_errors})",
        ]
        for strategy in sorted(self.strategy_declined):
            reasons = self.strategy_declined[strategy]
            top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)
            detail = ", ".join(f"{reason}={count}" for reason, count in top)
            lines.append(f"    {strategy}: {sum(reasons.values())} ({detail})")
        lines.extend(
            [
                f"Gate-rejected (hard filters): {self.gate_rejected}",
                f"Score-rejected (below threshold): {self.score_rejected}",
                f"Risk sizing produced zero shares: {self.sizing_zero}",
                f"Portfolio-limit rejected: {self.limits_rejected}",
                f"Signals generated (orders queued): {self.signals_generated}",
                f"Entries filled: {self.entries_filled}",
                f"Entries expired unfilled: {self.entries_expired_unfilled}",
                f"Entries still queued at run end: {self.entries_carried_to_end}",
            ]
        )
        return lines


@dataclass(slots=True)
class BacktestResult:
    """Complete backtest output: metadata, trades, equity curve, metrics."""

    run_id: str
    config: BacktestConfig
    start_session: dt.date
    end_session: dt.date
    strategy_names: list[str]
    universe_size: int

    trades: list[Trade]
    equity_curve: list[EquityPoint]
    metrics: dict  # Overall metrics
    segment_metrics: dict  # Segmented metrics by dimension
    warnings: list[str]
    #: ADR-0007 Decision 3(b): populated on every run, including a 0-trade
    #: one, so "why zero?" always has an answer.
    funnel: RejectionFunnel

    code_version: str
    config_hash: str
    data_snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = str(uuid.uuid4())


class BacktestEngine:
    """Event-driven bar-by-bar backtest simulator."""

    def __init__(self, config: AppConfig, signal_engine: SignalEngine | None = None):
        """Initialize the engine.

        Args:
            config: Application configuration (risk, cost model, trading mode).
            signal_engine: Optional signal engine; if omitted, one is built.
        """
        self.config = config
        self.backtest_config = config.backtest
        self.signal_engine = signal_engine or SignalEngine(config, generate_thesis=False)
        self.cost_model = CostModel(config.costs)
        self.execution = ExecutionSimulator(
            cost_model=self.cost_model,
            cost_config=config.costs,
            intrabar_policy="pessimistic",
        )

    def run(
        self,
        provider: ContextProvider,
        start_session: dt.date | None = None,
        end_session: dt.date | None = None,
        label: str = "",
    ) -> BacktestResult:
        """Run a complete backtest.

        Args:
            provider: Source of bars, contexts, and reference data.
            start_session: First session to process (default: first available).
            end_session: Last session to process (default: last available).
            label: Optional descriptive label for this run.

        Returns:
            A BacktestResult with trades, equity curve, and metrics.

        Raises:
            LookaheadError: if any context post-dates its decision session.
        """
        random.seed(self.backtest_config.random_seed)

        sessions = provider.sessions()
        if not sessions:
            raise ValueError("provider.sessions() returned no data")

        if start_session is None:
            start_session = sessions[0]
        if end_session is None:
            end_session = sessions[-1]

        sessions = [s for s in sessions if start_session <= s <= end_session]
        if not sessions:
            raise ValueError(f"no sessions in range [{start_session}, {end_session}]")

        run_id = str(uuid.uuid4())
        portfolio = BacktestPortfolio(
            config=self.config,
            cost_model=self.cost_model,
            execution=self.execution,
            cash=self.backtest_config.initial_capital_usd,
        )

        # Queued orders: (symbol, EntryOrder) tuples to execute on next bar
        queued_orders: list[tuple[str, EntryOrder]] = []
        strategy_names: set[str] = set()
        universe_size = 0

        # ADR-0007 Decision 3(b): rejection funnel, populated across the whole
        # run so a 0-trade result is always attributable. `all_symbols_seen`
        # vs `symbols_with_context` distinguishes a symbol structurally
        # excluded for the entire run from one with only transient per-session
        # gaps (halts, late listing, etc. -- see RejectionFunnel.no_context).
        funnel = RejectionFunnel()
        all_symbols_seen: set[str] = set()
        symbols_with_context: set[str] = set()

        log.info(
            f"Starting backtest {run_id}: {len(sessions)} sessions "
            f"from {sessions[0]} to {sessions[-1]}"
        )

        for i, session in enumerate(sessions):
            log.debug(f"[{i+1}/{len(sessions)}] Processing {session}")

            symbols = provider.symbols_for(session)
            universe_size = max(universe_size, len(symbols))
            regime = provider.regime(session)

            funnel.universe_candidates += len(symbols)
            all_symbols_seen.update(symbols)

            # --- Generate signals for this session ---
            contexts: list[StrategyContext] = []
            for symbol in symbols:
                ctx = provider.build_context(symbol, session)
                if ctx is None:
                    funnel.no_context += 1
                    continue
                ctx.assert_no_lookahead()
                contexts.append(ctx)
                symbols_with_context.add(symbol)

            portfolio_state = portfolio.portfolio_state(session)
            scan_result = self.signal_engine.scan(
                contexts,
                session=session,
                regime=regime,
                portfolio=portfolio_state,
                generate_thesis=False,
            )

            for signal in scan_result.signals:
                strategy_names.add(signal.strategy)
            funnel.signals_generated += len(scan_result.signals)

            # Aggregate why the rest of this session's candidates fell out.
            # `scan_result.rejected` is the signal engine's own pipeline
            # (strategy error/invalid proposal, hard gates, score/confidence
            # threshold, and its own sizing/limits check against the
            # portfolio snapshot at scan time).
            for rejected_candidate in scan_result.rejected:
                if rejected_candidate.stage == "strategy":
                    funnel.strategy_errors += 1
                elif rejected_candidate.stage == "gates":
                    funnel.gate_rejected += 1
                elif rejected_candidate.stage == "score":
                    funnel.score_rejected += 1
                elif rejected_candidate.stage == "sizing":
                    funnel.sizing_zero += 1
                elif rejected_candidate.stage == "limits":
                    funnel.limits_rejected += 1

            # Strategies record *why* they declined via Strategy.decline(),
            # independent of the signal engine's own pipeline above (a
            # decline never becomes a proposal at all, so it never appears in
            # `scan_result.rejected`). Drained every session so counts don't
            # accumulate unboundedly in the strategy instance.
            for strategy in self.signal_engine.strategies:
                for rejection in strategy.drain_rejections():
                    by_reason = funnel.strategy_declined.setdefault(rejection.strategy, {})
                    by_reason[rejection.reason] = by_reason.get(rejection.reason, 0) + 1

            # --- Execute queued orders from the previous session ---
            new_queued = []
            for symbol, order in queued_orders:
                bar = provider.bar(symbol, session)
                if bar is None or bar.volume <= 0:
                    # Halted / no data: carry order forward, subject to the
                    # same expiry as a working-but-unfilled order below -- a
                    # symbol that stays halted past the expiry window is just
                    # as stale an idea as one that simply never triggered.
                    age_sessions = (session - (order.queued_session or session)).days
                    if age_sessions >= self.config.signals.signal_expiry_days:
                        funnel.entries_expired_unfilled += 1
                        log.debug(f"  {symbol}: entry expired unfilled (halted/no data) after {age_sessions}d")
                        continue
                    new_queued.append((symbol, order))
                    continue

                fill = self.execution.try_fill_entry(order, bar)
                if fill is None:
                    # Did not fill this bar. An order that has been working
                    # since before the signal's own expiry window (the same
                    # window that governs the live scanner, see
                    # SignalConfig.signal_expiry_days) is dropped rather than
                    # left queued forever -- an entry chasing a price that
                    # never came back is a stale idea, not a pending trade.
                    age_sessions = (session - (order.queued_session or session)).days
                    if age_sessions >= self.config.signals.signal_expiry_days:
                        funnel.entries_expired_unfilled += 1
                        log.debug(f"  {symbol}: entry expired unfilled after {age_sessions}d")
                        continue
                    new_queued.append((symbol, order))
                    continue

                funnel.entries_filled += 1

                # Filled: open a position
                stop_loss = order.direction.sign * float("inf")
                targets: list[float] = []
                target_fractions: list[float] = []
                time_stop_session = session + dt.timedelta(
                    days=self.backtest_config.walk_forward_test_days
                )
                trailing_stop_atr: float | None = None

                # Look up the signal for plan details
                matching_signal = next(
                    (s for s in scan_result.signals if s.symbol == symbol),
                    None,
                )
                if matching_signal:
                    stop_loss = matching_signal.plan.stop_loss
                    targets = list(matching_signal.plan.targets)
                    target_fractions = list(matching_signal.plan.target_fractions)
                    trailing_stop_atr = matching_signal.plan.trailing_stop_atr
                    time_stop_session = session + dt.timedelta(
                        days=matching_signal.plan.time_stop_days
                    )

                position = portfolio.open_position(
                    trade_id=str(uuid.uuid4()),
                    signal_id=order.signal_id,
                    strategy=order.strategy,
                    entry_order=order,
                    fill=fill,
                    bar=bar,
                    stop_loss=stop_loss,
                    targets=targets,
                    target_fractions=target_fractions,
                    trailing_stop_atr=trailing_stop_atr,
                    time_stop_session=time_stop_session,
                )
                log.debug(
                    f"  Opened {position.symbol} {position.direction.value} "
                    f"@ {position.entry_price} ({position.shares} shares)"
                )

            queued_orders = new_queued

            # --- Queue new entries from today's signals ---
            for signal in scan_result.signals:
                if signal.symbol in portfolio.positions:
                    continue  # Already holding this symbol

                sizing, limit_check = portfolio.size_and_vet(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry_price=signal.plan.entry_reference,
                    stop_price=signal.plan.stop_loss,
                    as_of_session=session,
                    avg_dollar_volume=provider.security(signal.symbol).market_cap_usd,
                    sector=provider.security(signal.symbol).sector,
                )

                if not limit_check.allowed:
                    log.debug(f"  {signal.symbol}: limit breach: {limit_check.breaches}")
                    continue

                # Determine order type from config
                entry_type = self._entry_order_type()
                limit_price = None
                stop_price = None
                if entry_type == "limit":
                    limit_price = signal.plan.entry_reference
                elif entry_type == "stop_entry":
                    stop_price = signal.plan.entry_reference

                order = EntryOrder(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    shares=sizing.shares,
                    order_type=entry_type,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    strategy=signal.strategy,
                    signal_id=signal.signal_id,
                    queued_session=session,
                )
                queued_orders.append((signal.symbol, order))
                log.debug(f"  Queued {signal.symbol} {signal.direction.value} entry")

            # --- Process existing positions: exits, mark-to-market ---
            last_prices = {}
            for symbol in symbols:
                bar = provider.bar(symbol, session)
                if bar is not None:
                    last_prices[symbol] = bar.close

            closed_trades: list[Trade] = []
            for symbol in list(portfolio.positions.keys()):
                bar = provider.bar(symbol, session)
                if bar is None:
                    continue

                # Check for delisting
                security = provider.security(symbol)
                force_close = False
                force_close_reason = ExitReason.END_OF_BACKTEST
                force_close_price: float | None = None

                if security.delisted_date and session >= security.delisted_date:
                    force_close = True
                    force_close_reason = ExitReason.DELISTED
                    force_close_price = (
                        bar.close * self.config.backtest.delisting_recovery_factor
                        if hasattr(self.config.backtest, 'delisting_recovery_factor')
                        else bar.close * 0.30
                    )

                trade = portfolio.process_bar_for_position(
                    symbol,
                    bar,
                    force_close=force_close,
                    force_close_reason=force_close_reason,
                    force_close_price=force_close_price,
                )

                if trade is not None:
                    closed_trades.append(trade)
                    log.debug(
                        f"  Closed {trade.symbol} {trade.direction.value} "
                        f"P&L {trade.net_pnl:.0f}"
                    )

            # Daily borrow costs on shorts
            portfolio.accrue_daily_borrow_costs(session)

            # Mark-to-market and record equity
            portfolio.mark_to_market(session, last_prices)

        # --- Force-close all remaining positions ---
        last_session = sessions[-1]
        if self.backtest_config.force_close_open_positions:
            for symbol in list(portfolio.positions.keys()):
                bar = provider.bar(symbol, last_session)
                if bar is not None:
                    trade = portfolio.process_bar_for_position(
                        symbol,
                        bar,
                        force_close=True,
                        force_close_reason=ExitReason.END_OF_BACKTEST,
                    )
                    if trade is not None:
                        portfolio.closed_trades.append(trade)
                        log.debug(
                            f"  Force-closed {trade.symbol} at end-of-backtest "
                            f"P&L {trade.net_pnl:.0f}"
                        )

        assert not portfolio.positions, (
            f"backtest complete but {len(portfolio.positions)} position(s) still open; "
            "force_close_open_positions=true should have closed them"
        )

        # --- Compute metrics ---
        metrics_obj = compute_metrics(
            portfolio.closed_trades,
            portfolio.equity_curve,
            self.backtest_config,
        )
        # compute_metrics already attaches these; reuse rather than recompute.
        warnings_list = metrics_obj.warnings

        segment_dims = [
            "strategy",
            "direction",
            "year",
            "regime",
        ]
        all_segment_metrics = {}
        for dim in segment_dims:
            all_segment_metrics[dim] = segment_metrics(
                portfolio.closed_trades,
                portfolio.equity_curve,
                self.backtest_config,
                dim,
            )

        # Build result
        config_hash = self.config.config_hash
        data_snapshot_hash = ""  # Caller may provide via metadata

        from dataclasses import asdict
        result = BacktestResult(
            run_id=run_id,
            config=self.backtest_config,
            start_session=sessions[0],
            end_session=sessions[-1],
            strategy_names=sorted(strategy_names),
            universe_size=universe_size,
            trades=portfolio.closed_trades,
            equity_curve=portfolio.equity_curve,
            metrics=asdict(metrics_obj),
            segment_metrics=all_segment_metrics,
            warnings=warnings_list,
            code_version=CODE_VERSION,
            config_hash=config_hash,
            data_snapshot_hash=data_snapshot_hash,
        )

        log.info(
            f"Backtest complete: {len(portfolio.closed_trades)} trades, "
            f"final equity ${portfolio.equity:,.0f}"
        )

        return result

    def _entry_order_type(self) -> str:
        """Map BacktestConfig.entry_reference to EntryOrderType."""
        ref = self.backtest_config.entry_reference
        if ref == "next_open":
            return "market_on_open"
        elif ref == "next_open_limit":
            return "limit"
        elif ref == "stop_trigger":
            return "stop_entry"
        return "market_on_open"


__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "ContextProvider",
    "DictContextProvider",
]
