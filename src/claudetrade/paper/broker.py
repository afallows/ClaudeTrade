"""Paper broker.

Turns a ``Signal`` into a simulated position using the *same* cost model,
sizing rules and risk limits as the backtester. If paper trading were cheaper or
more permissive than the backtest, paper results would flatter the strategy and
the comparison between them would be meaningless.

Live trading is explicitly out of scope for this class. ``PaperBroker.is_live``
is always False, and there is no code path that transmits an order anywhere. A
live adapter would be a separate implementation of ``BrokerProvider`` and would
additionally have to verify ``trading.mode == 'live'`` and
``live_trading_authorised`` before doing anything.

``PaperBroker`` also implements ``claudetrade.brokers.base.BrokerProvider``
(ADR-0007 Decision 4): the ``get_*``/``submit_order``/``cancel_order``/
``modify_order`` methods below are a thin translation layer over
``submit_signal``/``portfolio``/``ledger``, added without changing any of
those methods' behaviour -- the DB-backed fills, MFE/MAE tracking and
immutability guarantees they provide are pinned by the tests in
``tests/test_ledger_immutability.py`` and friends and must stay exactly as
they were.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select

from claudetrade.backtest.costs import CostModel
from claudetrade.brokers.base import (
    Balances,
    BrokerOrder,
    BrokerOrderError,
    BrokerProvider,
    OrderRequest,
)
from claudetrade.config import AppConfig
from claudetrade.db.models import PaperOrderRow, PaperTradeRow, PriceBar
from claudetrade.db.session import Database
from claudetrade.domain import (
    ACTIVE_STATUSES,
    Bar,
    Direction,
    ExitReason,
    Fill,
    MarketRegime,
    OrderStatus,
    Signal,
    SignalStatus,
    Trade,
)
from claudetrade.logging_setup import audit_event, get_logger
from claudetrade.paper.portfolio import PaperPortfolio, PaperTradeError
from claudetrade.risk.limits import check_new_position
from claudetrade.signals.ledger import SignalLedger
from claudetrade.utils.hashing import short_hash
from claudetrade.utils.timeutils import utc_now
from claudetrade.version import CODE_VERSION

log = get_logger(__name__)


@dataclass(slots=True)
class PaperOrderResult:
    """Outcome of submitting one signal to the paper broker."""

    accepted: bool
    order_id: str = ""
    trade_id: str = ""
    symbol: str = ""
    shares: int = 0
    fill_price: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return not self.accepted


class PaperBroker(BrokerProvider):
    """Simulated execution against a persistent paper account."""

    name = "paper"
    is_live = False

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        portfolio: PaperPortfolio | None = None,
        ledger: SignalLedger | None = None,
    ):
        self.config = config
        self.db = db
        self.portfolio = portfolio or PaperPortfolio(config, db)
        self.ledger = ledger or SignalLedger(db)
        self.costs = CostModel(config.costs)

    # --- submission --------------------------------------------------------

    def submit_signal(
        self,
        signal: Signal,
        *,
        next_bar: Bar,
        marks: dict[str, float] | None = None,
    ) -> PaperOrderResult:
        """Attempt to enter the position described by ``signal``.

        Execution happens on ``next_bar`` -- the bar *after* the signal's
        session -- never on the bar that generated the signal. Filling on the
        signal bar would be look-ahead: the close that produced the decision was
        not tradable until it had already printed.

        Args:
            signal: The signal to act on.
            next_bar: The execution bar, which must post-date the signal.
            marks: Latest prices for open positions, used for risk checks.

        Returns:
            A ``PaperOrderResult``. Rejection is a normal outcome and carries
            the reasons.
        """
        if next_bar.session <= signal.session:
            raise PaperTradeError(
                f"execution bar {next_bar.session} does not post-date signal session "
                f"{signal.session}; filling on the signal bar would be look-ahead"
            )

        marks = marks or {}
        state = self.portfolio.portfolio_state(marks, as_of=next_bar.session)

        if state.kill_switch_engaged:
            return self._reject(signal, ["kill switch is engaged: no new positions"])

        status = self.ledger.current_status(signal.signal_id)
        if status in {SignalStatus.EXPIRED, SignalStatus.TRIGGERED, SignalStatus.REJECTED}:
            return self._reject(signal, [f"signal is already {status.value}"])
        if signal.expires_after and next_bar.session > signal.expires_after:
            self.ledger.append_revision(
                signal.signal_id,
                status=SignalStatus.EXPIRED,
                reason=f"entry window closed on {signal.expires_after}",
                actor="paper_broker",
            )
            return self._reject(signal, ["signal expired before it could be filled"])

        plan = signal.plan
        if plan.shares <= 0:
            return self._reject(signal, ["signal carries no position size"])

        fill = self._determine_fill(signal, next_bar)
        if fill is None:
            return self._reject(
                signal,
                [
                    f"price never reached the {plan.entry_low:.2f}-{plan.entry_high:.2f} entry "
                    f"zone on {next_bar.session}"
                ],
            )
        fill_price, shares = fill

        notional = fill_price * shares
        dollar_risk = abs(fill_price - plan.stop_loss) * shares
        check = check_new_position(
            config=self.config,
            state=state,
            symbol=signal.symbol,
            direction=signal.direction,
            notional=notional,
            dollar_risk=dollar_risk,
            sector=signal.extras.get("sector", ""),
            correlation_group=signal.extras.get("sector", ""),
        )
        if not check.allowed:
            return self._reject(signal, check.breaches)

        return self._record_entry(
            signal=signal,
            bar=next_bar,
            fill_price=fill_price,
            shares=shares,
            warnings=check.warnings,
        )

    def _determine_fill(self, signal: Signal, bar: Bar) -> tuple[float, int] | None:
        """Decide whether and where a limit entry fills on ``bar``.

        The order is treated as a limit at the top of the entry zone for a long
        (bottom for a short). It fills only if the bar's range actually reaches
        that level, and the fill is charged the half-spread plus size-scaled
        slippage. Size is additionally capped by the bar's own volume, because
        an order larger than the day's turnover would not have filled.
        """
        plan = signal.plan
        if bar.volume <= 0:
            return None

        if signal.direction is Direction.LONG:
            limit = plan.entry_high
            if bar.low > limit:
                return None
            base = min(bar.open, limit) if bar.open <= limit else limit
        else:
            limit = plan.entry_low
            if bar.high < limit:
                return None
            base = max(bar.open, limit) if bar.open >= limit else limit

        shares = plan.shares
        max_participation = int(bar.volume * self.config.costs.max_participation_rate)
        if max_participation <= 0:
            return None
        if shares > max_participation:
            if not self.config.costs.enable_partial_fills:
                return None
            shares = max_participation
            log.info(
                "%s: partial fill %d of %d shares (bar volume cap)",
                signal.symbol,
                shares,
                plan.shares,
            )

        slip = self.costs.slippage(base, shares, bar.volume)
        spread = self.costs.spread_cost(base)
        # Costs always move the fill against the trader.
        sign = signal.direction.sign
        fill_price = base + sign * (slip + spread)
        return round(fill_price, 4), shares

    def _record_entry(
        self,
        *,
        signal: Signal,
        bar: Bar,
        fill_price: float,
        shares: int,
        warnings: list[str],
    ) -> PaperOrderResult:
        order_id = f"ord-{short_hash([signal.signal_id, bar.session.isoformat()], 10)}"
        trade_id = f"trd-{short_hash([signal.signal_id, bar.session.isoformat(), shares], 10)}"
        commission = self.costs.commission(shares, fill_price)
        fees = self.costs.regulatory_fees("buy", shares, fill_price * shares)
        risk_per_share = abs(fill_price - signal.plan.stop_loss)

        with self.db.session() as session:
            session.add(
                PaperOrderRow(
                    order_id=order_id,
                    account_id=self.portfolio.account_id,
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    side="buy" if signal.direction is Direction.LONG else "sell_short",
                    order_type="limit",
                    quantity=shares,
                    limit_price=signal.plan.entry_high
                    if signal.direction is Direction.LONG
                    else signal.plan.entry_low,
                    stop_price=signal.plan.stop_loss,
                    status="filled",
                    signal_ts=signal.created_at,
                    filled_at=utc_now(),
                    filled_quantity=shares,
                    average_fill_price=fill_price,
                    commission=commission,
                    fees=fees,
                    detail={"execution_session": bar.session.isoformat()},
                )
            )
            session.add(
                PaperTradeRow(
                    trade_id=trade_id,
                    account_id=self.portfolio.account_id,
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    strategy=signal.strategy,
                    direction=signal.direction.value,
                    signal_ts=signal.created_at,
                    order_ts=utc_now(),
                    entry_session=bar.session,
                    entry_price=fill_price,
                    shares=shares,
                    stop_loss=signal.plan.stop_loss,
                    original_stop_loss=signal.plan.stop_loss,
                    targets=list(signal.plan.targets),
                    commission_total=commission,
                    fees_total=fees,
                    initial_risk_per_share=risk_per_share,
                    regime_at_entry=signal.regime.value,
                    sector=str(signal.extras.get("sector", "")),
                    days_to_earnings_at_entry=signal.days_to_earnings,
                    confidence_at_entry=signal.confidence,
                    code_version=CODE_VERSION,
                    notes=list(warnings),
                )
            )
            account = self.portfolio.account()
            row = session.get(type(account), self.portfolio.account_id)
            row.cash -= fill_price * shares + commission + fees
            row.updated_at = utc_now()

        self.ledger.append_revision(
            signal.signal_id,
            status=SignalStatus.TRIGGERED,
            reason=f"paper entry filled at {fill_price:.2f} on {bar.session}",
            observed_price=fill_price,
            actor="paper_broker",
            detail={"trade_id": trade_id, "shares": shares},
        )
        audit_event(
            "paper_order_filled",
            order_id=order_id,
            trade_id=trade_id,
            symbol=signal.symbol,
            shares=shares,
            price=fill_price,
        )
        log.info("paper filled %s: %d shares at %.2f", signal.symbol, shares, fill_price)
        return PaperOrderResult(
            accepted=True,
            order_id=order_id,
            trade_id=trade_id,
            symbol=signal.symbol,
            shares=shares,
            fill_price=fill_price,
            reasons=warnings,
        )

    def _reject(self, signal: Signal, reasons: list[str]) -> PaperOrderResult:
        log.info("paper order rejected for %s: %s", signal.symbol, "; ".join(reasons))
        return PaperOrderResult(accepted=False, symbol=signal.symbol, reasons=reasons)

    # --- lifecycle -----------------------------------------------------------

    def process_open_positions(
        self, bars: dict[str, Bar], session_date: dt.date
    ) -> list[PaperOrderResult]:
        """Apply stops, targets and time stops to open positions.

        Gap handling matches the backtester: when a bar opens beyond the stop,
        the fill is at the open, not at the stop price.
        """
        results: list[PaperOrderResult] = []
        for view in self.portfolio.positions(
            {s: b.close for s, b in bars.items()}, as_of=session_date
        ):
            bar = bars.get(view.symbol)
            if bar is None:
                continue
            decision = self._exit_decision(view, bar)
            if decision is None:
                continue
            price, reason = decision
            shares = view.shares
            commission = self.costs.commission(shares, price)
            fees = self.costs.regulatory_fees("sell", shares, price * shares)
            self.portfolio.close_trade(
                view.trade_id,
                exit_session=bar.session,
                exit_price=price,
                reason=reason,
                commission=commission,
                fees=fees,
            )
            results.append(
                PaperOrderResult(
                    accepted=True,
                    trade_id=view.trade_id,
                    symbol=view.symbol,
                    shares=shares,
                    fill_price=price,
                    reasons=[reason.value],
                )
            )

        self.portfolio.mark_to_market(bars, session_date)
        self.portfolio.apply_time_stops({s: b.close for s, b in bars.items()}, session_date)
        return results

    def _exit_decision(self, view, bar: Bar):
        """Return ``(price, reason)`` when the position should close on this bar."""
        from claudetrade.domain import ExitReason

        sign = view.direction.sign
        stop = view.stop_loss

        # Gap through the stop: the position is out at the open, worse than the
        # stop price. Assuming a stop-price fill here would be fiction.
        if sign > 0 and bar.open <= stop:
            return bar.open * (1 - self.config.costs.gap_slippage_bps / 10_000), (
                ExitReason.GAP_THROUGH_STOP
            )
        if sign < 0 and bar.open >= stop:
            return bar.open * (1 + self.config.costs.gap_slippage_bps / 10_000), (
                ExitReason.GAP_THROUGH_STOP
            )

        stop_hit = bar.low <= stop if sign > 0 else bar.high >= stop
        target = view.targets[0] if view.targets else None
        target_hit = (
            target is not None and (bar.high >= target if sign > 0 else bar.low <= target)
        )

        # Both inside one daily bar: which came first is unknowable from daily
        # data, so the pessimistic branch is taken. The optimistic reading is a
        # standard way to manufacture a flattering win rate.
        if stop_hit:
            return stop, ExitReason.STOP_LOSS
        if target_hit:
            return target, ExitReason.TARGET
        return None

    # --- kill switch -----------------------------------------------------------

    def cancel_all(self) -> int:
        """Emergency stop: block new entries. Returns positions left open.

        Deliberately does not liquidate. Force-selling into an unknown market is
        itself a risk decision and belongs to the operator.
        """
        self.portfolio.engage_kill_switch(True)
        open_count = len(self.portfolio.open_trades())
        audit_event("paper_cancel_all", open_positions=open_count)
        log.warning(
            "kill switch engaged; %d positions remain open with their stops in force", open_count
        )
        return open_count

    # --- CLI glue --------------------------------------------------------------
    #
    # Additive helpers backing `claudetrade paper open/process/close`. They do
    # not change any behaviour pinned by the tests above -- they only fetch
    # bars from the database "the way the pipeline does" (see
    # `DatabaseContextProvider._load_bars` in `claudetrade.data.context`) and
    # translate an existing lifecycle call into something the CLI can invoke
    # without duplicating the fill/exit logic.

    def latest_bar(self, symbol: str) -> Bar | None:
        """Most recent stored daily bar for ``symbol``, or ``None`` if none is stored yet."""
        with self.db.read_session() as session:
            row = session.execute(
                select(PriceBar)
                .where(PriceBar.symbol == symbol)
                .order_by(PriceBar.session.desc())
                .limit(1)
            ).scalar_one_or_none()
        return self._price_bar_to_bar(row) if row is not None else None

    def next_bar_after(self, symbol: str, session_date: dt.date) -> Bar | None:
        """First stored bar for ``symbol`` strictly after ``session_date``.

        Used to price a signal's entry without look-ahead: the same
        post-dates-the-signal rule ``submit_signal`` itself enforces.
        """
        with self.db.read_session() as session:
            row = session.execute(
                select(PriceBar)
                .where(PriceBar.symbol == symbol, PriceBar.session > session_date)
                .order_by(PriceBar.session.asc())
                .limit(1)
            ).scalar_one_or_none()
        return self._price_bar_to_bar(row) if row is not None else None

    def latest_bars_for_open_positions(self) -> dict[str, Bar]:
        """Most recent stored bar for every symbol currently held.

        Backs ``claudetrade paper process``: a symbol with no stored bar yet
        is simply absent from the result rather than raising, so the caller
        can report it and move on instead of one missing bar blocking every
        other position.
        """
        symbols = {row.symbol for row in self.portfolio.open_trades()}
        out: dict[str, Bar] = {}
        for symbol in symbols:
            bar = self.latest_bar(symbol)
            if bar is not None:
                out[symbol] = bar
        return out

    def marks_for_open_positions(self) -> dict[str, float]:
        """Latest close per open symbol, in the shape ``portfolio_state``/risk checks expect."""
        return {symbol: bar.close for symbol, bar in self.latest_bars_for_open_positions().items()}

    def close_at_latest_price(self, trade_id: str, *, reason: ExitReason = ExitReason.MANUAL) -> Trade:
        """Close an open trade at the latest stored price via the existing exit machinery.

        Costs are computed the same way ``process_open_positions`` computes
        them for a stop/target exit, so a manual close is not artificially
        cheaper than an automatic one.

        Raises:
            BrokerOrderError: unknown trade id, the trade is already closed,
                or no price has been stored yet for its symbol.
        """
        trade_row = self.portfolio.get_trade(trade_id)
        if trade_row is None:
            raise BrokerOrderError(f"no such paper trade: {trade_id}")
        if trade_row.exit_session is not None:
            raise BrokerOrderError(
                f"paper trade {trade_id} closed on {trade_row.exit_session} and cannot be closed again"
            )
        bar = self.latest_bar(trade_row.symbol)
        if bar is None:
            raise BrokerOrderError(
                f"no stored price for {trade_row.symbol}; run 'claudetrade refresh' first"
            )
        shares = trade_row.shares
        commission = self.costs.commission(shares, bar.close)
        fees = self.costs.regulatory_fees("sell", shares, bar.close * shares)
        self.portfolio.close_trade(
            trade_id,
            exit_session=bar.session,
            exit_price=bar.close,
            reason=reason,
            commission=commission,
            fees=fees,
        )
        return self._trade_row_to_trade(self.portfolio.get_trade(trade_id))

    @staticmethod
    def _price_bar_to_bar(row: PriceBar) -> Bar:
        return Bar(
            symbol=row.symbol,
            session=row.session,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            adj_close=row.adj_close,
            source=row.source,
        )

    # --- BrokerProvider: identity --------------------------------------------

    @property
    def is_paper(self) -> bool:
        return True

    @property
    def is_backtesting(self) -> bool:
        # PaperBroker replays signals against real-time-shaped bars one at a
        # time, persisting to the database as it goes -- it is not the bulk
        # historical replay the backtester (claudetrade.backtest) performs.
        return False

    # --- BrokerProvider: guarded order-affecting surface ----------------------

    def _submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Translate an ``OrderRequest`` into a ``submit_signal`` call.

        Paper prices a fill against a bar, not against "the market right now",
        so ``request.next_bar`` is required here even though the ABC leaves it
        optional for adapters that do not need it.
        """
        if request.next_bar is None:
            raise ValueError(
                "PaperBroker.submit_order requires OrderRequest.next_bar: a simulated fill "
                "has to be priced against a specific bar"
            )
        result = self.submit_signal(request.signal, next_bar=request.next_bar, marks=request.marks)
        return self._order_result_to_broker_order(request.signal, request.next_bar, result)

    def _cancel_order(self, order_id: str) -> BrokerOrder:
        """Cancel an order, if it is still in an active state.

        Paper fills a limit entry synchronously inside ``submit_order``, so in
        practice every persisted ``PaperOrderRow`` is already terminal
        (``filled``) by the time a caller could try to cancel it -- this path
        exists so the contract is correct if a future paper mode ever queues
        orders instead of filling them immediately.
        """
        row = self._get_order_row(order_id)
        if row is None:
            raise BrokerOrderError(f"no such paper order: {order_id}")
        if OrderStatus(row.status) not in ACTIVE_STATUSES:
            raise BrokerOrderError(
                f"paper order {order_id} is already {row.status!r} and cannot be cancelled"
            )
        with self.db.session() as session:  # pragma: no cover - unreachable today, see above
            live_row = session.get(PaperOrderRow, order_id)
            live_row.status = OrderStatus.CANCELLED.value
        return self._order_row_to_broker_order(self._get_order_row(order_id))

    def _modify_order(
        self, order_id: str, *, stop_loss: float | None, targets: list[float] | None
    ) -> BrokerOrder:
        """Adjust the stop/targets of the position an order opened.

        The stop and targets that matter for risk live on the ``PaperTradeRow``
        position, not the (already-filled) order row, so this looks up the
        matching open trade via the order's ``signal_id`` and delegates to
        ``PaperPortfolio.modify_stop``. The order row's own ``stop_price`` is
        kept in sync for display purposes only.
        """
        order_row = self._get_order_row(order_id)
        if order_row is None:
            raise BrokerOrderError(f"no such paper order: {order_id}")
        trade_row = self._open_trade_for_signal(order_row.signal_id)
        if trade_row is None:
            raise BrokerOrderError(
                f"paper order {order_id} has no open position to modify "
                "(already closed, or the order never filled)"
            )
        self.portfolio.modify_stop(trade_row.trade_id, stop_loss=stop_loss, targets=targets)
        if stop_loss is not None:
            with self.db.session() as session:
                live_row = session.get(PaperOrderRow, order_id)
                live_row.stop_price = stop_loss
        return self._order_row_to_broker_order(self._get_order_row(order_id))

    # --- BrokerProvider: read-only surface -------------------------------------

    def get_balances(self) -> Balances:
        account = self.portfolio.account()
        state = self.portfolio.portfolio_state({})
        return Balances(
            cash=account.cash,
            equity=account.equity,
            buying_power=account.cash,
            realised_pnl_today=state.realised_pnl_today,
            realised_pnl_week=state.realised_pnl_week,
            kill_switch_engaged=state.kill_switch_engaged,
        )

    def get_positions(self) -> list[Trade]:
        return [self._trade_row_to_trade(row) for row in self.portfolio.open_trades()]

    def get_position(self, symbol: str) -> Trade | None:
        for row in self.portfolio.open_trades():
            if row.symbol == symbol:
                return self._trade_row_to_trade(row)
        return None

    def get_order(self, order_id: str) -> BrokerOrder | None:
        row = self._get_order_row(order_id)
        return self._order_row_to_broker_order(row) if row is not None else None

    def get_open_orders(self) -> list[BrokerOrder]:
        with self.db.read_session() as session:
            rows = (
                session.execute(
                    select(PaperOrderRow).where(
                        PaperOrderRow.account_id == self.portfolio.account_id,
                        PaperOrderRow.status.in_([s.value for s in ACTIVE_STATUSES]),
                    )
                )
                .scalars()
                .all()
            )
        return [self._order_row_to_broker_order(row) for row in rows]

    # --- BrokerProvider: row <-> domain-type translation -----------------------

    def _get_order_row(self, order_id: str) -> PaperOrderRow | None:
        with self.db.read_session() as session:
            return session.get(PaperOrderRow, order_id)

    def _open_trade_for_signal(self, signal_id: str | None) -> PaperTradeRow | None:
        if not signal_id:
            return None
        with self.db.read_session() as session:
            return session.execute(
                select(PaperTradeRow).where(
                    PaperTradeRow.account_id == self.portfolio.account_id,
                    PaperTradeRow.signal_id == signal_id,
                    PaperTradeRow.exit_session.is_(None),
                )
            ).scalar_one_or_none()

    def _order_result_to_broker_order(
        self, signal: Signal, bar: Bar, result: PaperOrderResult
    ) -> BrokerOrder:
        status = OrderStatus.FILLED if result.accepted else OrderStatus.REJECTED
        fills: list[Fill] = []
        if result.accepted and result.fill_price is not None:
            fills.append(Fill(session=bar.session, price=result.fill_price, shares=result.shares))
        return BrokerOrder(
            order_id=result.order_id or f"rejected-{signal.signal_id}",
            symbol=result.symbol or signal.symbol,
            direction=signal.direction,
            status=status,
            requested_shares=signal.plan.shares,
            filled_shares=result.shares if result.accepted else 0,
            average_fill_price=result.fill_price,
            fills=fills,
            reasons=result.reasons,
        )

    @staticmethod
    def _order_row_to_broker_order(row: PaperOrderRow) -> BrokerOrder:
        direction = Direction.LONG if row.side == "buy" else Direction.SHORT
        fills: list[Fill] = []
        if row.filled_quantity:
            fill_session = (row.filled_at or row.created_at).date()
            fills.append(
                Fill(
                    session=fill_session,
                    price=row.average_fill_price or 0.0,
                    shares=row.filled_quantity,
                    commission=row.commission,
                    fees=row.fees,
                    slippage=row.slippage,
                )
            )
        return BrokerOrder(
            order_id=row.order_id,
            symbol=row.symbol,
            direction=direction,
            status=OrderStatus(row.status),
            requested_shares=row.quantity,
            filled_shares=row.filled_quantity,
            average_fill_price=row.average_fill_price,
            fills=fills,
            submitted_at=row.created_at,
            updated_at=row.filled_at or row.created_at,
            reasons=[row.reject_reason] if row.reject_reason else [],
        )

    @staticmethod
    def _trade_row_to_trade(row: PaperTradeRow) -> Trade:
        fills = [
            Fill(
                session=row.entry_session,
                price=row.entry_price,
                shares=row.shares,
                commission=row.commission_total,
                fees=row.fees_total,
            )
        ]
        return Trade(
            trade_id=row.trade_id,
            signal_id=row.signal_id,
            symbol=row.symbol,
            strategy=row.strategy,
            direction=Direction(row.direction),
            entry_session=row.entry_session,
            entry_price=row.entry_price,
            shares=row.shares,
            stop_loss=row.stop_loss,
            targets=list(row.targets or []),
            exit_session=row.exit_session,
            exit_price=row.exit_price,
            exit_reason=ExitReason(row.exit_reason) if row.exit_reason else None,
            fills=fills,
            commission_total=row.commission_total,
            fees_total=row.fees_total,
            slippage_total=row.slippage_total,
            borrow_cost_total=row.borrow_cost_total,
            mfe_pct=row.mfe_pct,
            mae_pct=row.mae_pct,
            mfe_r=row.mfe_r,
            mae_r=row.mae_r,
            initial_risk_per_share=row.initial_risk_per_share,
            thesis_intact_at_exit=row.thesis_intact_at_exit,
            regime_at_entry=MarketRegime(row.regime_at_entry),
            sector=row.sector,
            market_cap_bucket=row.market_cap_bucket,
            days_to_earnings_at_entry=row.days_to_earnings_at_entry,
            confidence_at_entry=row.confidence_at_entry,
            sentiment_source=row.sentiment_source,
            notes=list(row.notes or []),
        )
