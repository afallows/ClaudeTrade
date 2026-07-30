"""Dashboard bundle: regime, top candidates, status ribbon, provider health.

Everything here is read straight from ``pipeline.ledger``/``pipeline.
provider_status()``/``ui.data_access.data_freshness`` -- never sample data --
mirroring ``ui.screens.dashboard`` exactly (see that module's docstring).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from claudetrade.pipeline import Pipeline
from claudetrade.ui.data_access import data_freshness
from claudetrade.webapi.deps import get_pipeline
from claudetrade.webapi.schemas import (
    DashboardOut,
    ProviderStatusOut,
    RegimeCardOut,
    StatusRibbonOut,
)
from claudetrade.webapi.serialize import regime_label, signal_to_row, top_candidates

router = APIRouter(prefix="/api", tags=["dashboard"])

#: How many candidates per side the "top candidates" tables carry.
TOP_N = 5


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(pipeline: Pipeline = Depends(get_pipeline)) -> DashboardOut:
    recent = pipeline.ledger.recent(limit=200)
    freshness = data_freshness(pipeline.db)

    regime_value = "unknown"
    as_of_session = None
    last_scan_at = None
    if recent:
        latest = max(recent, key=lambda s: s.created_at)
        regime_value = str(latest.regime)
        as_of_session = latest.session
        # ``recent()`` is already ordered by created_at descending.
        last_scan_at = recent[0].created_at

    def _rows(direction: str) -> list:
        picks = top_candidates(recent, direction, n=TOP_N)
        status_by_id = {s.signal_id: pipeline.ledger.current_status(s.signal_id) for s in picks}
        return [signal_to_row(s, status_by_id[s.signal_id]) for s in picks]

    providers = [
        ProviderStatusOut(
            name=s.name,
            kind=s.kind,
            available=s.available,
            configured=s.configured,
            supports_point_in_time=s.supports_point_in_time,
            message=s.message,
        )
        for s in pipeline.provider_status()
    ]

    return DashboardOut(
        regime=RegimeCardOut(
            regime=regime_value,
            label=regime_label(regime_value),
            as_of_session=as_of_session,
            has_data=bool(recent),
        ),
        top_longs=_rows("long"),
        top_shorts=_rows("short"),
        status=StatusRibbonOut(
            last_refresh=freshness.latest_ingested_at,
            last_scan=last_scan_at,
            symbols_with_data=freshness.symbol_count,
        ),
        providers=providers,
    )


__all__ = ["router"]
