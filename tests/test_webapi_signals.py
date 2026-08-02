"""FastAPI ``TestClient`` coverage for the /api/signals endpoints.

Every test builds its own ``Pipeline`` against the ``tmp_db``/``tmp_app_config``
fixtures from ``tests/conftest.py`` (already-migrated, isolated per test) and
wraps it in a ``webapi`` app via ``create_app`` -- no network, no real
providers, no Streamlit.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.session import Database
from claudetrade.domain import Direction, Signal
from claudetrade.pipeline import Pipeline
from claudetrade.signals.ledger import SignalLedger
from claudetrade.signals.research import ResearchLedger
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    return TestClient(app, base_url="http://127.0.0.1")


def _record(db: Database, sig: Signal) -> None:
    SignalLedger(db).record(sig)


# --- GET /api/signals --------------------------------------------------------


def test_list_signals_empty_db_returns_empty_list(client: TestClient):
    resp = client.get("/api/signals")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"signals": [], "total": 0}


def test_list_signals_returns_recorded_signals(client: TestClient, tmp_db, make_signal):
    aaa = make_signal(symbol="AAA", overall_score=70.0)
    _record(tmp_db, aaa)
    _record(tmp_db, make_signal(symbol="BBB", overall_score=40.0, direction=Direction.SHORT))

    resp = client.get("/api/signals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    symbols = {row["symbol"] for row in body["signals"]}
    assert symbols == {"AAA", "BBB"}
    row = next(r for r in body["signals"] if r["symbol"] == "AAA")
    assert row["direction"] == "long"
    assert row["status"] == "actionable"
    assert row["reward_risk_ratio"] == aaa.plan.reward_risk_ratio
    assert row["entry_low"] == aaa.plan.entry_low
    assert row["entry_high"] == aaa.plan.entry_high


def test_list_signals_filters_by_direction(client: TestClient, tmp_db, make_signal):
    _record(tmp_db, make_signal(symbol="LONG1", direction=Direction.LONG))
    _record(tmp_db, make_signal(symbol="SHORT1", direction=Direction.SHORT))

    resp = client.get("/api/signals", params={"direction": ["short"]})
    body = resp.json()
    assert body["total"] == 1
    assert body["signals"][0]["symbol"] == "SHORT1"


def test_list_signals_filters_by_min_score_and_confidence(client: TestClient, tmp_db, make_signal):
    _record(tmp_db, make_signal(symbol="WEAK", overall_score=20.0, confidence=0.2))
    _record(tmp_db, make_signal(symbol="STRONG", overall_score=90.0, confidence=0.9))

    resp = client.get("/api/signals", params={"min_score": 50.0})
    assert {r["symbol"] for r in resp.json()["signals"]} == {"STRONG"}

    resp = client.get("/api/signals", params={"min_confidence": 0.5})
    assert {r["symbol"] for r in resp.json()["signals"]} == {"STRONG"}


def test_list_signals_filters_by_strategy(client: TestClient, tmp_db, make_signal):
    _record(tmp_db, make_signal(symbol="A", strategy="sentiment_breakout"))
    _record(tmp_db, make_signal(symbol="B", strategy="mean_reversion"))

    resp = client.get("/api/signals", params={"strategy": ["mean_reversion"]})
    assert {r["symbol"] for r in resp.json()["signals"]} == {"B"}


def test_list_signals_filters_by_max_days_to_earnings(client: TestClient, tmp_db, make_signal):
    _record(tmp_db, make_signal(symbol="SOON", days_to_earnings=2))
    _record(tmp_db, make_signal(symbol="FAR", days_to_earnings=60))
    _record(tmp_db, make_signal(symbol="UNKNOWN", days_to_earnings=None))

    resp = client.get("/api/signals", params={"max_days_to_earnings": 10})
    assert {r["symbol"] for r in resp.json()["signals"]} == {"SOON"}


# --- GET /api/signals: effective_score / has_research -----------------------


def test_list_signals_reports_overall_score_as_effective_when_no_research(
    client: TestClient, tmp_db, make_signal
):
    _record(tmp_db, make_signal(symbol="AAA", overall_score=70.0))

    resp = client.get("/api/signals")
    row = resp.json()["signals"][0]
    assert row["effective_score"] == row["overall_score"] == 70.0
    assert row["has_research"] is False


def test_list_signals_effective_score_reflects_research_adjustment(
    client: TestClient, tmp_db: Database, tmp_app_config: AppConfig, make_signal
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

    resp = client.get("/api/signals")
    row = resp.json()["signals"][0]
    assert row["has_research"] is True
    assert row["overall_score"] == 60.0
    # technical_setup's default weight (0.20) applied to the +20.0 delta over
    # the full 1.00 total weight -- a deterministic +4.0 move.
    assert row["effective_score"] == pytest.approx(64.0)


def test_list_signals_sorted_by_effective_score_orders_research_adjusted_rows_first(
    client: TestClient, tmp_db: Database, tmp_app_config: AppConfig, make_signal
):
    """The Screener grid sorts client-side on whichever column it's given --
    this just confirms the field it would sort on (``effective_score``)
    correctly re-ranks a research-adjusted row above a higher raw-score one,
    the same re-rank ``mcp_server.get_signals`` performs server-side."""
    high = make_signal(symbol="HIGH", overall_score=63.0)
    low = make_signal(symbol="LOW", overall_score=60.0)
    _record(tmp_db, high)
    _record(tmp_db, low)

    ResearchLedger(tmp_db).append_research_revision(
        low.signal_id,
        thesis=None,
        invalidation=None,
        score_adjustments={"technical_setup": 20.0},
        rationale="Strong new catalyst confirmed by two independent sources.",
        sources=["https://example.com/a", "https://example.com/b"],
        config=tmp_app_config,
    )

    resp = client.get("/api/signals")
    rows = {r["symbol"]: r for r in resp.json()["signals"]}

    assert rows["LOW"]["has_research"] is True
    assert rows["LOW"]["effective_score"] == pytest.approx(64.0)
    assert rows["HIGH"]["has_research"] is False
    assert rows["HIGH"]["overall_score"] == rows["HIGH"]["effective_score"] == 63.0
    ranked = sorted(rows.values(), key=lambda r: r["effective_score"], reverse=True)
    assert [r["symbol"] for r in ranked] == ["LOW", "HIGH"]


# --- GET /api/signals/{id} ---------------------------------------------------


def test_get_signal_detail(client: TestClient, tmp_db, make_signal):
    sig = make_signal(symbol="DETAIL", overall_score=55.0)
    _record(tmp_db, sig)

    resp = client.get(f"/api/signals/{sig.signal_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "DETAIL"
    assert "components" in body and "plan" in body
    assert body["plan"]["entry_low"] == sig.plan.entry_low
    assert set(body["components"].keys()) == set(sig.components.as_dict().keys())
    assert body["effective_score"] == body["overall_score"] == 55.0
    assert body["has_research"] is False
    assert body["research"] is None
    assert body["research_history"] == []


def test_get_signal_detail_404_for_unknown_id(client: TestClient):
    resp = client.get("/api/signals/does-not-exist")
    assert resp.status_code == 404


def test_get_signal_detail_includes_latest_research_and_full_history(
    client: TestClient, tmp_db: Database, tmp_app_config: AppConfig, make_signal
):
    sig = make_signal(symbol="RSCH", overall_score=50.0)
    _record(tmp_db, sig)

    ledger = ResearchLedger(tmp_db)
    ledger.append_research_revision(
        sig.signal_id,
        thesis=None,
        invalidation=None,
        score_adjustments={"technical_setup": 5.0},
        rationale="Initial confirmation from an earnings call transcript.",
        sources=["https://example.com/transcript"],
        config=tmp_app_config,
    )
    ledger.append_research_revision(
        sig.signal_id,
        thesis=None,
        invalidation=[f"Close below the {sig.plan.stop_loss:.2f} stop level"],
        score_adjustments={"technical_setup": 8.0},
        rationale="Follow-up: guidance raise confirmed by two analysts.",
        sources=["https://example.com/followup"],
        config=tmp_app_config,
    )

    resp = client.get(f"/api/signals/{sig.signal_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["has_research"] is True
    assert body["overall_score"] == 50.0
    # technical_setup's default weight (0.20) applied to the latest
    # revision's own +8.0 delta (revisions don't stack) -- a +1.6 move.
    assert body["effective_score"] == pytest.approx(51.6)

    assert body["research"] is not None
    assert body["research"]["revision"] == 2
    assert body["research"]["actor"] == "mcp"
    assert body["research"]["invalidation"] == [f"Close below the {sig.plan.stop_loss:.2f} stop level"]
    assert body["research"]["score_adjustments"] == {"technical_setup": 8.0}
    assert "guidance raise" in body["research"]["rationale"]
    assert body["research"]["sources"] == ["https://example.com/followup"]

    history = body["research_history"]
    assert [r["revision"] for r in history] == [1, 2]
    assert history[0]["score_adjustments"] == {"technical_setup": 5.0}


# --- GET /api/signals/rejected ----------------------------------------------


def test_rejected_unavailable_before_any_scan_this_process(client: TestClient):
    resp = client.get("/api/signals/rejected")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["rejected"] == []
    assert body["funnel"] is None
    assert "POST /api/scan" in body["reason"]


def test_rejected_serves_the_funnel_from_the_last_in_process_scan(client: TestClient):
    """The funnel travels alongside `rejected` on the same in-process
    ScanResult cache (see webapi.deps.get_last_scan) -- this injects one
    directly rather than needing a full real scan to produce rejections."""
    from claudetrade.signals.engine import NearMiss, ScanResult
    from claudetrade.utils.timeutils import utc_now

    scan_result = ScanResult(
        session=dt.date(2026, 7, 31), generated_at=utc_now(), regime=None, evaluated_symbols=1673
    )
    scan_result.funnel.record(strategy="sentiment_breakout", reason_code="illiquid")
    scan_result.funnel.record(strategy="sentiment_breakout", reason_code="illiquid")
    scan_result.funnel.record(strategy="sentiment_pullback", reason_code="score_below_threshold")
    scan_result.funnel.offer_near_miss(
        NearMiss(
            symbol="ZZZZ",
            strategy="sentiment_pullback",
            reason_code="score_below_threshold",
            metric=46.2,
            threshold=48.0,
            margin=-1.8,
            overall_score=46.2,
            confidence=0.55,
            weakest_components=[("volume_confirmation", 1.0)],
            strongest_components=[("technical_setup", 20.0)],
        )
    )
    scan_result.funnel.finalize()
    client.app.state.last_scan_result = scan_result

    resp = client.get("/api/signals/rejected")
    assert resp.status_code == 200
    body = resp.json()

    assert body["available"] is True
    assert body["evaluated_symbols"] == 1673
    funnel = body["funnel"]
    assert funnel["total_rejections"] == 3
    assert funnel["by_reason"] == {"illiquid": 2, "score_below_threshold": 1}
    assert funnel["by_strategy_reason"] == {
        "sentiment_breakout": {"illiquid": 2},
        "sentiment_pullback": {"score_below_threshold": 1},
    }
    [near_miss] = funnel["near_misses"]
    assert near_miss["symbol"] == "ZZZZ"
    assert near_miss["metric"] == pytest.approx(46.2)
    assert near_miss["threshold"] == pytest.approx(48.0)
    assert near_miss["weakest_components"] == [["volume_confirmation", 1.0]]


# --- POST /api/scan / /api/refresh: honest degradation on an empty universe -


def test_scan_with_no_stored_market_data_evaluates_nothing_but_does_not_crash(
    client: TestClient,
):
    """The bootstrap seed universe is non-empty (ADR-0008 Decision 3: seeds are
    always present), but with zero stored price bars nothing can be evaluated
    -- so the scan refuses loudly (an explicit warning naming the missing
    data) instead of fabricating an empty-but-"successful" result.
    """
    resp = client.post("/api/scan", json={"session": "2024-06-28"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["signal_count"] == 0
    assert body["evaluated_symbols"] == 0
    assert body["rejected_count"] == 0
    assert any("No price bars" in w for w in body["warnings"])

    # No scan actually ran (there was nothing to evaluate), so the near-miss
    # cache honestly reports unavailable rather than caching a fabricated
    # empty result.
    rejected = client.get("/api/signals/rejected").json()
    assert rejected["available"] is False


def test_refresh_degrades_without_configured_providers(client: TestClient):
    resp = client.post("/api/refresh", json={"start": "2024-01-01", "end": "2024-01-05"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["universe_size"], int)
    assert isinstance(body["warnings"], list)


def test_scan_request_defaults_session_to_today(client: TestClient):
    """Defaults to the ET trading session (F24) -- on a weekend or after
    Friday's ET close this is Friday's date, never the UTC calendar date."""
    from claudetrade.utils.timeutils import current_trading_session

    resp = client.post("/api/scan", json={})
    assert resp.status_code == 200
    assert resp.json()["session"] == current_trading_session().isoformat()
