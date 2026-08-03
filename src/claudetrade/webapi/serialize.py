"""Translate domain objects (``claudetrade.domain``) into ``webapi.schemas``.

Pure functions only -- no DB access, no pipeline calls -- so they're testable
without a database and reusable across routers.

Two small presentation helpers (``active_signal_for`` and ``top_candidates``)
are intentionally re-implemented here rather than imported from
``claudetrade.ui.screens.*``: those modules import ``streamlit`` at module
scope, and pulling that into the API layer would couple a from-scratch,
Node-free JSON API to the Streamlit UI it is meant to replace. The logic
itself is a presentation choice ("which signal do we highlight"), not domain
logic -- the domain rules (score, entry zone, ledger status) still come from
``claudetrade.signals``/``claudetrade.domain`` unchanged.
"""

from __future__ import annotations

import math

import pandas as pd

from claudetrade.domain import Bar, Signal, SignalStatus
from claudetrade.features.indicators import bollinger_bands, rsi, sma
from claudetrade.webapi.attention import AttentionAggregate
from claudetrade.webapi.schemas import (
    AttentionOut,
    ComponentScoresOut,
    IndicatorsOut,
    ResearchRevisionOut,
    SignalDetailOut,
    SignalRowOut,
    TradePlanOut,
)

#: Human labels for ``MarketRegime`` values, for the dashboard regime card.
#: Icon selection is a frontend concern (lucide-react); this only supplies text.
REGIME_LABELS: dict[str, str] = {
    "bull_quiet": "Bull -- Quiet",
    "bull_volatile": "Bull -- Volatile",
    "neutral": "Neutral",
    "bear_volatile": "Bear -- Volatile",
    "bear_quiet": "Bear -- Quiet",
    "unknown": "Unknown",
}


def regime_label(regime: str | None) -> str:
    return REGIME_LABELS.get((regime or "unknown").lower(), (regime or "Unknown").title())


def _components_out(sig: Signal) -> ComponentScoresOut:
    return ComponentScoresOut(**sig.components.as_dict())


def _plan_out(sig: Signal) -> TradePlanOut:
    plan = sig.plan
    return TradePlanOut(
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop_loss=plan.stop_loss,
        targets=list(plan.targets),
        reward_risk_ratio=plan.reward_risk_ratio,
        shares=plan.shares,
        notional_usd=plan.notional_usd,
        risk_per_share=plan.risk_per_share,
        reward_per_share=plan.reward_per_share,
        time_stop_days=plan.time_stop_days,
        expected_holding_days=plan.expected_holding_days,
    )


def _research_out(entry: dict[str, object]) -> ResearchRevisionOut:
    """One ``ResearchLedger``-shaped dict (latest or history) to the wire model."""
    return ResearchRevisionOut(
        revision=entry["revision"],  # type: ignore[arg-type]
        created_at=entry["created_at"],  # type: ignore[arg-type]
        actor=entry["actor"],  # type: ignore[arg-type]
        thesis=entry["thesis"],  # type: ignore[arg-type]
        invalidation=entry["invalidation"],  # type: ignore[arg-type]
        score_adjustments=entry["score_adjustments"],  # type: ignore[arg-type]
        rationale=entry["rationale"],  # type: ignore[arg-type]
        sources=entry["sources"],  # type: ignore[arg-type]
    )


def _attention_out(agg: AttentionAggregate) -> AttentionOut:
    return AttentionOut(
        session=agg.session,
        platforms=list(agg.platforms),
        total_mentions=agg.total_mentions,
        source_count=agg.source_count,
        buzz_score=agg.buzz_score,
        bullish_pct=agg.bullish_pct,
        bearish_pct=agg.bearish_pct,
        trend=agg.trend,
        trend_history=list(agg.trend_history),
    )


def signal_to_row(
    sig: Signal,
    status: SignalStatus | None,
    *,
    effective_score: float | None = None,
    has_research: bool = False,
    attention: AttentionAggregate | None = None,
) -> SignalRowOut:
    """One flat Screener/candidate-table row for ``sig``.

    ``effective_score``/``has_research`` are supplied by the caller (the
    router, which owns the ``ResearchLedger`` lookup and the
    ``adjusted_overall`` call) -- this stays a pure translation function with
    no DB access. Omitting ``effective_score`` defaults it to
    ``sig.overall_score``, matching "no research" (``has_research=False``).
    ``attention`` is likewise supplied by the caller (which owns the batched
    ``webapi.attention.latest_attention`` lookup); ``None`` means no Adanos
    snapshot exists for this symbol yet.
    """
    return SignalRowOut(
        signal_id=sig.signal_id,
        symbol=sig.symbol,
        company_name=sig.company_name,
        strategy=sig.strategy,
        direction=str(sig.direction),
        status=status.value if status else "unknown",
        regime=str(sig.regime),
        overall_score=sig.overall_score,
        effective_score=effective_score if effective_score is not None else sig.overall_score,
        has_research=has_research,
        confidence=sig.confidence,
        reward_risk_ratio=sig.plan.reward_risk_ratio,
        entry_low=sig.plan.entry_low,
        entry_high=sig.plan.entry_high,
        stop_loss=sig.plan.stop_loss,
        days_to_earnings=sig.days_to_earnings,
        session=sig.session,
        created_at=sig.created_at,
        attention=_attention_out(attention) if attention is not None else None,
    )


def signal_to_detail(
    sig: Signal,
    status: SignalStatus | None,
    *,
    effective_score: float | None = None,
    has_research: bool = False,
    research: dict[str, object] | None = None,
    research_history: list[dict[str, object]] | None = None,
    attention: AttentionAggregate | None = None,
) -> SignalDetailOut:
    """Full ticker-detail/thesis view of ``sig``.

    ``research``/``research_history`` are the raw dicts
    ``ResearchLedger.latest_research_revisions``/``research_history`` return
    (the router fetches them); ``None``/empty means no research exists.
    ``attention`` is the same optional ``AttentionAggregate`` ``signal_to_row``
    takes.
    """
    row = signal_to_row(
        sig,
        status,
        effective_score=effective_score,
        has_research=has_research,
        attention=attention,
    )
    return SignalDetailOut(
        **row.model_dump(),
        components=_components_out(sig),
        plan=_plan_out(sig),
        thesis=sig.thesis,
        invalidation=list(sig.invalidation),
        exit_conditions=list(sig.exit_conditions),
        risks=list(sig.risks),
        evidence=list(sig.evidence),
        next_earnings_date=sig.next_earnings_date,
        data_warnings=list(sig.data_warnings),
        research=_research_out(research) if research is not None else None,
        research_history=[_research_out(r) for r in (research_history or [])],
    )


def active_signal_for(signals: list[Signal]) -> Signal | None:
    """The signal to draw entry/stop/target levels for: the newest tradable one.

    Falls back to the newest signal overall if none is currently tradable, so
    the chart still shows the most recent thesis's levels even after it has
    triggered or expired. Mirrors ``ui.screens.ticker_detail.active_signal``
    (see the module docstring for why this is reimplemented rather than
    imported).
    """
    if not signals:
        return None
    tradable = [s for s in signals if s.is_tradable]
    pool = tradable or signals
    return max(pool, key=lambda s: s.created_at)


def _series_out(series: pd.Series) -> list[float | None]:
    """A pandas float series to a JSON-safe list (``NaN`` -> ``None``)."""
    return [None if math.isnan(v) else float(v) for v in series.to_numpy(dtype=float)]


def compute_indicators(bars: list[Bar]) -> IndicatorsOut:
    """SMA(20/50/200), RSI(14) and Bollinger(20, 2) over ``bars``' closes.

    Delegates every calculation to ``claudetrade.features.indicators`` -- the
    same functions ``ui.charts.create_ticker_chart`` uses -- so the chart
    overlays in the new UI use the identical formulas as the old one, rather
    than a second implementation in TypeScript.
    """
    if not bars:
        return IndicatorsOut(
            sma_20=[], sma_50=[], sma_200=[], rsi_14=[], bollinger_upper=[], bollinger_lower=[]
        )
    close = pd.Series([b.close for b in bars], dtype=float)
    bands = bollinger_bands(close, 20, 2.0)
    return IndicatorsOut(
        sma_20=_series_out(sma(close, 20)),
        sma_50=_series_out(sma(close, 50)),
        sma_200=_series_out(sma(close, 200)),
        rsi_14=_series_out(rsi(close, 14)),
        bollinger_upper=_series_out(bands["upper"]),
        bollinger_lower=_series_out(bands["lower"]),
    )


def top_candidates(signals: list[Signal], direction: str, n: int = 5) -> list[Signal]:
    """The best ``n`` signals for one direction, ranked by overall score.

    Mirrors ``ui.screens.dashboard.top_candidates`` (see the module docstring
    for why this is reimplemented rather than imported).
    """
    side = [s for s in signals if str(s.direction) == direction]
    return sorted(side, key=lambda s: s.overall_score, reverse=True)[:n]


__all__ = [
    "active_signal_for",
    "compute_indicators",
    "regime_label",
    "signal_to_detail",
    "signal_to_row",
    "top_candidates",
]
