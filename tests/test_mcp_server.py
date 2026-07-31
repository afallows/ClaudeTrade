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
from claudetrade.utils.timeutils import utc_now
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
    assert result == {"disclaimer": DISCLAIMER, "count": 0, "signals": []}


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
    today = utc_now().date()
    with tmp_db.session() as session:
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
    today = utc_now().date()
    with tmp_db.session() as session:
        for i in range(5):
            session.add(
                SymbolSentimentDaily(symbol=f"SYM{i}", session=today, source="all", post_count=i + 1)
            )

    result = mcp_server.get_trending(pipeline, limit=2)
    assert result["count"] == 2
    assert len(result["symbols"]) == 2


def test_get_trending_empty_db_is_an_honest_empty_list(pipeline: Pipeline) -> None:
    result = mcp_server.get_trending(pipeline)
    assert result == {
        "window_days": mcp_server.DEFAULT_TRENDING_WINDOW_DAYS,
        "count": 0,
        "symbols": [],
    }


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
    assert result["session"] == utc_now().date().isoformat()


# --------------------------------------------------------------------------
# trigger_refresh / get_refresh_status (WRITE, background)
# --------------------------------------------------------------------------


def test_trigger_refresh_starts_in_background_and_reports_progress(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    state = RefreshState()
    started = threading.Event()
    release = threading.Event()

    def fake_refresh(*, start, end, symbols=None, progress_callback=None):
        started.set()
        if progress_callback:
            progress_callback("prices", 3, 10)
        release.wait(timeout=5)
        return PipelineResult()

    pipeline.refresh = fake_refresh

    result = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert result["started"] is True
    assert started.wait(timeout=2)

    status = mcp_server.get_refresh_status(state)
    assert status["running"] is True
    assert status["phase"] == "prices"
    assert status["symbols_done"] == 3
    assert status["symbols_total"] == 10

    release.set()


def test_trigger_refresh_refuses_a_concurrent_run(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    state = RefreshState()
    started = threading.Event()
    release = threading.Event()

    def fake_refresh(*, start, end, symbols=None, progress_callback=None):
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

    def failing_refresh(*, start, end, symbols=None, progress_callback=None):
        raise RuntimeError("boom")

    pipeline.refresh = failing_refresh

    mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    deadline = time.monotonic() + 5
    status = mcp_server.get_refresh_status(state)
    while status["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
        status = mcp_server.get_refresh_status(state)

    assert status["running"] is False
    assert status["last_error"] is not None and "boom" in status["last_error"]

    # Not left stuck: the next trigger starts cleanly.
    second = mcp_server.trigger_refresh(pipeline, tmp_app_config, state)
    assert second["started"] is True


# --------------------------------------------------------------------------
# FastMCP wiring
# --------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = {
    "get_signals",
    "get_sentiment",
    "get_trending",
    "get_market_status",
    "run_scan",
    "trigger_refresh",
    "get_refresh_status",
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

    assert properties("get_signals") == {"min_score", "limit"}
    assert properties("get_sentiment") == {"symbol", "days"}
    assert properties("get_trending") == {"limit"}
    assert properties("get_market_status") == set()
    assert properties("run_scan") == set()
    assert properties("trigger_refresh") == set()
    assert properties("get_refresh_status") == set()


def test_write_tools_are_named_and_described_as_writes(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """``run_scan``/``trigger_refresh`` must say WRITE in their description --
    every other tool must not claim to write."""
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    for name in ("run_scan", "trigger_refresh"):
        assert "WRITE" in tools[name].description

    read_only = EXPECTED_TOOL_NAMES - {"run_scan", "trigger_refresh"}
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
