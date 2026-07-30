"""FastAPI ``TestClient`` coverage for the /api/tickers endpoints."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.models import EarningsEventRow, PriceBar, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.pipeline import Pipeline
from claudetrade.signals.ledger import SignalLedger
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    return TestClient(app)


def _add_bars(db: Database, symbol: str, n: int = 10) -> None:
    with db.session() as session:
        for i in range(n):
            session.add(
                PriceBar(
                    symbol=symbol,
                    session=dt.date(2024, 1, 2) + dt.timedelta(days=i),
                    open=10 + i,
                    high=11 + i,
                    low=9 + i,
                    close=10.5 + i,
                    volume=1_000_000,
                )
            )


def test_list_tickers_empty_db(client: TestClient):
    resp = client.get("/api/tickers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_tickers_returns_symbols_with_bars(client: TestClient, tmp_db):
    _add_bars(tmp_db, "AAA")
    _add_bars(tmp_db, "BBB")
    resp = client.get("/api/tickers")
    assert resp.status_code == 200
    assert resp.json() == ["AAA", "BBB"]


def test_ticker_detail_404_for_unknown_symbol(client: TestClient):
    resp = client.get("/api/tickers/NOPE")
    assert resp.status_code == 404


def test_ticker_detail_reports_honest_notes_when_data_missing(client: TestClient, tmp_db):
    # Symbol known via known_symbols() (has a bar far outside the lookback
    # window) but nothing inside the requested window -- the price note must
    # explain why the chart is empty rather than silently rendering nothing.
    _add_bars(tmp_db, "STALE", n=1)
    with tmp_db.session() as session:
        session.execute(
            PriceBar.__table__.update()
            .where(PriceBar.symbol == "STALE")
            .values(session=dt.date(2000, 1, 1))
        )

    resp = client.get("/api/tickers/STALE", params={"lookback_days": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["bars"] == []
    assert "No price history stored" in body["price_note"]
    assert "No sentiment/mention data" in body["sentiment_note"]
    assert body["current_signal"] is None
    assert body["signal_history"] == []


def test_ticker_detail_bundles_bars_sentiment_earnings_and_signals(
    client: TestClient, tmp_db, make_signal
):
    _add_bars(tmp_db, "FULL", n=20)
    with tmp_db.session() as session:
        session.add(
            SymbolSentimentDaily(
                symbol="FULL", session=dt.date(2024, 1, 5), source="all", post_count=12
            )
        )
        session.add(
            EarningsEventRow(symbol="FULL", report_date=dt.date(2024, 1, 20), source="synthetic")
        )
    sig = make_signal(symbol="FULL", session=dt.date(2024, 1, 3))
    SignalLedger(tmp_db).record(sig)

    resp = client.get("/api/tickers/FULL", params={"lookback_days": 3650})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["bars"]) == 20
    assert body["price_note"] is None
    assert len(body["sentiment"]) == 1
    assert body["sentiment_note"] is None
    assert body["earnings_dates"] == ["2024-01-20"]
    assert body["current_signal"] is not None
    assert body["current_signal"]["signal_id"] == sig.signal_id
    assert len(body["signal_history"]) == 1
    assert len(body["indicators"]["sma_20"]) == 20
    assert len(body["indicators"]["rsi_14"]) == 20
    # 20 bars: SMA-20/RSI-14 warm up within the window, SMA-200 never does.
    assert body["indicators"]["sma_20"][-1] is not None
    assert all(v is None for v in body["indicators"]["sma_200"])
