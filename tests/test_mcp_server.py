"""Unit tests for ``claudetrade.mcp_server``.

Tool functions are exercised directly against ``tmp_db``/``tmp_app_config``
(the shared fixtures from ``tests/conftest.py``, not modified here) -- no MCP
transport involved. The FastMCP wiring itself is covered separately by
building a server with :func:`~claudetrade.mcp_server.build_server` and
asserting its registered tool names/schemas; no stdio integration test is
needed (``run_stdio`` blocks forever by design and is not exercised here).

The whole module is skipped cleanly if the optional ``mcp`` package is not
installed, matching how ``claudetrade mcp`` itself degrades.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' package is not installed")

from claudetrade import mcp_server
from claudetrade.config import AppConfig
from claudetrade.db.models import SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.domain import Direction, MarketRegime, Signal
from claudetrade.pipeline import Pipeline, PipelineResult
from claudetrade.signals.ledger import SignalLedger
from claudetrade.signals.research import ResearchLedger
from claudetrade.utils.timeutils import current_trading_session, utc_now
from claudetrade.version import DISCLAIMER
from claudetrade.webapi.refresh_state import RefreshState


@pytest.fixture
def pipeline(tmp_app_config: AppConfig, tmp_db: Database) -> Pipeline:
    return Pipeline(tmp_app_config, tmp_db)


def _record(db: Database, sig: Signal) -> None:
    SignalLedger(db).record(sig)


# --------------------------------------------------------------------------
# get_signals
# --------------------------------------------------------------------------


def test_get_signals_empty_ledger_is_an_honest_empty_list(pipeline: Pipeline) -> None:
    result = mcp_server.get_signals(pipeline)
    assert result["disclaimer"] == DISCLAIMER
    assert result["count"] == 0
    assert result["signals"] == []
    # An empty ledger is complete, not truncated -- the caller must be able
    # to tell "there is nothing" from "there is more you did not get".
    assert result["total_matching"] == 0
    assert result["truncated"] is False


def test_get_signals_returns_recorded_signals_with_disclaimer_once(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    aaa = make_signal(symbol="AAA", overall_score=70.0)
    _record(tmp_db, aaa)
    _record(tmp_db, make_signal(symbol="BBB", overall_score=40.0, direction=Direction.SHORT))

    result = mcp_server.get_signals(pipeline)
    assert result["disclaimer"] == DISCLAIMER
    assert "disclaimer" not in result["signals"][0]  # not repeated per-row
    assert result["count"] == 2
    assert {row["symbol"] for row in result["signals"]} == {"AAA", "BBB"}

    row = next(r for r in result["signals"] if r["symbol"] == "AAA")
    assert row["direction"] == "long"
    assert row["status"] == "actionable"
    assert row["strategy"] == aaa.strategy
    assert row["overall_score"] == aaa.overall_score
    assert row["confidence"] == aaa.confidence
    assert row["entry_low"] == aaa.plan.entry_low
    assert row["entry_high"] == aaa.plan.entry_high
    assert row["stop_loss"] == aaa.plan.stop_loss
    assert row["targets"] == aaa.plan.targets
    assert row["days_to_earnings"] == aaa.days_to_earnings
    assert row["session"] == aaa.session.isoformat()


def test_get_signals_filters_by_min_score(pipeline: Pipeline, tmp_db: Database, make_signal) -> None:
    _record(tmp_db, make_signal(symbol="WEAK", overall_score=20.0))
    _record(tmp_db, make_signal(symbol="STRONG", overall_score=90.0))

    result = mcp_server.get_signals(pipeline, min_score=50.0)
    assert {r["symbol"] for r in result["signals"]} == {"STRONG"}


def test_get_signals_respects_limit(pipeline: Pipeline, tmp_db: Database, make_signal) -> None:
    for i in range(5):
        _record(tmp_db, make_signal(symbol=f"SYM{i}", overall_score=50.0 + i))

    result = mcp_server.get_signals(pipeline, limit=2)
    assert result["count"] == 2
    assert len(result["signals"]) == 2


def test_get_signals_returns_the_best_scoring_not_the_most_recently_written(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    """The bug this ordering exists to fix.

    Signals were read newest-first and truncated in Python, so ``limit=N``
    returned the N most recently *written* rows. A scan writes in roughly
    symbol order, so that was an alphabetical slice that silently excluded
    the best candidates: QA's limit=20 call returned GIL/TTEK/THC while
    MSFT (73.90), LPLA (75.53) and AMZN (73.40) sat outside the window, and
    two reviewers comparing the same scan via this tool and the UI reached
    different conclusions.

    Written here in the order the scan would write them -- alphabetically,
    with the best scores deliberately written FIRST so a newest-first read
    pushes them out of the window.
    """
    for symbol, score in [
        ("AMZN", 73.40), ("LPLA", 75.53), ("MSFT", 73.90),  # best, written first
        ("GIL", 70.84), ("GSAT", 70.97), ("THC", 71.20), ("TTEK", 70.83),
    ]:
        _record(tmp_db, make_signal(symbol=symbol, overall_score=score))

    result = mcp_server.get_signals(pipeline, limit=3)

    assert [r["symbol"] for r in result["signals"]] == ["LPLA", "MSFT", "AMZN"]
    assert result["sorted_by"] == "score"


def test_get_signals_reports_that_it_truncated(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    """A caller could not previously tell 20-of-47 from 20-of-20, so a
    non-representative slice looked like the whole answer."""
    for i in range(7):
        _record(tmp_db, make_signal(symbol=f"SYM{i}", overall_score=50.0 + i))

    result = mcp_server.get_signals(pipeline, limit=3)

    assert result["count"] == 3
    assert result["total_matching"] == 7
    assert result["truncated"] is True

    full = mcp_server.get_signals(pipeline, limit=20)
    assert full["truncated"] is False
    assert full["total_matching"] == 7


def test_get_signals_total_matching_respects_min_score(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    _record(tmp_db, make_signal(symbol="WEAK", overall_score=20.0))
    for i in range(3):
        _record(tmp_db, make_signal(symbol=f"OK{i}", overall_score=80.0 + i))

    result = mcp_server.get_signals(pipeline, min_score=50.0, limit=1)

    assert result["total_matching"] == 3  # not 4 -- the filter is applied first
    assert result["truncated"] is True


def test_get_signals_can_still_be_read_chronologically(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    """Chronological access stays available -- audit and ledger inspection
    are real needs. It is simply the wrong default for 'show me candidates'."""
    # Distinct sessions, because make_signal derives created_at from the
    # session date -- same-session records share a timestamp.
    _record(
        tmp_db,
        make_signal(symbol="FIRST", overall_score=99.0, session=dt.date(2024, 1, 3)),
    )
    _record(
        tmp_db,
        make_signal(symbol="SECOND", overall_score=10.0, session=dt.date(2024, 1, 4)),
    )

    by_time = mcp_server.get_signals(pipeline, limit=1, sort="created_at")
    assert by_time["signals"][0]["symbol"] == "SECOND"
    assert by_time["sorted_by"] == "created_at"

    by_score = mcp_server.get_signals(pipeline, limit=1)
    assert by_score["signals"][0]["symbol"] == "FIRST"


def test_get_signals_ties_break_deterministically(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    """Equal scores must not shuffle between identical calls."""
    for symbol in ("CCC", "AAA", "BBB"):
        _record(tmp_db, make_signal(symbol=symbol, overall_score=70.0))

    first = [r["symbol"] for r in mcp_server.get_signals(pipeline)["signals"]]
    second = [r["symbol"] for r in mcp_server.get_signals(pipeline)["signals"]]
    assert first == second


# --------------------------------------------------------------------------
# get_signals: effective_score / has_research
# --------------------------------------------------------------------------


def test_get_signals_reports_overall_score_as_effective_when_no_research(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    _record(tmp_db, make_signal(symbol="AAA", overall_score=70.0))
    row = mcp_server.get_signals(pipeline)["signals"][0]
    assert row["effective_score"] == row["overall_score"] == 70.0
    assert row["has_research"] is False


def test_get_signals_reorders_by_effective_score_when_research_exists(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    """A research revision's clamped score adjustment can re-rank the page
    it landed on -- the whole point of ``adjusted_overall``. A close-enough
    starting gap (3 points) so the default ``technical_setup`` weight (0.20)
    applied to the cap (20.0) -- a +4.0 effective-score move -- is enough to
    flip the order, deterministically rather than conditionally."""
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

    result = mcp_server.get_signals(pipeline, tmp_app_config)
    symbols = [r["symbol"] for r in result["signals"]]
    low_row = next(r for r in result["signals"] if r["symbol"] == "LOW")
    high_row = next(r for r in result["signals"] if r["symbol"] == "HIGH")

    assert low_row["has_research"] is True
    assert low_row["overall_score"] == 60.0
    assert low_row["effective_score"] == pytest.approx(64.0)
    assert high_row["has_research"] is False
    assert high_row["overall_score"] == high_row["effective_score"] == 63.0
    # LOW's effective score (64.0) now beats HIGH's (63.0) -- the page must
    # reflect that, not the raw overall_score order the SQL query used.
    assert symbols.index("LOW") < symbols.index("HIGH")


def test_get_signals_effective_score_uses_the_batched_read_not_n_plus_one(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    """Same F26 discipline as ``recent_with_status``: fetching research per
    row instead of in one batched query is the exact class of regression
    that produced the original production stall."""
    from sqlalchemy import event

    signals = [make_signal(symbol=f"SYM{i}", overall_score=50.0 + i) for i in range(10)]
    for sig in signals:
        _record(tmp_db, sig)
    for sig in signals[:4]:
        ResearchLedger(tmp_db).append_research_revision(
            sig.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 1.0},
            rationale="minor confirmation",
            sources=["https://example.com"],
            config=tmp_app_config,
        )

    statements: list[str] = []

    def _record_stmt(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(tmp_db.engine, "before_cursor_execute", _record_stmt)
    try:
        result = mcp_server.get_signals(pipeline, tmp_app_config, limit=10)
    finally:
        event.remove(tmp_db.engine, "before_cursor_execute", _record_stmt)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert result["count"] == 10
    # list_with_status issues two SELECTs (a count, then the joined page);
    # research adds exactly one more batched query -- never one per row,
    # which would have been 10+ additional SELECTs here.
    assert len(selects) == 3, f"expected 3 SELECTs, got {len(selects)}: {selects}"


# --------------------------------------------------------------------------
# submit_research_revision / get_research_revisions
# --------------------------------------------------------------------------


def test_submit_research_revision_happy_path(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    sig = make_signal(symbol="AAPL", overall_score=65.0)
    _record(tmp_db, sig)

    result = mcp_server.submit_research_revision(
        pipeline,
        tmp_app_config,
        sig.signal_id,
        None,
        None,
        {"technical_setup": 5.0},
        "Confirmed the setup with a fresh earnings call transcript.",
        ["https://example.com/transcript"],
    )

    assert result["accepted"] is True
    assert result["disclaimer"] == DISCLAIMER
    assert result["signal_id"] == sig.signal_id
    assert result["revision"] == 1
    assert result["original_score"] == 65.0
    # technical_setup's default weight (0.20) applied to an unclamped +5.0
    # delta over the full 1.00 total weight -- a deterministic +1.0 move.
    assert result["effective_score"] == pytest.approx(66.0)
    assert result["clamped"] == {}


def test_submit_research_revision_disabled_by_config(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    tmp_app_config.mcp.research_writes_enabled = False
    sig = make_signal(symbol="AAPL")
    _record(tmp_db, sig)

    result = mcp_server.submit_research_revision(
        pipeline, tmp_app_config, sig.signal_id, None, None, None, "reason", ["https://x.com"]
    )

    assert result["accepted"] is False
    assert "disabled" in result["reason"]
    # Nothing was written.
    assert ResearchLedger(tmp_db).research_history(sig.signal_id) == []


def test_submit_research_revision_rejection_payload_shape(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    """A guardrail rejection returns a structured payload, never raises."""
    sig = make_signal(symbol="AAPL")
    _record(tmp_db, sig)

    result = mcp_server.submit_research_revision(
        pipeline,
        tmp_app_config,
        sig.signal_id,
        None,
        None,
        {"not_a_real_component": 5.0},
        "reason",
        ["https://x.com"],
    )

    assert set(result) == {"accepted", "reason"}
    assert result["accepted"] is False
    assert "unknown component" in result["reason"]


def test_submit_research_revision_missing_rationale_is_rejected(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    sig = make_signal(symbol="AAPL")
    _record(tmp_db, sig)

    result = mcp_server.submit_research_revision(
        pipeline, tmp_app_config, sig.signal_id, None, None, None, "", ["https://x.com"]
    )
    assert result["accepted"] is False


def test_submit_research_revision_cannot_touch_the_trade_plan(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    """No signature parameter accepts entry/stop/targets/size/direction --
    the bare function itself is the enforcement, not a runtime check."""
    import inspect

    params = set(inspect.signature(mcp_server.submit_research_revision).parameters)
    assert params.isdisjoint({"entry_low", "entry_high", "stop_loss", "targets", "shares",
                               "size", "direction", "plan"})


def test_get_research_revisions_returns_full_history(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    sig = make_signal(symbol="AAPL")
    _record(tmp_db, sig)
    mcp_server.submit_research_revision(
        pipeline, tmp_app_config, sig.signal_id, None, None, None, "first", ["https://x.com/1"]
    )
    mcp_server.submit_research_revision(
        pipeline, tmp_app_config, sig.signal_id, None, None, None, "second", ["https://x.com/2"]
    )

    result = mcp_server.get_research_revisions(pipeline, sig.signal_id)
    assert result["signal_id"] == sig.signal_id
    assert result["count"] == 2
    assert [r["rationale"] for r in result["revisions"]] == ["first", "second"]
    assert isinstance(result["revisions"][0]["created_at"], str)  # JSON-serialisable


def test_get_research_revisions_empty_for_unresearched_signal(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    sig = make_signal(symbol="AAPL")
    _record(tmp_db, sig)
    result = mcp_server.get_research_revisions(pipeline, sig.signal_id)
    assert result["count"] == 0
    assert result["revisions"] == []


# --------------------------------------------------------------------------
# get_signals empty-state: why_no_signals (the rejection funnel)
# --------------------------------------------------------------------------


def test_get_signals_empty_without_a_config_omits_why_no_signals(pipeline: Pipeline) -> None:
    """Without a config there is nowhere to read a funnel artifact from --
    the field is omitted rather than a misleading always-empty placeholder."""
    result = mcp_server.get_signals(pipeline)
    assert "why_no_signals" not in result


def test_get_signals_empty_with_config_but_no_scan_yet_says_so(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    result = mcp_server.get_signals(pipeline, tmp_app_config)
    assert result["count"] == 0
    assert result["why_no_signals"]["available"] is False
    assert "run_scan" in result["why_no_signals"]["note"] or "scan" in result["why_no_signals"]["note"]


def test_get_signals_empty_includes_the_persisted_funnel(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """A scan run elsewhere (CLI, web UI -- a different process/Pipeline,
    see the module docstring) persisted a funnel artifact; get_signals reads
    it back and surfaces it instead of a bare empty list."""
    from claudetrade.signals import funnel_store
    from claudetrade.signals.engine import NearMiss, ScanResult
    from claudetrade.utils.timeutils import utc_now

    scan_result = ScanResult(
        session=dt.date(2026, 7, 31), generated_at=utc_now(), regime=None, evaluated_symbols=1673
    )
    scan_result.funnel.record(strategy="sentiment_breakout", reason_code="illiquid")
    scan_result.funnel.offer_near_miss(
        NearMiss(
            symbol="ZZZZ",
            strategy="sentiment_pullback",
            reason_code="score_below_threshold",
            metric=46.2,
            threshold=48.0,
            margin=-1.8,
        )
    )
    scan_result.funnel.finalize()
    scan_result.rejected = [None] * 8365  # only len() matters to save()
    funnel_store.save(tmp_app_config, scan_result)

    result = mcp_server.get_signals(pipeline, tmp_app_config)

    why = result["why_no_signals"]
    assert why["available"] is True
    assert why["evaluated_symbols"] == 1673
    assert why["rejected_count"] == 8365
    assert why["funnel"]["by_reason"] == {"illiquid": 1}
    assert why["funnel"]["near_misses"][0]["symbol"] == "ZZZZ"


def test_get_signals_with_matches_never_includes_why_no_signals(
    pipeline: Pipeline, tmp_db: Database, tmp_app_config: AppConfig, make_signal
) -> None:
    _record(tmp_db, make_signal(symbol="AAA", overall_score=70.0))
    result = mcp_server.get_signals(pipeline, tmp_app_config)
    assert "why_no_signals" not in result


# --------------------------------------------------------------------------
# get_sentiment
# --------------------------------------------------------------------------


def test_get_sentiment_no_data_is_an_honest_empty_result(pipeline: Pipeline) -> None:
    result = mcp_server.get_sentiment(pipeline, "nope")
    assert result["symbol"] == "NOPE"
    assert result["points"] == []
    assert result["total_mentions"] == 0
    assert result["average_bull_bear_ratio"] is None
    assert "No sentiment/mention data" in result["note"]


def test_get_sentiment_returns_points_within_the_window_only(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    today = utc_now().date()
    with tmp_db.session() as session:
        session.add(
            SymbolSentimentDaily(
                symbol="AAA", session=today, source="all", post_count=10, bull_bear_ratio=2.0
            )
        )
        session.add(
            SymbolSentimentDaily(
                symbol="AAA",
                session=today - dt.timedelta(days=3),
                source="all",
                post_count=5,
                bull_bear_ratio=1.0,
            )
        )
        # Outside the default 7-day window -- must not be counted.
        session.add(
            SymbolSentimentDaily(
                symbol="AAA",
                session=today - dt.timedelta(days=30),
                source="all",
                post_count=100,
                bull_bear_ratio=0.1,
            )
        )

    result = mcp_server.get_sentiment(pipeline, "aaa", days=7)
    assert result["symbol"] == "AAA"
    assert result["note"] is None
    assert result["total_mentions"] == 15
    assert result["average_bull_bear_ratio"] == pytest.approx(1.5)
    assert len(result["points"]) == 2
    assert {p["post_count"] for p in result["points"]} == {10, 5}


def test_get_sentiment_days_window_is_clamped(pipeline: Pipeline) -> None:
    # Out-of-range days must not raise -- clamped to a sane bound instead.
    result = mcp_server.get_sentiment(pipeline, "AAA", days=100_000)
    assert result["days"] <= 365
    result = mcp_server.get_sentiment(pipeline, "AAA", days=0)
    assert result["days"] >= 1


# --------------------------------------------------------------------------
# get_trending
# --------------------------------------------------------------------------


def test_get_trending_ranks_by_recent_mention_volume(pipeline: Pipeline, tmp_db: Database) -> None:
    from claudetrade.db.models import Security

    today = utc_now().date()
    with tmp_db.session() as session:
        # get_trending joins against ``securities`` as a junk-symbol guard --
        # only known symbols may rank, so each test symbol needs a row.
        for sym in ("HOT", "WARM", "OLD"):
            session.add(Security(symbol=sym, name=sym))
        session.add(SymbolSentimentDaily(symbol="HOT", session=today, source="all", post_count=50))
        session.add(SymbolSentimentDaily(symbol="WARM", session=today, source="all", post_count=10))
        # Outside the trending window -- must not appear at all.
        session.add(
            SymbolSentimentDaily(
                symbol="OLD", session=today - dt.timedelta(days=30), source="all", post_count=1000
            )
        )
        # A non-"all" per-source row must not double-count.
        session.add(SymbolSentimentDaily(symbol="HOT", session=today, source="reddit", post_count=50))

    result = mcp_server.get_trending(pipeline)
    symbols = [row["symbol"] for row in result["symbols"]]
    assert symbols == ["HOT", "WARM"]
    assert result["symbols"][0]["mentions"] == 50
    assert result["window_days"] == mcp_server.DEFAULT_TRENDING_WINDOW_DAYS


def test_get_trending_respects_limit(pipeline: Pipeline, tmp_db: Database) -> None:
    from claudetrade.db.models import Security

    today = utc_now().date()
    with tmp_db.session() as session:
        for i in range(5):
            session.add(Security(symbol=f"SYM{i}", name=f"SYM{i}"))
            session.add(
                SymbolSentimentDaily(symbol=f"SYM{i}", session=today, source="all", post_count=i + 1)
            )

    result = mcp_server.get_trending(pipeline, limit=2)
    assert result["count"] == 2
    assert len(result["symbols"]) == 2


def _seed_trending(tmp_db: Database, rows: list[tuple[str, str, int]]) -> None:
    """``(symbol, source, post_count)`` rows for today, with securities."""
    from claudetrade.db.models import Security

    today = utc_now().date()
    with tmp_db.session() as session:
        for symbol in {r[0] for r in rows}:
            session.merge(Security(symbol=symbol, name=symbol))
        for symbol, source, count in rows:
            session.add(
                SymbolSentimentDaily(
                    symbol=symbol, session=today, source=source, post_count=count
                )
            )


def test_get_trending_auto_prefers_apewisdom_over_local_extraction(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """ApeWisdom rows arrive as tickers, so they cannot carry the
    common-word junk local extraction kept minting (QA F25). When both
    exist, 'auto' ranks the aggregator's view."""
    _seed_trending(
        tmp_db,
        [
            ("AS", "all", 1741),  # the QA junk symbol, from local extraction
            ("NVDA", "apewisdom:all-stocks", 812),
            ("MU", "apewisdom:4chan", 96),
        ],
    )
    result = mcp_server.get_trending(pipeline)

    assert result["source"] == "apewisdom"
    assert [row["symbol"] for row in result["symbols"]] == ["NVDA", "MU"]
    assert "AS" not in {row["symbol"] for row in result["symbols"]}


def test_get_trending_sums_a_symbol_across_communities(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    _seed_trending(
        tmp_db,
        [
            ("MU", "apewisdom:all-stocks", 430),
            ("MU", "apewisdom:4chan", 96),
            ("NVDA", "apewisdom:all-stocks", 500),
        ],
    )
    result = mcp_server.get_trending(pipeline)

    assert [row["symbol"] for row in result["symbols"]] == ["MU", "NVDA"]
    assert result["symbols"][0]["mentions"] == 526


def test_get_trending_reports_null_polarity_for_attention_rows(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """ApeWisdom measures no direction. Reporting its untouched column
    defaults as numbers would read as 'measured, and neutral' -- exactly the
    misreading QA drew from the all-1.0 bull/bear ratios."""
    _seed_trending(tmp_db, [("NVDA", "apewisdom:4chan", 61)])
    row = mcp_server.get_trending(pipeline)["symbols"][0]

    assert row["average_bull_bear_ratio"] is None
    assert row["average_confidence"] is None


def test_get_trending_auto_falls_back_to_local_rows(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """An installation with ApeWisdom disabled (or not yet refreshed) keeps
    exactly its previous behaviour, polarity included."""
    _seed_trending(tmp_db, [("AAPL", "all", 40)])
    result = mcp_server.get_trending(pipeline)

    assert result["source"] == "all"
    assert [r["symbol"] for r in result["symbols"]] == ["AAPL"]
    assert result["symbols"][0]["average_bull_bear_ratio"] is not None


def test_get_trending_source_can_be_forced_either_way(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    _seed_trending(
        tmp_db, [("AAPL", "all", 40), ("NVDA", "apewisdom:all-stocks", 812)]
    )

    local = mcp_server.get_trending(pipeline, source="all")
    assert local["source"] == "all"
    assert [r["symbol"] for r in local["symbols"]] == ["AAPL"]

    aggregated = mcp_server.get_trending(pipeline, source="apewisdom")
    assert aggregated["source"] == "apewisdom"
    assert [r["symbol"] for r in aggregated["symbols"]] == ["NVDA"]


def test_get_trending_empty_db_is_an_honest_empty_list(pipeline: Pipeline) -> None:
    result = mcp_server.get_trending(pipeline)
    assert result["count"] == 0
    assert result["symbols"] == []
    assert result["window_days"] == mcp_server.DEFAULT_TRENDING_WINDOW_DAYS
    # With nothing stored under either source, 'auto' reports the local
    # fallback rather than implying an aggregator answered.
    assert result["source"] == "all"


# --------------------------------------------------------------------------
# get_market_status
# --------------------------------------------------------------------------


def test_get_market_status_shape(pipeline: Pipeline) -> None:
    result = mcp_server.get_market_status(pipeline)
    assert result["regime"] == "unknown"
    assert result["market_session"] in {"pre_market", "open", "after_hours", "closed"}
    assert isinstance(result["is_trading_day"], bool)
    assert isinstance(result["current_time_et"], str)
    assert isinstance(result["current_time_utc"], str)
    assert result["symbols_with_data"] == 0
    assert result["last_refresh_utc"] is None
    assert isinstance(result["providers"], list) and result["providers"]
    assert isinstance(result["degraded_providers"], list)
    for provider in result["providers"]:
        assert {"name", "kind", "available", "configured", "supports_point_in_time", "message"} <= set(
            provider
        )


def test_get_market_status_reports_a_pending_sentiment_rebuild(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """``run_stdio`` skips the stored-sentiment self-heal so a minute-scale
    rebuild cannot run inside the MCP initialize handshake. Skipping it
    silently would leave trending serving exactly the junk the rebuild
    clears, with nothing saying why -- so the status tool reports it."""
    from claudetrade.sentiment import EXTRACTION_VERSION
    from claudetrade.sentiment.rebuild import record_extraction_version

    record_extraction_version(tmp_db, EXTRACTION_VERSION - 1)
    pending = mcp_server.get_market_status(pipeline)["sentiment_rebuild_pending"]
    assert pending is not None
    assert pending["stored_extraction_version"] == EXTRACTION_VERSION - 1
    assert pending["current_extraction_version"] == EXTRACTION_VERSION
    assert "rebuild-sentiment" in pending["note"]

    # Healed databases say nothing at all rather than carrying a dead field.
    record_extraction_version(tmp_db, EXTRACTION_VERSION)
    assert mcp_server.get_market_status(pipeline)["sentiment_rebuild_pending"] is None


def test_get_market_status_reports_collected_history_readiness(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """An assistant reading a rising-sentiment list needs to know how much
    baseline is behind it. Social history cannot be backfilled, so "installed
    a week ago" and "has a usable baseline" are different facts -- and the
    tier is computed from stored sessions, never asserted."""
    from claudetrade.db.models import Security

    empty = mcp_server.get_market_status(pipeline)["sentiment_readiness"]
    assert empty["tier"] == "warming_up"
    assert empty["sessions_collected"] == 0
    assert empty["blocking"] is False

    latest = current_trading_session()
    with tmp_db.session() as session:
        session.merge(Security(symbol="NVDA", name="NVDA Inc"))
    with tmp_db.session() as session:
        for offset in range(60):
            session.add(
                SymbolSentimentDaily(
                    symbol="NVDA",
                    session=latest - dt.timedelta(days=offset),
                    source="all",
                    post_count=6,
                )
            )

    readiness = mcp_server.get_market_status(pipeline)["sentiment_readiness"]
    assert readiness["sessions_collected"] == 60
    assert readiness["tier"] == "partial"
    assert readiness["next_tier"] == "ready"
    assert readiness["sessions_to_next_tier"] == 60
    assert isinstance(readiness["degraded_sources"], list)


def test_get_refresh_status_marks_an_automatic_collection_as_scheduled(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    """The web server collects social data hourly on its own. A caller seeing
    "a refresh is running" must be able to tell that nobody asked for it."""
    from claudetrade.db import refresh_state_store
    from claudetrade.scheduler import SCHEDULER_ENTRY_POINT

    state = RefreshState()
    assert mcp_server.get_refresh_status(pipeline, state)["scheduled"] is False

    scheduled = refresh_state_store.try_acquire(tmp_db, SCHEDULER_ENTRY_POINT)
    status = mcp_server.get_refresh_status(pipeline, state)
    assert status["running"] is True
    assert status["entry_point"] == SCHEDULER_ENTRY_POINT
    assert status["scheduled"] is True
    scheduled.handle.finish("done")

    manual = refresh_state_store.try_acquire(tmp_db, "cli")
    assert mcp_server.get_refresh_status(pipeline, state)["scheduled"] is False
    manual.handle.finish("done")


def test_get_market_status_reports_the_most_recent_signals_regime(
    pipeline: Pipeline, tmp_db: Database, make_signal
) -> None:
    _record(tmp_db, make_signal(symbol="AAA", regime=MarketRegime.BULL_VOLATILE))
    result = mcp_server.get_market_status(pipeline)
    assert result["regime"] == "bull_volatile"


def test_market_session_state_composition() -> None:
    """``_market_session_state`` composes ``is_trading_day``/``MARKET_OPEN``/
    ``MARKET_CLOSE`` rather than reinventing the calendar -- exercised
    directly against fixed instants so it doesn't depend on wall-clock time.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    # A known Tuesday.
    trading_day = dt.date(2024, 6, 4)
    weekend_day = dt.date(2024, 6, 8)  # Saturday

    assert mcp_server._market_session_state(dt.datetime(2024, 6, 4, 7, 0, tzinfo=et)) == "pre_market"
    assert mcp_server._market_session_state(dt.datetime(2024, 6, 4, 10, 0, tzinfo=et)) == "open"
    assert mcp_server._market_session_state(dt.datetime(2024, 6, 4, 17, 0, tzinfo=et)) == "after_hours"
    assert mcp_server._market_session_state(dt.datetime(2024, 6, 8, 10, 0, tzinfo=et)) == "closed"
    assert trading_day.weekday() < 5
    assert weekend_day.weekday() >= 5


# --------------------------------------------------------------------------
# run_scan (WRITE)
# --------------------------------------------------------------------------


def test_run_scan_on_a_fresh_database_does_not_crash(pipeline: Pipeline) -> None:
    """Seeded universe, zero stored price bars: the honest outcome is zero
    evaluated symbols and zero signals, not a crash -- mirrors
    ``test_webapi_signals.test_scan_with_no_stored_market_data_evaluates_nothing_but_does_not_crash``.
    """
    result = mcp_server.run_scan(pipeline)
    assert result["disclaimer"] == DISCLAIMER
    assert result["evaluated_symbols"] == 0
    assert result["signal_count"] == 0
    assert result["rejected_count"] == 0
    assert isinstance(result["warnings"], list)
    assert result["session"] == current_trading_session().isoformat()


def test_run_scan_requests_the_et_trading_session_never_a_weekend_date(
    pipeline: Pipeline, monkeypatch
) -> None:
    """QA handoff v3, F24: at 22:40 ET on a Friday the UTC date is already
    Saturday, and ``run_scan`` used to request that nonexistent session.
    The requested session must be Friday's date -- and never a weekend --
    regardless of the UTC calendar.
    """
    from claudetrade.utils import timeutils

    friday_evening_utc = dt.datetime(2026, 8, 1, 2, 40, tzinfo=dt.UTC)  # Fri 22:40 ET
    monkeypatch.setattr(timeutils, "utc_now", lambda: friday_evening_utc)

    result = mcp_server.run_scan(pipeline)
    requested = dt.date.fromisoformat(result["requested_session"])
    assert requested == dt.date(2026, 7, 31)  # Friday, in ET
    assert requested.weekday() < 5


# --------------------------------------------------------------------------
# trigger_refresh / get_refresh_status (WRITE, background)
# --------------------------------------------------------------------------


def test_trigger_refresh_starts_in_background_and_reports_progress(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    state = RefreshState()
    started = threading.Event()
    release = threading.Event()

    def fake_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        started.set()
        if progress_callback:
            progress_callback("prices", 3, 10)
        release.wait(timeout=5)
        return PipelineResult()

    pipeline.refresh = fake_refresh

    result = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert result["started"] is True
    assert started.wait(timeout=2)

    status = mcp_server.get_refresh_status(pipeline, state)
    assert status["running"] is True
    assert status["phase"] == "prices"
    assert status["symbols_done"] == 3
    assert status["symbols_total"] == 10
    # The local run's fine-grained in-process detail wins (source=local),
    # and the merged view names this process as the owner.
    assert status["source"] == "local"
    assert status["entry_point"] == "mcp"

    release.set()


def test_trigger_refresh_refuses_a_concurrent_run(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    state = RefreshState()
    started = threading.Event()
    release = threading.Event()

    def fake_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        started.set()
        release.wait(timeout=5)
        return PipelineResult()

    pipeline.refresh = fake_refresh

    first = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert first["started"] is True
    assert started.wait(timeout=2)

    second = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert second["started"] is False
    assert "already running" in second["reason"]

    release.set()


def test_trigger_refresh_failure_is_reported_and_unblocks_the_next_run(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    state = RefreshState()

    def failing_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        raise RuntimeError("boom")

    pipeline.refresh = failing_refresh

    mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    deadline = time.monotonic() + 5
    status = mcp_server.get_refresh_status(pipeline, state)
    while status["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
        status = mcp_server.get_refresh_status(pipeline, state)

    assert status["running"] is False
    assert status["last_error"] is not None and "boom" in status["last_error"]

    # Not left stuck: the next trigger starts cleanly.
    second = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert second["started"] is True


# --------------------------------------------------------------------------
# F27: cross-process refresh visibility + single-flight
# --------------------------------------------------------------------------


@pytest.fixture
def second_db(tmp_app_config: AppConfig, tmp_db) -> object:
    """A second handle on the same SQLite file -- what another OS process
    (e.g. the owner's CLI) looks like to this server's database."""
    from claudetrade.db.session import Database

    db_path = tmp_app_config.paths.app_dir / "test.db"
    db = Database(f"sqlite:///{db_path}", config=tmp_app_config)
    yield db
    db.dispose()


def test_get_refresh_status_sees_a_cli_run_from_another_process(
    pipeline: Pipeline, second_db
) -> None:
    """THE F27 acceptance check from QA: the CLI was refreshing while MCP
    ``get_refresh_status`` said idle/started_at:null. The DB row must make
    the CLI's run visible here, with its entry point and progress."""
    from claudetrade.db import refresh_state_store

    outcome = refresh_state_store.try_acquire(second_db, "cli")
    assert outcome.acquired
    outcome.handle._last_write = 0.0
    outcome.handle.update_progress("prices", 42, 2400)

    status = mcp_server.get_refresh_status(pipeline, RefreshState())
    assert status["running"] is True
    assert status["entry_point"] == "cli"
    assert status["phase"] == "prices"
    assert status["symbols_done"] == 42
    assert status["symbols_total"] == 2400
    assert status["started_at"] is not None
    assert status["source"] == "db"


def test_trigger_refresh_refuses_while_the_cli_holds_the_lock(
    pipeline: Pipeline, tmp_app_config: AppConfig, second_db
) -> None:
    from claudetrade.db import refresh_state_store

    outcome = refresh_state_store.try_acquire(second_db, "cli")
    assert outcome.acquired

    result = mcp_server.trigger_refresh(pipeline, tmp_app_config, RefreshState())
    assert result["started"] is False
    assert "cli" in result["reason"]
    assert "already running" in result["reason"]

    # Once the CLI's run finishes, this server may start its own.
    outcome.handle.finish("done")
    started = threading.Event()
    release = threading.Event()

    def fake_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        started.set()
        release.wait(timeout=5)
        return PipelineResult()

    pipeline.refresh = fake_refresh
    state = RefreshState()
    second = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert second["started"] is True
    assert started.wait(timeout=2)
    release.set()


def test_trigger_refresh_local_refusal_does_not_wedge_local_state(
    pipeline: Pipeline, tmp_app_config: AppConfig, second_db
) -> None:
    """A cross-process refusal must roll back the eagerly-set in-process
    running flag, or this server would 409 itself forever afterwards."""
    from claudetrade.db import refresh_state_store

    holder = refresh_state_store.try_acquire(second_db, "cli")
    state = RefreshState()
    refused = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert refused["started"] is False
    assert state.snapshot()["running"] is False
    assert state.snapshot()["phase"] == "idle"
    holder.handle.finish("done")


def test_trigger_refresh_marks_the_db_run_done_and_failed(
    pipeline: Pipeline, tmp_app_config: AppConfig, tmp_db
) -> None:
    """The lock must be released on both outcomes -- the ``refresh_runs`` row
    ends done on success and failed (with the error) on an exception."""
    from sqlalchemy import select

    from claudetrade.db.models import RefreshRunRow

    def ok_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        return PipelineResult()

    pipeline.refresh = ok_refresh
    state = RefreshState()
    mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    deadline = time.monotonic() + 5
    while state.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(0.05)

    def failing_refresh(*, start, end, symbols=None, social_lookback_hours=None, progress_callback=None):
        raise RuntimeError("kaput")

    pipeline.refresh = failing_refresh
    mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    deadline = time.monotonic() + 5
    while state.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(0.05)

    with tmp_db.read_session() as session:
        rows = session.execute(
            select(RefreshRunRow).order_by(RefreshRunRow.id)
        ).scalars().all()
    assert [r.status for r in rows] == ["done", "failed"]
    assert rows[1].last_error == "kaput"
    assert all(r.finished_at is not None for r in rows)


# --------------------------------------------------------------------------
# get_backtest_report
# --------------------------------------------------------------------------


def test_get_backtest_report_with_no_report_yet_says_so(tmp_app_config: AppConfig) -> None:
    result = mcp_server.get_backtest_report(tmp_app_config)
    assert result["available"] is False
    assert "claudetrade backtest report" in result["note"]


def test_get_backtest_report_reads_back_the_latest_saved_report(tmp_app_config: AppConfig) -> None:
    from claudetrade.backtest.report import (
        BacktestReport,
        DataCoverage,
        save_report,
    )

    report = BacktestReport(
        generated_at=utc_now(),
        window_start=dt.date(2024, 1, 1),
        window_end=dt.date(2024, 6, 1),
        coverage=DataCoverage(
            symbol_count=3,
            session_start=dt.date(2024, 1, 2),
            session_end=dt.date(2024, 5, 31),
            total_sessions=100,
            sentiment_row_count=42,
            config_hash="deadbeef",
            code_version="test-version",
        ),
        sections=[],
    )
    exports_dir = tmp_app_config.paths.resolve("exports_dir")
    save_report(report, exports_dir)

    result = mcp_server.get_backtest_report(tmp_app_config)
    assert result["available"] is True
    assert result["disclaimer"] == DISCLAIMER
    assert result["report"]["coverage"]["config_hash"] == "deadbeef"
    assert result["report"]["coverage"]["symbol_count"] == 3


# --------------------------------------------------------------------------
# FastMCP wiring
# --------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = {
    "get_signals",
    "get_sentiment",
    "get_sentiment_history",
    "get_rising_sentiment",
    "get_trending",
    "get_market_status",
    "run_scan",
    "trigger_refresh",
    "get_refresh_status",
    "get_backtest_report",
    "submit_research_revision",
    "get_research_revisions",
}


def test_build_server_registers_every_tool_with_a_description(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    assert set(tools) == EXPECTED_TOOL_NAMES
    for name, tool in tools.items():
        assert tool.description.strip(), f"{name} has no description"

    assert server.name == "claudetrade"
    assert DISCLAIMER in (server.instructions or "")


def test_build_server_tool_schemas_match_the_documented_signatures(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    def properties(name: str) -> set[str]:
        return set(tools[name].parameters.get("properties", {}))

    assert properties("get_signals") == {"min_score", "limit", "sort"}
    assert properties("get_sentiment") == {"symbol", "days"}
    assert properties("get_trending") == {"limit", "source"}
    assert properties("get_market_status") == set()
    assert properties("run_scan") == set()
    assert properties("trigger_refresh") == set()
    assert properties("get_refresh_status") == set()
    assert properties("get_backtest_report") == set()
    assert properties("submit_research_revision") == {
        "signal_id", "thesis", "invalidation", "score_adjustments", "rationale", "sources",
    }
    assert properties("get_research_revisions") == {"signal_id"}


def test_write_tools_are_named_and_described_as_writes(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """``run_scan``/``trigger_refresh`` must say WRITE in their description --
    every other tool must not claim to write."""
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    write_tools = {"run_scan", "trigger_refresh", "submit_research_revision"}
    for name in write_tools:
        assert "WRITE" in tools[name].description

    read_only = EXPECTED_TOOL_NAMES - write_tools
    for name in read_only:
        assert "WRITE" not in tools[name].description


def test_require_fastmcp_succeeds_when_mcp_is_installed() -> None:
    fastmcp_cls = mcp_server._require_fastmcp()
    assert fastmcp_cls.__name__ == "FastMCP"


def test_require_fastmcp_missing_package_gives_an_actionable_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.server.fastmcp":
            raise ImportError("simulated: no module named mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError) as exc_info:
        mcp_server._require_fastmcp()
    assert "claudetrade[mcp]" in str(exc_info.value)
    assert "pip install" in str(exc_info.value)


# --------------------------------------------------------------------------
# F26: the per-tool watchdog -- "MCP reads never hang"
#
# The production failure QA hit: FastMCP runs a *sync* tool function directly
# on the server's event-loop thread, so one stalled call froze the transport's
# message reader and every later call went dead. These tests pin both halves
# of the fix -- the bounded call helper itself, and the end-to-end property
# that a slow tool no longer wedges the server -- through the real in-memory
# MCP transport, not a mock of it.
# --------------------------------------------------------------------------


def test_call_bounded_returns_the_tool_result_when_it_finishes_in_time() -> None:
    import anyio

    result = anyio.run(
        lambda: mcp_server._call_bounded("fast", 5.0, lambda: {"ok": True})
    )
    assert result == {"ok": True}


def test_call_bounded_returns_a_structured_timeout_payload() -> None:
    """On expiry the client gets an actionable payload, never silence."""
    import anyio

    result = anyio.run(
        lambda: mcp_server._call_bounded("stuck", 0.1, lambda: time.sleep(3.0))
    )
    assert result["timed_out"] is True
    assert "stuck" in result["error"]
    assert "refresh" in result["hint"] and "retry" in result["hint"]


def test_call_bounded_preserves_a_falsy_result() -> None:
    """A tool that legitimately returns something falsy must not be mistaken
    for a timeout (the reason the helper uses a sentinel, not ``if result``)."""
    import anyio

    for value in ({}, [], 0, False, None):
        assert (
            anyio.run(lambda v=value: mcp_server._call_bounded("t", 5.0, lambda: v)) == value
        )


def test_call_bounded_propagates_tool_exceptions_unchanged(pipeline: Pipeline) -> None:
    """The watchdog bounds time only. A tool that raises must still raise, so
    FastMCP reports a real error instead of it being masked as a timeout."""
    import anyio

    def boom():
        raise ValueError("kaput")

    with pytest.raises(ValueError, match="kaput"):
        anyio.run(lambda: mcp_server._call_bounded("t", 5.0, boom))


def test_a_slow_tool_does_not_wedge_the_server(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """The exact QA scenario, end to end: while one tool call is stuck (a
    concurrent refresh holding the database, simulated here by a blocking
    read), OTHER tool calls must still be served -- and the stuck one must
    come back with a timed_out payload rather than hanging forever.

    Before the fix this test deadlocks: the sync tool body ran on the event
    loop, so the second call could not even be read off the transport.
    """
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    tmp_app_config.mcp.tool_timeout_seconds = 1.0
    stuck = threading.Event()

    # ``**_kw`` deliberately: pinning the real signature here makes this
    # guard fail *open*. When get_trending later gained a ``source``
    # argument, a fixed-signature stub raised TypeError instantly instead of
    # blocking, so the tool returned promptly and this test would have
    # reported the watchdog working while never exercising it.
    def blocking_trending(_pipeline, **_kw):
        stuck.set()
        time.sleep(30.0)  # far past the deadline; never completes in-test
        return {}

    server = mcp_server.build_server(pipeline, tmp_app_config)

    async def scenario() -> dict[str, float]:
        elapsed: dict[str, float] = {}
        async with create_connected_server_and_client_session(server) as client:
            t0 = time.monotonic()

            async def call(name: str) -> None:
                result = await client.call_tool(name, {})
                elapsed[name] = time.monotonic() - t0
                elapsed[f"{name}_text"] = result.content[0].text

            async with anyio.create_task_group() as tg:
                tg.start_soon(call, "get_trending")
                # Give the slow call time to be dispatched and block.
                await anyio.sleep(0.3)
                tg.start_soon(call, "get_market_status")
        return elapsed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_server, "get_trending", blocking_trending)
        elapsed = anyio.run(scenario)

    assert stuck.is_set()
    # The healthy call was served WHILE the other one was stuck -- it did not
    # queue behind it (the deadline is 1.0s; a wedged server would have made
    # this at least that long, and before the fix, 30s).
    assert elapsed["get_market_status"] < 1.0
    assert "regime" in elapsed["get_market_status_text"]
    # And the stuck call returned a structured timeout rather than hanging.
    assert '"timed_out": true' in elapsed["get_trending_text"]
    assert "get_trending" in elapsed["get_trending_text"]


def test_every_registered_tool_is_wrapped_in_the_watchdog(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """A tool registered as a plain sync function would bypass the watchdog
    entirely and reintroduce F26 for that one tool -- so the wiring itself is
    asserted, not just the two tools exercised above."""
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    assert set(tools) == EXPECTED_TOOL_NAMES
    for name, tool in tools.items():
        assert tool.is_async, f"{name} is registered sync -- it would run on the event loop"


def test_scan_gets_its_own_longer_deadline(
    pipeline: Pipeline, tmp_app_config: AppConfig, monkeypatch
) -> None:
    """A full-universe scan is legitimately slow; bounding it at the read
    deadline would make the tool useless. It must use scan_timeout_seconds."""
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    tmp_app_config.mcp.tool_timeout_seconds = 5.0
    tmp_app_config.mcp.scan_timeout_seconds = 123.0
    seen: dict[str, float] = {}

    real_call_bounded = mcp_server._call_bounded

    async def spy(tool_name, timeout_s, fn):
        seen[tool_name] = timeout_s
        return await real_call_bounded(tool_name, timeout_s, fn)

    monkeypatch.setattr(mcp_server, "_call_bounded", spy)
    server = mcp_server.build_server(pipeline, tmp_app_config)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("run_scan", {})
            await client.call_tool("get_market_status", {})

    anyio.run(scenario)

    assert seen["run_scan"] == 123.0
    assert seen["get_market_status"] == 5.0


def test_tool_timeouts_are_read_per_call_not_frozen_at_build_time(
    pipeline: Pipeline, tmp_app_config: AppConfig, monkeypatch
) -> None:
    """The deadline is read off the live config at call time, so an operator
    changing it does not need a server restart."""
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    tmp_app_config.mcp.tool_timeout_seconds = 5.0
    seen: list[float] = []

    real_call_bounded = mcp_server._call_bounded

    async def spy(tool_name, timeout_s, fn):
        seen.append(timeout_s)
        return await real_call_bounded(tool_name, timeout_s, fn)

    monkeypatch.setattr(mcp_server, "_call_bounded", spy)
    server = mcp_server.build_server(pipeline, tmp_app_config)  # built at 5.0

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("get_market_status", {})
            tmp_app_config.mcp.tool_timeout_seconds = 9.0  # changed after build
            await client.call_tool("get_market_status", {})

    anyio.run(scenario)

    assert seen == [5.0, 9.0]
