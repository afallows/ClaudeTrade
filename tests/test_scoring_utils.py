"""Tests for the shared score-accumulation helpers (ADR-0007 Decision 2)."""

from __future__ import annotations

import math

from claudetrade.strategies.scoring_utils import (
    ScoreAccumulator,
    band_credit,
    percentile_rank,
    ramp_down,
    ramp_up,
    zscore,
)


class TestRampUp:
    def test_below_low_is_zero(self):
        assert ramp_up(0.0, 1.0, 2.0) == 0.0

    def test_at_or_above_high_is_one(self):
        assert ramp_up(2.0, 1.0, 2.0) == 1.0
        assert ramp_up(5.0, 1.0, 2.0) == 1.0

    def test_midpoint_is_half(self):
        assert math.isclose(ramp_up(1.5, 1.0, 2.0), 0.5)

    def test_degenerate_bounds(self):
        assert ramp_up(5.0, 1.0, 1.0) == 1.0
        assert ramp_up(0.5, 1.0, 1.0) == 0.0


class TestRampDown:
    def test_at_or_below_low_is_one(self):
        assert ramp_down(0.0, 2.0, 1.0) == 1.0

    def test_above_high_is_zero(self):
        assert ramp_down(3.0, 2.0, 1.0) == 0.0

    def test_mirrors_ramp_up(self):
        # ramp_down(x, high, low) == 1 - ramp_up(x, low, high)
        for x in (0.2, 0.8, 1.5, 3.0):
            assert math.isclose(ramp_down(x, 2.0, 1.0), 1.0 - ramp_up(x, 1.0, 2.0))


class TestBandCredit:
    def test_full_credit_inside_band(self):
        assert band_credit(5.0, 3.0, 8.0, 1.0) == 1.0

    def test_tapers_below_band(self):
        assert 0.0 < band_credit(2.5, 3.0, 8.0, 1.0) < 1.0

    def test_tapers_above_band(self):
        assert 0.0 < band_credit(8.5, 3.0, 8.0, 1.0) < 1.0

    def test_zero_far_outside_band(self):
        assert band_credit(-100.0, 3.0, 8.0, 1.0) == 0.0
        assert band_credit(100.0, 3.0, 8.0, 1.0) == 0.0


class TestPercentileRank:
    def test_includes_self_and_ranks_correctly(self):
        history = [1.0, 2.0, 3.0, 4.0]
        # value equals the max of a 4-element sample including itself -> 1.0
        assert percentile_rank(history, 4.0) == 1.0

    def test_minimum_is_low_but_not_zero(self):
        history = [1.0, 2.0, 3.0, 4.0]
        assert percentile_rank(history, 1.0) == 0.25

    def test_empty_history_is_neutral(self):
        assert percentile_rank([], 5.0) == 0.5

    def test_nan_is_dropped(self):
        nan = float("nan")
        history = [1.0, 2.0, nan]
        assert percentile_rank(history, 2.0) == 1.0


class TestZScore:
    def test_mean_value_is_near_zero(self):
        history = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert abs(zscore(history, 3.0)) < 1e-9

    def test_short_history_is_neutral(self):
        assert zscore([1.0], 1.0) == 0.0
        assert zscore([], 1.0) == 0.0

    def test_zero_variance_is_neutral(self):
        assert zscore([5.0, 5.0, 5.0], 5.0) == 0.0

    def test_above_mean_is_positive(self):
        history = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert zscore(history, 5.0) > 0.0


class TestScoreAccumulator:
    def test_baseline_with_no_components(self):
        acc = ScoreAccumulator(baseline=30.0)
        assert acc.score == 30.0
        assert acc.breakdown == ""

    def test_add_scales_by_fraction(self):
        acc = ScoreAccumulator(baseline=0.0)
        acc.add("cond", 0.5, 20.0)
        assert acc.score == 10.0
        assert "cond=" in acc.breakdown

    def test_add_clamps_fraction_to_unit_interval(self):
        acc = ScoreAccumulator(baseline=0.0)
        acc.add("over", 5.0, 10.0)
        acc.add("under", -5.0, 10.0)
        assert acc.score == 10.0  # only the "over" component's clamped 10 points

    def test_score_clamped_to_0_100(self):
        acc = ScoreAccumulator(baseline=90.0)
        acc.add("big", 1.0, 50.0)
        assert acc.score == 100.0

        acc2 = ScoreAccumulator(baseline=5.0)
        acc2.penalty("big_penalty", -50.0)
        assert acc2.score == 0.0

    def test_penalty_is_unscaled(self):
        acc = ScoreAccumulator(baseline=50.0)
        acc.penalty("risk_flag", -12.5)
        assert acc.score == 37.5

    def test_summary_includes_threshold_and_breakdown(self):
        acc = ScoreAccumulator(baseline=10.0)
        acc.add("x", 0.5, 10.0)
        summary = acc.summary(threshold=48.0)
        assert "48.0" in summary
        assert "x=" in summary
