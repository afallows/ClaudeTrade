"""Signal listing, detail, near-miss rejections, and scan/refresh actions.

Every read here goes through ``pipeline.ledger`` (the same append-only,
integrity-checked store the CLI and Streamlit UI read); every action
(``POST /api/scan``, ``POST /api/refresh``) calls the same ``Pipeline``
methods ``claudetrade scan``/``claudetrade refresh`` call. Nothing in this
module recomputes a score, a filter rule, or a fill decision itself.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from claudetrade.pipeline import Pipeline
from claudetrade.signals.dedupe import collapse_recommendations
from claudetrade.signals.research import ResearchLedger
from claudetrade.signals.scoring import adjusted_overall
from claudetrade.utils.timeutils import current_trading_session, utc_now
from claudetrade.webapi.attention import latest_attention
from claudetrade.webapi.deps import get_config, get_last_scan, get_pipeline, set_last_scan
from claudetrade.webapi.schemas import (
    NearMissOut,
    RefreshRequest,
    RefreshResponse,
    RejectedCandidateOut,
    RejectedResponse,
    ScanFunnelOut,
    ScanRequest,
    ScanResponse,
    SignalDetailOut,
    SignalListOut,
)
from claudetrade.webapi.serialize import signal_to_detail, signal_to_row

router = APIRouter(prefix="/api", tags=["signals"])


@router.get("/signals", response_model=SignalListOut)
def list_signals(
    pipeline: Pipeline = Depends(get_pipeline),
    config=Depends(get_config),
    direction: list[str] | None = Query(default=None, description="long, short, or both"),
    min_score: float = Query(default=0.0, ge=0.0, le=100.0),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    strategy: list[str] | None = Query(default=None),
    max_days_to_earnings: int | None = Query(default=None, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
    distinct: bool = Query(
        default=True,
        description=(
            "Collapse read-time duplicates (same-session re-scans after a code/config "
            "change, and cross-strategy overlap on one symbol+direction) into one row "
            "per recommendation. False returns the raw, uncollapsed per-strategy rows."
        ),
    ),
) -> SignalListOut:
    """The candidate universe for the Screener grid, most-recent-first.

    Filters mirror the Streamlit Scanner's ``apply_candidate_filters``
    exactly: minimum score, minimum confidence, direction set, strategy set,
    and a maximum days-to-earnings ceiling.

    Statuses arrive with the signals in one ledger query
    (``recent_with_status``) -- the per-row ``current_status`` loop this
    replaces issued up to ``limit`` extra queries per request, which under a
    concurrent data refresh compounded into multi-minute responses (QA
    handoff v3, F26; the MCP server's ``get_signals`` shared the pattern).

    Each row also carries ``effective_score`` (``overall_score`` re-ranked by
    any accepted research revisions, via ``signals.scoring.adjusted_overall``)
    and ``has_research``, fetched with ONE extra batched query keyed to
    exactly the signal ids on this page (``ResearchLedger
    .latest_research_revisions``) -- never a per-row lookup, mirroring
    ``mcp_server.get_signals``'s discipline. ``attention`` (cross-platform
    Adanos buzz/sentiment) is fetched the same way, with a second batched
    query keyed to exactly the symbols on this page
    (``webapi.attention.latest_attention``).

    ``distinct`` (default ``True``) collapses read-time duplicates via
    ``signals.dedupe.collapse_recommendations`` -- the same pure helper
    ``mcp_server.get_signals``/the Streamlit Scanner use, so all three
    surfaces agree (a past incident, F26, came from surfaces disagreeing).
    Each collapsed row carries ``corroborating_strategies``/
    ``corroborating_count``/``duplicates_collapsed`` (see ``SignalRowOut``).
    When ``distinct=True`` the response is ordered by representative
    ``effective_score`` descending (``collapse_recommendations``'s own
    contract) rather than ledger recency; ``distinct=False`` keeps the
    original ledger (recency) order for client-side sorting/filtering (the
    Screener grid sorts by whichever column -- including
    ``effective_score`` -- the user picks either way).
    """
    recent = pipeline.ledger.recent_with_status(limit=limit)
    directions = {d.lower() for d in direction} if direction else None
    strategies = set(strategy) if strategy else None

    matched: list[tuple] = []
    for sig, status in recent:
        if sig.overall_score < min_score:
            continue
        if sig.confidence < min_confidence:
            continue
        if directions is not None and str(sig.direction) not in directions:
            continue
        if strategies is not None and sig.strategy not in strategies:
            continue
        if max_days_to_earnings is not None and (
            sig.days_to_earnings is None or sig.days_to_earnings > max_days_to_earnings
        ):
            continue
        matched.append((sig, status))

    # Batched, not per-row (same F26 discipline as recent_with_status above).
    research = ResearchLedger(pipeline.db).latest_research_revisions(
        [sig.signal_id for sig, _ in matched]
    )
    attention = latest_attention(pipeline.db, [sig.symbol for sig, _ in matched])

    effective_scores: dict[str, float] = {}
    has_research_by_id: dict[str, bool] = {}
    for sig, _status in matched:
        revision = research.get(sig.signal_id)
        has_research = revision is not None
        if revision is not None:
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
        groups = collapse_recommendations(matched, effective_scores)
        rows = [
            signal_to_row(
                group.signal,
                group.status,
                effective_score=group.effective_score,
                has_research=has_research_by_id.get(group.signal.signal_id, False),
                attention=attention.get(group.signal.symbol),
                corroborating_strategies=group.corroborating_strategies,
                duplicates_collapsed=group.duplicates_collapsed,
            )
            for group in groups
        ]
    else:
        rows = [
            signal_to_row(
                sig,
                status,
                effective_score=effective_scores[sig.signal_id],
                has_research=has_research_by_id[sig.signal_id],
                attention=attention.get(sig.symbol),
            )
            for sig, status in matched
        ]

    return SignalListOut(signals=rows, total=len(rows))


@router.get("/signals/rejected", response_model=RejectedResponse)
def rejected_candidates(scan_result=Depends(get_last_scan)) -> RejectedResponse:
    """Near-miss candidates from the last scan run in this server process.

    Honest empty state: ``ScanResult.rejected`` is never persisted, so this is
    empty (with a reason, not a bare ``[]``) until ``POST /api/scan`` has run
    at least once against this process.
    """
    if scan_result is None:
        return RejectedResponse(
            available=False,
            reason=(
                "Rejected candidates are only available for the scan just run in this "
                "server process -- they are not persisted to the database. "
                "POST /api/scan to populate this list."
            ),
        )
    funnel = scan_result.funnel
    return RejectedResponse(
        available=True,
        generated_at=scan_result.generated_at,
        evaluated_symbols=scan_result.evaluated_symbols,
        rejected=[
            RejectedCandidateOut(
                symbol=r.symbol,
                strategy=r.strategy,
                stage=r.stage,
                reasons=list(r.reasons),
                reason_codes=list(r.reason_codes),
            )
            for r in scan_result.rejected
        ],
        funnel=ScanFunnelOut(
            top_n=funnel.top_n,
            total_rejections=funnel.total_rejections,
            by_reason=dict(funnel.by_reason),
            by_strategy_reason={k: dict(v) for k, v in funnel.by_strategy_reason.items()},
            near_misses=[
                NearMissOut(
                    symbol=nm.symbol,
                    strategy=nm.strategy,
                    reason_code=nm.reason_code,
                    metric=nm.metric,
                    threshold=nm.threshold,
                    margin=nm.margin,
                    overall_score=nm.overall_score,
                    confidence=nm.confidence,
                    weakest_components=nm.weakest_components,
                    strongest_components=nm.strongest_components,
                )
                for nm in funnel.near_misses
            ],
        ),
    )


@router.get("/signals/{signal_id}", response_model=SignalDetailOut)
def get_signal(
    signal_id: str,
    pipeline: Pipeline = Depends(get_pipeline),
    config=Depends(get_config),
) -> SignalDetailOut:
    """One signal's full detail, plus its latest research revision and history.

    ``research``/``research_history`` come straight from ``ResearchLedger``
    (the same append-only store ``mcp_server.submit_research_revision``
    writes to); ``effective_score``/``has_research`` are derived from the
    latest revision the same way ``list_signals`` derives them for the grid.
    ``attention`` comes from the same ``webapi.attention.latest_attention``
    helper the grid uses, keyed to this one symbol.
    """
    sig = pipeline.ledger.get(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"unknown signal {signal_id}")
    status = pipeline.ledger.current_status(signal_id)

    ledger = ResearchLedger(pipeline.db)
    history = ledger.research_history(signal_id)
    latest = history[-1] if history else None
    has_research = latest is not None
    if latest is not None:
        effective = adjusted_overall(
            sig.components.as_dict(), sig.overall_score, latest["score_adjustments"], config
        )
    else:
        effective = sig.overall_score

    attention = latest_attention(pipeline.db, [sig.symbol]).get(sig.symbol)

    return signal_to_detail(
        sig,
        status,
        effective_score=effective,
        has_research=has_research,
        research=latest,
        research_history=history,
        attention=attention,
    )


@router.post("/scan", response_model=ScanResponse)
def run_scan(
    body: ScanRequest,
    request: Request,
    pipeline: Pipeline = Depends(get_pipeline),
) -> ScanResponse:
    """Run ``Pipeline.scan`` for one session -- identical to ``claudetrade scan``.

    Caches the returned ``ScanResult`` on ``app.state`` so
    ``GET /api/signals/rejected`` can surface its near-miss candidates.
    ``generate_thesis`` defaults to ``False`` here (unlike the CLI) so an
    interactive scan from the UI doesn't block on AI-provider latency by
    default; the caller can opt in.
    """
    # ET trading calendar, not the UTC date: a Friday-evening scan is
    # Friday's session, never a weekend date (timeutils.current_trading_session).
    session_date = body.session or current_trading_session()
    result = pipeline.scan(
        session_date,
        lookback_days=body.lookback_days,
        generate_thesis=body.generate_thesis,
    )
    set_last_scan(request, result.scan)
    return ScanResponse(
        # The session actually evaluated -- the pipeline may fall back to the
        # latest stored session when the requested one has no data (the
        # warnings explain when it does).
        session=result.scan.session if result.scan else session_date,
        evaluated_symbols=result.scan.evaluated_symbols if result.scan else 0,
        signal_count=len(result.scan.signals) if result.scan else 0,
        rejected_count=len(result.scan.rejected) if result.scan else 0,
        warnings=result.warnings,
    )


@router.post("/refresh", response_model=RefreshResponse)
def run_refresh(
    body: RefreshRequest,
    pipeline: Pipeline = Depends(get_pipeline),
    config=Depends(get_config),
) -> RefreshResponse:
    """Run ``Pipeline.refresh`` -- identical to ``claudetrade refresh``."""
    end = body.end or utc_now().date()
    # Default price window is 90 days -- context building needs 30+ bars, so
    # defaulting to the 14-day social lookback guaranteed empty scans on a
    # fresh install (same fix as the background-refresh endpoint and the MCP
    # server). Social sources stay bounded to the sentiment window.
    start = body.start or (end - dt.timedelta(days=90))
    result = pipeline.refresh(
        start=start,
        end=end,
        social_lookback_hours=config.sentiment.lookback_days * 24,
    )
    return RefreshResponse(
        universe_size=result.universe_size,
        sentiment_rows=result.sentiment_rows,
        degraded_sources=dict(result.degraded_sources),
        warnings=result.warnings,
    )


__all__ = ["router"]
