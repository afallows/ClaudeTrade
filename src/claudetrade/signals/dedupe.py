"""Read-time de-duplication of stock recommendations.

Two distinct duplication modes pollute a raw read of the signal ledger (the
ledger itself is append-only by design -- see ``signals.ledger``'s module
docstring for why nothing is ever deleted or merged there):

1. **Exact re-scan duplicates.** ``ledger.make_signal_id`` folds
   ``code_version``/``config_hash`` into the signal id (see that function's
   docstring), so re-scanning the SAME trading session after a code or
   config change mints a brand-new id for what is, in substance, the same
   recommendation: same symbol, same strategy, same session -- a score or
   plan that moved only because the underlying build changed. Two (or more)
   ledger rows for what a human reading the Screener would call "one idea".
2. **Cross-strategy overlap.** Every registered strategy scores every
   symbol independently, so a name with a genuinely strong setup often
   clears the bar under more than one strategy on the same session (e.g.
   both ``volume_breakout`` and ``sentiment_pullback``) with near-identical
   entry zones -- correct, since each strategy is a real, separately
   -reasoned thesis, but still noise when every surface just lists every
   ledger row unfiltered.

Both are read-time problems, not storage problems. The ledger's append-only,
integrity-checked guarantee (``signals.ledger.SignalLedger``) means every
signal that was ever generated stays there, forever, exactly as recorded --
that is what makes the reported win/loss ratio honest (see that module's
docstring). Collapsing duplicates for DISPLAY happens here instead: a pure
function with no I/O, so it is unit-testable in isolation and can be applied
identically by every surface that lists recommendations (MCP
``get_signals``, the web API's ``list_signals``, and the Streamlit Scanner).
A past incident (F26) came from those three surfaces disagreeing about
ordering because each reimplemented its own version of "the top N" --
sharing this one module is what stops read-time dedup from repeating that
mistake.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from claudetrade.domain import Signal, SignalStatus

#: The identity a raw ledger row collapses on for pass 1 (exact re-scan
#: duplicates): symbol, strategy, session and direction. Two rows sharing
#: this key are the same idea recorded twice under different ids, never a
#: coincidence to double-check -- see the module docstring.
_ExactDuplicateKey = tuple[str, str, dt.date, str]

#: The identity a raw ledger row collapses on for pass 2 (the group a
#: caller sees as one recommendation): symbol and direction. Long and short
#: theses for the same symbol are always kept separate -- they are opposite
#: bets, never "the same idea".
_GroupKey = tuple[str, str]


@dataclass(slots=True, frozen=True)
class CorroboratingSignal:
    """One OTHER strategy's signal for the same (symbol, direction, session)
    as a :class:`RecommendationGroup`'s representative.

    This is evidence that more than one independently-reasoned strategy
    agrees on the name -- not noise to hide. Every field here mirrors a
    field already on the underlying ``Signal``/effective-score contract
    every surface (MCP, web API, Streamlit) already exposes, so a caller
    never has to look anything up to render it.
    """

    signal_id: str
    strategy: str
    overall_score: float
    effective_score: float


@dataclass(slots=True, frozen=True)
class RecommendationGroup:
    """One collapsed (symbol, direction) recommendation row.

    ``signal``/``status`` are the REPRESENTATIVE: the newest session's
    highest-``effective_score`` signal in the group (ties broken by the
    newest ``created_at``) -- see :func:`collapse_recommendations`.
    ``corroborating`` lists every OTHER strategy's signal from that SAME
    representative session (never a stale session -- see the function
    docstring); it is empty when only one strategy fired for this
    symbol+direction on the representative session. ``duplicates_collapsed``
    counts exact re-scan duplicates folded away across the WHOLE group's
    representative session (every strategy's own (symbol, strategy,
    session) triple that had more than one ledger row) -- not just the
    representative's own strategy, so a caller sees the true amount of
    read-time collapsing behind this one row.
    """

    signal: Signal
    status: SignalStatus | None
    effective_score: float
    corroborating: tuple[CorroboratingSignal, ...] = field(default_factory=tuple)
    duplicates_collapsed: int = 0

    @property
    def corroborating_count(self) -> int:
        """How many OTHER strategies corroborate this recommendation.

        A group with 2+ distinct strategies (i.e. ``corroborating_count >=
        1``, since the representative itself is one strategy) is genuinely
        stronger evidence than a single strategy firing alone -- every
        surface exposes this so a caller can weight it without walking
        ``corroborating`` itself.
        """
        return len(self.corroborating)

    @property
    def corroborating_strategies(self) -> list[str]:
        """Just the strategy names, in ``corroborating``'s own (sorted) order."""
        return [c.strategy for c in self.corroborating]


def collapse_recommendations(
    signals_with_status: Sequence[tuple[Signal, SignalStatus | None]],
    effective_scores: Mapping[str, float],
) -> list[RecommendationGroup]:
    """Collapse raw ledger rows into one row per (symbol, direction).

    Pure and side-effect-free: no ledger access, no config, nothing but the
    rows and scores the caller already fetched -- so every surface calls
    this on top of its OWN existing batched reads rather than
    reimplementing the grouping rules three times (see the module
    docstring). Safe to call with zero, one, or many thousands of rows;
    running time is linear in ``len(signals_with_status)``.

    Two passes:

    1. **Exact re-scan duplicates.** Rows are grouped by ``(symbol,
       strategy, session, direction)`` -- the identity a human would call
       "the same idea" even though ``make_signal_id`` minted a new id for
       it (see ``signals.ledger.make_signal_id``). Only the newest
       ``created_at`` row in each such group survives; the rest are folded
       into that group's ``duplicates_collapsed`` count. This pass never
       compares scores or plans for equality -- a routine re-scan after a
       code/config change is treated as a refinement of the same idea, not
       a fork of it, so the newest write always wins outright.
    2. **Cross-strategy grouping.** The survivors of pass 1 are grouped by
       ``(symbol, direction)`` -- long and short recommendations for the
       same symbol are always kept as separate groups, never merged (they
       are opposite theses). Within each group, the REPRESENTATIVE session
       is the newest session present; only that session's rows count from
       here on, so a stale prior-session signal for the same symbol+
       direction never leaks into the current recommendation or its
       corroboration (which would otherwise look like two strategies
       agreeing today when one of them actually fired yesterday). Within
       the representative session, the representative row is the highest
       ``effective_score`` (falling back to ``overall_score`` when a
       signal id has no entry in ``effective_scores``, matching the "equals
       overall_score exactly when there is no research" contract every
       surface already documents), ties broken by the newest
       ``created_at``. Every other representative-session row becomes a
       :class:`CorroboratingSignal`.

    Args:
        signals_with_status: Exactly the ``(Signal, SignalStatus | None)``
            pairs a caller's own ledger read already produced (e.g.
            ``SignalLedger.list_with_status``/``recent_with_status``) --
            this function does not fetch anything itself.
        effective_scores: ``signal_id -> effective_score`` for every id in
            ``signals_with_status`` that has one (i.e. has an accepted
            research revision). An id with no entry falls back to that
            signal's own ``overall_score``.

    Returns:
        One :class:`RecommendationGroup` per surviving (symbol, direction)
        pair, sorted by representative ``effective_score`` descending (ties
        broken by ``signal_id`` for a deterministic, repeatable order across
        calls -- the same discipline ``SignalLedger.list_with_status`` uses
        for its own SQL ordering). Stable: the same input always produces
        the same output in the same order, regardless of input order.
    """

    def effective_score(sig: Signal) -> float:
        return effective_scores.get(sig.signal_id, sig.overall_score)

    # --- Pass 1: exact re-scan duplicates -----------------------------
    by_exact_key: dict[_ExactDuplicateKey, list[tuple[Signal, SignalStatus | None]]] = (
        defaultdict(list)
    )
    for item in signals_with_status:
        sig, _status = item
        by_exact_key[(sig.symbol, sig.strategy, sig.session, str(sig.direction))].append(item)

    survivors: list[tuple[Signal, SignalStatus | None]] = []
    duplicates_by_exact_key: dict[_ExactDuplicateKey, int] = {}
    for key, items in by_exact_key.items():
        if len(items) == 1:
            survivors.append(items[0])
            duplicates_by_exact_key[key] = 0
            continue
        newest = max(items, key=lambda it: (it[0].created_at, it[0].signal_id))
        survivors.append(newest)
        duplicates_by_exact_key[key] = len(items) - 1

    # --- Pass 2: cross-strategy grouping by (symbol, direction) --------
    by_group_key: dict[_GroupKey, list[tuple[Signal, SignalStatus | None]]] = defaultdict(list)
    for item in survivors:
        sig, _status = item
        by_group_key[(sig.symbol, str(sig.direction))].append(item)

    groups: list[RecommendationGroup] = []
    for items in by_group_key.values():
        newest_session = max(it[0].session for it in items)
        # Only the representative session's rows count from here -- a stale
        # prior-session row for the same symbol+direction must never leak
        # into the current recommendation or its corroboration.
        current_session_items = [it for it in items if it[0].session == newest_session]

        representative_item = max(
            current_session_items,
            key=lambda it: (effective_score(it[0]), it[0].created_at, it[0].signal_id),
        )
        representative_signal, representative_status = representative_item

        corroborating: list[CorroboratingSignal] = []
        duplicates_collapsed = 0
        for sig, _status in current_session_items:
            duplicates_collapsed += duplicates_by_exact_key.get(
                (sig.symbol, sig.strategy, sig.session, str(sig.direction)), 0
            )
            if sig.signal_id == representative_signal.signal_id:
                continue
            corroborating.append(
                CorroboratingSignal(
                    signal_id=sig.signal_id,
                    strategy=sig.strategy,
                    overall_score=sig.overall_score,
                    effective_score=effective_score(sig),
                )
            )
        # Deterministic order: strongest corroboration first, then strategy
        # name so two calls over the same input never disagree.
        corroborating.sort(key=lambda c: (-c.effective_score, c.strategy))

        groups.append(
            RecommendationGroup(
                signal=representative_signal,
                status=representative_status,
                effective_score=effective_score(representative_signal),
                corroborating=tuple(corroborating),
                duplicates_collapsed=duplicates_collapsed,
            )
        )

    groups.sort(key=lambda g: (-g.effective_score, g.signal.signal_id))
    return groups


__all__ = ["CorroboratingSignal", "RecommendationGroup", "collapse_recommendations"]
