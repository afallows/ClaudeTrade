"""MCP (Model Context Protocol) stdio server.

Lets an MCP client running on the same machine -- primarily the Claude
Desktop app, launched via ``claude_desktop_config.json`` (see
``docs/claude-desktop-mcp.md``) -- query this owner's locally-running
ClaudeTrade installation directly, without going through the web UI. The
canonical use case is checking sentiment/signals before or right at market
open, from Claude Desktop rather than a browser tab.

Design notes:

* This module bootstraps its own :class:`~claudetrade.pipeline.Pipeline`
  (``Pipeline.bootstrap(config)``, the same call every other entry point --
  CLI, web UI -- makes) so it works whether or not ``claudetrade ui`` is
  running. SQLite is configured WAL-mode (see ``claudetrade.db.session``),
  so a second process reading concurrently is safe.
* Every tool below except ``run_scan``/``trigger_refresh`` is read-only and
  side-effect-free: no writes, no vendor requests, no recomputation of a
  score or filter rule the rest of the app doesn't already own. Reads go
  through the exact same objects the CLI and the web API use --
  ``pipeline.ledger``, ``ui.data_access``, ``pipeline.provider_status()`` --
  never a second implementation of the same query.
* ``mcp`` (the PyPI package providing the high-level ``FastMCP`` API used
  here) is an optional dependency (the ``claudetrade[mcp]`` extra). Nothing
  at import time of this module requires it to be installed; only
  :func:`build_server`/:func:`run_stdio` -- reached exclusively via
  ``claudetrade mcp`` -- do, and they raise a clear, actionable error if it
  is missing rather than a bare ``ModuleNotFoundError`` traceback.
* stdio transport means the MCP protocol itself is framed on stdout. Nothing
  in this module (or in ``claudetrade.logging_setup``, whose console handler
  is explicitly attached to stderr) writes anything else to stdout.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from claudetrade.config import AppConfig
from claudetrade.db.models import Security, SymbolSentimentDaily
from claudetrade.domain import Signal, SignalStatus
from claudetrade.logging_setup import get_logger
from claudetrade.pipeline import Pipeline
from claudetrade.signals import funnel_store
from claudetrade.ui.data_access import data_freshness, sentiment_timeline
from claudetrade.utils.timeutils import (
    MARKET_CLOSE,
    MARKET_OPEN,
    is_trading_day,
    to_display,
    utc_now,
)
from claudetrade.version import DISCLAIMER
from claudetrade.webapi.refresh_state import RefreshState

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = get_logger(__name__)

#: How many days of stored ``symbol_sentiment_daily`` rows count as "recent"
#: mention volume for ``get_trending`` -- long enough to smooth over a quiet
#: day, short enough that a name from last month doesn't linger at the top.
DEFAULT_TRENDING_WINDOW_DAYS = 7

#: Upper bound on how many ledger rows ``get_signals`` scans before giving up
#: on finding ``limit`` matches -- mirrors ``webapi.routers.signals``'s own
#: default query limit (500) for the same grid.
SIGNAL_SCAN_LIMIT = 500


def _require_fastmcp() -> type[FastMCP]:
    """Import and return ``FastMCP``, or raise a clear, actionable error.

    Kept as its own function (rather than a bare module-level import) so
    that importing ``claudetrade.mcp_server`` itself never requires the
    ``mcp`` package -- only actually building/running a server does.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package (the official Model Context Protocol Python SDK) "
            "is required to run the ClaudeTrade MCP server. Install it with "
            "`pip install claudetrade[mcp]` (or `pip install mcp`), then re-run "
            "`claudetrade mcp`."
        ) from exc
    return FastMCP


# --------------------------------------------------------------------------
# Tool implementations
#
# Each function takes the already-bootstrapped ``Pipeline`` explicitly (and,
# where needed, the ``AppConfig``/``RefreshState``) rather than closing over
# module-level globals -- this is what makes them independently unit
# testable against a ``tmp_db`` without spinning up any MCP transport.
# ``build_server`` below wraps each one in a thin closure registered as the
# actual MCP tool.
# --------------------------------------------------------------------------


def _signal_summary(sig: Signal, status: SignalStatus | None) -> dict[str, Any]:
    """One signal, flattened to the fields an MCP client needs to act on it."""
    return {
        "signal_id": sig.signal_id,
        "symbol": sig.symbol,
        "company_name": sig.company_name,
        "strategy": sig.strategy,
        "direction": str(sig.direction),
        "status": status.value if status else "unknown",
        "regime": str(sig.regime),
        "overall_score": sig.overall_score,
        "confidence": sig.confidence,
        "entry_low": sig.plan.entry_low,
        "entry_high": sig.plan.entry_high,
        "stop_loss": sig.plan.stop_loss,
        "targets": list(sig.plan.targets),
        "reward_risk_ratio": sig.plan.reward_risk_ratio,
        "days_to_earnings": sig.days_to_earnings,
        "session": sig.session.isoformat(),
        "created_at": sig.created_at.isoformat(),
    }


def get_signals(
    pipeline: Pipeline,
    config: AppConfig | None = None,
    *,
    min_score: float = 0.0,
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only. Current signals from the immutable ledger, most-recent-first.

    Mirrors ``webapi.routers.signals.list_signals`` (the Screener grid's data
    source) filtered to ``min_score``, via the same ``pipeline.ledger`` calls
    -- not the HTTP layer. The standing research-only disclaimer is included
    once at the top level, not repeated per row.

    Args:
        config: When given and there are no matching signals, a
            ``why_no_signals`` block is added from the most recent scan's
            persisted rejection funnel (see :func:`_why_no_signals`) -- "why
            no picks today?" answered from data instead of a bare empty list.
            Optional (defaults to omitting that block) only so callers that
            genuinely do not have a config on hand still get a valid result;
            :func:`build_server` always passes one.
    """
    limit = max(1, min(int(limit), 200))
    recent = pipeline.ledger.recent(limit=SIGNAL_SCAN_LIMIT)

    rows: list[dict[str, Any]] = []
    for sig in recent:
        if sig.overall_score < min_score:
            continue
        status = pipeline.ledger.current_status(sig.signal_id)
        rows.append(_signal_summary(sig, status))
        if len(rows) >= limit:
            break

    result: dict[str, Any] = {"disclaimer": DISCLAIMER, "count": len(rows), "signals": rows}
    if not rows and config is not None:
        result["why_no_signals"] = _why_no_signals(config)
    return result


def _why_no_signals(config: AppConfig) -> dict[str, Any]:
    """The most recent scan's rejection funnel, for an empty ``get_signals`` result.

    This MCP server bootstraps its own ``Pipeline`` (see the module
    docstring) -- separate from the CLI's and the web API server's -- so a
    scan run from either of those has no in-memory ``ScanResult`` here to
    read. ``signals.funnel_store`` persists the funnel from every
    ``Pipeline.scan()`` call, by any process, to a small file under this
    installation's ``snapshots_dir``; this reads that artifact back.
    """
    funnel_data = funnel_store.load_latest(config)
    if funnel_data is None:
        return {
            "available": False,
            "note": (
                "No scan has been run on this installation yet (or its funnel record is "
                "missing). Run the run_scan tool, or `claudetrade scan`, then check "
                "get_signals again."
            ),
        }
    return {
        "available": True,
        "session": funnel_data.get("session"),
        "generated_at": funnel_data.get("generated_at"),
        "evaluated_symbols": funnel_data.get("evaluated_symbols"),
        "rejected_count": funnel_data.get("rejected_count"),
        "note": (
            "Rejection funnel and closest near-misses from the most recent scan on this "
            "installation -- may predate the current get_signals call if no scan has run "
            "since."
        ),
        "funnel": funnel_data.get("funnel"),
    }


def get_sentiment(pipeline: Pipeline, symbol: str, *, days: int = 7) -> dict[str, Any]:
    """Read-only. Recent daily sentiment/mention rows for one symbol.

    Mirrors what ``webapi.routers.tickers.ticker_detail`` serves for the
    sentiment panel: the ``symbol_sentiment_daily`` timeline from
    ``ui.data_access.sentiment_timeline`` (the same helper, not a
    re-implementation), clipped to the requested window.
    """
    symbol = symbol.strip().upper()
    days = max(1, min(int(days), 365))
    end = utc_now().date()
    start = end - dt.timedelta(days=days)

    points = [p for p in sentiment_timeline(pipeline.db, symbol) if start <= p.session <= end]
    if not points:
        return {
            "symbol": symbol,
            "days": days,
            "total_mentions": 0,
            "average_bull_bear_ratio": None,
            "points": [],
            "note": f"No sentiment/mention data stored for {symbol} in the last {days} day(s). "
            "Run `claudetrade refresh` (or the trigger_refresh tool) with social sources enabled.",
        }

    total_mentions = sum(p.post_count for p in points)
    avg_bull_bear = sum(p.bull_bear_ratio for p in points) / len(points)
    return {
        "symbol": symbol,
        "days": days,
        "total_mentions": total_mentions,
        "average_bull_bear_ratio": round(avg_bull_bear, 4),
        "points": [
            {
                "session": p.session.isoformat(),
                "post_count": p.post_count,
                "unique_authors": p.unique_authors,
                "engagement_weighted": p.engagement_weighted,
                "bull_bear_ratio": p.bull_bear_ratio,
                "manipulation_risk": p.manipulation_risk,
                "confidence": p.confidence,
            }
            for p in points
        ],
        "note": None,
    }


def get_trending(pipeline: Pipeline, *, limit: int = 20) -> dict[str, Any]:
    """Read-only. Symbols ranked by recent mention volume.

    No existing screen aggregates mention volume *across* symbols (the
    Screener/dashboard rank signals, not raw mention counts), so this sums
    ``symbol_sentiment_daily.post_count`` -- the same column
    ``ui.data_access.sentiment_timeline``/the ticker-detail sentiment panel
    already reads -- across the source="all" combined rows (the same
    aggregate ``sentiment_timeline``'s own default reads) over the last
    ``DEFAULT_TRENDING_WINDOW_DAYS`` days, grouped by symbol. A read-only
    aggregate query, not a new table or a write path.
    """
    limit = max(1, min(int(limit), 200))
    end = utc_now().date()
    start = end - dt.timedelta(days=DEFAULT_TRENDING_WINDOW_DAYS)

    with pipeline.db.read_session() as session:
        rows = session.execute(
            select(
                SymbolSentimentDaily.symbol,
                func.sum(SymbolSentimentDaily.post_count).label("mentions"),
                func.avg(SymbolSentimentDaily.bull_bear_ratio).label("avg_bull_bear_ratio"),
                func.avg(SymbolSentimentDaily.confidence).label("avg_confidence"),
                func.max(SymbolSentimentDaily.session).label("latest_session"),
            )
            # The join against ``securities`` is a guard, not an
            # optimisation: stored sentiment rows can carry junk "symbols"
            # left behind by earlier extraction bugs (bare English words) or
            # by the synthetic demo provider's fabricated tickers, and a
            # trending list is exactly where such rows would surface. Only
            # symbols that exist in the reference table rank.
            .join(Security, Security.symbol == SymbolSentimentDaily.symbol)
            .where(
                SymbolSentimentDaily.source == "all",
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= end,
            )
            .group_by(SymbolSentimentDaily.symbol)
            .order_by(func.sum(SymbolSentimentDaily.post_count).desc())
            .limit(limit)
        ).all()

    symbols = [
        {
            "symbol": row.symbol,
            "mentions": int(row.mentions or 0),
            "average_bull_bear_ratio": round(float(row.avg_bull_bear_ratio or 0.0), 4),
            "average_confidence": round(float(row.avg_confidence or 0.0), 4),
            "latest_session": row.latest_session.isoformat() if row.latest_session else None,
        }
        for row in rows
    ]
    return {"window_days": DEFAULT_TRENDING_WINDOW_DAYS, "count": len(symbols), "symbols": symbols}


def _market_session_state(now_et: dt.datetime) -> str:
    """``pre_market`` / ``open`` / ``after_hours`` / ``closed``, in ET.

    Composed entirely from the existing exchange-calendar/session primitives
    in ``claudetrade.utils.timeutils`` (``is_trading_day``, ``MARKET_OPEN``,
    ``MARKET_CLOSE``) -- no holiday or session-hours rule is reimplemented
    here.
    """
    if not is_trading_day(now_et.date()):
        return "closed"
    current_time = now_et.time()
    if current_time < MARKET_OPEN:
        return "pre_market"
    if current_time < MARKET_CLOSE:
        return "open"
    return "after_hours"


def get_market_status(pipeline: Pipeline) -> dict[str, Any]:
    """Read-only. Regime, ET clock/session state, freshness and provider health.

    The piece that makes "before/at market open" answerable: ``market_session``
    plus the current Eastern time, alongside the same regime (most recent
    signal's), freshness (``ui.data_access.data_freshness``) and provider
    status (``pipeline.provider_status()``) the dashboard shows.
    """
    now_utc = utc_now()
    now_et = to_display(now_utc, "America/New_York")

    freshness = data_freshness(pipeline.db)
    recent = pipeline.ledger.recent(limit=1)
    regime_value = str(recent[0].regime) if recent else "unknown"

    providers = [
        {
            "name": s.name,
            "kind": s.kind,
            "available": s.available,
            "configured": s.configured,
            "supports_point_in_time": s.supports_point_in_time,
            "message": s.message,
        }
        for s in pipeline.provider_status()
    ]
    degraded_providers = [p["name"] for p in providers if p["configured"] and not p["available"]]

    return {
        "regime": regime_value,
        "market_session": _market_session_state(now_et),
        "is_trading_day": is_trading_day(now_et.date()),
        "current_time_et": now_et.isoformat(),
        "current_time_utc": now_utc.isoformat(),
        "last_refresh_utc": freshness.latest_ingested_at.isoformat()
        if freshness.latest_ingested_at
        else None,
        "latest_session_with_data": freshness.latest_session.isoformat()
        if freshness.latest_session
        else None,
        "symbols_with_data": freshness.symbol_count,
        "providers": providers,
        "degraded_providers": degraded_providers,
    }


def run_scan(pipeline: Pipeline) -> dict[str, Any]:
    """WRITE. Runs ``Pipeline.scan`` for today's session and records signals.

    Identical to ``claudetrade scan`` / ``POST /api/scan``: builds
    point-in-time contexts, classifies the regime, ranks candidates and
    appends any new signals to the immutable ledger. ``generate_thesis`` is
    left at its default ``False`` (as the web API's interactive scan does)
    so this does not block on AI-provider latency. Returns summary counts
    only -- call ``get_signals`` afterwards for the actual candidates.
    """
    session_date = utc_now().date()
    result = pipeline.scan(session_date, generate_thesis=False)
    scan_result = result.scan
    return {
        "disclaimer": DISCLAIMER,
        # The pipeline may have fallen back to the latest stored session when
        # the requested one has no data yet (see ``Pipeline.scan``); report
        # both so the caller can tell which session was actually evaluated.
        "requested_session": session_date.isoformat(),
        "session": scan_result.session.isoformat() if scan_result else session_date.isoformat(),
        "evaluated_symbols": scan_result.evaluated_symbols if scan_result else 0,
        "signal_count": len(scan_result.signals) if scan_result else 0,
        "rejected_count": len(scan_result.rejected) if scan_result else 0,
        "warnings": list(result.warnings),
        # The full rejection funnel (reason -> count per strategy, plus the
        # closest near-misses): a zero-signal scan must explain itself in the
        # same response, not require a separate diagnostic call.
        "funnel": scan_result.funnel.to_dict() if scan_result else None,
    }


def trigger_refresh(pipeline: Pipeline, config: AppConfig, state: RefreshState) -> dict[str, Any]:
    """WRITE (background). Starts a data refresh; poll with ``get_refresh_status``.

    Reuses ``webapi.refresh_state.RefreshState`` -- the same
    progress-tracking mechanism ``POST /api/system/refresh`` uses -- rather
    than inventing a second one; this MCP server holds its own instance,
    matching the app's one-``Pipeline``-per-process model (each of the CLI,
    the web UI and this server has its own ``Pipeline`` and its own refresh
    state; SQLite's WAL mode is what makes concurrent *reads* across them
    safe, not a shared in-memory refresh state). 409-equivalent: if a refresh
    is already running here, this reports that instead of starting a second
    one that would race the first one's writes.
    """
    with state.lock:
        already_running = state.running
        if not already_running:
            state.running = True
            state.phase = "starting"
            state.symbols_done = 0
            state.symbols_total = 0
            state.started_at = utc_now()
            state.finished_at = None
            state.last_error = None

    # ``state.snapshot()`` acquires ``state.lock`` itself (a plain, non-
    # reentrant ``threading.Lock``) -- it must never be called while the lock
    # above is still held, or the same thread deadlocks trying to re-acquire it.
    if already_running:
        return {
            "started": False,
            "reason": "a refresh is already running",
            "status": state.snapshot(),
        }

    def _run() -> None:
        end = utc_now().date()
        # Price history needs its own, much longer window: contexts require
        # 30+ bars (data.context.MIN_CONTEXT_BARS), so a window sized to the
        # 14-day social lookback guaranteed every scan on a fresh install
        # evaluated zero symbols. 90 days matches the CLI refresh default;
        # social sources are bounded separately to the sentiment window.
        start = end - dt.timedelta(days=90)
        try:
            pipeline.refresh(
                start=start,
                end=end,
                social_lookback_hours=config.sentiment.lookback_days * 24,
                progress_callback=state.update_progress,
            )
        except Exception as exc:  # matches webapi.routers.system's own catch-all
            with state.lock:
                state.last_error = str(exc)
            log.exception("MCP-triggered background refresh failed")
        finally:
            with state.lock:
                state.running = False
                state.phase = "idle"
                state.finished_at = utc_now()

    threading.Thread(target=_run, name="claudetrade-mcp-refresh", daemon=True).start()
    return {"started": True, "status": state.snapshot()}


def get_refresh_status(state: RefreshState) -> dict[str, Any]:
    """Read-only. Progress of the background refresh started by ``trigger_refresh``."""
    return state.snapshot()


def get_backtest_report(config: AppConfig) -> dict[str, Any]:
    """Read-only. The latest generated backtest report, or a clear "run it first" message.

    Reads back the JSON twin ``claudetrade backtest report`` writes under
    this installation's exports directory (``backtest.report.save_report``)
    -- never recomputes anything here: a walk-forward backtest across every
    strategy can take a while, and this tool must answer instantly even when
    no report exists yet. Mirrors ``get_signals``' ``why_no_signals`` shape:
    a bool ``available`` flag plus either the report or a ``note`` explaining
    what to run.
    """
    from claudetrade.backtest.report import load_latest_report

    exports_dir = config.paths.resolve("exports_dir")
    report = load_latest_report(exports_dir)
    if report is None:
        return {
            "available": False,
            "note": (
                "No backtest report has been generated on this installation yet. Run "
                "`claudetrade backtest report` on the machine with the real database, "
                "then ask again -- it walk-forward backtests every registered strategy "
                "and can take a while on a large universe/window."
            ),
        }
    return {"available": True, "disclaimer": DISCLAIMER, "report": report}


# --------------------------------------------------------------------------
# Server wiring
# --------------------------------------------------------------------------

INSTRUCTIONS = (
    "Local, read-mostly access to one owner's ClaudeTrade research database "
    "(swing-trading sentiment/technical signals). " + DISCLAIMER
)


def build_server(pipeline: Pipeline, config: AppConfig) -> FastMCP:
    """Construct the FastMCP server and register every tool against ``pipeline``.

    Separated from :func:`run_stdio` so tests can build a server (and
    introspect its registered tools) without ever starting the stdio
    transport loop.
    """
    FastMCP = _require_fastmcp()
    server = FastMCP(name="claudetrade", instructions=INSTRUCTIONS)
    refresh_state = RefreshState()

    @server.tool(
        name="get_signals",
        description=(
            "Read-only. Current signals/recommendations from the immutable ledger, "
            "most-recent-first: symbol, strategy, direction, score, confidence, "
            "entry/stop/targets and days_to_earnings. Includes the standing "
            "research-only disclaimer once, not per row. When there are no matching "
            "signals, includes a why_no_signals block: the rejection funnel (reasons "
            "and counts) and closest near-misses from the most recent scan on this "
            "installation, so 'why no picks today?' has a real answer."
        ),
    )
    def _get_signals(min_score: float = 0.0, limit: int = 20) -> dict[str, Any]:
        return get_signals(pipeline, config, min_score=min_score, limit=limit)

    @server.tool(
        name="get_sentiment",
        description=(
            "Read-only. Recent daily sentiment for one symbol: post counts, unique "
            "authors, engagement, bull/bear ratio, manipulation risk and confidence, "
            "one row per trading day over the last N days."
        ),
    )
    def _get_sentiment(symbol: str, days: int = 7) -> dict[str, Any]:
        return get_sentiment(pipeline, symbol, days=days)

    @server.tool(
        name="get_trending",
        description=(
            "Read-only. Symbols ranked by recent social mention volume "
            f"(last {DEFAULT_TRENDING_WINDOW_DAYS} days of stored daily sentiment "
            "aggregates), most-mentioned first."
        ),
    )
    def _get_trending(limit: int = 20) -> dict[str, Any]:
        return get_trending(pipeline, limit=limit)

    @server.tool(
        name="get_market_status",
        description=(
            "Read-only. Market regime, current Eastern Time, and whether the market "
            "is pre-market/open/after-hours/closed right now; last data-refresh time, "
            "how many symbols have stored data, and provider health/degradations. "
            "The tool to check before asking about 'this morning's' sentiment."
        ),
    )
    def _get_market_status() -> dict[str, Any]:
        return get_market_status(pipeline)

    @server.tool(
        name="run_scan",
        description=(
            "WRITE -- records new signals to the immutable ledger. Runs a full scan "
            "for today's session (identical to `claudetrade scan`) and returns "
            "summary counts only; call get_signals afterwards for the candidates."
        ),
    )
    def _run_scan() -> dict[str, Any]:
        return run_scan(pipeline)

    @server.tool(
        name="trigger_refresh",
        description=(
            "WRITE, runs in the background -- pulls fresh market data, earnings and "
            "social sentiment from every configured provider and stores it; can take "
            "several minutes on a large universe. Returns immediately; poll "
            "get_refresh_status for progress. Refuses to start a second refresh while "
            "one is already running here."
        ),
    )
    def _trigger_refresh() -> dict[str, Any]:
        return trigger_refresh(pipeline, config, refresh_state)

    @server.tool(
        name="get_refresh_status",
        description="Read-only. Progress of the background refresh started by trigger_refresh.",
    )
    def _get_refresh_status() -> dict[str, Any]:
        return get_refresh_status(refresh_state)

    @server.tool(
        name="get_backtest_report",
        description=(
            "Read-only. The most recently generated backtest report (see `claudetrade "
            "backtest report`): per-strategy walk-forward win rate with a 95% confidence "
            "interval, expectancy per trade after costs, profit factor, max drawdown, average "
            "hold time, and a prominent significance verdict -- a strategy without enough "
            "out-of-sample evidence is headlined 'INSUFFICIENT EVIDENCE', not its "
            "best-looking point estimate. Also includes the data-coverage header (symbols, "
            "session range, config hash) and per-window rejection funnels. Returns "
            "available=false with instructions if no report has been generated yet -- this "
            "never runs a backtest itself."
        ),
    )
    def _get_backtest_report() -> dict[str, Any]:
        return get_backtest_report(config)

    return server


def run_stdio(config: AppConfig) -> None:
    """Bootstrap the pipeline and serve every tool over MCP stdio (blocking).

    ``Pipeline.bootstrap`` opens the database, applies migrations and wires
    the configured providers -- the same call ``claudetrade refresh``/``scan``
    make -- so this works on a fresh install with no other entry point ever
    having run first.
    """
    pipeline = Pipeline.bootstrap(config)
    server = build_server(pipeline, config)
    server.run(transport="stdio")


__all__ = [
    "build_server",
    "get_backtest_report",
    "get_market_status",
    "get_refresh_status",
    "get_sentiment",
    "get_signals",
    "get_trending",
    "run_scan",
    "run_stdio",
    "trigger_refresh",
]
