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
* Every tool below except ``run_scan``/``trigger_refresh``/
  ``submit_research_revision`` is read-only and writes nothing: no
  recomputation of a score or filter rule the rest of the app doesn't
  already own. Reads go through the exact same objects the CLI and the web
  API use -- ``pipeline.ledger``, ``ui.data_access``, ``pipeline
  .provider_status()`` -- never a second implementation of the same query.
  ``submit_research_revision`` writes too, but only to the append-only
  research ledger (``signals.research``) -- it can never touch a
  ``SignalRow`` or its trade plan; see that module for the guarantee.
  **Two further exceptions make a real vendor request while staying
  read-only from this app's own state:** ``get_adanos_detail`` and
  ``get_adanos_explain`` call Adanos's on-demand official API directly
  (``providers.social.adanos.AdanosProvider.fetch_stock_detail``/
  ``fetch_explain``) and SPEND a metered request from its ~250/month free
  tier each time -- see those tools' descriptions, which say so plainly.
  ``get_adanos_budget`` is free (it only reads locally stored budget state).
* ``mcp`` (the PyPI package providing the high-level ``FastMCP`` API used
  here) is an optional dependency (the ``claudetrade[mcp]`` extra). Nothing
  at import time of this module requires it to be installed; only
  :func:`build_server`/:func:`run_stdio` -- reached exclusively via
  ``claudetrade mcp`` -- do, and they raise a clear, actionable error if it
  is missing rather than a bare ``ModuleNotFoundError`` traceback.
* stdio transport means the MCP protocol itself is framed on stdout. Nothing
  in this module (or in ``claudetrade.logging_setup``, whose console handler
  is explicitly attached to stderr) writes anything else to stdout.
* **Every registered tool is bounded (QA handoff v3, F26).** FastMCP calls a
  *sync* tool function directly on the server's event loop thread
  (``mcp.server.fastmcp.utilities.func_metadata`` -- no thread offload), so
  one blocking tool call used to freeze the whole server, including the
  transport's message reader: QA observed ``get_signals`` stall under a
  concurrent CLI refresh and every subsequent call go dead. The closures
  ``build_server`` registers are therefore *async*, running the sync tool
  body on a worker thread with a hard deadline
  (``McpConfig.tool_timeout_seconds`` / ``scan_timeout_seconds``); on expiry
  the client gets a structured ``{"timed_out": true, ...}`` payload instead
  of silence, and the event loop keeps serving other calls throughout.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from claudetrade.config import AppConfig
from claudetrade.db import refresh_state_store
from claudetrade.db.models import Security, SymbolSentimentDaily
from claudetrade.domain import Signal, SignalStatus
from claudetrade.logging_setup import get_logger
from claudetrade.pipeline import Pipeline
from claudetrade.providers.base import ProviderError
from claudetrade.signals import funnel_store
from claudetrade.signals.dedupe import collapse_recommendations
from claudetrade.signals.research import ResearchGuardrailError, ResearchLedger
from claudetrade.signals.scoring import adjusted_overall
from claudetrade.ui.data_access import data_freshness, sentiment_timeline
from claudetrade.utils.timeutils import (
    MARKET_CLOSE,
    MARKET_OPEN,
    current_trading_session,
    is_trading_day,
    to_display,
    utc_now,
)
from claudetrade.version import DISCLAIMER
from claudetrade.webapi.refresh_state import RefreshState

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from claudetrade.providers.social.adanos import AdanosProvider

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


def _signal_summary(
    sig: Signal, status: SignalStatus | None, *, effective_score: float, has_research: bool
) -> dict[str, Any]:
    """One signal, flattened to the fields an MCP client needs to act on it.

    ``effective_score`` is ``overall_score`` re-ranked by any accepted MCP
    research revisions (``signals.scoring.adjusted_overall``); it equals
    ``overall_score`` exactly when ``has_research`` is False. ``overall_score``
    itself is always the original, engine-computed, audited number -- research
    never rewrites it.
    """
    return {
        "signal_id": sig.signal_id,
        "symbol": sig.symbol,
        "company_name": sig.company_name,
        "strategy": sig.strategy,
        "direction": str(sig.direction),
        "status": status.value if status else "unknown",
        "regime": str(sig.regime),
        "overall_score": sig.overall_score,
        "effective_score": effective_score,
        "has_research": has_research,
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
    sort: str = "score",
    distinct: bool = True,
) -> dict[str, Any]:
    """Read-only. Current signals from the immutable ledger, best-scoring first.

    Mirrors ``webapi.routers.signals.list_signals`` (the Screener grid's data
    source) filtered to ``min_score``, via the same ``pipeline.ledger`` calls
    -- not the HTTP layer. The standing research-only disclaimer is included
    once at the top level, not repeated per row.

    **Ordering is score-descending, and that is a correction.** This used to
    return the most recently *written* rows: the ledger was read newest-first
    and truncated in Python, so ``limit=N`` yielded whatever the last scan
    happened to write last. Scans write in roughly symbol order, so the
    result was an alphabetical slice that silently excluded the best
    candidates -- QA saw a ``limit=20`` call return GIL/TTEK/THC while MSFT
    (73.90), LPLA (75.53) and AMZN (73.40) sat outside the window, and two
    reviewers comparing the same scan through this tool and the UI reached
    different conclusions. ``min_score`` was the only way to reach the top,
    which required already knowing the score distribution.

    Args:
        sort: ``"score"`` (default, best first -- matches the UI screener) or
            ``"created_at"`` (newest first). Chronological order is a real
            need for audit and ledger inspection, so it stays available; it
            is simply the wrong default for "show me the candidates". When
            ``distinct=True`` it still governs which rows get *scanned* (see
            below), and it also re-sorts the final, already-collapsed rows
            by the representative's ``created_at`` -- otherwise the
            collapsed rows stay in their natural (representative
            ``effective_score`` descending) order.
        config: When given and there are no matching signals, a
            ``why_no_signals`` block is added from the most recent scan's
            persisted rejection funnel (see :func:`_why_no_signals`) -- "why
            no picks today?" answered from data instead of a bare empty list.
            Also supplies the component weights ``effective_score`` needs
            (see below); without it every row's ``effective_score`` falls
            back to the unadjusted ``overall_score``. Optional only so
            callers that genuinely do not have a config on hand still get a
            valid result; :func:`build_server` always passes one.
        distinct: Default ``True``. Collapses read-time duplicates via
            ``signals.dedupe.collapse_recommendations`` before applying
            ``limit`` -- see that module's docstring for exactly what
            "duplicate" means here (same-session re-scans after a code/
            config change, and cross-strategy overlap on the same symbol
            +direction). ``distinct=False`` returns the raw, uncollapsed
            per-strategy ledger rows -- today's original behaviour --
            unchanged. Each collapsed row gains ``corroborating_strategies``
            (the OTHER strategies that agree, with their own scores) and
            ``duplicates_collapsed`` (how many exact re-scan duplicates were
            folded into this one row). ``total_matching``/``truncated`` count
            GROUPS, not raw ledger rows, when ``distinct=True`` -- a caller
            asking "how many recommendations are there" gets an honest
            answer in either mode, but the unit changes.

    Each row carries ``effective_score`` (``overall_score`` re-ranked by any
    accepted MCP research revisions -- see ``submit_research_revision`` --
    via ``signals.scoring.adjusted_overall``) and ``has_research`` (whether
    any revision exists at all). When ``distinct=False`` and ``sort="score"``
    and at least one returned row has research, the page is re-sorted by
    ``effective_score`` instead of the raw ``overall_score`` the SQL query
    used to select and order it -- a revision's clamped adjustment can only
    ever move a score by a bounded amount
    (``McpConfig.max_component_adjustment``), so it can reorder rows already
    on the page, but it cannot pull in a row that did not qualify for the
    page on ``overall_score`` in the first place. When ``distinct=True`` the
    collapsed groups are already ordered by representative
    ``effective_score`` (see :func:`~claudetrade.signals.dedupe
    .collapse_recommendations`), so no extra re-sort is needed. Research
    revisions are fetched with ONE extra batched query, keyed to exactly the
    signal ids scanned (see ``ResearchLedger.latest_research_revisions``) --
    never a per-row lookup.
    """
    limit = max(1, min(int(limit), 200))
    order = "created_at" if str(sort).lower() == "created_at" else "score"
    # Ordering and filtering happen in SQL (``overall_score`` is indexed), so
    # ``limit`` genuinely means "the N best" rather than "the N newest that
    # happened to qualify". One query for signals AND statuses: the per-row
    # ``current_status`` loop this replaced issued up to SIGNAL_SCAN_LIMIT+1
    # sequential queries and never broke early when fewer than ``limit`` rows
    # cleared the filter -- that aggregate, on the MCP event loop, is what QA
    # observed as a hung-then-dead server (F26).
    #
    # ``distinct=True`` scans up to SIGNAL_SCAN_LIMIT raw rows (rather than
    # exactly ``limit``) BEFORE collapsing: collapsing can only ever shrink a
    # page (duplicates/siblings fold together), so scanning only ``limit``
    # raw rows first would silently under-fill a ``limit``-sized page of
    # GROUPS whenever any collapsing happened on the page.
    scan_limit = SIGNAL_SCAN_LIMIT if distinct else limit
    matches, total = pipeline.ledger.list_with_status(
        min_score=min_score, limit=scan_limit, order=order
    )

    # Batched, not per-row (same F26 discipline as the query above).
    research = ResearchLedger(pipeline.db).latest_research_revisions(
        [sig.signal_id for sig, _ in matches]
    )
    effective_scores: dict[str, float] = {}
    has_research_by_id: dict[str, bool] = {}
    for sig, _status in matches:
        revision = research.get(sig.signal_id)
        has_research = revision is not None
        if revision is not None and config is not None:
            effective = adjusted_overall(
                sig.components.as_dict(),
                sig.overall_score,
                revision["score_adjustments"],
                config,
            )
        else:
            effective = sig.overall_score
        effective_scores[sig.signal_id] = effective
        has_research_by_id[sig.signal_id] = has_research

    if distinct:
        groups = collapse_recommendations(matches, effective_scores)
        if order == "created_at":
            # Groups come back sorted by representative effective_score
            # descending (collapse_recommendations' own contract); honour
            # the caller's explicit chronological request on the final,
            # already-collapsed list instead.
            groups.sort(key=lambda g: g.signal.created_at, reverse=True)
        page = groups[:limit]
        rows = []
        for group in page:
            sig = group.signal
            row = _signal_summary(
                sig,
                group.status,
                effective_score=group.effective_score,
                has_research=has_research_by_id.get(sig.signal_id, False),
            )
            row["corroborating_strategies"] = [
                {
                    "signal_id": c.signal_id,
                    "strategy": c.strategy,
                    "overall_score": c.overall_score,
                    "effective_score": c.effective_score,
                }
                for c in group.corroborating
            ]
            row["duplicates_collapsed"] = group.duplicates_collapsed
            rows.append(row)
        total_matching = len(groups)
        truncated = total_matching > len(page) or total > len(matches)
    else:
        rows = [
            _signal_summary(
                sig,
                status,
                effective_score=effective_scores[sig.signal_id],
                has_research=has_research_by_id[sig.signal_id],
            )
            for sig, status in matches
        ]
        if order == "score" and any(row["has_research"] for row in rows):
            rows.sort(key=lambda row: row["effective_score"], reverse=True)
        total_matching = total
        truncated = total > len(rows)

    result: dict[str, Any] = {
        "disclaimer": DISCLAIMER,
        "count": len(rows),
        # Callers could not previously tell a complete answer from a slice.
        # Counts GROUPS, not raw ledger rows, when distinct=True -- see the
        # ``distinct`` arg's docstring.
        "total_matching": total_matching,
        "truncated": truncated,
        "sorted_by": order,
        "distinct": distinct,
        "signals": rows,
    }
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


def _analyst_rating_action_payload(action) -> dict[str, Any]:
    return {
        "date": action.date.isoformat(),
        "firm": action.firm,
        "analyst_name": action.analyst_name,
        "rating_id": action.rating_id,
        "rating_label": action.rating_label,
        "action_id": action.action_id,
        "action_label": action.action_label,
        "price_target": action.price_target,
        "old_price_target": action.old_price_target,
        "analyst_stars": action.analyst_stars,
        "analyst_success_rate": action.analyst_success_rate,
        "included_in_consensus": action.included_in_consensus,
    }


def _analyst_snapshot_payload(snapshot) -> dict[str, Any]:
    return {
        "as_of_session": snapshot.as_of_session.isoformat(),
        "consensus_rating": snapshot.consensus_rating,
        "buy_count": snapshot.buy_count,
        "hold_count": snapshot.hold_count,
        "sell_count": snapshot.sell_count,
        "analyst_count": snapshot.analyst_count,
        "consensus_rate": snapshot.consensus_rate,
        "price_target_mean": snapshot.price_target_mean,
        "price_target_high": snapshot.price_target_high,
        "price_target_low": snapshot.price_target_low,
        "price_target_currency": snapshot.price_target_currency,
        "consensus_over_time": [
            {
                "date": p.date.isoformat(),
                "buy": p.buy,
                "hold": p.hold,
                "sell": p.sell,
                "consensus": p.consensus,
                "price_target": p.price_target,
            }
            for p in snapshot.consensus_over_time
        ],
        "recent_rating_actions": [
            _analyst_rating_action_payload(a) for a in snapshot.recent_rating_actions
        ],
        "last_eps_surprise_pct": snapshot.last_eps_surprise_pct,
        "next_earnings_estimate_eps": snapshot.next_earnings_estimate_eps,
        "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
    }


def get_analyst_sentiment(pipeline: Pipeline, symbol: str) -> dict[str, Any]:
    """Read-only. TipRanks-sourced analyst-consensus snapshot for one symbol.

    Reads ``data.analyst.latest_and_previous_snapshots`` -- the same batched
    read helper the Streamlit ticker-detail screen's "Analyst sentiment"
    block uses -- for the latest stored ``analyst_snapshots`` row plus the
    prior stored session, and ``data.analyst.analyst_delta`` for the
    comparison between them. Nothing here makes a network call: this only
    reads what the last ``claudetrade refresh`` already harvested from
    TipRanks' ``dataForTicker`` response for this symbol (see
    ``providers.market.tipranks_analyst`` for the field mapping and its
    documented ``ratingId``/``actionId`` semantics, including where those
    are confirmed vs. best-effort).

    ``available=False`` (with ``snapshot``/``delta`` both ``None``) means
    this installation has never stored a snapshot for the symbol -- either
    it has not been refreshed yet, or TipRanks has no analyst-coverage layer
    for it at all (common for small/illiquid names) -- never an error.
    """
    from claudetrade.data.analyst import analyst_delta, latest_and_previous_snapshots

    symbol = symbol.strip().upper()
    latest, previous = latest_and_previous_snapshots(pipeline.db, [symbol])[symbol]
    if latest is None:
        return {
            "symbol": symbol,
            "available": False,
            "snapshot": None,
            "delta": None,
            "note": (
                f"No stored analyst-sentiment snapshot for {symbol}. Either this "
                "installation has not refreshed since this feature was added, or "
                "TipRanks has no analyst-coverage layer for this symbol at all "
                "(common for small/illiquid names) -- run `claudetrade refresh` "
                "(or the trigger_refresh tool) and check again."
            ),
        }

    delta = analyst_delta(latest, previous)
    return {
        "symbol": symbol,
        "available": True,
        "snapshot": _analyst_snapshot_payload(latest),
        "delta": {
            "previous_session": (
                delta.previous_session.isoformat() if delta.previous_session else None
            ),
            "has_previous": delta.has_previous,
            "buy_count_change": delta.buy_count_change,
            "hold_count_change": delta.hold_count_change,
            "sell_count_change": delta.sell_count_change,
            "coverage_change": delta.coverage_change,
            "consensus_rating_change": delta.consensus_rating_change,
            "price_target_mean_change": delta.price_target_mean_change,
            "price_target_mean_change_pct": delta.price_target_mean_change_pct,
            "new_rating_actions": [
                _analyst_rating_action_payload(a) for a in delta.new_rating_actions
            ],
        },
        "note": None,
    }


def _insider_transaction_month_payload(month) -> dict[str, Any]:
    return {
        "month": month.month,
        "year": month.year,
        "shares_bought": month.shares_bought,
        "insiders_buy_count": month.insiders_buy_count,
        "shares_sold": month.shares_sold,
        "insiders_sell_count": month.insiders_sell_count,
        "trans_buy_count": month.trans_buy_count,
        "trans_sell_count": month.trans_sell_count,
        "trans_buy_amount": month.trans_buy_amount,
        "trans_sell_amount": month.trans_sell_amount,
        "informative_buy_count": month.informative_buy_count,
        "informative_sell_count": month.informative_sell_count,
        "informative_buy_amount": month.informative_buy_amount,
        "informative_sell_amount": month.informative_sell_amount,
    }


def _insider_transaction_payload(txn) -> dict[str, Any]:
    return {
        "name": txn.name,
        "is_officer": txn.is_officer,
        "is_director": txn.is_director,
        "is_ten_percent_owner": txn.is_ten_percent_owner,
        "officer_title": txn.officer_title,
        "action": txn.action,
        "operation_description": txn.operation_description,
        "amount": txn.amount,
        "number_of_shares": txn.number_of_shares,
        "r_date": txn.r_date.isoformat() if txn.r_date else None,
        "estimated_shares_value": txn.estimated_shares_value,
        "link": txn.link,
    }


def _hedge_fund_holding_quarter_payload(quarter) -> dict[str, Any]:
    return {
        "date": quarter.date.isoformat(),
        "holding_amount": quarter.holding_amount,
        "institution_holding_percentage": quarter.institution_holding_percentage,
        "net_shares_change": quarter.net_shares_change,
        "number_of_shares_bought": quarter.number_of_shares_bought,
        "number_of_shares_sold": quarter.number_of_shares_sold,
        "is_complete": quarter.is_complete,
    }


def _hedge_fund_holder_move_payload(move) -> dict[str, Any]:
    return {
        "manager_name": move.manager_name,
        "institution_name": move.institution_name,
        "action": move.action,
        "effective_date": move.effective_date.isoformat() if move.effective_date else None,
        "value": move.value,
        "change_pct": move.change_pct,
        "change_amount": move.change_amount,
        "percentage_of_portfolio": move.percentage_of_portfolio,
        "stars": move.stars,
        "is_active": move.is_active,
    }


def _institutional_snapshot_payload(snapshot, score_result) -> dict[str, Any]:
    return {
        "as_of_session": snapshot.as_of_session.isoformat(),
        "insider_monthly": [
            _insider_transaction_month_payload(m) for m in snapshot.insider_monthly
        ],
        "insider_net_3m_usd": snapshot.insider_net_3m_usd,
        "insider_net_3m_usd_vendor": snapshot.insider_net_3m_usd_vendor,
        "insider_confidence_stock_score": snapshot.insider_confidence_stock_score,
        "insider_confidence_sector_score": snapshot.insider_confidence_sector_score,
        "insider_confidence_raw_score": snapshot.insider_confidence_raw_score,
        "num_of_insiders": snapshot.num_of_insiders,
        "recent_insider_transactions": [
            _insider_transaction_payload(t) for t in snapshot.recent_insider_transactions
        ],
        "hedge_fund_sentiment": snapshot.hedge_fund_sentiment,
        "hedge_fund_trend_action": snapshot.hedge_fund_trend_action,
        "hedge_fund_trend_value": snapshot.hedge_fund_trend_value,
        "hedge_fund_holdings_by_quarter": [
            _hedge_fund_holding_quarter_payload(h) for h in snapshot.hedge_fund_holdings_by_quarter
        ],
        "notable_holder_moves": [
            _hedge_fund_holder_move_payload(m) for m in snapshot.notable_holder_moves
        ],
        "market_cap_usd": snapshot.market_cap_usd,
        "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
        "score": score_result.score,
        "insider_subscore": score_result.insider_subscore,
        "insider_weight_applied": score_result.insider_weight_applied,
        "insider_age_days": score_result.insider_age_days,
        "hedge_fund_subscore": score_result.hedge_fund_subscore,
        "hedge_fund_weight_applied": score_result.hedge_fund_weight_applied,
        "hedge_fund_age_days": score_result.hedge_fund_age_days,
    }


def get_institutional_sentiment(pipeline: Pipeline, symbol: str) -> dict[str, Any]:
    """Read-only. TipRanks-sourced insider/hedge-fund ("institutional")
    sentiment snapshot for one symbol.

    Reads ``data.institutional.latest_and_previous_snapshots`` -- the same
    batched read helper the Streamlit ticker-detail screen's "Institutional
    sentiment" block uses -- for the latest stored ``institutional_snapshots``
    row plus the prior stored session, and ``data.institutional
    .institutional_delta`` for the comparison between them.
    ``providers.market.tipranks_institutional.institutional_score`` is
    recomputed here (pure, no I/O) rather than read back from the row's own
    stored ``score`` column, so the reported score always reflects the
    current scoring formula even against an older stored row. Nothing here
    makes a network call: this only reads what the last ``claudetrade
    refresh`` already harvested from TipRanks' ``dataForTicker`` response for
    this symbol (see ``providers.market.tipranks_institutional`` for the
    field mapping and the scoring formula's own documented weights/
    staleness half-lives).

    ``available=False`` (with ``snapshot``/``delta`` both ``None``) means
    this installation has never stored a snapshot for the symbol -- either
    it has not been refreshed yet, or TipRanks has no institutional content
    for it at all (common for small/illiquid names) -- never an error.

    **Not fed into ``signals.scoring.ComponentScores`` or any strategy** --
    see ``domain.InstitutionalSnapshot``'s own docstring. This is a
    read-only research overlay.
    """
    from claudetrade.data.institutional import institutional_delta, latest_and_previous_snapshots
    from claudetrade.providers.market.tipranks_institutional import institutional_score

    symbol = symbol.strip().upper()
    latest, previous = latest_and_previous_snapshots(pipeline.db, [symbol])[symbol]
    if latest is None:
        return {
            "symbol": symbol,
            "available": False,
            "snapshot": None,
            "delta": None,
            "note": (
                f"No stored institutional-sentiment snapshot for {symbol}. Either "
                "this installation has not refreshed since this feature was added, "
                "or TipRanks has no insider/hedge-fund content for this symbol at "
                "all (common for small/illiquid names) -- run `claudetrade refresh` "
                "(or the trigger_refresh tool) and check again."
            ),
        }

    latest_score = institutional_score(latest, latest.as_of_session)
    score_change: float | None = None
    if previous is not None:
        previous_score = institutional_score(previous, previous.as_of_session)
        if latest_score.score is not None and previous_score.score is not None:
            score_change = latest_score.score - previous_score.score

    delta = institutional_delta(latest, previous)
    return {
        "symbol": symbol,
        "available": True,
        "snapshot": _institutional_snapshot_payload(latest, latest_score),
        "delta": {
            "previous_session": (
                delta.previous_session.isoformat() if delta.previous_session else None
            ),
            "has_previous": delta.has_previous,
            "score_change": score_change,
            "net_flow_change": delta.net_flow_change,
            "hedge_fund_sentiment_change": delta.hedge_fund_sentiment_change,
            "new_holder_moves": [
                _hedge_fund_holder_move_payload(m) for m in delta.new_holder_moves
            ],
            "new_insider_transactions": [
                _insider_transaction_payload(t) for t in delta.new_insider_transactions
            ],
        },
        "note": None,
    }


def get_trending(pipeline: Pipeline, *, limit: int = 20, source: str = "auto") -> dict[str, Any]:
    """Read-only. Symbols ranked by recent mention volume.

    No existing screen aggregates mention volume *across* symbols (the
    Screener/dashboard rank signals, not raw mention counts), so this sums
    ``symbol_sentiment_daily.post_count`` -- the same column
    ``ui.data_access.sentiment_timeline``/the ticker-detail sentiment panel
    already reads -- over the last ``DEFAULT_TRENDING_WINDOW_DAYS`` days,
    grouped by symbol. A read-only aggregate query, not a new table or a
    write path.

    Args:
        source: Which stored rows to rank.

            * ``"auto"`` (default) prefers the ApeWisdom attention rows when
              this installation has any in the window, and falls back to the
              locally-derived ``"all"`` aggregate otherwise.
            * ``"all"`` forces the locally-derived aggregate (posts this
              application fetched and resolved itself).
            * ``"apewisdom"`` forces the aggregator rows.

            ``auto`` prefers ApeWisdom for two reasons, both of which QA hit
            directly: its rows arrive as tickers, so they cannot contain the
            common-word junk local extraction kept minting (AS/YOU/DAY --
            F25), and it counts entire communities rather than the narrow,
            rate-limited windows the local Reddit/X fetches see. The local
            aggregate remains the fallback and the only source with polarity
            -- ApeWisdom rows carry attention volume and nothing else, which
            is why ``average_bull_bear_ratio`` reads as ``None`` for them
            rather than a fabricated 1.0.
    """
    limit = max(1, min(int(limit), 200))
    end = utc_now().date()
    start = end - dt.timedelta(days=DEFAULT_TRENDING_WINDOW_DAYS)

    def _query(session, source_filter) -> list:
        return session.execute(
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
                source_filter,
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= end,
            )
            .group_by(SymbolSentimentDaily.symbol)
            .order_by(func.sum(SymbolSentimentDaily.post_count).desc())
            .limit(limit)
        ).all()

    #: Attention rows are stored one per community (``apewisdom:4chan``,
    #: ``apewisdom:all-stocks``), so ranking sums a symbol's mentions across
    #: every community that named it.
    apewisdom_filter = SymbolSentimentDaily.source.like("apewisdom:%")
    local_filter = SymbolSentimentDaily.source == "all"

    with pipeline.db.read_session() as session:
        if source == "apewisdom":
            rows, resolved = _query(session, apewisdom_filter), "apewisdom"
        elif source == "all":
            rows, resolved = _query(session, local_filter), "all"
        else:
            rows = _query(session, apewisdom_filter)
            resolved = "apewisdom"
            if not rows:
                rows, resolved = _query(session, local_filter), "all"

    # Attention rows have no polarity at all, so their bull/bear and
    # confidence columns sit at their neutral defaults. Reporting those as
    # numbers would read as "measured, and neutral" -- the exact
    # misinterpretation QA drew from the all-1.0 ratios in v2/v3. ``None``
    # says "not measured by this source", which is the truth.
    attention_only = resolved == "apewisdom"
    symbols = [
        {
            "symbol": row.symbol,
            "mentions": int(row.mentions or 0),
            "average_bull_bear_ratio": None
            if attention_only
            else round(float(row.avg_bull_bear_ratio or 0.0), 4),
            "average_confidence": None
            if attention_only
            else round(float(row.avg_confidence or 0.0), 4),
            "latest_session": row.latest_session.isoformat() if row.latest_session else None,
        }
        for row in rows
    ]
    return {
        "window_days": DEFAULT_TRENDING_WINDOW_DAYS,
        "count": len(symbols),
        "source": resolved,
        "note": (
            "ApeWisdom aggregate mention counts across Reddit and 4chan -- attention "
            "volume only, no sentiment direction (hence null bull/bear and confidence)."
            if attention_only
            else "Mention volume from posts this installation fetched and resolved itself."
        ),
        "symbols": symbols,
    }


def get_sentiment_history(
    pipeline: Pipeline, symbol: str, *, days: int = 90
) -> dict[str, Any]:
    """Read-only. One symbol's daily mention/sentiment series.

    Reads ``sentiment.history.symbol_series`` -- the same densified view the
    CLI's ``claudetrade sentiment history`` prints, not a second query. Gap
    -filled across trading sessions, so a caller can chart or difference the
    series directly; ``observed`` distinguishes a real zero ("nobody
    mentioned it") from an absent row.
    """
    from claudetrade.sentiment.history import symbol_series

    days = max(1, min(int(days), 365))
    window = symbol_series(
        pipeline.db, symbol, as_of=current_trading_session(), days=days
    )
    payload = window.to_dict()
    if window.total_mentions == 0:
        payload["note"] = (
            f"No stored mentions for {window.symbol} in the last {days} day(s). "
            "Sentiment history accumulates one session per refresh; run "
            "`claudetrade refresh` daily, and check get_market_status for a "
            "pending sentiment rebuild."
        )
    return payload


def get_rising_sentiment(
    pipeline: Pipeline,
    *,
    limit: int = 25,
    recent_sessions: int = 3,
    baseline_sessions: int = 20,
    min_recent_mentions: int = 5,
) -> dict[str, Any]:
    """Read-only. Symbols whose mentions are accelerating vs their own baseline.

    The question this application is built to answer -- "what is starting to
    get talked about?" -- as opposed to ``get_trending``, which ranks by
    absolute volume and therefore returns the same mega-caps every day. A
    quiet symbol waking up outranks a permanently-loud one here.

    Sentiment change rides along per row but is never ranked on: attention
    moves first, and a mention surge with deteriorating tone is a short
    setup rather than a row worth suppressing. ``coverage`` reports how much
    history actually backs the answer, so a warming-up database says so
    instead of reporting a confident "nothing is rising".
    """
    from claudetrade.sentiment.history import coverage_summary, rising_symbols

    as_of = current_trading_session()
    coverage = coverage_summary(pipeline.db, as_of=as_of, days=90)
    trends = rising_symbols(
        pipeline.db,
        as_of=as_of,
        recent_sessions=max(1, int(recent_sessions)),
        baseline_sessions=max(1, int(baseline_sessions)),
        limit=max(1, min(int(limit), 200)),
        min_recent_mentions=max(0, int(min_recent_mentions)),
    )
    result: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "recent_sessions": recent_sessions,
        "baseline_sessions": baseline_sessions,
        "coverage": coverage,
        "count": len(trends),
        "rising": [t.to_dict() for t in trends],
    }
    if coverage["sessions_with_data"] < baseline_sessions:
        result["warming_up"] = (
            f"Only {coverage['sessions_with_data']} session(s) of stored history back this "
            f"ranking, fewer than the {baseline_sessions}-session baseline it compares "
            "against. Treat these as provisional until history accumulates."
        )
    return result


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

    Also carries ``sentiment_readiness`` (``scheduler.collection_readiness``):
    how many sessions of social history this installation has actually
    accumulated, as a tier. Social history cannot be backfilled, so "the app
    has been running for a week" and "the app has a usable baseline" are
    genuinely different facts, and an assistant reading a rising-sentiment
    list needs the second one to weight it. It is a label, not a gate --
    nothing here or anywhere refuses to answer because of a tier.
    """
    from claudetrade.scheduler import collection_readiness

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
        "sentiment_rebuild_pending": _sentiment_rebuild_pending(pipeline),
        "sentiment_readiness": collection_readiness(pipeline.db, pipeline.config),
    }


def _sentiment_rebuild_pending(pipeline: Pipeline) -> dict[str, Any] | None:
    """Whether stored sentiment predates the current extractor, or ``None``.

    ``run_stdio`` deliberately declines to run the stored-sentiment self-heal
    at start-up (a minute-scale rebuild inside the MCP initialize handshake
    would risk the client giving up before the server ever answers -- see
    that function). Declining silently would be worse than the delay: the
    trending list would keep serving the very junk the rebuild exists to
    clear, with nothing anywhere saying why. So the deferral is reported on
    the status tool QA already reaches for first.

    Read-only and cheap: one indexed point read on ``settings_kv``.
    """
    try:
        from claudetrade.sentiment.entity_resolution import EXTRACTION_VERSION
        from claudetrade.sentiment.rebuild import stored_extraction_version

        stored = stored_extraction_version(pipeline.db)
    except Exception:  # pragma: no cover - diagnostics must never break status
        log.debug("sentiment extraction-version probe failed", exc_info=True)
        return None
    if stored >= EXTRACTION_VERSION:
        return None
    return {
        "stored_extraction_version": stored,
        "current_extraction_version": EXTRACTION_VERSION,
        "note": (
            "Stored sentiment aggregates were built by an older extractor, so trending "
            "and sentiment reads may still show common-word tickers and neutral "
            "bull/bear ratios. This server does not rebuild them at start-up (it would "
            "delay the MCP handshake); run `claudetrade db rebuild-sentiment`, or any "
            "CLI/UI command, to heal them."
        ),
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
    # The ET trading calendar, not the UTC date: a Friday-evening call is
    # Friday's session, never the (nonexistent) Saturday one -- see
    # ``timeutils.current_trading_session``.
    session_date = current_trading_session()
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

    Two layers of single-flight, in order:

    * The in-process ``RefreshState`` (the same mechanism
      ``POST /api/system/refresh`` uses) refuses a second refresh started
      from THIS server -- fine-grained live progress stays here.
    * The cross-process lock in ``db.refresh_state_store`` (QA handoff v3,
      F27) refuses when ANY entry point -- the CLI, the web API, another MCP
      server -- holds a running refresh with a fresh heartbeat, naming the
      holder, its start time and its progress; a stale holder (dead process)
      is taken over rather than blocking forever. The database row, not any
      process's memory, is the cross-process truth.
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
            "status": get_refresh_status(pipeline, state),
        }

    outcome = refresh_state_store.try_acquire(pipeline.db, "mcp")
    if not outcome.acquired:
        # Roll the eager local claim back so a later trigger (once the other
        # entry point finishes) starts cleanly rather than 409ing on
        # this process's own leftover state.
        with state.lock:
            state.running = False
            state.phase = "idle"
        holder = outcome.holder
        return {
            "started": False,
            "reason": holder.describe()
            if holder
            else "another process holds the refresh lock",
            "status": get_refresh_status(pipeline, state),
        }
    handle = outcome.handle
    progress = _compose_progress(state.update_progress, handle.update_progress)

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
                progress_callback=progress,
            )
        except Exception as exc:  # matches webapi.routers.system's own catch-all
            with state.lock:
                state.last_error = str(exc)
            handle.finish("failed", error=str(exc))
            log.exception("MCP-triggered background refresh failed")
        else:
            handle.finish("done")
        finally:
            with state.lock:
                state.running = False
                state.phase = "idle"
                state.finished_at = utc_now()

    threading.Thread(target=_run, name="claudetrade-mcp-refresh", daemon=True).start()
    return {"started": True, "status": state.snapshot()}


def _compose_progress(
    *callbacks: Callable[[str, int, int], None],
) -> Callable[[str, int, int], None]:
    """Fan one ``(phase, done, total)`` progress stream out to several sinks.

    Each sink is isolated: the in-process ``RefreshState`` update must land
    even if the database heartbeat write hiccups, and vice versa.
    """

    def _fanout(phase: str, done: int, total: int) -> None:
        for callback in callbacks:
            try:
                callback(phase, done, total)
            except Exception:
                log.debug("refresh progress sink raised; ignored", exc_info=True)

    return _fanout


def get_refresh_status(pipeline: Pipeline, state: RefreshState) -> dict[str, Any]:
    """Read-only. Refresh progress, merged across every entry point.

    The QA acceptance check for F27: a refresh started by the CLI (a
    different process -- this server's in-process ``RefreshState`` knows
    nothing about it) must be visible here. The DB row is the cross-process
    truth; the in-process snapshot supplies the finer-grained detail when
    the running refresh is this server's own.

    ``scheduled`` decodes ``entry_point`` for the one case a caller cannot
    infer from context: the web API server's hourly collector
    (``claudetrade.scheduler``) starts collections on its own, so "a refresh
    is running" here does not imply anybody asked for one.
    """
    from claudetrade.scheduler import is_scheduled_run

    payload = refresh_state_store.merged_status(pipeline.db, state.snapshot(), "mcp")
    payload["scheduled"] = is_scheduled_run(payload)
    return payload


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
# Adanos hybrid mode -- on-demand official-API calls
#
# See providers.social.adanos's module docstring (Hybrid mode section) and
# config.AdanosConfig for the full picture: bulk trending collection
# (get_trending) never spends the official-API budget -- it stays keyless
# (site mode) unless prefer_official_api is set. These three tools are the
# SEPARATE, on-demand half: whenever an adanos_api_key credential resolves
# at all, get_adanos_detail/get_adanos_explain spend one metered request
# each (of the ~250/month free tier) for a single ticker's detail/AI
# explanation, always budget-guarded. get_adanos_budget is free.
# --------------------------------------------------------------------------


def _adanos_provider(pipeline: Pipeline) -> AdanosProvider | None:
    """The configured ``AdanosProvider`` instance, or ``None`` when Adanos
    is disabled or has no feeds enabled on this installation.
    ``pipeline.adanos`` is always a 0-or-1-element list (see
    ``providers.registry.get_adanos_providers`` -- one instance covers all
    four feeds)."""
    return pipeline.adanos[0] if pipeline.adanos else None


def get_adanos_budget(pipeline: Pipeline) -> dict[str, Any]:
    """Read-only, FREE -- never spends the official-API quota.

    Current Adanos on-demand official-API budget state (used/remaining/
    reserve/month/resets_hint) and whether an ``adanos_api_key`` credential
    resolves on this installation at all -- see ``AdanosProvider
    .budget_status``. Bulk trending collection never spends this budget
    either way (it stays keyless unless ``prefer_official_api`` is set);
    only ``get_adanos_detail``/``get_adanos_explain`` do.
    """
    provider = _adanos_provider(pipeline)
    if provider is None:
        return {
            "configured": False,
            "key_resolved": False,
            "note": (
                "Adanos is disabled or has no feeds enabled on this installation -- "
                "on-demand detail/explain calls have nothing to run against."
            ),
        }
    return {"configured": True, **provider.budget_status()}


def get_adanos_detail(pipeline: Pipeline, symbol: str, *, platform: str = "x") -> dict[str, Any]:
    """SPENDS 1 official-API request (unless served from the same-session
    top-candidate enrichment cache -- see below). One ticker's Adanos
    detail via ``AdanosProvider.fetch_stock_detail``: daily trend,
    sentiment breakdown, top mentions/authors, passed through from the
    vendor without inventing schema, plus a normalized header block.

    Checks ``AdanosProvider.cached_detail`` first: if this installation's
    most recent scan already enriched ``symbol`` on ``platform`` THIS
    trading session (``config.AdanosConfig.enrich_top_candidates``), that
    cached result is returned with ``from_cache: true`` and NO quota is
    spent. Otherwise makes a fresh on-demand call, which does spend one
    request. Refuses with ``accepted: false`` (never raises) when Adanos is
    not configured, no key resolves, or the monthly budget is at its
    reserve floor; a genuine HTTP failure (401/403/429/404/5xx) is also
    caught and reported the same structured way rather than propagating as
    a bare exception. ``budget`` (used/remaining/reserve/month) is included
    in every response, success or refusal.
    """
    symbol = symbol.strip().upper()
    provider = _adanos_provider(pipeline)
    if provider is None:
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": "Adanos is disabled or has no feeds enabled on this installation",
            "budget": None,
        }

    cached = provider.cached_detail(symbol, current_trading_session(), platform=platform)
    if cached is not None:
        payload = dict(cached)
        payload["from_cache"] = True
        payload["note"] = "cached from enrichment, no quota spent"
        payload["budget"] = provider.budget_status()
        return payload

    try:
        result = provider.fetch_stock_detail(symbol, platform=platform)
    except ProviderError as exc:
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": str(exc),
            "budget": provider.budget_status(),
        }
    result.setdefault("from_cache", False)
    return result


def get_adanos_explain(pipeline: Pipeline, symbol: str, *, platform: str = "x") -> dict[str, Any]:
    """SPENDS 1 official-API request of the ~250/month free tier, every
    call -- unlike ``get_adanos_detail`` there is no enrichment cache for
    this one. Adanos's own AI trend explanation
    (``AdanosProvider.fetch_explain``, llama-3.1-8b) for one ticker. The
    vendor itself caches the explanation text for 6h server-side: a repeat
    call within that window still spends this installation's request here
    (the vendor still counts it), but returns the same text -- ``cached``
    and ``generated_at`` in the response say whether this particular answer
    was freshly generated or served from the vendor's own cache. Refuses
    with ``accepted: false`` (never raises) when Adanos is not configured,
    no key resolves, or the monthly budget is at its reserve floor; other
    HTTP failures are caught the same structured way. ``budget`` is
    included in every response, success or refusal.
    """
    symbol = symbol.strip().upper()
    provider = _adanos_provider(pipeline)
    if provider is None:
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": "Adanos is disabled or has no feeds enabled on this installation",
            "budget": None,
        }
    try:
        return provider.fetch_explain(symbol, platform=platform)
    except ProviderError as exc:
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": str(exc),
            "budget": provider.budget_status(),
        }


# --------------------------------------------------------------------------
# Research revisions
# --------------------------------------------------------------------------


def submit_research_revision(
    pipeline: Pipeline,
    config: AppConfig,
    signal_id: str,
    thesis: str | None,
    invalidation: list[str] | None,
    score_adjustments: dict[str, float] | None,
    rationale: str,
    sources: list[str],
) -> dict[str, Any]:
    """WRITE -- appends an MCP client's web research to the research ledger.

    The intended flow: an MCP client (Claude Desktop) does web research on a
    signal ``get_signals`` returned, then calls this to record an updated
    thesis, updated invalidation conditions and/or small score nudges as a
    new, append-only ``SignalResearchRevisionRow``. ``SignalRow`` itself is
    NEVER touched -- this is a new row, not an edit -- and entry, stop,
    targets and position size (``TradePlan``) are engine-owned and
    structurally unreachable here: this function's signature has no
    plan/price/size/direction parameter at all, on purpose. Research can
    re-rank a signal; it cannot re-price or re-size one.

    Guardrails (``signals.research.ResearchLedger.append_research_revision``):
    ``thesis``/each ``invalidation`` item is checked against the same
    rewrite guardrail the AI thesis-polish path uses -- no unrecognised
    decimal price level beyond the signal's own plan, no directive phrase
    ("widen the stop", "increase the position", ...), plausible length.
    ``score_adjustments`` component names must be real ``ComponentScores``
    fields and every delta is clamped to +/- ``McpConfig
    .max_component_adjustment`` before it is stored. ``rationale`` and
    ``sources`` are required and non-empty.

    Revision semantics: the NEWEST revision is what takes effect, and a
    ``None``/omitted field means "unchanged" -- it is carried forward from
    the previous revision, so updating only the thesis never silently drops
    earlier score adjustments. Pass an explicit empty dict ``{}`` for
    ``score_adjustments`` to deliberately clear them; to walk back a thesis
    or invalidation rewrite, resubmit the engine's original text.

    Refuses outright, without writing anything, when
    ``config.mcp.research_writes_enabled`` is false. A guardrail rejection
    returns ``{"accepted": false, "reason": ...}`` rather than raising --
    this is an expected, common outcome (an AI proposing a bad rewrite), not
    a system error.
    """
    if not config.mcp.research_writes_enabled:
        return {
            "accepted": False,
            "reason": (
                "research writes are disabled on this installation "
                "(config.mcp.research_writes_enabled is false)"
            ),
        }

    ledger = ResearchLedger(pipeline.db)
    try:
        result = ledger.append_research_revision(
            signal_id,
            thesis=thesis,
            invalidation=invalidation,
            score_adjustments=score_adjustments,
            rationale=rationale,
            sources=sources,
            config=config,
            actor="mcp",
        )
    except ResearchGuardrailError as exc:
        return {"accepted": False, "reason": str(exc)}

    return {
        "disclaimer": DISCLAIMER,
        "accepted": True,
        "signal_id": result.signal_id,
        "revision": result.revision,
        "original_score": result.original_score,
        "effective_score": result.effective_score,
        "clamped": result.clamped,
    }


def get_research_revisions(pipeline: Pipeline, signal_id: str) -> dict[str, Any]:
    """Read-only. Full research-revision history for one signal, oldest first.

    Every accepted call to ``submit_research_revision`` for this signal, in
    the order they were recorded -- the audit trail behind whatever
    ``effective_score``/``has_research`` ``get_signals`` is currently
    reporting for it.
    """
    history = ResearchLedger(pipeline.db).research_history(signal_id)
    revisions = [
        {**entry, "created_at": entry["created_at"].isoformat()} for entry in history
    ]
    return {
        "signal_id": signal_id,
        "count": len(revisions),
        "revisions": revisions,
    }


# --------------------------------------------------------------------------
# Server wiring
# --------------------------------------------------------------------------

INSTRUCTIONS = (
    "Local, read-mostly access to one owner's ClaudeTrade research database "
    "(swing-trading sentiment/technical signals). " + DISCLAIMER
)

#: Distinguishes "the tool timed out" from "the tool legitimately returned
#: None/falsy" inside :func:`_call_bounded` -- a sentinel, never surfaced.
_TIMED_OUT = object()


def _timeout_payload(tool_name: str, timeout_s: float) -> dict[str, Any]:
    """The structured error a client sees instead of a hang (F26)."""
    return {
        "error": f"{tool_name} did not complete within {timeout_s:.0f}s",
        "timed_out": True,
        "hint": "a data refresh may be holding the database; retry shortly",
    }


async def _call_bounded(tool_name: str, timeout_s: float, fn: Callable[[], Any]) -> Any:
    """Run one sync tool body on a worker thread with a hard deadline.

    This is the per-tool watchdog fixing F26's two failure modes at once:

    * The body runs via ``anyio.to_thread.run_sync``, so the event loop --
      and with it the transport's message reader -- keeps serving other
      calls while a tool is busy. (FastMCP itself would have called a sync
      tool directly ON the loop thread; see the module docstring.)
    * ``move_on_after`` bounds the wait. On expiry the client receives a
      structured ``timed_out`` payload rather than silence.

    Trade-off, deliberate: ``abandon_on_cancel=True`` means a timed-out
    body's worker thread is ABANDONED, not killed -- Python has no safe way
    to kill a thread mid-SQLite-call. The abandoned thread finishes on its
    own (each individual query is bounded by the connection's busy_timeout,
    so it drains rather than leaking forever) and its result is discarded.
    Until it drains it holds one worker-thread slot and possibly one pooled
    database connection; a pathological burst of timed-out calls therefore
    degrades throughput before it degrades correctness -- responses stay
    bounded either way, which is the property QA's acceptance check names.

    ``anyio`` is imported here, not at module top: it arrives with the
    optional ``mcp`` extra, and importing *this module* must stay dependency-
    free (see ``_require_fastmcp``).
    """
    import anyio

    result: Any = _TIMED_OUT
    with anyio.move_on_after(timeout_s):
        result = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
    if result is _TIMED_OUT:
        log.warning(
            "MCP tool %s exceeded its %.0fs deadline; returning a timed_out payload "
            "(its worker thread is left to drain in the background)",
            tool_name, timeout_s,
        )
        return _timeout_payload(tool_name, timeout_s)
    return result


def build_server(pipeline: Pipeline, config: AppConfig) -> FastMCP:
    """Construct the FastMCP server and register every tool against ``pipeline``.

    Separated from :func:`run_stdio` so tests can build a server (and
    introspect its registered tools) without ever starting the stdio
    transport loop.

    Every closure below is ``async`` and delegates its sync body to
    :func:`_call_bounded` -- the per-tool watchdog (F26). Deadlines are read
    from ``config.mcp`` at CALL time, not captured here, so a runtime config
    tweak applies without a server restart (matching how ``/api/system``'s
    ai-config mutation behaves).
    """
    FastMCP = _require_fastmcp()
    server = FastMCP(name="claudetrade", instructions=INSTRUCTIONS)
    refresh_state = RefreshState()

    @server.tool(
        name="get_signals",
        description=(
            "Read-only. Current signals/recommendations from the immutable ledger, "
            "BEST-SCORING FIRST by default, so limit=N means the N best candidates "
            "and matches what the web Screener shows. Returns symbol, strategy, "
            "direction, score, confidence, entry/stop/targets and days_to_earnings, "
            "plus total_matching and truncated so you can tell a complete answer "
            "from a page. sort='created_at' gives newest-first instead, for audit or "
            "ledger inspection. distinct=True (default) collapses read-time "
            "duplicates -- same-session re-scans after a code/config change, and "
            "cross-strategy overlap on one symbol+direction -- into one row per "
            "recommendation, with corroborating_strategies (the other strategies "
            "that agree) and duplicates_collapsed on each row; pass distinct=False "
            "for the raw, uncollapsed per-strategy rows. Includes the standing "
            "research-only disclaimer once, not per row. When there are no matching "
            "signals, includes a why_no_signals block: the rejection funnel (reasons "
            "and counts) and closest near-misses from the most recent scan on this "
            "installation, so 'why no picks today?' has a real answer."
        ),
    )
    async def _get_signals(
        min_score: float = 0.0, limit: int = 20, sort: str = "score", distinct: bool = True
    ) -> dict[str, Any]:
        return await _call_bounded(
            "get_signals",
            config.mcp.tool_timeout_seconds,
            lambda: get_signals(
                pipeline, config, min_score=min_score, limit=limit, sort=sort, distinct=distinct
            ),
        )

    @server.tool(
        name="get_sentiment",
        description=(
            "Read-only. Recent daily sentiment for one symbol: post counts, unique "
            "authors, engagement, bull/bear ratio, manipulation risk and confidence, "
            "one row per trading day over the last N days."
        ),
    )
    async def _get_sentiment(symbol: str, days: int = 7) -> dict[str, Any]:
        return await _call_bounded(
            "get_sentiment",
            config.mcp.tool_timeout_seconds,
            lambda: get_sentiment(pipeline, symbol, days=days),
        )

    @server.tool(
        name="get_analyst_sentiment",
        description=(
            "Read-only. TipRanks-sourced analyst-consensus snapshot for one symbol: "
            "consensus rating, ranked Buy/Hold/Sell counts and analyst count, price-"
            "target mean/high/low, a bounded consensus-over-time series, recent "
            "individual analyst rating actions (firm, analyst, rating, best-effort "
            "action label, new/old price target, analyst stars/success rate), and "
            "the last earnings surprise / next earnings EPS estimate. Also reports "
            "the delta against the previous stored session (count/coverage/rating/"
            "price-target changes, plus rating actions dated after that prior "
            "session). Makes no network call -- reads only what the last "
            "`claudetrade refresh` already stored; available=false (never an error) "
            "means no snapshot has been stored yet, or TipRanks has no analyst "
            "coverage for this symbol at all."
        ),
    )
    async def _get_analyst_sentiment(symbol: str) -> dict[str, Any]:
        return await _call_bounded(
            "get_analyst_sentiment",
            config.mcp.tool_timeout_seconds,
            lambda: get_analyst_sentiment(pipeline, symbol),
        )

    @server.tool(
        name="get_institutional_sentiment",
        description=(
            "Read-only. TipRanks-sourced insider/hedge-fund ('institutional') "
            "sentiment snapshot for one symbol: monthly insider buy/sell "
            "transaction aggregates, a derived trailing-3-month net insider $ "
            "flow (market-cap-normalized), the vendor's insider confidence "
            "signal, recent individual insider transactions (role flags, SEC "
            "link), hedge-fund sentiment/trend, quarterly institutional-holdings "
            "history, and notable holder moves. Includes a blended [-1, +1] "
            "score with per-axis subscores, applied weights and staleness ages "
            "(see the tool's snapshot payload). Also reports the delta against "
            "the previous stored session (score/net-flow/hedge-fund-sentiment "
            "changes, plus holder moves and insider transactions dated after "
            "that prior session). Makes no network call -- reads only what the "
            "last `claudetrade refresh` already stored; available=false (never "
            "an error) means no snapshot has been stored yet, or TipRanks has "
            "no institutional content for this symbol at all. Research signal "
            "only, not fed into any scan/backtest strategy."
        ),
    )
    async def _get_institutional_sentiment(symbol: str) -> dict[str, Any]:
        return await _call_bounded(
            "get_institutional_sentiment",
            config.mcp.tool_timeout_seconds,
            lambda: get_institutional_sentiment(pipeline, symbol),
        )

    @server.tool(
        name="get_trending",
        description=(
            "Read-only. Symbols ranked by recent social mention volume "
            f"(last {DEFAULT_TRENDING_WINDOW_DAYS} days of stored daily sentiment "
            "aggregates), most-mentioned first. source='auto' (default) uses "
            "ApeWisdom's Reddit/4chan mention counts when available -- broader "
            "coverage, and pre-resolved tickers so ordinary English words can "
            "never appear -- falling back to locally-resolved posts; 'all' forces "
            "the local aggregate, 'apewisdom' forces the aggregator. ApeWisdom "
            "rows carry attention volume only, so their bull/bear ratio and "
            "confidence are null rather than a fabricated neutral value."
        ),
    )
    async def _get_trending(limit: int = 20, source: str = "auto") -> dict[str, Any]:
        return await _call_bounded(
            "get_trending",
            config.mcp.tool_timeout_seconds,
            lambda: get_trending(pipeline, limit=limit, source=source),
        )

    @server.tool(
        name="get_rising_sentiment",
        description=(
            "Read-only. THE screen for 'what should I look at?': symbols whose "
            "mention rate is accelerating against their OWN recent baseline, so a "
            "quiet name waking up ranks above a permanently-loud mega-cap (which "
            "is all get_trending's absolute-volume ranking can ever show you). "
            "Each row carries the mention change, the recent vs baseline rate, and "
            "the sentiment change where polarity was actually measured -- tone is "
            "reported but never ranked on, since a mention surge with collapsing "
            "sentiment is a short setup, not noise. Includes a coverage block "
            "stating how much stored history backs the ranking."
        ),
    )
    async def _get_rising_sentiment(
        limit: int = 25,
        recent_sessions: int = 3,
        baseline_sessions: int = 20,
        min_recent_mentions: int = 5,
    ) -> dict[str, Any]:
        return await _call_bounded(
            "get_rising_sentiment",
            config.mcp.tool_timeout_seconds,
            lambda: get_rising_sentiment(
                pipeline,
                limit=limit,
                recent_sessions=recent_sessions,
                baseline_sessions=baseline_sessions,
                min_recent_mentions=min_recent_mentions,
            ),
        )

    @server.tool(
        name="get_sentiment_history",
        description=(
            "Read-only. One symbol's daily mention and sentiment series over the "
            "last N days, gap-filled across trading sessions so it can be charted "
            "or differenced directly. Each point carries locally-resolved mentions, "
            "ApeWisdom attention mentions, their total, sentiment, bull/bear ratio "
            "and confidence; 'observed' marks whether a row was actually stored, "
            "distinguishing a real zero from absent data."
        ),
    )
    async def _get_sentiment_history(symbol: str, days: int = 90) -> dict[str, Any]:
        return await _call_bounded(
            "get_sentiment_history",
            config.mcp.tool_timeout_seconds,
            lambda: get_sentiment_history(pipeline, symbol, days=days),
        )

    @server.tool(
        name="get_market_status",
        description=(
            "Read-only. Market regime, current Eastern Time, and whether the market "
            "is pre-market/open/after-hours/closed right now; last data-refresh time, "
            "how many symbols have stored data, and provider health/degradations. "
            "The tool to check before asking about 'this morning's' sentiment. Also "
            "reports sentiment_readiness: how many sessions of social history this "
            "installation has actually accumulated, as a tier (warming_up < 20, "
            "provisional 20+, partial 60+, ready 120+), plus which social sources are "
            "currently degraded. Social history cannot be backfilled, so use the tier "
            "to weight any mention/sentiment trend -- it never blocks an answer."
        ),
    )
    async def _get_market_status() -> dict[str, Any]:
        return await _call_bounded(
            "get_market_status",
            config.mcp.tool_timeout_seconds,
            lambda: get_market_status(pipeline),
        )

    @server.tool(
        name="run_scan",
        description=(
            "WRITE -- records new signals to the immutable ledger. Runs a full scan "
            "for today's session (identical to `claudetrade scan`) and returns "
            "summary counts only; call get_signals afterwards for the candidates. "
            "If it reports timed_out, the scan keeps running in the background -- "
            "check get_signals again shortly rather than re-running it."
        ),
    )
    async def _run_scan() -> dict[str, Any]:
        # The one legitimately-slow tool gets its own, larger deadline; an
        # abandoned (timed-out) scan still runs to completion on its worker
        # thread and its signals land in the ledger as usual.
        return await _call_bounded(
            "run_scan",
            config.mcp.scan_timeout_seconds,
            lambda: run_scan(pipeline),
        )

    @server.tool(
        name="trigger_refresh",
        description=(
            "WRITE, runs in the background -- pulls fresh market data, earnings and "
            "social sentiment from every configured provider and stores it; can take "
            "several minutes on a large universe. Returns immediately; poll "
            "get_refresh_status for progress. Refuses to start a second refresh while "
            "one is already running from ANY entry point (CLI, web UI, or MCP), "
            "naming the current holder."
        ),
    )
    async def _trigger_refresh() -> dict[str, Any]:
        return await _call_bounded(
            "trigger_refresh",
            config.mcp.tool_timeout_seconds,
            lambda: trigger_refresh(pipeline, config, refresh_state),
        )

    @server.tool(
        name="get_refresh_status",
        description=(
            "Read-only. Progress of the current data refresh or automatic social "
            "collection, whichever entry point started it (CLI, web UI, this server, "
            "or the web server's hourly collector) -- entry_point names the owner and "
            "scheduled=true means nobody asked for it, the app collects social data "
            "on its own schedule because that data cannot be backfilled later."
        ),
    )
    async def _get_refresh_status() -> dict[str, Any]:
        return await _call_bounded(
            "get_refresh_status",
            config.mcp.tool_timeout_seconds,
            lambda: get_refresh_status(pipeline, refresh_state),
        )

    @server.tool(
        name="submit_research_revision",
        description=(
            "WRITE -- appends web research to a signal's append-only research "
            "ledger; never edits the original signal. Submit an updated thesis "
            "and/or invalidation list and/or small score_adjustments (component "
            "name -> signed delta, each clamped to "
            f"+/-{config.mcp.max_component_adjustment:.0f} points) after doing "
            "web research on a signal from get_signals. rationale (why) and "
            "sources (list of URLs/citations) are required. Entry, stop, "
            "targets and position size are ENGINE-OWNED and cannot be "
            "submitted here at all -- there is no field for them; research "
            "can re-rank a signal, never re-price or re-size one. Thesis and "
            "invalidation text are guardrailed against introducing an "
            "unrecognised price level or a directive phrase (e.g. 'widen the "
            "stop'). The newest revision takes effect: an omitted field is "
            "carried forward unchanged from the previous revision, and an "
            "explicit empty score_adjustments dict clears the adjustments. "
            "Returns accepted=false with a reason on any rejection "
            "(including when research writes are disabled for this "
            "installation) rather than raising. " + DISCLAIMER
        ),
    )
    async def _submit_research_revision(
        signal_id: str,
        thesis: str | None = None,
        invalidation: list[str] | None = None,
        score_adjustments: dict[str, float] | None = None,
        rationale: str = "",
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        return await _call_bounded(
            "submit_research_revision",
            config.mcp.tool_timeout_seconds,
            lambda: submit_research_revision(
                pipeline,
                config,
                signal_id,
                thesis,
                invalidation,
                score_adjustments,
                rationale,
                sources or [],
            ),
        )

    @server.tool(
        name="get_research_revisions",
        description=(
            "Read-only. Full research-revision history for one signal "
            "(everything previously submitted via submit_research_revision), "
            "oldest first -- the audit trail behind its current "
            "effective_score/has_research."
        ),
    )
    async def _get_research_revisions(signal_id: str) -> dict[str, Any]:
        return await _call_bounded(
            "get_research_revisions",
            config.mcp.tool_timeout_seconds,
            lambda: get_research_revisions(pipeline, signal_id),
        )

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
    async def _get_backtest_report() -> dict[str, Any]:
        return await _call_bounded(
            "get_backtest_report",
            config.mcp.tool_timeout_seconds,
            lambda: get_backtest_report(config),
        )

    @server.tool(
        name="get_adanos_budget",
        description=(
            "Read-only, FREE -- never spends the official-API quota. Current Adanos "
            "on-demand official-API budget state (used/remaining/reserve/month/"
            "resets_hint) and whether an adanos_api_key credential resolves on this "
            "installation at all. Bulk trending collection (get_trending) never spends "
            "this budget -- it always prefers Adanos's keyless site endpoints for the "
            "same trending data; only get_adanos_detail/get_adanos_explain spend it."
        ),
    )
    async def _get_adanos_budget() -> dict[str, Any]:
        return await _call_bounded(
            "get_adanos_budget",
            config.mcp.tool_timeout_seconds,
            lambda: get_adanos_budget(pipeline),
        )

    @server.tool(
        name="get_adanos_detail",
        description=(
            "SPENDS 1 official-API request of the ~250/month free tier -- UNLESS this "
            "ticker was already enriched by this session's scan (top-candidate "
            "enrichment), in which case the cached result is returned free with "
            "from_cache=true. One ticker's Adanos detail: daily trend, sentiment "
            "breakdown, top mentions/authors, passed through from the vendor without "
            "inventing schema, plus a normalized buzz_score/sentiment_score/"
            "bullish_pct/bearish_pct/mentions header. Refuses with accepted=false "
            "(never raises) when no adanos_api_key credential resolves on this "
            "installation, or when the monthly budget is down to its reserve floor -- "
            "the reason names the reset date. Remaining budget is included in every "
            "response, success or refusal, under the budget key."
        ),
    )
    async def _get_adanos_detail(symbol: str, platform: str = "x") -> dict[str, Any]:
        return await _call_bounded(
            "get_adanos_detail",
            config.mcp.tool_timeout_seconds,
            lambda: get_adanos_detail(pipeline, symbol, platform=platform),
        )

    @server.tool(
        name="get_adanos_explain",
        description=(
            "SPENDS 1 official-API request of the ~250/month free tier, on every call "
            "(there is no enrichment cache for this one, unlike get_adanos_detail). "
            "Adanos's own AI-generated trend explanation for one ticker "
            "(llama-3.1-8b). The vendor caches the explanation text for 6h "
            "server-side, so a repeat call within that window still spends a request "
            "here but returns the same cached text -- cached and generated_at in the "
            "response say which. Refuses with accepted=false (never raises) when no "
            "adanos_api_key credential resolves, or when the monthly budget is down "
            "to its reserve floor. Remaining budget is included in every response."
        ),
    )
    async def _get_adanos_explain(symbol: str, platform: str = "x") -> dict[str, Any]:
        return await _call_bounded(
            "get_adanos_explain",
            config.mcp.tool_timeout_seconds,
            lambda: get_adanos_explain(pipeline, symbol, platform=platform),
        )

    return server


def run_stdio(config: AppConfig) -> None:
    """Bootstrap the pipeline and serve every tool over MCP stdio (blocking).

    ``Pipeline.bootstrap`` opens the database, applies migrations and wires
    the configured providers -- the same call ``claudetrade refresh``/``scan``
    make -- so this works on a fresh install with no other entry point ever
    having run first.

    ``allow_data_fixes=False`` is the one deliberate difference from those
    other entry points. Migrations still run (they are fast and the schema
    must match the code), but the stored-sentiment self-heal
    (``sentiment.rebuild.ensure_extraction_version``) is left to a CLI or UI
    bootstrap: it rebuilds aggregates from every stored post, and a
    minute-scale job here runs *before* ``server.run()`` accepts the first
    message -- i.e. inside the client's initialize handshake, which is
    exactly the window an MCP client (Claude Desktop launching this as a
    subprocess) will time out. Bounding tool calls (see this module's
    docstring) would not have helped: this happens before any tool exists.
    ``get_market_status`` reports the pending heal so the deferral is
    visible rather than silent.
    """
    pipeline = Pipeline.bootstrap(config, allow_data_fixes=False)
    server = build_server(pipeline, config)
    server.run(transport="stdio")


__all__ = [
    "build_server",
    "get_adanos_budget",
    "get_adanos_detail",
    "get_adanos_explain",
    "get_backtest_report",
    "get_market_status",
    "get_refresh_status",
    "get_research_revisions",
    "get_rising_sentiment",
    "get_sentiment",
    "get_sentiment_history",
    "get_signals",
    "get_trending",
    "run_scan",
    "run_stdio",
    "submit_research_revision",
    "trigger_refresh",
]
