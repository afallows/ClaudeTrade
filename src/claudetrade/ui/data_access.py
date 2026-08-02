"""Read-only database helpers shared by multiple UI screens.

Every function here is a side-effect-free query against the existing schema
(``claudetrade.db.models``) -- no new tables, and no writes except where a
screen explicitly submits a paper order or a config write is documented
elsewhere. Centralising these means the freshness/earnings/sentiment/price
queries a screen needs are written once and unit-tested once instead of
copy-pasted per screen.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select

from claudetrade.config import AppConfig
from claudetrade.db.models import EarningsEventRow, PriceBar, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.domain import Bar, Signal
from claudetrade.signals.research import ResearchLedger
from claudetrade.signals.scoring import adjusted_overall


@dataclass(slots=True)
class DataFreshness:
    """Summary of how current the stored market data is."""

    latest_session: dt.date | None
    latest_ingested_at: dt.datetime | None
    symbol_count: int

    @property
    def has_data(self) -> bool:
        return self.latest_session is not None


def data_freshness(db: Database) -> DataFreshness:
    """Latest stored trading session, last ingest timestamp, and symbol count."""
    with db.read_session() as session:
        latest_session = session.execute(select(func.max(PriceBar.session))).scalar()
        latest_ingested = session.execute(select(func.max(PriceBar.ingested_at))).scalar()
        symbol_count = session.execute(
            select(func.count(func.distinct(PriceBar.symbol)))
        ).scalar()
    return DataFreshness(
        latest_session=latest_session,
        latest_ingested_at=latest_ingested,
        symbol_count=int(symbol_count or 0),
    )


def price_bars(
    db: Database,
    symbol: str,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[Bar]:
    """Stored daily bars for one symbol, oldest first."""
    with db.read_session() as session:
        stmt = select(PriceBar).where(PriceBar.symbol == symbol)
        if start is not None:
            stmt = stmt.where(PriceBar.session >= start)
        if end is not None:
            stmt = stmt.where(PriceBar.session <= end)
        rows = session.execute(stmt.order_by(PriceBar.session.asc())).scalars().all()
    return [_row_to_bar(r) for r in rows]


def _row_to_bar(row: PriceBar) -> Bar:
    return Bar(
        symbol=row.symbol,
        session=row.session,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        adj_close=row.adj_close,
        source=row.source,
    )


@dataclass(slots=True)
class SentimentPoint:
    """One day's aggregated sentiment for a symbol (``symbol_sentiment_daily``)."""

    session: dt.date
    post_count: int
    unique_authors: int
    engagement_weighted: float
    bull_bear_ratio: float
    manipulation_risk: float
    confidence: float


def sentiment_timeline(
    db: Database, symbol: str, *, source: str = "all"
) -> list[SentimentPoint]:
    """Daily sentiment/mention-volume series for one symbol, oldest first."""
    with db.read_session() as session:
        rows = (
            session.execute(
                select(SymbolSentimentDaily)
                .where(
                    SymbolSentimentDaily.symbol == symbol,
                    SymbolSentimentDaily.source == source,
                )
                .order_by(SymbolSentimentDaily.session.asc())
            )
            .scalars()
            .all()
        )
    return [
        SentimentPoint(
            session=r.session,
            post_count=r.post_count,
            unique_authors=r.unique_authors,
            engagement_weighted=r.engagement_weighted,
            bull_bear_ratio=r.bull_bear_ratio,
            manipulation_risk=r.manipulation_risk,
            confidence=r.confidence,
        )
        for r in rows
    ]


def earnings_dates(db: Database, symbol: str) -> list[dt.date]:
    """Every known earnings report date for ``symbol``, ascending."""
    with db.read_session() as session:
        rows = (
            session.execute(
                select(EarningsEventRow.report_date)
                .where(EarningsEventRow.symbol == symbol)
                .distinct()
                .order_by(EarningsEventRow.report_date.asc())
            )
            .scalars()
            .all()
        )
    return list(rows)


@dataclass(slots=True, frozen=True)
class ResearchOverlay:
    """Read-time research overlay for one signal (``signals.research``).

    ``effective_score`` equals the signal's own ``overall_score`` and
    ``has_research`` is False when no revision exists -- the same "never
    null, equal to overall_score when absent" contract ``webapi`` and the
    MCP server report, so all three surfaces agree.
    """

    effective_score: float
    has_research: bool
    #: The latest revision, in ``ResearchLedger.latest_research_revisions``'s
    #: dict shape (``revision``, ``created_at``, ``actor``, ``thesis``,
    #: ``invalidation``, ``score_adjustments``, ``rationale``, ``sources``,
    #: ``detail``), or ``None`` when ``has_research`` is False.
    latest: dict[str, object] | None


def research_overlay(
    db: Database, signals: list[Signal], config: AppConfig
) -> dict[str, ResearchOverlay]:
    """Effective score + latest research revision per signal, ONE batched query.

    Mirrors ``webapi.routers.signals.list_signals``/``mcp_server.get_signals``:
    ``ResearchLedger.latest_research_revisions`` is fetched once for every
    signal id in ``signals`` -- never per-row -- and ``signals.scoring
    .adjusted_overall`` computes the same read-time overlay those layers use,
    so the Streamlit screens agree with the API/MCP surfaces on both the
    effective score and whether research exists. Signal ids with no revision
    are still present in the result, with ``has_research=False`` and
    ``effective_score`` equal to the signal's own ``overall_score``.
    """
    revisions = ResearchLedger(db).latest_research_revisions([s.signal_id for s in signals])
    overlay: dict[str, ResearchOverlay] = {}
    for sig in signals:
        revision = revisions.get(sig.signal_id)
        if revision is None:
            overlay[sig.signal_id] = ResearchOverlay(
                effective_score=sig.overall_score, has_research=False, latest=None
            )
        else:
            effective = adjusted_overall(
                sig.components.as_dict(), sig.overall_score, revision["score_adjustments"], config
            )
            overlay[sig.signal_id] = ResearchOverlay(
                effective_score=effective, has_research=True, latest=revision
            )
    return overlay


def known_symbols(db: Database, *, limit: int = 5000) -> list[str]:
    """Every symbol with at least one stored bar, for the ticker-detail picker."""
    with db.read_session() as session:
        rows = (
            session.execute(
                select(PriceBar.symbol).distinct().order_by(PriceBar.symbol.asc()).limit(limit)
            )
            .scalars()
            .all()
        )
    return list(rows)


__all__ = [
    "DataFreshness",
    "ResearchOverlay",
    "SentimentPoint",
    "data_freshness",
    "earnings_dates",
    "known_symbols",
    "price_bars",
    "research_overlay",
    "sentiment_timeline",
]
