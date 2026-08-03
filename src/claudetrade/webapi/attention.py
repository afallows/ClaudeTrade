"""Cross-platform Adanos attention aggregate for the Screener grid.

``db.models.AdanosSnapshotRow`` stores one row per ``(session, platform,
symbol)`` -- one reading per Adanos feed (``x``, ``reddit``, ``polymarket``,
``news``, and whatever else ``providers.social.adanos`` enables in future;
nothing here hardcodes the platform count or names). The Screener grid wants
one number per symbol, not one row per platform, so this module folds
however many platform rows a symbol has on its latest session into a single
:class:`AttentionAggregate`.

Batched the same way ``signals.research.ResearchLedger.latest_research_revisions``
is (max-per-group subquery, joined back, ONE query for the whole page) -- a
per-symbol loop here would be exactly the N+1 that produced the production
stall documented in that module (QA handoff v3, F26).

Pure read, no writes -- lives under ``webapi/`` rather than
``ui/data_access.py`` only because this feature's file boundary excludes
``ui/data_access.py``; the shape (module-level functions taking a
``Database`` and returning dataclasses) matches that module's own
conventions.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select

from claudetrade.db.models import AdanosSnapshotRow
from claudetrade.db.session import Database

#: The one platform whose ``engagement`` column holds a mention-source count
#: rather than an engagement total (see ``providers.social.adanos``'s
#: ``_ENGAGEMENT_FIELD``: news rows have no upvotes/likes/liquidity
#: analogue, so ``source_count`` -- distinct outlets -- is stored through the
#: same column instead of a fifth schema column for one platform's quirk).
_SOURCE_COUNT_PLATFORM = "news"


@dataclass(slots=True)
class AttentionAggregate:
    """One symbol's cross-platform Adanos reading for its latest session.

    Every averaged field documents its own weighting choice below --
    none of this is invented data, it is a documented fold of whatever
    platform rows exist for the symbol on that session.
    """

    symbol: str
    session: dt.date
    #: Platform names with a row on this session, sorted for stable output
    #: (e.g. ``["news", "reddit", "x"]``) -- never a hardcoded 3 or 4.
    platforms: list[str]
    #: Sum of each platform's own "how many times" column (``mentions`` for
    #: x/reddit/news, ``trade_count`` for polymarket -- see
    #: ``AdanosSnapshotRow.mentions``'s docstring for that one column's
    #: double duty). Treated as the best available per-platform volume
    #: proxy; not a claim that a Polymarket trade and an X mention are the
    #: same unit.
    total_mentions: int
    #: Distinct news outlets reporting on the symbol, from the ``news``
    #: platform's row if one exists this session; ``None`` when no ``news``
    #: row is present (feed disabled, not yet collected, or vendor returned
    #: nothing for this symbol) -- never a fabricated 0.
    source_count: int | None
    #: Mention-weighted mean of each platform's ``buzz_score`` -- a platform
    #: with more mentions this session gets proportionally more say, since
    #: its buzz score is built from more underlying activity ("best
    #: supported" reading rather than a flat average across platforms of
    #: wildly different volume). Falls back to a flat average when every
    #: platform reports zero mentions.
    buzz_score: float
    #: Mention-weighted mean of ``bullish_pct``/``bearish_pct`` across
    #: platforms that reported a value (``None`` values are excluded, not
    #: treated as 0). ``None`` when no platform reported either.
    bullish_pct: float | None
    bearish_pct: float | None
    #: The mention-weighted-majority trend direction ("rising"/"falling"/
    #: "stable") among platforms that reported one; ties (including the
    #: all-quiet zero-mention case) favour "stable". Empty string when no
    #: platform reported a trend.
    trend: str
    #: Mention-weighted elementwise mean of each platform's 7-point
    #: ``trend_history``, restricted to platforms whose history actually has
    #: 7 points (the vendor's documented shape -- a shorter/longer list is
    #: schema drift, excluded rather than guessed at). Empty when no
    #: platform qualifies.
    trend_history: list[float] = field(default_factory=list)


def _weighted_mean(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Weight-aware mean over ``[(value, weight), ...]``.

    Falls back to a flat (unweighted) average when every weight is
    non-positive -- a quiet session where no contributing platform reported
    any mentions should not divide by zero or silently drop every reading.
    """
    if not pairs:
        return None
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total_weight


def _mention_weight(row: AdanosSnapshotRow) -> float:
    return float(row.mentions) if row.mentions and row.mentions > 0 else 0.0


def _dominant_trend(rows: Sequence[AdanosSnapshotRow]) -> str:
    votes: dict[str, float] = {}
    for row in rows:
        if not row.trend:
            continue
        # A platform with zero mentions still gets an equal-weight vote
        # (weight 1) rather than being silently dropped from the ballot.
        votes[row.trend] = votes.get(row.trend, 0.0) + (_mention_weight(row) or 1.0)
    if not votes:
        return ""
    return max(votes.items(), key=lambda kv: (kv[1], kv[0] == "stable"))[0]


def _combined_trend_history(rows: Sequence[AdanosSnapshotRow]) -> list[float]:
    candidates = [
        (list(row.trend_history), _mention_weight(row)) for row in rows if len(row.trend_history) == 7
    ]
    if not candidates:
        return []
    total_weight = sum(w for _, w in candidates)
    if total_weight <= 0:
        return [sum(hist[i] for hist, _ in candidates) / len(candidates) for i in range(7)]
    return [sum(hist[i] * w for hist, w in candidates) / total_weight for i in range(7)]


def _source_count(rows: Sequence[AdanosSnapshotRow]) -> int | None:
    for row in rows:
        if row.platform == _SOURCE_COUNT_PLATFORM:
            return int(row.engagement)
    return None


def _aggregate(symbol: str, rows: list[AdanosSnapshotRow]) -> AttentionAggregate:
    buzz_pairs = [(row.buzz_score, _mention_weight(row)) for row in rows]
    bull_pairs = [(row.bullish_pct, _mention_weight(row)) for row in rows if row.bullish_pct is not None]
    bear_pairs = [(row.bearish_pct, _mention_weight(row)) for row in rows if row.bearish_pct is not None]
    return AttentionAggregate(
        symbol=symbol,
        session=rows[0].session,
        platforms=sorted({row.platform for row in rows}),
        total_mentions=sum(row.mentions for row in rows),
        source_count=_source_count(rows),
        buzz_score=_weighted_mean(buzz_pairs) or 0.0,
        bullish_pct=_weighted_mean(bull_pairs),
        bearish_pct=_weighted_mean(bear_pairs),
        trend=_dominant_trend(rows),
        trend_history=_combined_trend_history(rows),
    )


def latest_attention(db: Database, symbols: Sequence[str]) -> dict[str, AttentionAggregate]:
    """The latest-session Adanos attention aggregate for each of ``symbols``.

    ONE query pattern (max-per-symbol-session subquery, joined back), never a
    per-symbol loop -- see the module docstring. "Latest session" is per
    *symbol*, not per ``(symbol, platform)``: every enabled platform is
    collected in the same hourly cycle, so in the normal case they share a
    session; if one platform's collection failed on the newest session, that
    platform is simply absent from the aggregate rather than contributing a
    stale row from an earlier session.

    A symbol with no stored Adanos snapshot at all is absent from the
    returned dict -- callers treat that absence as "no attention data yet",
    matching ``ResearchLedger.latest_research_revisions``'s own convention.
    """
    ids = list(dict.fromkeys(symbols))
    if not ids:
        return {}

    with db.read_session() as session:
        latest_sub = (
            select(
                AdanosSnapshotRow.symbol.label("symbol"),
                func.max(AdanosSnapshotRow.session).label("max_session"),
            )
            .where(AdanosSnapshotRow.symbol.in_(ids))
            .group_by(AdanosSnapshotRow.symbol)
            .subquery()
        )
        rows = (
            session.execute(
                select(AdanosSnapshotRow).join(
                    latest_sub,
                    (latest_sub.c.symbol == AdanosSnapshotRow.symbol)
                    & (latest_sub.c.max_session == AdanosSnapshotRow.session),
                )
            )
            .scalars()
            .all()
        )

    by_symbol: dict[str, list[AdanosSnapshotRow]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)

    return {symbol: _aggregate(symbol, platform_rows) for symbol, platform_rows in by_symbol.items()}


__all__ = ["AttentionAggregate", "latest_attention"]
