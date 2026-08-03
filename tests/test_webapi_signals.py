"""FastAPI ``TestClient`` coverage for the /api/signals endpoints.

Every test builds its own ``Pipeline`` against the ``tmp_db``/``tmp_app_config``
fixtures from ``tests/conftest.py`` (already-migrated, isolated per test) and
wraps it in a ``webapi`` app via ``create_app`` -- no network, no real
providers, no Streamlit.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from claudetrade.config import AppConfig
from claudetrade.db.models import AdanosSnapshotRow
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


def _add_snapshot(
    db: Database,
    *,
    symbol: str,
    platform: str,
    session: dt.date = dt.date(2026, 8, 1),
    buzz_score: float = 50.0,
    mentions: int = 10,
    trend: str = "stable",
    sentiment_score: float | None = 0.0,
    bullish_pct: float | None = 50.0,
    bearish_pct: float | None = 50.0,
    engagement: float = 0.0,
    trend_history: list[float] | None = None,
) -> None:
    """Insert one ``AdanosSnapshotRow`` directly, bypassing the provider --
    these tests only need the stored shape ``webapi.attention`` reads, not a
    real Adanos fetch."""
    with db.session() as db_session:
        db_session.add(
            AdanosSnapshotRow(
                symbol=symbol,
                session=session,
                platform=platform,
                company_name="",
                buzz_score=buzz_score,
                mentions=mentions,
                trend=trend,
                sentiment_score=sentiment_score,
                bullish_pct=bullish_pct,
                bearish_pct=bearish_pct,
                engagement=engagement,
                trend_history=trend_history or [],
            )
        )


def _count_selects(db: Database, fn):
    """Run ``fn`` and return its result plus the SELECTs it issued.

    Same helper as ``tests/test_research_revisions.py``'s -- duplicated
    locally rather than imported so this module has no cross-test-module
    dependency.
    """
    statements: list[str] = []

    def _record_stmt(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _record_stmt)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", _record_stmt)
    return result, [s for s in statements if s.lstrip().upper().startswith("SELECT")]


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


# --- GET /api/signals: attention (Adanos cross-platform aggregate) ---------


def test_list_signals_attention_is_null_when_no_snapshots(client: TestClient, tmp_db, make_signal):
    _record(tmp_db, make_signal(symbol="NOATT", overall_score=50.0))

    resp = client.get("/api/signals")
    row = resp.json()["signals"][0]
    assert row["attention"] is None


def test_list_signals_attention_aggregates_across_two_platforms(
    client: TestClient, tmp_db: Database, make_signal
):
    """Two platform rows on the same (latest) session fold into one
    mention-weighted aggregate -- see ``webapi.attention._aggregate``."""
    _record(tmp_db, make_signal(symbol="AAA", overall_score=50.0))
    _add_snapshot(
        tmp_db,
        symbol="AAA",
        platform="x",
        buzz_score=80.0,
        mentions=30,
        trend="rising",
        bullish_pct=70.0,
        bearish_pct=30.0,
        trend_history=[10, 20, 30, 40, 50, 60, 70],
    )
    _add_snapshot(
        tmp_db,
        symbol="AAA",
        platform="reddit",
        buzz_score=20.0,
        mentions=10,
        trend="falling",
        bullish_pct=30.0,
        bearish_pct=70.0,
        trend_history=[70, 60, 50, 40, 30, 20, 10],
    )

    resp = client.get("/api/signals")
    row = resp.json()["signals"][0]
    attention = row["attention"]
    assert attention is not None
    assert attention["platforms"] == ["reddit", "x"]
    assert attention["total_mentions"] == 40
    assert attention["source_count"] is None  # no "news" row this session
    # Mention-weighted mean: (80*30 + 20*10) / 40 = 65.0
    assert attention["buzz_score"] == pytest.approx(65.0)
    assert attention["bullish_pct"] == pytest.approx((70 * 30 + 30 * 10) / 40)
    assert attention["bearish_pct"] == pytest.approx((30 * 30 + 70 * 10) / 40)
    # x (weight 30) dominates reddit (weight 10) -- dominant trend is "rising".
    assert attention["trend"] == "rising"
    # Elementwise mention-weighted mean of the two 7-point histories.
    expected_point0 = (10 * 30 + 70 * 10) / 40
    assert attention["trend_history"][0] == pytest.approx(expected_point0)
    assert len(attention["trend_history"]) == 7


def test_list_signals_attention_reads_news_source_count_from_engagement_column(
    client: TestClient, tmp_db: Database, make_signal
):
    """News rows carry ``source_count`` through the shared ``engagement``
    column (no dedicated column) -- see ``providers.social.adanos``'s
    ``_ENGAGEMENT_FIELD`` and ``webapi.attention._source_count``."""
    _record(tmp_db, make_signal(symbol="BBB", overall_score=50.0))
    _add_snapshot(tmp_db, symbol="BBB", platform="news", mentions=5, engagement=12.0)

    resp = client.get("/api/signals")
    attention = resp.json()["signals"][0]["attention"]
    assert attention["source_count"] == 12
    assert attention["platforms"] == ["news"]


def test_list_signals_attention_uses_only_the_latest_session(
    client: TestClient, tmp_db: Database, make_signal
):
    """A stale earlier-session row for the same symbol must not leak into the
    aggregate once a newer session exists."""
    _record(tmp_db, make_signal(symbol="CCC", overall_score=50.0))
    _add_snapshot(
        tmp_db, symbol="CCC", platform="x", session=dt.date(2026, 7, 1), buzz_score=1.0, mentions=1
    )
    _add_snapshot(
        tmp_db, symbol="CCC", platform="x", session=dt.date(2026, 8, 1), buzz_score=99.0, mentions=99
    )

    resp = client.get("/api/signals")
    attention = resp.json()["signals"][0]["attention"]
    assert attention["session"] == "2026-08-01"
    assert attention["total_mentions"] == 99
    assert attention["buzz_score"] == pytest.approx(99.0)


def test_list_signals_attention_issues_one_batched_query_not_one_per_row(
    client: TestClient, tmp_db: Database, make_signal
):
    """Same F26 discipline as ``latest_research_revisions`` -- one extra
    query for the whole page's attention data, never a per-symbol loop."""
    from claudetrade.webapi.attention import latest_attention

    symbols = [f"SYM{i}" for i in range(8)]
    for symbol in symbols:
        _record(tmp_db, make_signal(symbol=symbol, overall_score=50.0))
        _add_snapshot(tmp_db, symbol=symbol, platform="x", mentions=5)
        _add_snapshot(tmp_db, symbol=symbol, platform="reddit", mentions=5)

    result, selects = _count_selects(tmp_db, lambda: latest_attention(tmp_db, symbols))

    assert len(result) == len(symbols)
    assert len(selects) == 1, f"expected one SELECT, got {len(selects)}"


# --- GET /api/signals: distinct (read-time de-duplication) ------------------


def test_list_signals_distinct_defaults_to_true_and_collapses_duplicates(
    client: TestClient, tmp_db: Database, make_signal
):
    """End-to-end reproduction of the reported bug: two identical-content
    re-scans of the same session (different signal_id from a code/config
    change) plus one cross-strategy sibling collapse into ONE row by
    default, with corroborating/duplicates summary fields set."""
    session = dt.date(2026, 7, 31)
    dup_early = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=60.0),
        signal_id="dup-early",
        created_at=dt.datetime(2026, 7, 31, 5, 39, tzinfo=dt.UTC),
    )
    dup_late = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=61.0),
        signal_id="dup-late",
        created_at=dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC),
    )
    sibling = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="sentiment_pullback", session=session, overall_score=90.0),
        signal_id="sibling",
        created_at=dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC),
    )
    for sig in (dup_early, dup_late, sibling):
        _record(tmp_db, sig)

    resp = client.get("/api/signals")
    body = resp.json()

    assert body["total"] == 1
    row = body["signals"][0]
    assert row["signal_id"] == "sibling"
    assert row["duplicates_collapsed"] == 1
    assert row["corroborating_strategies"] == ["volume_breakout"]
    assert row["corroborating_count"] == 1


def test_list_signals_distinct_false_returns_raw_per_strategy_rows(
    client: TestClient, tmp_db: Database, make_signal
):
    session = dt.date(2026, 7, 31)
    dup_early = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=60.0),
        signal_id="dup-early",
        created_at=dt.datetime(2026, 7, 31, 5, 39, tzinfo=dt.UTC),
    )
    dup_late = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=61.0),
        signal_id="dup-late",
        created_at=dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC),
    )
    sibling = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="sentiment_pullback", session=session, overall_score=90.0),
        signal_id="sibling",
        created_at=dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC),
    )
    for sig in (dup_early, dup_late, sibling):
        _record(tmp_db, sig)

    resp = client.get("/api/signals", params={"distinct": False})
    body = resp.json()

    assert body["total"] == 3
    ids = {row["signal_id"] for row in body["signals"]}
    assert ids == {"dup-early", "dup-late", "sibling"}
    for row in body["signals"]:
        assert row["corroborating_strategies"] == []
        assert row["corroborating_count"] == 0
        assert row["duplicates_collapsed"] == 0


def test_list_signals_distinct_keeps_long_and_short_separate(
    client: TestClient, tmp_db: Database, make_signal
):
    _record(tmp_db, make_signal(symbol="TSLA", direction=Direction.LONG, overall_score=70.0))
    _record(tmp_db, make_signal(symbol="TSLA", direction=Direction.SHORT, overall_score=65.0))

    resp = client.get("/api/signals")
    body = resp.json()

    assert body["total"] == 2
    directions = {row["direction"] for row in body["signals"]}
    assert directions == {"long", "short"}


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


def test_get_signal_detail_includes_attention(client: TestClient, tmp_db: Database, make_signal):
    sig = make_signal(symbol="DETAILATT", overall_score=55.0)
    _record(tmp_db, sig)
    _add_snapshot(tmp_db, symbol="DETAILATT", platform="x", mentions=5, buzz_score=40.0)

    resp = client.get(f"/api/signals/{sig.signal_id}")
    body = resp.json()
    assert body["attention"] is not None
    assert body["attention"]["platforms"] == ["x"]
    assert body["attention"]["total_mentions"] == 5


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
