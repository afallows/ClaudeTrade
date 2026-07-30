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
from claudetrade.utils.timeutils import utc_now
from claudetrade.webapi.deps import get_config, get_last_scan, get_pipeline, set_last_scan
from claudetrade.webapi.schemas import (
    RefreshRequest,
    RefreshResponse,
    RejectedCandidateOut,
    RejectedResponse,
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
    direction: list[str] | None = Query(default=None, description="long, short, or both"),
    min_score: float = Query(default=0.0, ge=0.0, le=100.0),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    strategy: list[str] | None = Query(default=None),
    max_days_to_earnings: int | None = Query(default=None, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
) -> SignalListOut:
    """The candidate universe for the Screener grid, most-recent-first.

    Filters mirror the Streamlit Scanner's ``apply_candidate_filters``
    exactly: minimum score, minimum confidence, direction set, strategy set,
    and a maximum days-to-earnings ceiling.
    """
    recent = pipeline.ledger.recent(limit=limit)
    directions = {d.lower() for d in direction} if direction else None
    strategies = set(strategy) if strategy else None

    rows = []
    for sig in recent:
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
        status = pipeline.ledger.current_status(sig.signal_id)
        rows.append(signal_to_row(sig, status))

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
    return RejectedResponse(
        available=True,
        generated_at=scan_result.generated_at,
        evaluated_symbols=scan_result.evaluated_symbols,
        rejected=[
            RejectedCandidateOut(
                symbol=r.symbol, strategy=r.strategy, stage=r.stage, reasons=list(r.reasons)
            )
            for r in scan_result.rejected
        ],
    )


@router.get("/signals/{signal_id}", response_model=SignalDetailOut)
def get_signal(signal_id: str, pipeline: Pipeline = Depends(get_pipeline)) -> SignalDetailOut:
    sig = pipeline.ledger.get(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"unknown signal {signal_id}")
    status = pipeline.ledger.current_status(signal_id)
    return signal_to_detail(sig, status)


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
    session_date = body.session or utc_now().date()
    result = pipeline.scan(
        session_date,
        lookback_days=body.lookback_days,
        generate_thesis=body.generate_thesis,
    )
    set_last_scan(request, result.scan)
    return ScanResponse(
        session=session_date,
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
    start = body.start or (end - dt.timedelta(days=config.sentiment.lookback_days))
    result = pipeline.refresh(start=start, end=end)
    return RefreshResponse(
        universe_size=result.universe_size,
        sentiment_rows=result.sentiment_rows,
        degraded_sources=dict(result.degraded_sources),
        warnings=result.warnings,
    )


__all__ = ["router"]
