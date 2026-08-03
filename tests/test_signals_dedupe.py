"""Unit tests for ``claudetrade.signals.dedupe.collapse_recommendations``.

Pure-function tests: no database, no pipeline -- only ``Signal`` objects
built via the ``make_signal`` factory fixture (``tests/conftest.py``) and, in
a few cases, ``dataclasses.replace`` to force a specific ``signal_id``/
``created_at``/``session`` the factory itself can't parameterise directly
(``make_signal`` derives ``created_at`` from ``session`` alone, so two
same-session signals share a timestamp unless overridden here).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from claudetrade.domain import Direction, SignalStatus
from claudetrade.signals.dedupe import collapse_recommendations


def _at(session: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(session.year, session.month, session.day, hour, minute, tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# Exact re-scan duplicates
# --------------------------------------------------------------------------


def test_exact_rescan_duplicate_collapses_to_the_newest_created_at(make_signal) -> None:
    """The reported bug: re-scanning the same session after a code/config
    change mints a new signal_id for the same idea. Only the newest survives,
    and the group reports exactly one duplicate collapsed."""
    session = dt.date(2026, 7, 31)
    early = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=70.0),
        signal_id="early",
        created_at=_at(session, 5, 39),
    )
    late = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=71.0),
        signal_id="late",
        created_at=_at(session, 12, 0),
    )

    groups = collapse_recommendations(
        [(early, SignalStatus.ACTIONABLE), (late, SignalStatus.ACTIONABLE)], {}
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "late"
    assert group.duplicates_collapsed == 1
    assert group.corroborating == ()
    assert group.corroborating_count == 0


def test_three_exact_rescans_collapse_to_one_with_two_duplicates_collapsed(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    items = []
    for i, hour in enumerate((5, 9, 12)):
        sig = dataclasses.replace(
            make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=70.0 + i),
            signal_id=f"scan{i}",
            created_at=_at(session, hour),
        )
        items.append((sig, None))

    groups = collapse_recommendations(items, {})

    assert len(groups) == 1
    assert groups[0].signal.signal_id == "scan2"  # the 12:00 scan, latest
    assert groups[0].duplicates_collapsed == 2


# --------------------------------------------------------------------------
# Cross-strategy grouping + representative selection
# --------------------------------------------------------------------------


def test_cross_strategy_overlap_groups_into_one_row_with_corroboration(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    volume = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="volume_breakout", session=session, overall_score=75.53),
        signal_id="volume",
    )
    sentiment = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="sentiment_pullback", session=session, overall_score=74.10),
        signal_id="sentiment",
    )

    groups = collapse_recommendations(
        [(volume, SignalStatus.ACTIONABLE), (sentiment, SignalStatus.APPROACHING)], {}
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "volume"  # higher overall_score, no research
    assert group.corroborating_count == 1
    assert group.corroborating_strategies == ["sentiment_pullback"]
    [corroborator] = group.corroborating
    assert corroborator.signal_id == "sentiment"
    assert corroborator.overall_score == 74.10
    assert corroborator.effective_score == 74.10  # no research -> falls back to overall_score


def test_representative_choice_uses_effective_score_not_overall_score(make_signal) -> None:
    """A research-boosted lower overall_score can still win representative
    status -- the whole point of ``effective_score`` re-ranking."""
    session = dt.date(2026, 7, 31)
    high_raw = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="volume_breakout", session=session, overall_score=75.0),
        signal_id="high_raw",
    )
    research_boosted = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="sentiment_pullback", session=session, overall_score=68.0),
        signal_id="research_boosted",
    )

    groups = collapse_recommendations(
        [(high_raw, None), (research_boosted, None)],
        {"research_boosted": 80.0},
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "research_boosted"
    assert group.effective_score == 80.0
    [corroborator] = group.corroborating
    assert corroborator.signal_id == "high_raw"
    assert corroborator.overall_score == 75.0
    assert corroborator.effective_score == 75.0  # no entry in effective_scores -> fallback


def test_representative_tie_break_by_newest_created_at(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    earlier = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=70.0),
        signal_id="earlier",
        created_at=_at(session, 9),
    )
    later = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="sentiment_pullback", session=session, overall_score=70.0),
        signal_id="later",
        created_at=_at(session, 16),
    )

    groups = collapse_recommendations([(earlier, None), (later, None)], {})

    assert groups[0].signal.signal_id == "later"


# --------------------------------------------------------------------------
# Session preference: newest session wins, stale sessions excluded from
# corroboration
# --------------------------------------------------------------------------


def test_newest_session_wins_even_over_a_higher_scoring_stale_session(make_signal) -> None:
    old_session = dt.date(2026, 7, 30)
    new_session = dt.date(2026, 7, 31)
    stale = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="sentiment_pullback", session=old_session, overall_score=95.0),
        signal_id="stale",
    )
    current = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="volume_breakout", session=new_session, overall_score=60.0),
        signal_id="current",
    )

    groups = collapse_recommendations([(stale, None), (current, None)], {})

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "current"
    # The stale prior-session signal must not leak into corroboration --
    # it would misleadingly look like two strategies agreeing today.
    assert group.corroborating == ()
    assert group.duplicates_collapsed == 0


def test_stale_session_sibling_never_mixed_into_corroboration(make_signal) -> None:
    old_session = dt.date(2026, 7, 30)
    new_session = dt.date(2026, 7, 31)
    stale_sibling = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="mean_reversion", session=old_session, overall_score=50.0),
        signal_id="stale_sibling",
    )
    rep = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="volume_breakout", session=new_session, overall_score=70.0),
        signal_id="rep",
    )
    current_sibling = dataclasses.replace(
        make_signal(symbol="LPLA", strategy="sentiment_pullback", session=new_session, overall_score=65.0),
        signal_id="current_sibling",
    )

    groups = collapse_recommendations(
        [(stale_sibling, None), (rep, None), (current_sibling, None)], {}
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "rep"
    assert [c.signal_id for c in group.corroborating] == ["current_sibling"]


# --------------------------------------------------------------------------
# Direction split
# --------------------------------------------------------------------------


def test_long_and_short_same_symbol_are_not_merged(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    long_sig = dataclasses.replace(
        make_signal(symbol="TSLA", direction=Direction.LONG, session=session, overall_score=70.0),
        signal_id="long",
    )
    short_sig = dataclasses.replace(
        make_signal(symbol="TSLA", direction=Direction.SHORT, session=session, overall_score=65.0),
        signal_id="short",
    )

    groups = collapse_recommendations([(long_sig, None), (short_sig, None)], {})

    assert len(groups) == 2
    ids = {g.signal.signal_id for g in groups}
    assert ids == {"long", "short"}
    for group in groups:
        assert group.corroborating == ()


# --------------------------------------------------------------------------
# Combined scenario mirroring the MCP/web API fixture: two identical-content
# rescans plus one cross-strategy sibling -> one row.
# --------------------------------------------------------------------------


def test_rescan_duplicates_plus_cross_strategy_sibling_collapse_to_one_row(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    dup_early = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=60.0),
        signal_id="dup_early",
        created_at=_at(session, 5, 39),
    )
    dup_late = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="volume_breakout", session=session, overall_score=61.0),
        signal_id="dup_late",
        created_at=_at(session, 12, 0),
    )
    sibling = dataclasses.replace(
        make_signal(symbol="SPSC", strategy="sentiment_pullback", session=session, overall_score=90.0),
        signal_id="sibling",
        created_at=_at(session, 12, 0),
    )

    groups = collapse_recommendations([(dup_early, None), (dup_late, None), (sibling, None)], {})

    assert len(groups) == 1
    group = groups[0]
    assert group.signal.signal_id == "sibling"  # highest score becomes representative
    assert group.duplicates_collapsed == 1
    assert [c.signal_id for c in group.corroborating] == ["dup_late"]
    assert group.corroborating_strategies == ["volume_breakout"]


# --------------------------------------------------------------------------
# Stable ordering
# --------------------------------------------------------------------------


def test_groups_are_ordered_by_representative_effective_score_descending(make_signal) -> None:
    session = dt.date(2026, 7, 31)
    low = dataclasses.replace(
        make_signal(symbol="LOW", session=session, overall_score=40.0), signal_id="low"
    )
    high = dataclasses.replace(
        make_signal(symbol="HIGH", session=session, overall_score=90.0), signal_id="high"
    )
    mid = dataclasses.replace(
        make_signal(symbol="MID", session=session, overall_score=60.0), signal_id="mid"
    )

    groups = collapse_recommendations([(low, None), (high, None), (mid, None)], {})
    assert [g.signal.symbol for g in groups] == ["HIGH", "MID", "LOW"]

    # Deterministic: the same input, given in a different order, still
    # produces the same output order.
    groups_reordered_input = collapse_recommendations([(mid, None), (low, None), (high, None)], {})
    assert [g.signal.symbol for g in groups_reordered_input] == ["HIGH", "MID", "LOW"]


def test_empty_input_returns_empty_list() -> None:
    assert collapse_recommendations([], {}) == []
