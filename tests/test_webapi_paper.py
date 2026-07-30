"""FastAPI ``TestClient`` coverage for the /api/paper endpoints.

``POST /api/paper/open`` exercises the exact ``PaperBroker``/``OrderRequest``
seam ``ui.screens.scanner._open_paper_trade`` uses, so these tests cover the
three honest outcomes that seam can produce: filled, rejected (kill switch),
and not-yet-fillable (no stored bar after the signal's session).
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.models import PriceBar
from claudetrade.db.session import Database
from claudetrade.paper.portfolio import PaperPortfolio
from claudetrade.pipeline import Pipeline
from claudetrade.signals.ledger import SignalLedger
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    return TestClient(app, base_url="http://127.0.0.1")


def _bar(symbol: str, session: dt.date, price: float = 25.0) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        session=session,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        volume=1_000_000,
    )


# --- GET /api/paper/account ---------------------------------------------------


def test_paper_account_on_a_fresh_account(client: TestClient, tmp_app_config):
    resp = client.get("/api/paper/account")
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"]["cash"] == tmp_app_config.risk.account_size_usd
    assert body["account"]["equity"] == tmp_app_config.risk.account_size_usd
    assert body["positions"] == []
    assert body["closed_trades"] == []
    assert body["equity_curve"] == []
    assert "No recorded equity history yet" in body["equity_curve_note"]


# --- GET /api/paper/performance ----------------------------------------------


def test_paper_performance_with_no_trades_is_honestly_unavailable(client: TestClient):
    resp = client.get("/api/paper/performance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["closed_trades"] == 0
    assert body["win_loss_ratio"] is None
    assert body["win_loss_display"] == "n/a"
    assert body["expectancy"] is None
    assert body["is_significant"] is False
    assert body["significance_reason"] is not None
    assert body["max_drawdown_note"] == "No equity history recorded yet."


# --- POST /api/paper/open -----------------------------------------------------


def test_paper_open_not_fillable_without_a_following_bar(client: TestClient, tmp_db, make_signal):
    sig = make_signal(symbol="NOBAR", session=dt.date(2024, 1, 3))
    SignalLedger(tmp_db).record(sig)

    resp = client.post("/api/paper/open", json={"signal_id": sig.signal_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["status"] == "not_fillable"
    assert "Run a data refresh" in body["message"]


def test_paper_open_fills_when_price_reaches_entry_zone(client: TestClient, tmp_db, make_signal):
    sig = make_signal(
        symbol="FILLME",
        session=dt.date(2024, 1, 3),
        entry_low=24.0,
        entry_high=26.0,
        stop_loss=22.0,
    )
    SignalLedger(tmp_db).record(sig)
    with tmp_db.session() as session:
        session.add(_bar("FILLME", dt.date(2024, 1, 4), price=25.0))

    resp = client.post("/api/paper/open", json={"signal_id": sig.signal_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["status"] == "filled"
    assert body["symbol"] == "FILLME"
    assert body["filled_shares"] > 0
    assert body["fill_price"] is not None
    assert body["fill_session"] == "2024-01-04"
    assert "Filled" in body["message"]


def test_paper_open_rejects_when_kill_switch_engaged(
    client: TestClient, tmp_db, tmp_app_config, make_signal
):
    PaperPortfolio(tmp_app_config, tmp_db).engage_kill_switch(True)
    sig = make_signal(symbol="HALTED", session=dt.date(2024, 1, 3))
    SignalLedger(tmp_db).record(sig)
    with tmp_db.session() as session:
        session.add(_bar("HALTED", dt.date(2024, 1, 4)))

    resp = client.post("/api/paper/open", json={"signal_id": sig.signal_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["status"] == "rejected"
    assert any("kill switch" in r.lower() for r in body["reasons"])


def test_paper_open_404_for_unknown_signal(client: TestClient):
    resp = client.post("/api/paper/open", json={"signal_id": "does-not-exist"})
    assert resp.status_code == 404
