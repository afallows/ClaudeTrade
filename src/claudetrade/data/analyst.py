"""Reads and diffs over the stored TipRanks analyst-sentiment snapshots.

Two things live here, deliberately kept apart from ``data.ingest`` (the
write path) and ``providers.market.tipranks_analyst`` (the parser):

* :func:`latest_and_previous_snapshots` -- the ONE batched read every caller
  wanting "this symbol's analyst picture" needs (UI, MCP tool, a future
  webapi route). Two queries total, however many symbols are asked for --
  see its own docstring for why this matters (F26).
* :func:`analyst_delta` -- a pure function over two already-loaded
  ``domain.AnalystSnapshot`` objects. No database access, no I/O; trivially
  unit-testable and reusable by any caller that already has both snapshots
  in hand.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select

from claudetrade.db.models import AnalystSnapshotRow
from claudetrade.db.session import Database
from claudetrade.domain import AnalystConsensusPoint, AnalystRatingAction, AnalystSnapshot


def _row_to_snapshot(row: AnalystSnapshotRow) -> AnalystSnapshot:
    """Flatten one stored row back into the domain shape -- built while the
    session that loaded it is still open, so no lazy-load can fire later."""
    consensus_over_time = [
        AnalystConsensusPoint(
            date=dt.date.fromisoformat(p["date"]) if isinstance(p.get("date"), str) else p["date"],
            buy=int(p.get("buy", 0)),
            hold=int(p.get("hold", 0)),
            sell=int(p.get("sell", 0)),
            consensus=p.get("consensus"),
            price_target=p.get("price_target"),
        )
        for p in (row.consensus_over_time or [])
        if isinstance(p, dict) and p.get("date")
    ]
    recent_rating_actions = [
        AnalystRatingAction(
            date=dt.date.fromisoformat(a["date"]) if isinstance(a.get("date"), str) else a["date"],
            firm=a.get("firm", ""),
            analyst_name=a.get("analyst_name", ""),
            rating_id=a.get("rating_id"),
            rating_label=a.get("rating_label"),
            action_id=a.get("action_id"),
            action_label=a.get("action_label"),
            price_target=a.get("price_target"),
            old_price_target=a.get("old_price_target"),
            analyst_stars=a.get("analyst_stars"),
            analyst_success_rate=a.get("analyst_success_rate"),
            included_in_consensus=bool(a.get("included_in_consensus", False)),
        )
        for a in (row.recent_rating_actions or [])
        if isinstance(a, dict) and a.get("date")
    ]
    return AnalystSnapshot(
        symbol=row.symbol,
        as_of_session=row.session,
        consensus_rating=row.consensus_rating,
        buy_count=row.buy_count,
        hold_count=row.hold_count,
        sell_count=row.sell_count,
        consensus_rate=row.consensus_rate,
        price_target_mean=row.price_target_mean,
        price_target_high=row.price_target_high,
        price_target_low=row.price_target_low,
        price_target_currency=row.price_target_currency,
        analyst_count=row.analyst_count,
        consensus_over_time=consensus_over_time,
        recent_rating_actions=recent_rating_actions,
        last_eps_surprise_pct=row.last_eps_surprise_pct,
        next_earnings_estimate_eps=row.next_earnings_estimate_eps,
        fetched_at=row.fetched_at,
    )


def snapshot_to_row_fields(snapshot: AnalystSnapshot) -> dict[str, object]:
    """The column values ``data.ingest.DataIngestor.ingest_analyst_snapshots``
    writes onto an ``AnalystSnapshotRow`` -- the write-side mirror of
    :func:`_row_to_snapshot`, kept here so the two stay in lockstep instead
    of drifting if a field is added to one but not the other.
    """
    return {
        "consensus_rating": snapshot.consensus_rating,
        "buy_count": snapshot.buy_count,
        "hold_count": snapshot.hold_count,
        "sell_count": snapshot.sell_count,
        "consensus_rate": snapshot.consensus_rate,
        "price_target_mean": snapshot.price_target_mean,
        "price_target_high": snapshot.price_target_high,
        "price_target_low": snapshot.price_target_low,
        "price_target_currency": snapshot.price_target_currency,
        "analyst_count": snapshot.analyst_count,
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
            {
                "date": a.date.isoformat(),
                "firm": a.firm,
                "analyst_name": a.analyst_name,
                "rating_id": a.rating_id,
                "rating_label": a.rating_label,
                "action_id": a.action_id,
                "action_label": a.action_label,
                "price_target": a.price_target,
                "old_price_target": a.old_price_target,
                "analyst_stars": a.analyst_stars,
                "analyst_success_rate": a.analyst_success_rate,
                "included_in_consensus": a.included_in_consensus,
            }
            for a in snapshot.recent_rating_actions
        ],
        "last_eps_surprise_pct": snapshot.last_eps_surprise_pct,
        "next_earnings_estimate_eps": snapshot.next_earnings_estimate_eps,
        "fetched_at": snapshot.fetched_at,
    }


def latest_and_previous_snapshots(
    db: Database, symbols: Sequence[str]
) -> dict[str, tuple[AnalystSnapshot | None, AnalystSnapshot | None]]:
    """The latest and second-latest stored snapshot per symbol, in TWO
    queries total -- never one query per symbol (F26: a per-signal_id loop
    over ``ResearchLedger.research_history`` was exactly this class of bug
    in production; ``ResearchLedger.latest_research_revisions`` is the
    pattern this mirrors).

    Returns every symbol in ``symbols`` as a key. A symbol with no stored
    snapshot at all maps to ``(None, None)``; one with exactly one stored
    session maps to ``(latest, None)``.

    Query 1 finds each symbol's latest session (a ``GROUP BY`` over
    ``symbols`` only) and joins back for the full row. Query 2 finds each
    symbol's latest session STRICTLY BEFORE its own latest (a second
    ``GROUP BY``, self-joined against query 1's subquery) and joins back for
    that row -- so "previous" always means "the prior stored session", not
    merely "any other row", even if a symbol has more than two sessions on
    file.
    """
    ids = list(dict.fromkeys(symbols))  # de-dup, preserve order
    out: dict[str, tuple[AnalystSnapshot | None, AnalystSnapshot | None]] = dict.fromkeys(
        ids, (None, None)
    )
    if not ids:
        return out

    with db.read_session() as session:
        latest_sub = (
            select(
                AnalystSnapshotRow.symbol.label("symbol"),
                func.max(AnalystSnapshotRow.session).label("max_session"),
            )
            .where(AnalystSnapshotRow.symbol.in_(ids))
            .group_by(AnalystSnapshotRow.symbol)
            .subquery()
        )
        latest_rows = (
            session.execute(
                select(AnalystSnapshotRow).join(
                    latest_sub,
                    (latest_sub.c.symbol == AnalystSnapshotRow.symbol)
                    & (latest_sub.c.max_session == AnalystSnapshotRow.session),
                )
            )
            .scalars()
            .all()
        )
        latest_by_symbol = {row.symbol: _row_to_snapshot(row) for row in latest_rows}

        previous_sub = (
            select(
                AnalystSnapshotRow.symbol.label("symbol"),
                func.max(AnalystSnapshotRow.session).label("prev_session"),
            )
            .join(
                latest_sub,
                (latest_sub.c.symbol == AnalystSnapshotRow.symbol)
                & (AnalystSnapshotRow.session < latest_sub.c.max_session),
            )
            .group_by(AnalystSnapshotRow.symbol)
            .subquery()
        )
        previous_rows = (
            session.execute(
                select(AnalystSnapshotRow).join(
                    previous_sub,
                    (previous_sub.c.symbol == AnalystSnapshotRow.symbol)
                    & (previous_sub.c.prev_session == AnalystSnapshotRow.session),
                )
            )
            .scalars()
            .all()
        )
        previous_by_symbol = {row.symbol: _row_to_snapshot(row) for row in previous_rows}

    for symbol in ids:
        out[symbol] = (latest_by_symbol.get(symbol), previous_by_symbol.get(symbol))
    return out


def load_history(
    db: Database, symbols: Sequence[str], *, end: dt.date
) -> dict[str, list[AnalystSnapshot]]:
    """Every stored ``AnalystSnapshot`` with ``session <= end`` for each of
    ``symbols``, ascending by session -- ONE query for the whole batch (F26),
    never a per-symbol loop. Feeds ``data.context.DatabaseContextProvider
    ._load`` so ``StrategyContext.analyst_history`` can be truncated
    per-session exactly like ``sentiment_history`` already is (see
    ``data.context.ContextBuilder``'s module docstring): loading bounded by
    ``end`` (the scan's own end-of-range date) rather than "latest ever" is
    what keeps a backtest built for an earlier session from ever seeing a
    snapshot stored after it.

    Returns every symbol in ``symbols`` as a key, mapped to ``[]`` when no
    stored snapshot at or before ``end`` exists.
    """
    ids = list(dict.fromkeys(symbols))  # de-dup, preserve order
    out: dict[str, list[AnalystSnapshot]] = {symbol: [] for symbol in ids}
    if not ids:
        return out

    with db.read_session() as session:
        rows = (
            session.execute(
                select(AnalystSnapshotRow)
                .where(AnalystSnapshotRow.symbol.in_(ids), AnalystSnapshotRow.session <= end)
                .order_by(AnalystSnapshotRow.symbol, AnalystSnapshotRow.session)
            )
            .scalars()
            .all()
        )
        for row in rows:
            out.setdefault(row.symbol, []).append(_row_to_snapshot(row))
    return out


@dataclass(slots=True, frozen=True)
class AnalystDelta:
    """What changed between two consecutive stored ``AnalystSnapshot`` rows
    for one symbol -- a pure, read-time comparison, never stored itself.

    Every ``*_change`` field is ``None`` when either side of the comparison
    is missing the underlying value (no previous snapshot at all, or one
    side genuinely had no data for that field) -- never a fabricated zero
    standing in for "unknown", the same absent-vs-neutral discipline the
    rest of this codebase's snapshot tables already follow (see
    ``db.models.AdanosSnapshotRow.sentiment_score``'s docstring for the same
    principle applied elsewhere).
    """

    symbol: str
    current_session: dt.date
    previous_session: dt.date | None
    has_previous: bool
    buy_count_change: int | None = None
    hold_count_change: int | None = None
    sell_count_change: int | None = None
    #: ``current.analyst_count - previous.analyst_count`` -- a positive value
    #: is new ranked-analyst coverage, a negative one is analysts dropping
    #: coverage or falling out of the ranked subset.
    coverage_change: int | None = None
    consensus_rating_change: int | None = None
    price_target_mean_change: float | None = None
    price_target_mean_change_pct: float | None = None
    #: Rating actions from the CURRENT snapshot's own ``recent_rating_actions``
    #: dated strictly after ``previous_session`` -- i.e. actions that could
    #: not have been reflected in the previous snapshot. Empty (not "all of
    #: current's actions") when there is no previous snapshot at all: with
    #: no prior session to compare against, nothing is meaningfully "new"
    #: yet, and dumping the whole ``recent_rating_actions`` list here would
    #: silently duplicate what ``get_analyst_sentiment``'s own
    #: ``recent_rating_actions`` field already reports.
    new_rating_actions: list[AnalystRatingAction] = field(default_factory=list)


def analyst_delta(
    current: AnalystSnapshot, previous: AnalystSnapshot | None
) -> AnalystDelta:
    """Compare ``current`` against ``previous`` (the prior stored session,
    or ``None`` when this is the first snapshot this installation has ever
    stored for the symbol). Pure function -- no I/O, no database access.
    """
    if previous is None:
        return AnalystDelta(
            symbol=current.symbol,
            current_session=current.as_of_session,
            previous_session=None,
            has_previous=False,
        )

    def _int_delta(a: int | None, b: int | None) -> int | None:
        return (a - b) if a is not None and b is not None else None

    pt_change: float | None = None
    pt_change_pct: float | None = None
    if current.price_target_mean is not None and previous.price_target_mean is not None:
        pt_change = current.price_target_mean - previous.price_target_mean
        if previous.price_target_mean:
            pt_change_pct = (pt_change / previous.price_target_mean) * 100.0

    new_actions = [
        action
        for action in current.recent_rating_actions
        if action.date > previous.as_of_session
    ]

    return AnalystDelta(
        symbol=current.symbol,
        current_session=current.as_of_session,
        previous_session=previous.as_of_session,
        has_previous=True,
        buy_count_change=_int_delta(current.buy_count, previous.buy_count),
        hold_count_change=_int_delta(current.hold_count, previous.hold_count),
        sell_count_change=_int_delta(current.sell_count, previous.sell_count),
        coverage_change=_int_delta(current.analyst_count, previous.analyst_count),
        consensus_rating_change=_int_delta(current.consensus_rating, previous.consensus_rating),
        price_target_mean_change=pt_change,
        price_target_mean_change_pct=pt_change_pct,
        new_rating_actions=new_actions,
    )


__all__ = [
    "AnalystDelta",
    "analyst_delta",
    "latest_and_previous_snapshots",
    "load_history",
    "snapshot_to_row_fields",
]
