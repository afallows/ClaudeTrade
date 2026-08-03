"""Point-in-time Adanos cross-platform attention history (ADR-0009).

Duplicates ``webapi.attention``'s ``AttentionAggregate`` dataclass and
per-session folding logic rather than importing them: this feature's file
boundary excludes editing ``src/claudetrade/webapi/**`` (including
``webapi/attention.py``), and that module's ``latest_attention`` only ever
loads each symbol's LATEST session -- exactly the "latest ever" shortcut
``data.context``'s module docstring forbids for anything that reaches a
``StrategyContext``. This module loads FULL history bounded by
``session <= end`` instead, so ``ContextBuilder.build`` can truncate it
per-session exactly like ``sentiment_history`` already is, and a context
built for an earlier session can never see a row stored after it.

The duplication is deliberately narrow: only what
``signals.scoring._cross_source_attention_score`` needs (one
``AttentionAggregate`` per symbol per session, oldest first) is reproduced
here. Any future change to the folding rules in ``webapi.attention`` should
be mirrored here by hand -- there are two independent copies now, not a
shared one, which is the coordinator-sanctioned trade-off for this feature
(see ``HANDOFF.md`` task #24) rather than an oversight.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from claudetrade.db.models import AdanosSnapshotRow
from claudetrade.db.session import Database

#: The one platform whose ``engagement`` column holds a mention-source count
#: rather than an engagement total -- see ``webapi.attention``'s identical
#: constant for the full rationale.
_SOURCE_COUNT_PLATFORM = "news"


@dataclass(slots=True)
class AttentionAggregate:
    """One symbol's cross-platform Adanos reading for ONE stored session.

    Field-for-field identical to ``webapi.attention.AttentionAggregate``
    (see that class's docstring for what each averaged field means and how
    it is weighted) -- the only difference is that THIS module returns one
    instance per stored session per symbol, not only the latest.
    """

    symbol: str
    session: dt.date
    platforms: list[str]
    total_mentions: int
    source_count: int | None
    buzz_score: float
    bullish_pct: float | None
    bearish_pct: float | None
    trend: str
    trend_history: list[float] = field(default_factory=list)


def _weighted_mean(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Identical to ``webapi.attention._weighted_mean`` -- see there."""
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


def _aggregate(symbol: str, session: dt.date, rows: list[AdanosSnapshotRow]) -> AttentionAggregate:
    buzz_pairs = [(row.buzz_score, _mention_weight(row)) for row in rows]
    bull_pairs = [(row.bullish_pct, _mention_weight(row)) for row in rows if row.bullish_pct is not None]
    bear_pairs = [(row.bearish_pct, _mention_weight(row)) for row in rows if row.bearish_pct is not None]
    return AttentionAggregate(
        symbol=symbol,
        session=session,
        platforms=sorted({row.platform for row in rows}),
        total_mentions=sum(row.mentions for row in rows),
        source_count=_source_count(rows),
        buzz_score=_weighted_mean(buzz_pairs) or 0.0,
        bullish_pct=_weighted_mean(bull_pairs),
        bearish_pct=_weighted_mean(bear_pairs),
        trend=_dominant_trend(rows),
        trend_history=_combined_trend_history(rows),
    )


def load_history(
    db: Database, symbols: Sequence[str], *, end: dt.date
) -> dict[str, list[AttentionAggregate]]:
    """Every stored Adanos session's cross-platform aggregate, per symbol,
    bounded by ``session <= end`` -- ONE query for the whole batch (F26),
    mirroring ``data.analyst.load_history``/``data.institutional
    .load_history``. Ascending by session per symbol.

    Feeds ``StrategyContext.adanos_history`` via ``data.context
    .DatabaseContextProvider._load``, truncated per-session in
    ``ContextBuilder.build`` exactly like ``sentiment_history``.

    Returns every symbol in ``symbols`` as a key, mapped to ``[]`` when no
    stored Adanos row at or before ``end`` exists.
    """
    ids = list(dict.fromkeys(symbols))  # de-dup, preserve order
    out: dict[str, list[AttentionAggregate]] = {symbol: [] for symbol in ids}
    if not ids:
        return out

    with db.read_session() as session:
        rows = (
            session.execute(
                select(AdanosSnapshotRow)
                .where(AdanosSnapshotRow.symbol.in_(ids), AdanosSnapshotRow.session <= end)
                .order_by(AdanosSnapshotRow.symbol, AdanosSnapshotRow.session)
            )
            .scalars()
            .all()
        )

    by_symbol_session: dict[tuple[str, dt.date], list[AdanosSnapshotRow]] = {}
    for row in rows:
        by_symbol_session.setdefault((row.symbol, row.session), []).append(row)

    for (symbol, sess), platform_rows in by_symbol_session.items():
        out.setdefault(symbol, []).append(_aggregate(symbol, sess, platform_rows))
    for series in out.values():
        series.sort(key=lambda agg: agg.session)
    return out


__all__ = ["AttentionAggregate", "load_history"]
