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
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from claudetrade.backtest.costs import CostModel
from claudetrade.config import AppConfig
from claudetrade.db.models import PaperOrderRow, PaperTradeRow
from claudetrade.db.session import Database
from claudetrade.domain import Bar, Direction, Signal, SignalStatus
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


class PaperBroker:
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
