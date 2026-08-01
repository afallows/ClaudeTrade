"""Tests for the market-signal adoption package's new pattern features.

Covers ``features/patterns.py``'s ``gap_continuation``, ``pivot_points``,
``fibonacci_levels``, ``round_number_level``, ``level_confluence_count`` and
``volume_divergence``. Each gets: a hand-computed value check on a synthetic
bar sequence, a causality check via ``indicators.assert_causal`` (the same
mechanism ``tests/test_indicators.py`` uses for every indicator/pattern
function in this codebase), and a warm-up/degrade-not-crash check.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from claudetrade.features import indicators, patterns


def _flat_bars(n: int, price: float = 100.0, volume: float = 1_000_000.0) -> pd.DataFrame:
    """``n`` quiet bars: tight range, flat volume -- a clean baseline for a
    later breakout/gap to stand out against."""
    return pd.DataFrame(
        {
            "open": [price * 0.999] * n,
            "high": [price * 1.005] * n,
            "low": [price * 0.995] * n,
            "close": [price] * n,
            "volume": [volume] * n,
        }
    )


class TestGapContinuation:
    """Gap-and-go confirmation, mirroring detect_failed_breakout's shape."""

    def test_up_continuation_marked_on_confirmation_day_not_event_day(self):
        bars = _flat_bars(25)
        # Breakout day (index 25): closes well above the ~100.5 prior high on
        # heavy volume.
        breakout = pd.DataFrame(
            {
                "open": [104.0],
                "high": [106.0],
                "low": [103.5],
                "close": [105.0],
                "volume": [2_000_000.0],
            }
        )
        # Confirmation day (index 26): opens well above both the level and
        # the breakout bar's own close, with a real overnight gap.
        confirm = pd.DataFrame(
            {"open": [110.0], "high": [111.0], "low": [109.0], "close": [110.5], "volume": [1_200_000.0]}
        )
        bars = pd.concat([bars, breakout, confirm], ignore_index=True)

        result = patterns.gap_continuation(bars, direction="up", lookback=20, confirm_within_bars=3)

        assert bool(result.iloc[25]) is False  # event day itself never marked
        assert bool(result.iloc[26]) is True  # confirmation day marked

    def test_up_continuation_not_marked_without_a_real_gap(self):
        bars = _flat_bars(25)
        breakout = pd.DataFrame(
            {"open": [104.0], "high": [106.0], "low": [103.5], "close": [105.0], "volume": [2_000_000.0]}
        )
        # Next day opens BELOW the prior close (no gap up), even though it
        # later trades higher intraday -- must not count as a gap continuation.
        drift = pd.DataFrame(
            {"open": [104.5], "high": [107.0], "low": [104.0], "close": [106.0], "volume": [1_200_000.0]}
        )
        bars = pd.concat([bars, breakout, drift], ignore_index=True)

        result = patterns.gap_continuation(bars, direction="up", lookback=20, confirm_within_bars=3)

        assert not result.any()

    def test_down_continuation_marked_after_a_failed_breakout(self):
        bars = _flat_bars(25)
        breakout = pd.DataFrame(
            {"open": [104.0], "high": [106.0], "low": [103.5], "close": [105.0], "volume": [2_000_000.0]}
        )
        # Failure-confirmation day: closes back below the ~100.5 level.
        failure = pd.DataFrame(
            {"open": [104.0], "high": [104.5], "low": [98.0], "close": [99.0], "volume": [1_500_000.0]}
        )
        # Breakdown-continuation day: gaps down hard, well below the level.
        continuation = pd.DataFrame(
            {"open": [90.0], "high": [91.0], "low": [88.0], "close": [89.0], "volume": [1_500_000.0]}
        )
        bars = pd.concat([bars, breakout, failure, continuation], ignore_index=True)

        result = patterns.gap_continuation(bars, direction="down", lookback=20, confirm_within_bars=3)

        assert bool(result.iloc[26]) is False  # the failure-confirmation day itself
        assert bool(result.iloc[27]) is True  # the breakdown-continuation day

    def test_no_event_no_continuation(self):
        """A quiet series with no breakout/failure never marks anything."""
        bars = _flat_bars(40)
        for direction in ("up", "down"):
            result = patterns.gap_continuation(bars, direction=direction, lookback=20)
            assert not result.any()

    def test_invalid_direction_raises(self):
        bars = _flat_bars(10)
        try:
            patterns.gap_continuation(bars, direction="sideways")
        except ValueError as exc:
            assert "direction" in str(exc)
        else:
            raise AssertionError("expected ValueError for an invalid direction")

    def test_up_continuation_causal(self):
        rng = np.random.default_rng(3)
        n = 120
        price = 100.0
        rows = []
        for _ in range(n):
            open_ = price * (1 + rng.normal(0, 0.004))
            close = open_ * (1 + rng.normal(0, 0.015))
            high = max(open_, close) * (1 + abs(rng.normal(0, 0.005)))
            low = min(open_, close) * (1 - abs(rng.normal(0, 0.005)))
            vol = 1_000_000 * (1 + abs(rng.normal(0, 0.4)))
            rows.append({"open": open_, "high": high, "low": low, "close": close, "volume": vol})
            price = close
        bars = pd.DataFrame(rows)

        def fn(b: pd.DataFrame) -> pd.Series:
            return patterns.gap_continuation(b, direction="up", lookback=20, confirm_within_bars=3)

        indicators.assert_causal(fn, bars, min_index=25, n_checks=25)

    def test_down_continuation_causal(self):
        rng = np.random.default_rng(4)
        n = 120
        price = 100.0
        rows = []
        for _ in range(n):
            open_ = price * (1 + rng.normal(0, 0.004))
            close = open_ * (1 + rng.normal(0, 0.015))
            high = max(open_, close) * (1 + abs(rng.normal(0, 0.005)))
            low = min(open_, close) * (1 - abs(rng.normal(0, 0.005)))
            vol = 1_000_000 * (1 + abs(rng.normal(0, 0.4)))
            rows.append({"open": open_, "high": high, "low": low, "close": close, "volume": vol})
            price = close
        bars = pd.DataFrame(rows)

        def fn(b: pd.DataFrame) -> pd.Series:
            return patterns.gap_continuation(b, direction="down", lookback=20, confirm_within_bars=3)

        indicators.assert_causal(fn, bars, min_index=25, n_checks=25)


class TestPivotPoints:
    def test_matches_hand_computed_floor_pivot(self):
        bars = pd.DataFrame(
            {
                "high": [110.0, 108.0, 112.0],
                "low": [100.0, 101.0, 104.0],
                "close": [105.0, 104.0, 109.0],
            }
        )
        result = patterns.pivot_points(bars)

        assert pd.isna(result["pivot"].iloc[0])  # no prior session
        expected_pivot = (110.0 + 100.0 + 105.0) / 3.0
        assert result["pivot"].iloc[1] == expected_pivot
        assert result["pivot_r1"].iloc[1] == 2 * expected_pivot - 100.0
        assert result["pivot_s1"].iloc[1] == 2 * expected_pivot - 110.0
        assert result["pivot_r2"].iloc[1] == expected_pivot + (110.0 - 100.0)
        assert result["pivot_s2"].iloc[1] == expected_pivot - (110.0 - 100.0)

        expected_pivot_2 = (108.0 + 101.0 + 104.0) / 3.0
        assert result["pivot"].iloc[2] == expected_pivot_2

    def test_causal(self):
        bars = pd.DataFrame(
            {
                "high": np.linspace(100, 130, 60) + np.sin(np.arange(60)),
                "low": np.linspace(98, 128, 60) - np.abs(np.sin(np.arange(60))),
                "close": np.linspace(99, 129, 60),
            }
        )
        indicators.assert_causal(patterns.pivot_points, bars, min_index=5, n_checks=25)


class TestFibonacciLevels:
    def test_matches_hand_computed_ratios(self):
        swing_high = pd.Series([110.0] * 5)
        swing_low = pd.Series([100.0] * 5)

        result = patterns.fibonacci_levels(swing_high, swing_low)

        assert np.isclose(result["fib_23_6"].iloc[0], 100.0 + 0.236 * 10.0)
        assert np.isclose(result["fib_38_2"].iloc[0], 100.0 + 0.382 * 10.0)
        assert np.isclose(result["fib_50_0"].iloc[0], 105.0)
        assert np.isclose(result["fib_61_8"].iloc[0], 100.0 + 0.618 * 10.0)
        assert np.isclose(result["fib_78_6"].iloc[0], 100.0 + 0.786 * 10.0)
        # Ascending as the ratio rises.
        assert (result.iloc[0].to_numpy() == np.sort(result.iloc[0].to_numpy())).all()

    def test_nan_inputs_propagate(self):
        swing_high = pd.Series([np.nan, 110.0])
        swing_low = pd.Series([100.0, 100.0])

        result = patterns.fibonacci_levels(swing_high, swing_low)

        assert result.iloc[0].isna().all()
        assert result.iloc[1].notna().all()

    def test_causal(self):
        high = pd.Series(np.linspace(100, 130, 60))
        swing_high = patterns.recent_swing_level(high, patterns.find_swing_highs(high))
        low = pd.Series(np.linspace(90, 120, 60))
        swing_low = patterns.recent_swing_level(low, patterns.find_swing_lows(low))
        indicators.assert_causal(patterns.fibonacci_levels, swing_high, swing_low, min_index=10, n_checks=25)


class TestRoundNumberLevel:
    def test_low_price_tier_rounds_to_nearest_dollar(self):
        price = pd.Series([15.4, 15.6])
        result = patterns.round_number_level(price)
        assert result.iloc[0] == 15.0
        assert result.iloc[1] == 16.0

    def test_mid_price_tier_rounds_to_nearest_five(self):
        price = pd.Series([53.0])
        result = patterns.round_number_level(price)
        assert result.iloc[0] == 55.0

    def test_high_price_tier_rounds_to_nearest_ten(self):
        price = pd.Series([204.0])
        result = patterns.round_number_level(price)
        assert result.iloc[0] == 200.0

    def test_causal(self):
        price = pd.Series(np.linspace(10, 300, 80))
        indicators.assert_causal(patterns.round_number_level, price, min_index=1, n_checks=25)


class TestLevelConfluenceCount:
    def test_counts_agreeing_methods_not_raw_levels(self):
        price = pd.Series([100.0])
        levels = {
            "m1_hit": [pd.Series([100.5])],  # 0.5% away -> hit
            "m2_miss": [pd.Series([200.0])],  # 100% away -> miss
            # Two candidates, only one hits -- method still counts once.
            "m3_hit_via_second_candidate": [pd.Series([150.0]), pd.Series([99.7])],
        }

        result = patterns.level_confluence_count(price, levels, tolerance_pct=1.0)

        assert result.iloc[0] == 2.0

    def test_no_levels_gives_zero(self):
        price = pd.Series([100.0, 101.0])
        result = patterns.level_confluence_count(price, {})
        assert (result == 0.0).all()

    def test_nan_candidate_never_counts(self):
        price = pd.Series([100.0])
        levels = {"m1": [pd.Series([np.nan])]}
        result = patterns.level_confluence_count(price, levels, tolerance_pct=1.0)
        assert result.iloc[0] == 0.0

    def test_causal(self):
        price = pd.Series(np.linspace(100, 130, 40))
        pivot = patterns.pivot_points(
            pd.DataFrame({"high": price + 1, "low": price - 1, "close": price})
        )["pivot"]

        def fn(p: pd.Series, piv: pd.Series) -> pd.Series:
            return patterns.level_confluence_count(p, {"pivots": [piv]})

        indicators.assert_causal(fn, price, pivot, min_index=5, n_checks=20)


class TestVolumeDivergence:
    def test_loud_volume_flat_price_is_divergence(self):
        bars = pd.DataFrame({"close": [100.0, 100.3]})  # +0.3%, well under 1.0%
        rel_volume = pd.Series([1.0, 2.0])  # today's rel_volume = 2.0x >= 1.5x

        result = patterns.volume_divergence(bars, rel_volume)

        assert bool(result.iloc[1]) is True

    def test_big_move_is_not_divergence_even_on_loud_volume(self):
        bars = pd.DataFrame({"close": [100.0, 106.0]})  # +6%, well over 1.0%
        rel_volume = pd.Series([1.0, 2.0])

        result = patterns.volume_divergence(bars, rel_volume)

        assert bool(result.iloc[1]) is False

    def test_quiet_volume_is_not_divergence_even_on_flat_price(self):
        bars = pd.DataFrame({"close": [100.0, 100.2]})  # +0.2%, under 1.0%
        rel_volume = pd.Series([1.0, 0.8])  # 0.8x < 1.5x threshold

        result = patterns.volume_divergence(bars, rel_volume)

        assert bool(result.iloc[1]) is False

    def test_nan_rel_volume_degrades_to_false_not_a_crash(self):
        bars = pd.DataFrame({"close": [100.0, 100.1]})
        rel_volume = pd.Series([1.0, np.nan])

        result = patterns.volume_divergence(bars, rel_volume)

        assert bool(result.iloc[1]) is False

    def test_causal(self):
        rng = np.random.default_rng(7)
        close = pd.Series(100.0 + np.cumsum(rng.normal(0, 1.0, 80)))
        volume = pd.Series(1_000_000 * (1 + np.abs(rng.normal(0, 0.5, 80))))
        rel_volume = indicators.relative_volume(volume, 20)
        bars = pd.DataFrame({"close": close, "volume": volume})

        def fn(b: pd.DataFrame, rv: pd.Series) -> pd.Series:
            return patterns.volume_divergence(b, rv)

        indicators.assert_causal(fn, bars, rel_volume, min_index=25, n_checks=25)
