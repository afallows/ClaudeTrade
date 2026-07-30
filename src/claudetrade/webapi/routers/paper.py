"""Paper account, performance, and the Open Paper Trade action.

``POST /api/paper/open`` reuses the exact seam ``ui.screens.scanner.
_open_paper_trade`` uses: ``PaperBroker.next_bar_after`` to find the first
fillable bar without look-ahead, then ``BrokerProvider.submit_order`` (the
guarded ``OrderRequest``/``BrokerOrder`` path shared with every future broker
adapter) -- so a fill, a rejection, and "not fillable yet" are reported with
the same honesty the Streamlit action has, never a fabricated success.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from claudetrade.brokers.base import OrderRequest, TradingHaltedError
from claudetrade.config import AppConfig
from claudetrade.paper.broker import PaperBroker
from claudetrade.paper.portfolio import PaperPortfolio
from claudetrade.pipeline import Pipeline
from claudetrade.webapi.deps import get_config, get_pipeline
from claudetrade.webapi.schemas import (
    ClosedTradeOut,
    EquityPointOut,
    PaperAccountOut,
    PaperAccountResponse,
    PaperOpenRequest,
    PaperOpenResponse,
    PaperPositionOut,
    PerformanceOut,
)

router = APIRouter(prefix="/api/paper", tags=["paper"])


@router.get("/account", response_model=PaperAccountResponse)
def paper_account(
    pipeline: Pipeline = Depends(get_pipeline), config: AppConfig = Depends(get_config)
) -> PaperAccountResponse:
    portfolio = PaperPortfolio(config, pipeline.db)
    account = portfolio.account()
    broker = PaperBroker(config, pipeline.db, portfolio=portfolio)
    marks = {s: b.close for s, b in broker.latest_bars_for_open_positions().items()}
    positions = portfolio.positions(marks)
    closed = portfolio.closed_trades(limit=10)
    curve = portfolio.equity_curve()

    return PaperAccountResponse(
        account=PaperAccountOut(
            equity=account.equity,
            cash=account.cash,
            realised_pnl=account.realised_pnl,
            kill_switch_engaged=bool(
                account.kill_switch_engaged or config.trading.kill_switch_engaged
            ),
        ),
        positions=[
            PaperPositionOut(
                trade_id=p.trade_id,
                symbol=p.symbol,
                direction=str(p.direction),
                shares=p.shares,
                entry_price=p.entry_price,
                last_price=p.last_price,
                unrealised_pnl=p.unrealised_pnl,
                unrealised_pct=p.unrealised_pct,
                days_held=p.days_held,
                needs_attention=p.needs_attention(),
            )
            for p in positions
        ],
        closed_trades=[
            ClosedTradeOut(
                trade_id=t.trade_id,
                symbol=t.symbol,
                direction=t.direction,
                exit_session=t.exit_session,
                outcome=t.outcome,
                net_pnl=t.net_pnl,
                r_multiple=t.r_multiple,
                reason=t.exit_reason,
            )
            for t in closed
        ],
        equity_curve=[EquityPointOut(session=p.session, equity=p.equity) for p in curve],
        equity_curve_note=None
        if curve
        else (
            "No recorded equity history yet -- the curve fills in once a paper position "
            "has been opened and marked at least once. POST /api/paper/open, then "
            "POST /api/refresh to mark it."
        ),
    )


@router.get("/performance", response_model=PerformanceOut)
def paper_performance(
    pipeline: Pipeline = Depends(get_pipeline), config: AppConfig = Depends(get_config)
) -> PerformanceOut:
    portfolio = PaperPortfolio(config, pipeline.db)
    perf = portfolio.performance()
    closed_count = perf["closed_trades"]

    floor = config.backtest.min_trades_for_validation
    is_significant = closed_count >= floor
    reason = (
        None
        if is_significant
        else f"only {closed_count} completed paper trade(s), below the {floor}-trade minimum"
    )

    def _ratio(value: float) -> tuple[float | None, str]:
        if closed_count == 0:
            return None, "n/a"
        if value == float("inf"):
            return None, "∞"
        return value, f"{value:.2f}"

    wl_value, wl_display = _ratio(perf["win_loss_ratio"])
    pf_value, pf_display = _ratio(perf["profit_factor"])

    curve = portfolio.equity_curve()
    max_dd = max((p.drawdown_pct for p in curve), default=None)

    return PerformanceOut(
        closed_trades=closed_count,
        open_trades=perf["open_trades"],
        win_loss_ratio=wl_value,
        win_loss_display=wl_display,
        win_rate=perf["win_rate"] if closed_count else None,
        expectancy=perf["expectancy"] if closed_count else None,
        average_win=perf["average_win"] if perf["wins"] else None,
        average_loss=perf["average_loss"] if perf["losses"] else None,
        profit_factor=pf_value,
        profit_factor_display=pf_display,
        max_drawdown_pct=max_dd,
        max_drawdown_note=None if curve else "No equity history recorded yet.",
        is_significant=is_significant,
        significance_reason=reason,
        warnings=list(perf.get("warnings", [])),
    )


@router.post("/open", response_model=PaperOpenResponse)
def paper_open(
    body: PaperOpenRequest,
    pipeline: Pipeline = Depends(get_pipeline),
    config: AppConfig = Depends(get_config),
) -> PaperOpenResponse:
    sig = pipeline.ledger.get(body.signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"unknown signal {body.signal_id}")

    broker = PaperBroker(config, pipeline.db)
    next_bar = broker.next_bar_after(sig.symbol, sig.session)
    if next_bar is None:
        return PaperOpenResponse(
            accepted=False,
            status="not_fillable",
            order_id=None,
            symbol=sig.symbol,
            direction=str(sig.direction),
            requested_shares=sig.plan.shares,
            filled_shares=0,
            fill_price=None,
            fill_session=None,
            reasons=[],
            message=(
                f"Not fillable yet: no stored bar for {sig.symbol} after {sig.session}. "
                "Run a data refresh, then try again."
            ),
        )

    request = OrderRequest(signal=sig, next_bar=next_bar, marks=broker.marks_for_open_positions())
    try:
        order = broker.submit_order(request)
    except TradingHaltedError as exc:
        return PaperOpenResponse(
            accepted=False,
            status="rejected",
            order_id=None,
            symbol=sig.symbol,
            direction=str(sig.direction),
            requested_shares=sig.plan.shares,
            filled_shares=0,
            fill_price=None,
            fill_session=None,
            reasons=[str(exc)],
            message=f"Refused: {exc}",
        )

    filled = order.status.value == "filled"
    return PaperOpenResponse(
        accepted=filled,
        status="filled" if filled else "rejected",
        order_id=order.order_id,
        symbol=order.symbol,
        direction=str(order.direction),
        requested_shares=order.requested_shares,
        filled_shares=order.filled_shares,
        fill_price=order.average_fill_price,
        fill_session=next_bar.session if filled else None,
        reasons=order.reasons,
        message=(
            f"Filled: {order.symbol} {order.filled_shares} shares @ "
            f"{order.average_fill_price:.2f} on {next_bar.session} (order {order.order_id})"
            if filled
            else f"Rejected: {'; '.join(order.reasons) or 'no reason given'}"
        ),
    )


__all__ = ["router"]
