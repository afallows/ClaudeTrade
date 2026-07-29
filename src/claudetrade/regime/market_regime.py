"""Market regime classification.

Determines whether the current market environment is bull/bear, quiet/volatile,
and adjusts position sizing and signal thresholds accordingly.

The classifier produces a RegimeState with:
  - regime: One of BULL_QUIET, BULL_VOLATILE, NEUTRAL, BEAR_VOLATILE, BEAR_QUIET, UNKNOWN
  - trend_score, breadth, volatility_percentile, risk_appetite: Components
  - Adjustment multipliers: size_multiplier, score_threshold_adjustment, etc.

LIMITATIONS (documented in docstrings):
  - Volatility percentile: uses realised vol. A licensed VIX feed is preferable
    for forward-looking vol regime.
  - Risk appetite: uses cyclical (XLY/XLK) vs defensive (XLP/XLU) performance.
    A complete risk-on/off measure needs credit spreads and rate sensitivity.
  - Breadth: uses specified symbols' 50-day MA. For best results, pass the full
    trading universe's closes, not a sample.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import numpy as np
import pandas as pd

from claudetrade.config import RegimeConfig
from claudetrade.domain import MarketRegime, RegimeState
from claudetrade.features.indicators import historical_volatility, rolling_percentile, slope

logger = logging.getLogger(__name__)


class RegimeClassifier:
    """Classify market regime from price, breadth, volatility, and risk-appetite measures."""

    def __init__(self, config: RegimeConfig) -> None:
        """Initialize classifier with regime configuration.

        Args:
            config: RegimeConfig instance defining thresholds and parameters.
        """
        self.config = config

    def classify(
        self,
        session: dt.date,
        benchmark_bars: pd.DataFrame | None = None,
        breadth_series: pd.Series | dict[str, float] | None = None,
        volatility_series: pd.Series | None = None,
        risk_appetite_value: float | None = None,
    ) -> RegimeState:
        """Classify market regime for a single session (point in time).

        Args:
            session: The session date.
            benchmark_close: Current close of the benchmark (e.g. SPY).
            benchmark_bars: DataFrame with open, high, low, close, volume columns.
                Used to compute trend score, volatility, etc.
            breadth_series: Either a pandas Series indexed by session (preferred;
                value = fraction of universe above its 50-day MA) or a dict of
                symbol -> close Series. If dict, breadth is computed on the fly.
            volatility_series: Optional externally-computed volatility series
                (e.g. VIX). If not provided, computed from benchmark returns.
            risk_appetite_value: Optional risk-appetite score in [0, 1] where
                1.0 = maximum risk-on. If not provided, uses XLY/XLK vs XLP/XLU.

        Returns:
            RegimeState with regime classification and adjustment fields populated
            from config.
        """
        if benchmark_bars is None or benchmark_bars.empty:
            return RegimeState(
                session=session,
                regime=MarketRegime.UNKNOWN,
                notes=["insufficient benchmark data"],
            )

        trend_score = self._compute_trend_score(benchmark_bars)
        breadth_val = self._compute_breadth(breadth_series)
        vol_pct = self._compute_volatility_percentile(benchmark_bars, volatility_series)
        risk_appetite = risk_appetite_value if risk_appetite_value is not None else 0.5

        regime = self._map_to_regime(trend_score, breadth_val, vol_pct, risk_appetite)
        adjustments = self._get_adjustments(regime)

        return RegimeState(
            session=session,
            regime=regime,
            trend_score=float(trend_score),
            breadth=float(breadth_val),
            volatility_percentile=float(vol_pct),
            risk_appetite=float(risk_appetite),
            size_multiplier=adjustments["size_multiplier"],
            score_threshold_adjustment=adjustments["score_threshold_adjustment"],
            max_positions_multiplier=adjustments["max_positions_multiplier"],
            long_short_bias=adjustments["long_short_bias"],
            notes=adjustments.get("notes", []),
        )

    def classify_series(
        self,
        benchmark_bars: pd.DataFrame,
        breadth_series: pd.Series | dict[str, pd.Series] | None = None,
        volatility_series: pd.Series | None = None,
        risk_appetite_series: pd.Series | None = None,
    ) -> list[RegimeState]:
        """Classify regime for every session in benchmark_bars.

        Args:
            benchmark_bars: DataFrame with index = session date, columns include
                open, high, low, close, volume.
            breadth_series: Series indexed by session, or dict[symbol, close_series].
            volatility_series: Series indexed by session.
            risk_appetite_series: Series indexed by session.

        Returns:
            List of RegimeState, one per row of benchmark_bars, in order.

        Raises:
            ValueError: if benchmark_bars is empty or index is not a date index.
        """
        if benchmark_bars.empty:
            raise ValueError("benchmark_bars is empty")

        # Precompute rolling metrics for efficiency
        close_series = benchmark_bars["close"].astype(float)

        trend_scores = self._compute_trend_score_series(benchmark_bars)
        if volatility_series is None:
            vol_values = historical_volatility(close_series, window=252)
            volatility_series = vol_values

        vol_pct_series = rolling_percentile(volatility_series, self.config.vol_lookback_days)

        # Handle breadth
        if breadth_series is None:
            breadth_values = pd.Series(0.5, index=benchmark_bars.index)
        elif isinstance(breadth_series, dict):
            breadth_values = self._compute_breadth_series(breadth_series)
        else:
            breadth_values = breadth_series.reindex(benchmark_bars.index, fill_value=0.5)

        # Handle risk appetite
        if risk_appetite_series is None:
            risk_appetite_values = pd.Series(0.5, index=benchmark_bars.index)
        else:
            risk_appetite_values = risk_appetite_series.reindex(
                benchmark_bars.index, fill_value=0.5
            )

        # Classify each session
        results = []
        for session in benchmark_bars.index:
            if pd.isna(session):
                continue
            trend = float(trend_scores.loc[session]) if session in trend_scores.index else 0.0
            breadth = float(breadth_values.loc[session]) if session in breadth_values.index else 0.5
            vol_pct = float(vol_pct_series.loc[session]) if session in vol_pct_series.index else 0.5
            risk_app = (
                float(risk_appetite_values.loc[session])
                if session in risk_appetite_values.index
                else 0.5
            )

            regime = self._map_to_regime(trend, breadth, vol_pct, risk_app)
            adjustments = self._get_adjustments(regime)
            results.append(
                RegimeState(
                    session=session,
                    regime=regime,
                    trend_score=trend,
                    breadth=breadth,
                    volatility_percentile=vol_pct,
                    risk_appetite=risk_app,
                    size_multiplier=adjustments["size_multiplier"],
                    score_threshold_adjustment=adjustments["score_threshold_adjustment"],
                    max_positions_multiplier=adjustments["max_positions_multiplier"],
                    long_short_bias=adjustments["long_short_bias"],
                    notes=adjustments.get("notes", []),
                )
            )
        return results

    # ---- Private computation methods ----

    def _compute_trend_score(self, benchmark_bars: pd.DataFrame) -> float:
        """Trend score [-1, 1] from price vs 20/50/200 MAs and their slopes.

        Score factors:
          - Is price above/below each MA? Each above = +0.2, below = -0.2.
          - Are the MAs in uptrend (slope > 0) or downtrend? +/- 0.1 each.

        Returns float in [-1, 1].
        """
        close = benchmark_bars["close"].astype(float)
        if close.empty or len(close) < self.config.trend_long_ma:
            return 0.0

        sma_20 = close.rolling(window=self.config.trend_fast_ma, min_periods=1).mean()
        sma_50 = close.rolling(
            window=self.config.trend_slow_ma, min_periods=1
        ).mean()
        sma_200 = close.rolling(
            window=self.config.trend_long_ma, min_periods=1
        ).mean()

        last_close = close.iloc[-1]
        last_sma_20 = sma_20.iloc[-1]
        last_sma_50 = sma_50.iloc[-1]
        last_sma_200 = sma_200.iloc[-1]

        score = 0.0
        # Price position relative to MAs
        if last_close > last_sma_20:
            score += 0.2
        else:
            score -= 0.2

        if last_close > last_sma_50:
            score += 0.2
        else:
            score -= 0.2

        if last_close > last_sma_200:
            score += 0.2
        else:
            score -= 0.2

        # MA slopes (trending up = positive)
        slope_20 = slope(sma_20, window=20).iloc[-1] if len(sma_20) >= 20 else 0.0
        slope_50 = slope(sma_50, window=20).iloc[-1] if len(sma_50) >= 20 else 0.0
        slope_200 = slope(sma_200, window=20).iloc[-1] if len(sma_200) >= 20 else 0.0

        if np.isnan(slope_20) or slope_20 > 0:
            score += 0.05
        else:
            score -= 0.05

        if np.isnan(slope_50) or slope_50 > 0:
            score += 0.05
        else:
            score -= 0.05

        if np.isnan(slope_200) or slope_200 > 0:
            score += 0.05
        else:
            score -= 0.05

        return max(-1.0, min(1.0, score))

    def _compute_trend_score_series(self, benchmark_bars: pd.DataFrame) -> pd.Series:
        """Compute trend score for every row in benchmark_bars."""
        close = benchmark_bars["close"].astype(float)

        sma_20 = close.rolling(window=self.config.trend_fast_ma, min_periods=1).mean()
        sma_50 = close.rolling(
            window=self.config.trend_slow_ma, min_periods=1
        ).mean()
        sma_200 = close.rolling(
            window=self.config.trend_long_ma, min_periods=1
        ).mean()

        scores = []
        for i in range(len(close)):
            score = 0.0
            if close.iloc[i] > sma_20.iloc[i]:
                score += 0.2
            else:
                score -= 0.2

            if close.iloc[i] > sma_50.iloc[i]:
                score += 0.2
            else:
                score -= 0.2

            if close.iloc[i] > sma_200.iloc[i]:
                score += 0.2
            else:
                score -= 0.2

            s20 = slope(sma_20.iloc[: i + 1], window=20).iloc[-1] if i >= 20 else 0.0
            s50 = slope(sma_50.iloc[: i + 1], window=20).iloc[-1] if i >= 20 else 0.0
            s200 = slope(sma_200.iloc[: i + 1], window=20).iloc[-1] if i >= 20 else 0.0

            if np.isnan(s20) or s20 > 0:
                score += 0.05
            else:
                score -= 0.05

            if np.isnan(s50) or s50 > 0:
                score += 0.05
            else:
                score -= 0.05

            if np.isnan(s200) or s200 > 0:
                score += 0.05
            else:
                score -= 0.05

            scores.append(max(-1.0, min(1.0, score)))

        return pd.Series(scores, index=close.index)

    def _compute_breadth(self, breadth_series: pd.Series | dict[str, float] | None) -> float:
        """Fraction of universe above its own 50-day MA.

        If breadth_series is a Series: use its latest value.
        If dict[symbol, close]: compute for each symbol.
        If None: return 0.5 (neutral).
        """
        if breadth_series is None:
            return 0.5

        if isinstance(breadth_series, pd.Series):
            if breadth_series.empty:
                return 0.5
            return float(breadth_series.iloc[-1])

        if isinstance(breadth_series, dict):
            if not breadth_series:
                return 0.5
            above_ma = 0
            total = 0
            for _sym, close_series in breadth_series.items():
                if isinstance(close_series, pd.Series) and len(close_series) >= 50:
                    ma_50 = close_series.rolling(window=50, min_periods=50).mean()
                    if not ma_50.empty:
                        if close_series.iloc[-1] > ma_50.iloc[-1]:
                            above_ma += 1
                        total += 1
            return above_ma / total if total > 0 else 0.5

        return 0.5

    def _compute_breadth_series(self, closes_dict: dict[str, pd.Series]) -> pd.Series:
        """Compute breadth (fraction above 50-day MA) for each session."""
        if not closes_dict:
            return pd.Series(0.5)

        # Align all series to a common index
        common_index = set()
        for s in closes_dict.values():
            common_index.update(s.index)
        common_index = sorted(common_index)

        aligned = {}
        for name, series in closes_dict.items():
            aligned[name] = series.reindex(common_index, fill_value=np.nan)

        breadth_values = []
        for i in range(len(common_index)):
            above = 0
            total = 0
            for name in aligned:
                series = aligned[name]
                if i >= 49:  # Need 50 bars for MA
                    ma_50 = series.iloc[i - 49 : i + 1].mean()
                    if not np.isnan(ma_50) and not np.isnan(series.iloc[i]):
                        if series.iloc[i] > ma_50:
                            above += 1
                        total += 1
            breadth_values.append(above / total if total > 0 else 0.5)

        return pd.Series(breadth_values, index=common_index)

    def _compute_volatility_percentile(
        self,
        benchmark_bars: pd.DataFrame,
        external_vol: pd.Series | None = None,
    ) -> float:
        """Volatility percentile: trailing rank of realised vol.

        If external_vol is provided (e.g. a VIX-like series), uses that.
        Otherwise computes annualised historical volatility from close.

        Returns float in [0, 1].
        """
        close = benchmark_bars["close"].astype(float)

        if external_vol is not None and not external_vol.empty:
            vol_series = external_vol
        else:
            vol_series = historical_volatility(close, window=20)

        if vol_series.empty or len(vol_series) < self.config.vol_lookback_days:
            return 0.5

        last_vol = vol_series.iloc[-1]
        lookback = vol_series.tail(self.config.vol_lookback_days).to_numpy()
        valid = lookback[~np.isnan(lookback)]

        if valid.size == 0:
            return 0.5

        pct = float(np.sum(valid <= last_vol) / valid.size)
        return max(0.0, min(1.0, pct))

    def _map_to_regime(
        self,
        trend_score: float,
        breadth: float,
        vol_pct: float,
        _risk_appetite: float,
    ) -> MarketRegime:
        """Map trend/breadth/vol/risk components to a MarketRegime enum.

        Logic:
          - trend_score > 0 with breadth > breadth_bullish => BULL or NEUTRAL
          - trend_score < 0 with breadth < breadth_bearish => BEAR or NEUTRAL
          - vol_pct > high_vol_percentile => VOLATILE variant
          - vol_pct < low_vol_percentile => QUIET variant
        """
        is_bullish = (trend_score > 0) and (breadth > self.config.breadth_bullish)
        is_bearish = (trend_score < 0) and (breadth < self.config.breadth_bearish)
        is_volatile = vol_pct > self.config.high_vol_percentile
        is_quiet = vol_pct < self.config.low_vol_percentile

        if is_bullish:
            if is_volatile:
                return MarketRegime.BULL_VOLATILE
            elif is_quiet:
                return MarketRegime.BULL_QUIET
            else:
                return MarketRegime.BULL_QUIET if vol_pct < 0.5 else MarketRegime.BULL_VOLATILE

        if is_bearish:
            if is_volatile:
                return MarketRegime.BEAR_VOLATILE
            elif is_quiet:
                return MarketRegime.BEAR_QUIET
            else:
                return MarketRegime.BEAR_QUIET if vol_pct < 0.5 else MarketRegime.BEAR_VOLATILE

        return MarketRegime.NEUTRAL

    def _get_adjustments(self, regime: MarketRegime) -> dict[str, Any]:
        """Return position-sizing and threshold adjustments for a regime."""
        if regime == MarketRegime.BULL_QUIET:
            return {
                "size_multiplier": 1.0,
                "score_threshold_adjustment": -5.0,
                "max_positions_multiplier": 1.2,
                "long_short_bias": 0.8,
                "notes": ["bull quiet: larger positions, lower entry bar, long bias"],
            }
        elif regime == MarketRegime.BULL_VOLATILE:
            return {
                "size_multiplier": self.config.high_vol_size_multiplier,
                "score_threshold_adjustment": 0.0,
                "max_positions_multiplier": 1.0,
                "long_short_bias": 0.6,
                "notes": ["bull volatile: reduce size, neutral threshold, balanced bias"],
            }
        elif regime == MarketRegime.NEUTRAL:
            return {
                "size_multiplier": 0.8,
                "score_threshold_adjustment": 2.0,
                "max_positions_multiplier": 0.9,
                "long_short_bias": 0.0,
                "notes": ["neutral: smaller positions, higher entry bar"],
            }
        elif regime == MarketRegime.BEAR_VOLATILE:
            return {
                "size_multiplier": self.config.risk_off_size_multiplier,
                "score_threshold_adjustment": 5.0,
                "max_positions_multiplier": 0.6,
                "long_short_bias": -0.6,
                "notes": ["bear volatile: much smaller, high threshold, short bias"],
            }
        elif regime == MarketRegime.BEAR_QUIET:
            return {
                "size_multiplier": self.config.risk_off_size_multiplier,
                "score_threshold_adjustment": 3.0,
                "max_positions_multiplier": 0.7,
                "long_short_bias": -0.4,
                "notes": ["bear quiet: smaller positions, short bias permitted"],
            }
        else:  # UNKNOWN
            return {
                "size_multiplier": 0.5,
                "score_threshold_adjustment": 10.0,
                "max_positions_multiplier": 0.5,
                "long_short_bias": 0.0,
                "notes": ["unknown regime: very cautious"],
            }
