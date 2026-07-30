"""Tests for technical indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from claudetrade.features import indicators


class TestSMA:
    """Simple moving average tests."""

    def test_sma_hand_computed(self):
        """SMA matches hand-computed values."""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = indicators.sma(series, window=3)

        # First 2 should be NaN (warm-up)
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        # SMA of [1, 2, 3] = 2.0
        assert result.iloc[2] == 2.0
        # SMA of [2, 3, 4] = 3.0
        assert result.iloc[3] == 3.0
        # SMA of [3, 4, 5] = 4.0
        assert result.iloc[4] == 4.0

    def test_sma_causal(self, sample_bars):
        """SMA is causal: prefix recomputation matches."""
        series = pd.Series([b.close for b in sample_bars])
        indicators.assert_causal(indicators.sma, series, window=20)

    def test_sma_warmup_not_backfilled(self):
        """SMA warm-up period is NaN, never back-filled."""
        series = pd.Series([10.0] * 50)
        result = indicators.sma(series, window=10)
        assert result.iloc[0:9].isna().all()
        assert not result.iloc[9:].isna().any()


class TestRSI:
    """Relative Strength Index tests."""

    def test_rsi_bounds(self):
        """RSI values are in [0, 100] or NaN during warm-up."""
        series = pd.Series(np.random.randn(100).cumsum() + 100)
        result = indicators.rsi(series, window=14)

        valid = result[~result.isna()]
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_up_trend(self):
        """RSI is high in a consistent up-trend."""
        # Monotonically increasing series
        series = pd.Series(np.linspace(100, 200, 100))
        result = indicators.rsi(series, window=14)
        # After warm-up, should be near 100
        assert result.iloc[-1] > 90

    def test_rsi_down_trend(self):
        """RSI is low in a consistent down-trend."""
        # Monotonically decreasing series
        series = pd.Series(np.linspace(200, 100, 100))
        result = indicators.rsi(series, window=14)
        # After warm-up, should be near 0
        assert result.iloc[-1] < 10

    def test_rsi_flat_price(self):
        """RSI is 50 when price does not move."""
        series = pd.Series([100.0] * 50)
        result = indicators.rsi(series, window=14)
        # When price is flat, RSI should be 50
        assert result.iloc[-1] == 50.0

    def test_rsi_causal(self, sample_bars):
        """RSI is causal."""
        series = pd.Series([b.close for b in sample_bars])
        indicators.assert_causal(indicators.rsi, series, window=14)

    def test_rsi_warmup_is_nan(self):
        """RSI warm-up period is NaN."""
        series = pd.Series(range(100), dtype=float)
        result = indicators.rsi(series, window=14)
        # First 14 values should be NaN
        assert result.iloc[0:14].isna().all()
        # After warm-up should be valid
        assert not result.iloc[14:].isna().any()


class TestATR:
    """Average True Range tests."""

    def test_atr_positive(self):
        """ATR is always non-negative."""
        bars = pd.DataFrame(
            {
                "high": [100.0, 101.0, 102.0, 101.5, 102.5],
                "low": [99.0, 99.5, 100.5, 100.0, 101.0],
                "close": [100.0, 100.5, 101.5, 101.0, 102.0],
            }
        )
        result = indicators.atr(bars["high"], bars["low"], bars["close"], window=3)
        valid = result[~result.isna()]
        assert (valid >= 0).all()

    def test_atr_causal(self, sample_bars):
        """ATR is causal."""
        high = pd.Series([b.high for b in sample_bars])
        low = pd.Series([b.low for b in sample_bars])
        close = pd.Series([b.close for b in sample_bars])
        indicators.assert_causal(indicators.atr, high, low, close, window=14)

    def test_atr_percent_positive(self, sample_bars):
        """ATR% is always non-negative."""
        high = pd.Series([b.high for b in sample_bars])
        low = pd.Series([b.low for b in sample_bars])
        close = pd.Series([b.close for b in sample_bars])
        atr_val = indicators.atr(high, low, close, window=14)
        result = indicators.atr_percent(atr_val, close)

        valid = result[~result.isna()]
        assert (valid >= 0).all()


class TestBollingerBands:
    """Bollinger Bands tests."""

    def test_bb_bounds_logic(self):
        """Upper band > middle > lower band."""
        series = pd.Series(np.random.randn(50).cumsum() + 100)
        result = indicators.bollinger_bands(series, window=20, num_std=2)

        # After warm-up
        subset = result.iloc[20:]
        assert (subset["upper"] > subset["mid"]).all()
        assert (subset["mid"] > subset["lower"]).all()

    def test_bb_causal(self, sample_bars):
        """Bollinger Bands are causal."""
        series = pd.Series([b.close for b in sample_bars])
        indicators.assert_causal(indicators.bollinger_bands, series, window=20, num_std=2)


class TestMACD:
    """MACD tests."""

    def test_macd_divergence_is_hist(self):
        """MACD histogram = MACD line - signal line."""
        series = pd.Series(np.linspace(100, 150, 100))
        result = indicators.macd(series, fast=12, slow=26, signal=9)

        # After warm-up
        valid = result["hist"][30:]
        expected = (result["macd"][30:] - result["signal"][30:]).values
        np.testing.assert_allclose(valid.values, expected, rtol=1e-6, atol=1e-9)

    def test_macd_causal(self, sample_bars):
        """MACD is causal."""
        series = pd.Series([b.close for b in sample_bars])
        indicators.assert_causal(indicators.macd, series, fast=12, slow=26, signal=9)


class TestVolume:
    """Volume-based indicator tests."""

    def test_relative_volume_positive(self):
        """Relative volume is always positive."""
        volume = pd.Series([1_000_000, 1_500_000, 900_000, 1_200_000] * 10)
        result = indicators.relative_volume(volume, window=20)

        valid = result[~result.isna()]
        assert (valid > 0).all()

    def test_relative_volume_causal(self):
        """Relative volume is causal."""
        volume = pd.Series([1_000_000 + i * 10_000 for i in range(100)])
        indicators.assert_causal(indicators.relative_volume, volume, window=20)


class TestOBV:
    """On-Balance Volume tests."""

    def test_obv_up_on_volume_rise(self):
        """OBV increases when price rises on volume."""
        close = pd.Series([100.0, 101.0, 102.0, 103.0])
        volume = pd.Series([1_000_000, 1_000_000, 1_000_000, 1_000_000])
        result = indicators.obv(close, volume)

        # OBV should increase monotonically since price only goes up
        assert result.iloc[1] > result.iloc[0]
        assert result.iloc[2] > result.iloc[1]
        assert result.iloc[3] > result.iloc[2]

    def test_obv_causal(self):
        """OBV is causal."""
        close = pd.Series(np.linspace(100, 120, 100))
        volume = pd.Series([1_000_000] * 100)
        indicators.assert_causal(indicators.obv, close, volume)


class TestDistanceFromMA:
    """Tests for distance from moving average."""

    def test_distance_from_ma_zero_when_at_ma(self):
        """Distance is zero when price equals the MA."""
        close = pd.Series([100.0] * 50)
        ma = indicators.sma(close, window=10)
        result = indicators.distance_from_ma_pct(close, ma)

        # When close = MA, distance should be 0%
        valid = result[~result.isna()]
        np.testing.assert_allclose(valid.values, 0.0, rtol=1e-6, atol=1e-9)

    def test_distance_positive_above_ma(self):
        """Distance is positive when price is above MA."""
        close = pd.Series(np.linspace(100, 150, 50))
        ma = indicators.sma(close, window=10)
        result = indicators.distance_from_ma_pct(close, ma)

        # After warm-up, price is above its own SMA
        valid = result[15:]
        assert (valid > 0).all()


class TestCausalityEdgeCases:
    """Edge cases for the assert_causal function."""

    def test_assert_causal_requires_data(self):
        """assert_causal raises when no series provided."""
        with pytest.raises(ValueError, match="requires at least one"):
            indicators.assert_causal(indicators.sma)

    def test_assert_causal_requires_matching_length(self):
        """assert_causal raises on mismatched series lengths."""
        s1 = pd.Series([1.0, 2.0, 3.0])
        s2 = pd.Series([1.0, 2.0])
        with pytest.raises(ValueError, match="must share a length"):
            indicators.assert_causal(indicators.sma, s1, s2, window=2)

    def test_assert_causal_series_too_short(self):
        """assert_causal raises when series is too short."""
        s = pd.Series([1.0, 2.0])
        with pytest.raises(ValueError, match="too short"):
            indicators.assert_causal(indicators.sma, s, window=2, min_index=5)
