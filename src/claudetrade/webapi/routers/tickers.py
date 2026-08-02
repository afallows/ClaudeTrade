"""Ticker list and the ticker-detail bundle (candlestick + sentiment + signals).

Price/sentiment/earnings queries reuse ``claudetrade.ui.data_access`` verbatim
-- the same read-only, side-effect-free helpers the Streamlit ticker-detail
screen uses -- rather than re-querying the schema from scratch.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query

from claudetrade.pipeline import Pipeline
from claudetrade.ui.data_access import earnings_dates, known_symbols, price_bars, sentiment_timeline
from claudetrade.utils.timeutils import utc_now
from claudetrade.webapi.deps import get_pipeline
from claudetrade.webapi.schemas import BarOut, SentimentPointOut, TickerDetailOut
from claudetrade.webapi.serialize import (
    active_signal_for,
    compute_indicators,
    signal_to_detail,
    signal_to_row,
)

router = APIRouter(prefix="/api", tags=["tickers"])


@router.get("/tickers", response_model=list[str])
def list_tickers(pipeline: Pipeline = Depends(get_pipeline)) -> list[str]:
    """Every symbol with at least one stored bar, for the ticker picker."""
    return known_symbols(pipeline.db)


@router.get("/tickers/{symbol}", response_model=TickerDetailOut)
def ticker_detail(
    symbol: str,
    lookback_days: int = Query(default=180, ge=1, le=3650),
    pipeline: Pipeline = Depends(get_pipeline),
) -> TickerDetailOut:
    """Full technical + sentiment + signal picture for one symbol.

    Mirrors ``ui.screens.ticker_detail.page_ticker_detail``: bars for the
    requested lookback window, the daily sentiment/mention timeline clipped to
    the same window, every known earnings date, the current (most recent
    tradable, else most recent) signal in full, and up to 25 rows of signal
    history.
    """
    symbol = symbol.upper()
    known = known_symbols(pipeline.db)
    if symbol not in known:
        raise HTTPException(
            status_code=404,
            detail=f"no stored price history for {symbol}; run a data refresh first",
        )

    end = utc_now().date()
    start = end - dt.timedelta(days=lookback_days)

    bars = price_bars(pipeline.db, symbol, start=start, end=end)
    signals = pipeline.ledger.for_symbol(symbol, limit=50)
    report_dates = earnings_dates(pipeline.db, symbol)
    sentiment = [p for p in sentiment_timeline(pipeline.db, symbol) if start <= p.session <= end]

    sig = active_signal_for(signals)
    current_signal = None
    if sig is not None:
        status = pipeline.ledger.current_status(sig.signal_id)
        current_signal = signal_to_detail(sig, status)

    history_rows = []
    for hist_sig in signals[:25]:
        status = pipeline.ledger.current_status(hist_sig.signal_id)
        history_rows.append(signal_to_row(hist_sig, status))

    return TickerDetailOut(
        symbol=symbol,
        bars=[
            BarOut(
                session=b.session,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                adj_close=b.adj_close,
            )
            for b in bars
        ],
        indicators=compute_indicators(bars),
        sentiment=[
            SentimentPointOut(
                session=p.session,
                post_count=p.post_count,
                unique_authors=p.unique_authors,
                engagement_weighted=p.engagement_weighted,
                bull_bear_ratio=p.bull_bear_ratio,
                manipulation_risk=p.manipulation_risk,
                confidence=p.confidence,
            )
            for p in sentiment
        ],
        earnings_dates=report_dates,
        current_signal=current_signal,
        signal_history=history_rows,
        price_note=None
        if bars
        else f"No price history stored for {symbol} in the last {lookback_days} days. "
        "Run `claudetrade refresh`.",
        sentiment_note=None
        if sentiment
        else "No sentiment/mention data for this symbol in the selected window -- run "
        "`claudetrade refresh` with social sources enabled to populate it.",
    )


__all__ = ["router"]
