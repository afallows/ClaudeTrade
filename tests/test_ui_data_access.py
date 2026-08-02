"""Unit tests for ``claudetrade.ui.data_access``'s read-only query helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.config import AppConfig
from claudetrade.db.models import EarningsEventRow, PriceBar, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.signals.ledger import SignalLedger
from claudetrade.signals.research import ResearchLedger
from claudetrade.ui.data_access import (
    data_freshness,
    earnings_dates,
    known_symbols,
    price_bars,
    research_overlay,
    sentiment_timeline,
)


def test_data_freshness_reports_no_data_on_empty_db(memory_db):
    freshness = data_freshness(memory_db)
    assert not freshness.has_data
    assert freshness.latest_session is None
    assert freshness.symbol_count == 0


def test_data_freshness_reports_latest_session_and_symbol_count(memory_db):
    with memory_db.session() as session:
        session.add(PriceBar(symbol="AAA", session=dt.date(2024, 1, 2), open=1, high=1, low=1, close=1, volume=1))
        session.add(PriceBar(symbol="AAA", session=dt.date(2024, 1, 3), open=1, high=1, low=1, close=1, volume=1))
        session.add(PriceBar(symbol="BBB", session=dt.date(2024, 1, 2), open=1, high=1, low=1, close=1, volume=1))

    freshness = data_freshness(memory_db)
    assert freshness.has_data
    assert freshness.latest_session == dt.date(2024, 1, 3)
    assert freshness.symbol_count == 2


def test_price_bars_returns_oldest_first_and_respects_window(memory_db):
    with memory_db.session() as session:
        for i in range(5):
            session.add(
                PriceBar(
                    symbol="AAA",
                    session=dt.date(2024, 1, 1) + dt.timedelta(days=i),
                    open=10 + i, high=11 + i, low=9 + i, close=10.5 + i, volume=100,
                )
            )

    bars = price_bars(memory_db, "AAA")
    assert [b.session for b in bars] == [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(5)]

    windowed = price_bars(memory_db, "AAA", start=dt.date(2024, 1, 2), end=dt.date(2024, 1, 3))
    assert [b.session for b in windowed] == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]


def test_price_bars_empty_for_unknown_symbol(memory_db):
    assert price_bars(memory_db, "NOPE") == []


def test_sentiment_timeline_filters_by_source(memory_db):
    with memory_db.session() as session:
        session.add(
            SymbolSentimentDaily(symbol="AAA", session=dt.date(2024, 1, 2), source="all", post_count=10)
        )
        session.add(
            SymbolSentimentDaily(symbol="AAA", session=dt.date(2024, 1, 2), source="reddit", post_count=4)
        )

    points = sentiment_timeline(memory_db, "AAA")
    assert len(points) == 1
    assert points[0].post_count == 10

    reddit_points = sentiment_timeline(memory_db, "AAA", source="reddit")
    assert reddit_points[0].post_count == 4


def test_earnings_dates_ascending_and_deduplicated(memory_db):
    with memory_db.session() as session:
        session.add(EarningsEventRow(symbol="AAA", report_date=dt.date(2024, 4, 15), source="a"))
        session.add(EarningsEventRow(symbol="AAA", report_date=dt.date(2024, 1, 15), source="b"))

    dates = earnings_dates(memory_db, "AAA")
    assert dates == [dt.date(2024, 1, 15), dt.date(2024, 4, 15)]


def test_known_symbols_sorted_and_distinct(memory_db):
    with memory_db.session() as session:
        session.add(PriceBar(symbol="ZZZ", session=dt.date(2024, 1, 1), open=1, high=1, low=1, close=1, volume=1))
        session.add(PriceBar(symbol="ZZZ", session=dt.date(2024, 1, 2), open=1, high=1, low=1, close=1, volume=1))
        session.add(PriceBar(symbol="AAA", session=dt.date(2024, 1, 1), open=1, high=1, low=1, close=1, volume=1))

    assert known_symbols(memory_db) == ["AAA", "ZZZ"]


# --- research_overlay ---------------------------------------------------------


def _record(db: Database, sig) -> None:
    SignalLedger(db).record(sig)


def test_research_overlay_reports_overall_score_as_effective_when_no_research(
    tmp_db: Database, tmp_app_config: AppConfig, make_signal
):
    sig = make_signal(symbol="AAA", overall_score=70.0)
    _record(tmp_db, sig)

    overlay = research_overlay(tmp_db, [sig], tmp_app_config)

    result = overlay[sig.signal_id]
    assert result.effective_score == 70.0
    assert result.has_research is False
    assert result.latest is None


def test_research_overlay_computes_effective_score_from_latest_revision(
    tmp_db: Database, tmp_app_config: AppConfig, make_signal
):
    sig = make_signal(symbol="AAA", overall_score=60.0)
    _record(tmp_db, sig)

    ResearchLedger(tmp_db).append_research_revision(
        sig.signal_id,
        thesis=None,
        invalidation=None,
        score_adjustments={"technical_setup": 20.0},
        rationale="Strong new catalyst confirmed by two independent sources.",
        sources=["https://example.com/a", "https://example.com/b"],
        config=tmp_app_config,
    )

    overlay = research_overlay(tmp_db, [sig], tmp_app_config)

    result = overlay[sig.signal_id]
    assert result.has_research is True
    # technical_setup's default weight (0.20) applied to the +20.0 delta over
    # the full 1.00 total weight -- a deterministic +4.0 move.
    assert result.effective_score == pytest.approx(64.0)
    assert result.latest is not None
    assert result.latest["revision"] == 1


def test_research_overlay_uses_one_batched_query_for_multiple_signals(
    tmp_db: Database, tmp_app_config: AppConfig, make_signal
):
    """Same F26 discipline as ``mcp_server.get_signals``/``webapi.routers
    .signals.list_signals``: research must be fetched once for the whole
    page, never per-row."""
    from sqlalchemy import event

    signals = [make_signal(symbol=f"SYM{i}", overall_score=50.0 + i) for i in range(5)]
    for sig in signals:
        _record(tmp_db, sig)

    statements: list[str] = []

    def _record_stmt(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(tmp_db.engine, "before_cursor_execute", _record_stmt)
    try:
        overlay = research_overlay(tmp_db, signals, tmp_app_config)
    finally:
        event.remove(tmp_db.engine, "before_cursor_execute", _record_stmt)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(overlay) == 5
    assert len(selects) == 1
