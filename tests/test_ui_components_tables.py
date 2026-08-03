"""Unit tests for ``claudetrade.ui.components.tables``'s pure shaping logic."""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.signals.engine import RejectedCandidate
from claudetrade.ui.components.tables import (
    CANDIDATE_COLUMN_ORDER,
    apply_candidate_filters,
    rejected_dataframe,
    signals_column_config,
    signals_dataframe,
)
from claudetrade.ui.data_access import ResearchOverlay


def test_signals_dataframe_has_expected_columns_and_row_count(make_signal):
    signals = [make_signal(symbol="AAA"), make_signal(symbol="BBB", direction=Direction.SHORT)]
    df = signals_dataframe(signals)
    assert len(df) == 2
    for col in CANDIDATE_COLUMN_ORDER:
        assert col in df.columns
    assert "Signal ID" in df.columns


def test_signals_dataframe_direction_label_uses_diverging_pair(make_signal):
    df = signals_dataframe([make_signal(direction=Direction.LONG)])
    assert "LONG" in df.iloc[0]["Direction"]


def test_signals_dataframe_confidence_is_percentage(make_signal):
    df = signals_dataframe([make_signal(confidence=0.734)])
    assert df.iloc[0]["Confidence %"] == 73.4


def test_signals_dataframe_status_lookup_falls_back_to_unknown(make_signal):
    sig = make_signal()
    df = signals_dataframe([sig], status_by_id={})
    assert "Unknown" in df.iloc[0]["Status"]


def test_signals_dataframe_target_1_none_when_no_targets(make_signal):
    sig = make_signal(targets=[])
    df = signals_dataframe([sig])
    assert df.iloc[0]["Target 1"] is None


def test_signals_dataframe_without_research_matches_prior_shape(make_signal):
    """Backward-compat guard: omitting ``research`` (every existing caller,
    e.g. the Dashboard's top-candidate tables) must keep the exact same
    column set and ``Score`` == ``overall_score`` as before this field was
    added."""
    df = signals_dataframe([make_signal(overall_score=65.0)])
    assert "Research" not in df.columns
    assert df.iloc[0]["Score"] == 65.0


def test_signals_dataframe_score_is_effective_when_research_given(make_signal):
    sig = make_signal(overall_score=60.0)
    research = {
        sig.signal_id: ResearchOverlay(effective_score=64.0, has_research=True, latest={"revision": 1})
    }
    df = signals_dataframe([sig], research=research)
    assert df.iloc[0]["Score"] == 64.0
    assert "engine: 60" in df.iloc[0]["Research"]


def test_signals_dataframe_research_column_blank_without_a_revision(make_signal):
    sig = make_signal(overall_score=60.0)
    research = {sig.signal_id: ResearchOverlay(effective_score=60.0, has_research=False, latest=None)}
    df = signals_dataframe([sig], research=research)
    assert df.iloc[0]["Score"] == 60.0
    assert df.iloc[0]["Research"] == ""


def test_signals_dataframe_without_corroborating_matches_prior_shape(make_signal):
    """Backward-compat guard: omitting ``corroborating`` (every existing
    caller before dedup) keeps the exact same column set as before."""
    df = signals_dataframe([make_signal()])
    assert "Corroborating" not in df.columns


def test_signals_dataframe_corroborating_column_lists_other_strategies(make_signal):
    sig = make_signal(strategy="volume_breakout")
    df = signals_dataframe([sig], corroborating={sig.signal_id: ["sentiment_pullback"]})
    assert df.iloc[0]["Corroborating"] == "+sentiment_pullback"


def test_signals_dataframe_corroborating_column_blank_when_no_siblings(make_signal):
    sig = make_signal(strategy="volume_breakout")
    df = signals_dataframe([sig], corroborating={sig.signal_id: []})
    assert df.iloc[0]["Corroborating"] == ""


def test_signals_column_config_covers_progress_and_price_columns():
    config = signals_column_config()
    assert "Score" in config
    assert "Confidence %" in config
    assert "Entry Low" in config
    assert "Session" in config


def test_rejected_dataframe_orders_score_before_gates():
    rejected = [
        RejectedCandidate("AAA", "sentiment_breakout", "gates", ["min_price"]),
        RejectedCandidate("BBB", "sentiment_breakout", "score", ["score 40 below 55"]),
    ]
    df = rejected_dataframe(rejected)
    assert list(df["Symbol"]) == ["BBB", "AAA"]


def test_rejected_dataframe_empty_input():
    df = rejected_dataframe([])
    assert df.empty
    assert "Symbol" in df.columns


def test_rejected_dataframe_joins_multiple_reasons():
    rejected = [RejectedCandidate("AAA", "strat", "gates", ["reason one", "reason two"])]
    df = rejected_dataframe(rejected)
    assert df.iloc[0]["Reasons"] == "reason one; reason two"


def test_apply_candidate_filters_by_direction(make_signal):
    signals = [make_signal(direction=Direction.LONG), make_signal(direction=Direction.SHORT)]
    filtered = apply_candidate_filters(signals, directions={"long"})
    assert len(filtered) == 1
    assert filtered[0].direction is Direction.LONG


def test_apply_candidate_filters_by_score_and_confidence(make_signal):
    signals = [
        make_signal(overall_score=80.0, confidence=0.9),
        make_signal(overall_score=40.0, confidence=0.9),
        make_signal(overall_score=80.0, confidence=0.2),
    ]
    filtered = apply_candidate_filters(signals, min_score=55.0, min_confidence=0.5)
    assert len(filtered) == 1


def test_apply_candidate_filters_by_strategy(make_signal):
    signals = [make_signal(strategy="a"), make_signal(strategy="b")]
    filtered = apply_candidate_filters(signals, strategies={"a"})
    assert len(filtered) == 1
    assert filtered[0].strategy == "a"


def test_apply_candidate_filters_by_days_to_earnings(make_signal):
    signals = [
        make_signal(days_to_earnings=2),
        make_signal(days_to_earnings=20),
        make_signal(days_to_earnings=None),
    ]
    filtered = apply_candidate_filters(signals, max_days_to_earnings=5)
    assert len(filtered) == 1
    assert filtered[0].days_to_earnings == 2


def test_apply_candidate_filters_no_filters_returns_all(make_signal):
    signals = [make_signal(), make_signal(symbol="BBB")]
    assert len(apply_candidate_filters(signals)) == 2
