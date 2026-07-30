"""Unit tests for ``claudetrade.ui.data_access``'s read-only query helpers."""

from __future__ import annotations

import datetime as dt

from claudetrade.db.models import EarningsEventRow, PriceBar, SymbolSentimentDaily
from claudetrade.ui.data_access import (
    data_freshness,
    earnings_dates,
    known_symbols,
    price_bars,
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
