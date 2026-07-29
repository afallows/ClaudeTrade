"""Persistent paper portfolio.

Mirrors the backtest portfolio's accounting but stores state in the database so
it survives restarts, and records enough per trade to grade it honestly later:
signal timestamp, order timestamp, simulated fill, size, entry, stop, targets,
every adjustment, exit, exit reason, P&L, MFE/MAE, and whether the original
thesis still held at exit.

Two integrity properties, both enforced below *and* by database triggers
(migration 002):

* A **closed trade is final.** Its exit price and outcome cannot be rewritten,
  and it cannot be deleted. Reopening a loser or quietly dropping it are the two
  easiest ways to flatter a win/loss ratio, so neither has a code path.
* **Open trades are always classified eventually.** ``apply_time_stops`` closes
  anything past its time stop, so a losing position cannot be parked
  indefinitely and excluded from the statistics.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.db.models import PaperAccountRow, PaperEquityCurveRow, PaperTradeRow
from claudetrade.db.session import Database
from claudetrade.domain import Bar, Direction, ExitReason, TradeOutcome
from claudetrade.logging_setup import audit_event, get_logger
from claudetrade.risk.limits import OpenPosition, PortfolioState
from claudetrade.utils.timeutils import utc_now

log = get_logger(__name__)

#: Net returns inside this band count as breakeven and are excluded from the
#: win and loss counts alike.
BREAKEVEN_THRESHOLD_PCT = 0.05


class PaperTradeError(RuntimeError):
    """An operation would have violated the paper ledger's integrity rules."""


@dataclass(slots=True)
class PaperPositionView:
    """Open position with its live mark, for display and risk arithmetic."""

    trade_id: str
    symbol: str
    strategy: str
    direction: Direction
    shares: int
    entry_price: float
    stop_loss: float
    targets: list[float]
    entry_session: dt.date
    last_price: float
    sector: str = ""
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    initial_risk_per_share: float = 0.0
    days_held: int = 0
    time_stop_days: int = 20

    @property
    def unrealised_pnl(self) -> float:
        return (self.last_price - self.entry_price) * self.shares * self.direction.sign

    @property
    def unrealised_pct(self) -> float:
        notional = abs(self.entry_price * self.shares)
        return 100.0 * self.unrealised_pnl / notional if notional else 0.0

    @property
    def open_risk(self) -> float:
        """Dollars still at risk to the current stop, floored at zero."""
        per_share = (self.entry_price - self.stop_loss) * self.direction.sign
        return max(0.0, per_share * self.shares)

    @property
    def r_multiple(self) -> float:
        risk = abs(self.initial_risk_per_share * self.shares)
        return self.unrealised_pnl / risk if risk > 0 else 0.0

    def needs_attention(self) -> list[str]:
        """Reasons this position should be looked at today."""
        notes: list[str] = []
        if self.direction is Direction.LONG and self.last_price <= self.stop_loss:
            notes.append("price is at or below the stop")
        if self.direction is Direction.SHORT and self.last_price >= self.stop_loss:
            notes.append("price is at or above the stop")
        if self.targets:
            first = self.targets[0]
            hit = (
                self.last_price >= first
                if self.direction is Direction.LONG
                else self.last_price <= first
            )
            if hit:
                notes.append("first target reached")
        if self.days_held >= self.time_stop_days:
            notes.append(f"time stop due ({self.days_held} sessions held)")
        return notes


class PaperPortfolio:
    """Database-backed paper account."""

    def __init__(self, config: AppConfig, db: Database, *, account_name: str = "default"):
        self.config = config
        self.db = db
        self.account_name = account_name
        self.account_id = self._ensure_account()

    # --- account ----------------------------------------------------------

    def _ensure_account(self) -> int:
        with self.db.session() as session:
            row = session.execute(
                select(PaperAccountRow).where(PaperAccountRow.name == self.account_name)
            ).scalar_one_or_none()
            if row is None:
                starting = self.config.risk.account_size_usd
                row = PaperAccountRow(
                    name=self.account_name,
                    starting_cash=starting,
                    cash=starting,
                    equity=starting,
                    high_water_equity=starting,
                )
                session.add(row)
                session.flush()
                audit_event(
                    "paper_account_created", account=self.account_name, starting_cash=starting
                )
            return row.id

    def account(self) -> PaperAccountRow:
        with self.db.read_session() as session:
            row = session.get(PaperAccountRow, self.account_id)
            if row is None:  # pragma: no cover - created in __init__
                raise PaperTradeError(f"paper account {self.account_name} is missing")
            return row

    def engage_kill_switch(self, engaged: bool = True) -> None:
        """Halt or resume new entries. Existing stops remain in force."""
        with self.db.session() as session:
            row = session.get(PaperAccountRow, self.account_id)
            row.kill_switch_engaged = engaged
            row.updated_at = utc_now()
        audit_event("paper_kill_switch", account=self.account_name, engaged=engaged)
        log.warning("paper kill switch %s", "ENGAGED" if engaged else "released")

    # --- positions ---------------------------------------------------------

    def open_trades(self) -> list[PaperTradeRow]:
        with self.db.read_session() as session:
            return list(
                session.execute(
                    select(PaperTradeRow).where(
                        PaperTradeRow.account_id == self.account_id,
                        PaperTradeRow.exit_session.is_(None),
                    )
                ).scalars()
            )

    def closed_trades(self, *, limit: int | None = None) -> list[PaperTradeRow]:
        with self.db.read_session() as session:
            stmt = (
                select(PaperTradeRow)
                .where(
                    PaperTradeRow.account_id == self.account_id,
                    PaperTradeRow.exit_session.is_not(None),
                )
                .order_by(PaperTradeRow.exit_session.desc())
            )
            if limit:
                stmt = stmt.limit(limit)
            return list(session.execute(stmt).scalars())

    def positions(
        self, marks: dict[str, float], *, as_of: dt.date | None = None
    ) -> list[PaperPositionView]:
        """Open positions marked to the supplied prices.

        Args:
            as_of: Date used to compute holding periods. Callers replaying
                history must pass the session being replayed; defaulting to the
                wall clock there would age every position to today and fire
                every time stop at once.
        """
        today = as_of or utc_now().date()
        out: list[PaperPositionView] = []
        for row in self.open_trades():
            last = marks.get(row.symbol, row.entry_price)
            out.append(
                PaperPositionView(
                    trade_id=row.trade_id,
                    symbol=row.symbol,
                    strategy=row.strategy,
                    direction=Direction(row.direction),
                    shares=row.shares,
                    entry_price=row.entry_price,
                    stop_loss=row.stop_loss,
                    targets=list(row.targets or []),
                    entry_session=row.entry_session,
                    last_price=last,
                    sector=row.sector,
                    mfe_pct=row.mfe_pct,
                    mae_pct=row.mae_pct,
                    initial_risk_per_share=row.initial_risk_per_share,
                    days_held=(today - row.entry_session).days,
                )
            )
        return out

    def portfolio_state(
        self, marks: dict[str, float], *, as_of: dt.date | None = None
    ) -> PortfolioState:
        """Snapshot in the shape the shared risk limits expect."""
        account = self.account()
        today = as_of or utc_now().date()
        views = self.positions(marks, as_of=today)
        unrealised = sum(v.unrealised_pnl for v in views)
        return PortfolioState(
            equity=account.cash + sum(abs(v.entry_price * v.shares) for v in views) + unrealised,
            cash=account.cash,
            positions=[
                OpenPosition(
                    symbol=v.symbol,
                    direction=v.direction,
                    shares=v.shares,
                    entry_price=v.entry_price,
                    stop_price=v.stop_loss,
                    sector=v.sector,
                    correlation_group=v.sector,
                )
                for v in views
            ],
            realised_pnl_today=self._realised_since(today),
            realised_pnl_week=self._realised_since(today - dt.timedelta(days=7)),
            kill_switch_engaged=account.kill_switch_engaged
            or self.config.trading.kill_switch_engaged,
        )

    def _realised_since(self, since: dt.date) -> float:
        with self.db.read_session() as session:
            rows = session.execute(
                select(PaperTradeRow.net_pnl).where(
                    PaperTradeRow.account_id == self.account_id,
                    PaperTradeRow.exit_session.is_not(None),
                    PaperTradeRow.exit_session >= since,
                )
            ).scalars()
            return float(sum(rows))

    # --- marking ------------------------------------------------------------

    def mark_to_market(self, bars: dict[str, Bar], session_date: dt.date) -> None:
        """Update MFE/MAE on open positions and append an equity-curve point.

        MFE/MAE are tracked on every mark rather than only at exit, because the
        excursion a trade endured is not recoverable from its entry and exit
        prices alone -- and it is the honest way to see whether a winner was
        ever deeply underwater.
        """
        with self.db.session() as session:
            rows = session.execute(
                select(PaperTradeRow).where(
                    PaperTradeRow.account_id == self.account_id,
                    PaperTradeRow.exit_session.is_(None),
                )
            ).scalars().all()

            for row in rows:
                bar = bars.get(row.symbol)
                if bar is None:
                    continue
                sign = Direction(row.direction).sign
                # Excursions are measured against the bar's extremes, not its
                # close: the position genuinely experienced those levels.
                best = bar.high if sign > 0 else bar.low
                worst = bar.low if sign > 0 else bar.high
                fav_pct = 100.0 * (best - row.entry_price) * sign / row.entry_price
                adv_pct = 100.0 * (worst - row.entry_price) * sign / row.entry_price
                row.mfe_pct = max(row.mfe_pct, fav_pct)
                row.mae_pct = min(row.mae_pct, adv_pct)
                if row.initial_risk_per_share > 0:
                    row.mfe_r = max(
                        row.mfe_r, (best - row.entry_price) * sign / row.initial_risk_per_share
                    )
                    row.mae_r = min(
                        row.mae_r, (worst - row.entry_price) * sign / row.initial_risk_per_share
                    )
                row.updated_at = utc_now()

            account = session.get(PaperAccountRow, self.account_id)
            open_value = 0.0
            for row in rows:
                bar = bars.get(row.symbol)
                price = bar.close if bar else row.entry_price
                sign = Direction(row.direction).sign
                open_value += abs(row.entry_price * row.shares) + (
                    (price - row.entry_price) * row.shares * sign
                )
            equity = account.cash + open_value
            account.equity = equity
            account.high_water_equity = max(account.high_water_equity, equity)
            account.updated_at = utc_now()

            drawdown = (
                100.0 * (account.high_water_equity - equity) / account.high_water_equity
                if account.high_water_equity > 0
                else 0.0
            )
            heat = (
                100.0
                * sum(
                    max(
                        0.0,
                        (r.entry_price - r.stop_loss) * Direction(r.direction).sign * r.shares,
                    )
                    for r in rows
                )
                / equity
                if equity > 0
                else 0.0
            )
            existing = session.execute(
                select(PaperEquityCurveRow).where(
                    PaperEquityCurveRow.account_id == self.account_id,
                    PaperEquityCurveRow.session == session_date,
                )
            ).scalar_one_or_none()
            point = existing or PaperEquityCurveRow(
                account_id=self.account_id, session=session_date
            )
            point.equity = equity
            point.cash = account.cash
            point.open_positions = len(rows)
            point.portfolio_heat_pct = heat
            point.drawdown_pct = drawdown
            if existing is None:
                session.add(point)

    # --- closing -------------------------------------------------------------

    def close_trade(
        self,
        trade_id: str,
        *,
        exit_session: dt.date,
        exit_price: float,
        reason: ExitReason,
        commission: float = 0.0,
        fees: float = 0.0,
        slippage: float = 0.0,
        thesis_intact: bool | None = None,
        note: str = "",
    ) -> PaperTradeRow:
        """Close an open position and classify it.

        Raises:
            PaperTradeError: if the trade is unknown or already closed. A closed
                trade is final; there is deliberately no path to reopen one.
        """
        with self.db.session() as session:
            row = session.get(PaperTradeRow, trade_id)
            if row is None:
                raise PaperTradeError(f"unknown paper trade {trade_id}")
            if row.exit_session is not None:
                raise PaperTradeError(
                    f"paper trade {trade_id} closed on {row.exit_session} and is final; "
                    "closed trades cannot be reopened or rewritten"
                )

            sign = Direction(row.direction).sign
            gross = (exit_price - row.entry_price) * row.shares * sign
            borrow = self._borrow_cost(row, exit_session)
            net = gross - commission - fees - borrow

            row.exit_session = exit_session
            row.exit_price = exit_price
            row.exit_reason = reason.value
            row.gross_pnl = gross
            row.commission_total += commission
            row.fees_total += fees
            row.slippage_total += slippage
            row.borrow_cost_total = borrow
            row.net_pnl = net
            notional = abs(row.entry_price * row.shares)
            ret_pct = 100.0 * net / notional if notional else 0.0
            row.outcome = _classify(ret_pct).value
            row.r_multiple = (
                net / abs(row.initial_risk_per_share * row.shares)
                if row.initial_risk_per_share > 0
                else 0.0
            )
            row.thesis_intact_at_exit = thesis_intact
            if note:
                row.notes = [*(row.notes or []), note]
            row.updated_at = utc_now()

            account = session.get(PaperAccountRow, self.account_id)
            account.cash += abs(row.entry_price * row.shares) + net
            account.realised_pnl += net
            account.updated_at = utc_now()

            outcome, symbol = row.outcome, row.symbol

        audit_event(
            "paper_trade_closed",
            trade_id=trade_id,
            symbol=symbol,
            outcome=outcome,
            reason=reason.value,
            net_pnl=round(net, 2),
        )
        log.info("closed paper trade %s (%s): %s %.2f", trade_id, symbol, outcome, net)
        return row

    def _borrow_cost(self, row: PaperTradeRow, exit_session: dt.date) -> float:
        """Borrow cost for a short, accrued over the holding period."""
        if Direction(row.direction) is not Direction.SHORT:
            return 0.0
        days = max(0, (exit_session - row.entry_session).days)
        annual = self.config.costs.short_borrow_annual_pct / 100.0
        return abs(row.entry_price * row.shares) * annual * days / 365.0

    def apply_time_stops(self, marks: dict[str, float], session_date: dt.date) -> list[str]:
        """Close every position that has exceeded its holding limit.

        This is the mechanism that stops the win/loss ratio being improved by
        simply never closing losers.
        """
        closed: list[str] = []
        limit = self.config.signals.max_holding_days
        for view in self.positions(marks, as_of=session_date):
            if view.days_held < limit:
                continue
            self.close_trade(
                view.trade_id,
                exit_session=session_date,
                exit_price=view.last_price,
                reason=ExitReason.TIME_STOP,
                note=f"time stop after {view.days_held} sessions",
            )
            closed.append(view.trade_id)
        if closed:
            log.info("time stop closed %d paper positions", len(closed))
        return closed

    # --- reporting -------------------------------------------------------------

    def performance(self) -> dict[str, object]:
        """Headline statistics over closed paper trades.

        Reports the win/loss ratio *alongside* expectancy and the average
        win/loss sizes, and flags a sample too small to mean anything --
        a ratio without those companions is not interpretable.
        """
        trades = self.closed_trades()
        wins = [t for t in trades if t.outcome == TradeOutcome.WIN.value]
        losses = [t for t in trades if t.outcome == TradeOutcome.LOSS.value]
        breakeven = [t for t in trades if t.outcome == TradeOutcome.BREAKEVEN.value]

        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0
        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        graded = len(wins) + len(losses)

        warnings: list[str] = []
        if len(trades) < self.config.backtest.min_trades_for_confidence:
            warnings.append(
                f"only {len(trades)} closed trades, below the "
                f"{self.config.backtest.min_trades_for_confidence} needed for a meaningful "
                "win/loss ratio"
            )
        expectancy = (sum(t.net_pnl for t in trades) / len(trades)) if trades else 0.0
        if losses and wins and expectancy <= 0 and len(wins) > len(losses):
            warnings.append(
                "win/loss ratio is above 1 but expectancy is negative: wins are too small "
                "relative to losses"
            )
        if avg_loss and abs(avg_loss) > 2 * abs(avg_win) and wins:
            warnings.append("average loss exceeds twice the average win")

        return {
            "closed_trades": len(trades),
            "open_trades": len(self.open_trades()),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "win_loss_ratio": (len(wins) / len(losses)) if losses else float("inf"),
            "win_rate": (len(wins) / graded) if graded else 0.0,
            "average_win": avg_win,
            "average_loss": avg_loss,
            "profit_factor": (gross_profit / gross_loss) if gross_loss else float("inf"),
            "expectancy": expectancy,
            "total_net_pnl": sum(t.net_pnl for t in trades),
            "warnings": warnings,
        }

    def equity_curve(self) -> list[PaperEquityCurveRow]:
        with self.db.read_session() as session:
            return list(
                session.execute(
                    select(PaperEquityCurveRow)
                    .where(PaperEquityCurveRow.account_id == self.account_id)
                    .order_by(PaperEquityCurveRow.session)
                ).scalars()
            )


def _classify(net_return_pct: float) -> TradeOutcome:
    if abs(net_return_pct) <= BREAKEVEN_THRESHOLD_PCT:
        return TradeOutcome.BREAKEVEN
    return TradeOutcome.WIN if net_return_pct > 0 else TradeOutcome.LOSS
