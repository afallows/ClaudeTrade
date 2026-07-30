"""FastAPI ``TestClient`` coverage for the /api/dashboard endpoint."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.models import PriceBar
from claudetrade.db.session import Database
from claudetrade.domain import Direction, MarketRegime
from claudetrade.pipeline import Pipeline
from claudetrade.signals.ledger import SignalLedger
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    return TestClient(app, base_url="http://127.0.0.1")


def test_dashboard_on_empty_database_is_honestly_empty(client: TestClient):
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["regime"] == {
        "regime": "unknown",
        "label": "Unknown",
        "as_of_session": None,
        "has_data": False,
    }
    assert body["top_longs"] == []
    assert body["top_shorts"] == []
    assert body["status"]["symbols_with_data"] == 0
    assert body["status"]["last_refresh"] is None
    assert body["status"]["last_scan"] is None
    # The synthetic/offline providers are still "configured" by default, so
    # the provider list is non-empty even with no data ingested yet.
    assert isinstance(body["providers"], list)


def test_dashboard_reports_regime_and_ranked_top_candidates(
    client: TestClient, tmp_db, make_signal
):
    ledger = SignalLedger(tmp_db)
    ledger.record(
        make_signal(
            symbol="BEST_LONG",
            direction=Direction.LONG,
            overall_score=95.0,
            regime=MarketRegime.BULL_QUIET,
            session=dt.date(2024, 6, 1),
        )
    )
    ledger.record(
        make_signal(
            symbol="WORSE_LONG",
            direction=Direction.LONG,
            overall_score=40.0,
            regime=MarketRegime.BULL_QUIET,
            session=dt.date(2024, 6, 1),
        )
    )
    ledger.record(
        make_signal(
            symbol="BEST_SHORT",
            direction=Direction.SHORT,
            overall_score=80.0,
            regime=MarketRegime.BULL_QUIET,
            session=dt.date(2024, 6, 1),
        )
    )

    resp = client.get("/api/dashboard")
    body = resp.json()
    assert body["regime"]["regime"] == "bull_quiet"
    assert body["regime"]["label"] == "Bull -- Quiet"
    assert body["regime"]["has_data"] is True
    assert [r["symbol"] for r in body["top_longs"]] == ["BEST_LONG", "WORSE_LONG"]
    assert [r["symbol"] for r in body["top_shorts"]] == ["BEST_SHORT"]
    assert body["status"]["last_scan"] is not None


def test_dashboard_status_ribbon_reflects_stored_bars(client: TestClient, tmp_db):
    with tmp_db.session() as session:
        session.add(
            PriceBar(
                symbol="AAA",
                session=dt.date(2024, 1, 2),
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
            )
        )

    body = client.get("/api/dashboard").json()
    assert body["status"]["symbols_with_data"] == 1
    assert body["status"]["last_refresh"] is not None
