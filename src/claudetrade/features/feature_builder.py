"""High-level feature construction orchestrating all indicator/pattern/RS modules.

The FeatureBuilder accepts raw OHLCV data and produces a flat dict[str, float]
of features suitable for strategy evaluation and signal scoring.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from claudetrade.domain import Bar
from claudetrade.features import indicators, patterns, relative_strength

FEATURE_VERSION = "v1"

#: Trailing window (sessions) for the self-history percentile features below
#: (ADR-0007 Decision 2). 120 sessions is roughly half a trading year -- long
#: enough to span more than one short-term regime, short enough that a young
#: listing still warms up within the app's typical history requirements.
#: Computed via ``indicators.rolling_percentile``, which is causal by
#: construction (see that function's docstring): the percentile at row ``i``
#: only ever ranks against rows ``i - 119 .. i``.
PERCENTILE_WINDOW = 120

# Every feature name that strategies and scoring depend on.
# Grep src/claudetrade/strategies/ and src/claudetrade/signals/ to confirm.
REQUIRED_FEATURES = (
    "close",
    "open",
    "high",
    "low",
    "volume",
    "sma_10",
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_9",
    "ema_21",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "atr_14",
    "atr_pct",
    "adx_14",
    "plus_di_14",
    "minus_di_14",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "bb_pct_b",
    "bb_bandwidth",
    "roc_5",
    "roc_10",
    "roc_20",
    "roc_60",
    "rel_volume_20",
    "obv",
    "obv_slope_10",
    "ad_line",
    "vwap_20",
    "dist_from_sma20_pct",
    "dist_from_sma50_pct",
    "dist_from_sma200_pct",
    "hv_20",
    "hv_60",
    "avg_dollar_volume_20",
    "donchian_high_20",
    "donchian_low_20",
    "swing_high_recent",
    "swing_low_recent",
    "resistance_level",
    "support_level",
    "breakout_20d",
    "failed_breakout",
    "hh_hl_score",
    "vol_contraction_pct",
    "gap_pct",
    "gap_filled",
    "gap_continuation_up",
    "gap_continuation_down",
    "consecutive_up",
    "consecutive_down",
    "volume_divergence",
    # ---- Pivot points, Fibonacci retracements, round-number levels ----
    # Additional causal S/R candidate inputs (ADR market-signal adoption
    # package item 3) -- see the docstrings of ``patterns.pivot_points``,
    # ``patterns.fibonacci_levels`` and ``patterns.round_number_level``.
    "pivot",
    "pivot_r1",
    "pivot_s1",
    "pivot_r2",
    "pivot_s2",
    "fib_23_6",
    "fib_38_2",
    "fib_50_0",
    "fib_61_8",
    "fib_78_6",
    "round_number_level",
    "level_confluence_count",
    "rs_vs_benchmark_20",
    "rs_vs_benchmark_60",
    "rs_percentile",
    "rs_vs_sector_20",
    "beta_60",
    "corr_benchmark_60",
    "dist_from_52w_high_pct",
    "dist_from_52w_low_pct",
    "days_since_52w_high",
    # ---- Self-history percentiles (ADR-0007 Decision 2) ----
    # Trailing PERCENTILE_WINDOW-session percentile rank (0-1) of each series
    # within its OWN history -- the reference frame strategies use instead of
    # bare absolute constants (e.g. "is relative volume above 1.5x" becomes
    # "is relative volume in the top 30% of this symbol's own history").
    "rel_volume_pctl_120",
    "roc_20_pctl_120",
    "adx_pctl_120",
    "rsi_pctl_120",
    "dist_sma50_pctl_120",
)


class FeatureBuilder:
    """Vectorised feature-set computation from OHLCV bars.

    Accepts either a list of Bar objects or a DataFrame with columns
    open, high, low, close, volume. Outputs a DataFrame with one row per
    bar and one column per feature, indexed by session (dt.date).
    """

    def __init__(
        self,
        symbol: str = "",
        benchmark_symbol: str = "SPY",
    ) -> None:
        self.symbol = symbol
        self.benchmark_symbol = benchmark_symbol

    def build(
        self,
        symbol: str | None = None,
        bars: list[Bar] | pd.DataFrame | None = None,
        benchmark_bars: list[Bar] | pd.DataFrame | None = None,
        sector_bars: dict[str, list[Bar] | pd.DataFrame] | None = None,
    ) -> pd.DataFrame:
        """Build the full feature set from bars.

        Args:
            symbol: Security symbol (used for logging). Defaults to self.symbol.
            bars: List of Bar objects or DataFrame with OHLCV columns.
            benchmark_bars: Same format, for the benchmark (e.g. SPY).
            sector_bars: Dict[sector_name, bars] for relative-strength ranking.

        Returns:
            DataFrame indexed by session (dt.date), one row per bar,
            one column per feature. Short-history NaN warm-up periods are
            never back-filled.
        """
        if symbol is None:
            symbol = self.symbol
        if bars is None:
            return pd.DataFrame()

        # Convert Bar lists to DataFrames
        df = self._bars_to_df(bars)
        if df.empty:
            return pd.DataFrame()

        benchmark_df = None
        if benchmark_bars is not None:
            benchmark_df = self._bars_to_df(benchmark_bars)

        sector_dfs = {}
        if sector_bars is not None:
            for sname, sbars in sector_bars.items():
                sdf = self._bars_to_df(sbars)
                if not sdf.empty:
                    sector_dfs[sname] = sdf

        return self._compute_all_features(df, benchmark_df, sector_dfs)

    def build_point_in_time(
        self,
        symbol: str | None = None,
        bars: list[Bar] | pd.DataFrame | None = None,
        benchmark_bars: list[Bar] | pd.DataFrame | None = None,
        sector_bars: dict[str, list[Bar] | pd.DataFrame] | None = None,
    ) -> dict[str, float]:
        """Build features and return only the LAST row as a flat dict.

        This is what feeds StrategyContext.features for the current session.
        """
        df = self.build(symbol, bars, benchmark_bars, sector_bars)
        if df.empty:
            return {}
        last_row = df.iloc[-1]
        return {col: float(val) for col, val in last_row.items() if pd.notna(val)}

    @staticmethod
    def _bars_to_df(bars: list[Bar] | pd.DataFrame) -> pd.DataFrame:
        """Convert Bar list or DataFrame into a standardised DataFrame."""
        if isinstance(bars, pd.DataFrame):
            return bars.copy()
        if not bars:
            return pd.DataFrame()
        records = [
            {
                "session": b.session,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        df = pd.DataFrame(records)
        df["session"] = pd.to_datetime(df["session"]).dt.date
        return df.set_index("session")

    def _compute_all_features(
        self,
        df: pd.DataFrame,
        benchmark_df: pd.DataFrame | None,
        sector_dfs: dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        """Compute all required features."""
        result = pd.DataFrame(index=df.index)

        # Passthrough OHLCV
        result["open"] = df["open"].astype(float)
        result["high"] = df["high"].astype(float)
        result["low"] = df["low"].astype(float)
        result["close"] = df["close"].astype(float)
        result["volume"] = df["volume"].astype(float)

        close = result["close"]
        high = result["high"]
        low = result["low"]
        volume = result["volume"]

        # ---- Moving Averages ----
        result["sma_10"] = indicators.sma(close, 10)
        result["sma_20"] = indicators.sma(close, 20)
        result["sma_50"] = indicators.sma(close, 50)
        result["sma_200"] = indicators.sma(close, 200)
        result["ema_9"] = indicators.ema(close, 9)
        result["ema_21"] = indicators.ema(close, 21)

        # ---- Oscillators ----
        result["rsi_14"] = indicators.rsi(close, 14)

        macd_df = indicators.macd(close)
        result["macd"] = macd_df["macd"]
        result["macd_signal"] = macd_df["signal"]
        result["macd_hist"] = macd_df["hist"]

        # ---- Volatility ----
        result["atr_14"] = indicators.atr(high, low, close, 14)
        result["atr_pct"] = indicators.atr_percent(result["atr_14"], close)

        adx_df = indicators.adx(high, low, close, 14)
        result["adx_14"] = adx_df["adx"]
        result["plus_di_14"] = adx_df["plus_di"]
        result["minus_di_14"] = adx_df["minus_di"]

        bb_df = indicators.bollinger_bands(close, 20, 2.0)
        result["bb_upper"] = bb_df["upper"]
        result["bb_mid"] = bb_df["mid"]
        result["bb_lower"] = bb_df["lower"]
        result["bb_pct_b"] = bb_df["pct_b"]
        result["bb_bandwidth"] = bb_df["bandwidth"]

        result["hv_20"] = indicators.historical_volatility(close, 20)
        result["hv_60"] = indicators.historical_volatility(close, 60)

        # ---- Rate of Change ----
        result["roc_5"] = indicators.roc(close, 5)
        result["roc_10"] = indicators.roc(close, 10)
        result["roc_20"] = indicators.roc(close, 20)
        result["roc_60"] = indicators.roc(close, 60)

        # ---- Volume ----
        result["rel_volume_20"] = indicators.relative_volume(volume, 20)
        result["obv"] = indicators.obv(close, volume)
        result["obv_slope_10"] = indicators.slope(result["obv"], 10)
        result["ad_line"] = indicators.accumulation_distribution(high, low, close, volume)
        result["vwap_20"] = indicators.vwap(high, low, close, volume, 20)

        # ---- Volume divergence (market-signal adoption package item 4) ----
        # Elevated relative volume with little same-day price follow-through
        # -- a possible absorption/distribution warning. See
        # ``patterns.volume_divergence``.
        result["volume_divergence"] = patterns.volume_divergence(
            df, result["rel_volume_20"]
        ).astype(float)

        # ---- Price Levels ----
        result["dist_from_sma20_pct"] = indicators.distance_from_ma_pct(
            close, result["sma_20"]
        )
        result["dist_from_sma50_pct"] = indicators.distance_from_ma_pct(
            close, result["sma_50"]
        )
        result["dist_from_sma200_pct"] = indicators.distance_from_ma_pct(
            close, result["sma_200"]
        )

        result["avg_dollar_volume_20"] = (
            (close * volume)
            .rolling(window=20, min_periods=20)
            .mean()
        )

        # ---- Donchian Channels ----
        donchian_df = indicators.donchian_channels(high, low, 20)
        result["donchian_high_20"] = donchian_df["upper"]
        result["donchian_low_20"] = donchian_df["lower"]

        # ---- Swing Points & Support/Resistance ----
        swing_highs = patterns.find_swing_highs(high, left=3, right=3)
        swing_lows = patterns.find_swing_lows(low, left=3, right=3)
        result["swing_high_recent"] = patterns.recent_swing_level(high, swing_highs)
        result["swing_low_recent"] = patterns.recent_swing_level(low, swing_lows)

        sr_df = patterns.support_resistance_levels(df, lookback=100)
        result["resistance_level"] = sr_df["resistance_level"]
        result["support_level"] = sr_df["support_level"]

        # ---- Pivot points, Fibonacci retracements, round-number levels ----
        # Additional causal S/R candidate inputs (market-signal adoption
        # package item 3). Pivots use only the PRIOR session's H/L/C; the
        # Fibonacci levels are pure arithmetic off the already-causal
        # swing_high_recent/swing_low_recent series computed just above.
        pivot_df = patterns.pivot_points(df)
        result["pivot"] = pivot_df["pivot"]
        result["pivot_r1"] = pivot_df["pivot_r1"]
        result["pivot_s1"] = pivot_df["pivot_s1"]
        result["pivot_r2"] = pivot_df["pivot_r2"]
        result["pivot_s2"] = pivot_df["pivot_s2"]

        fib_df = patterns.fibonacci_levels(result["swing_high_recent"], result["swing_low_recent"])
        result["fib_23_6"] = fib_df["fib_23_6"]
        result["fib_38_2"] = fib_df["fib_38_2"]
        result["fib_50_0"] = fib_df["fib_50_0"]
        result["fib_61_8"] = fib_df["fib_61_8"]
        result["fib_78_6"] = fib_df["fib_78_6"]

        result["round_number_level"] = patterns.round_number_level(close)

        # How many independent methods (clustered swings, pivots, Fibonacci,
        # round-number, moving averages) place a level within tolerance of
        # today's close -- consumed as a small confluence bonus by Strategies
        # A and B. Feeding this into support_resistance_levels' own
        # clustering was considered and rejected: that clustering result is a
        # hard structural input elsewhere (Strategy A's "no reference level"
        # veto, both A's and B's stop placement), and folding pivot/fib/
        # round-number candidates into it would silently change that
        # existing, load-bearing level selection. A separate count keeps the
        # new signal purely additive.
        result["level_confluence_count"] = patterns.level_confluence_count(
            close,
            {
                "swings": [result["support_level"], result["resistance_level"]],
                "pivots": [
                    result["pivot"],
                    result["pivot_r1"],
                    result["pivot_s1"],
                    result["pivot_r2"],
                    result["pivot_s2"],
                ],
                "fib": [
                    result["fib_23_6"],
                    result["fib_38_2"],
                    result["fib_50_0"],
                    result["fib_61_8"],
                    result["fib_78_6"],
                ],
                "round_number": [result["round_number_level"]],
                "ma": [result["sma_20"], result["sma_50"], result["sma_200"]],
            },
        )

        # ---- Breakouts ----
        result["breakout_20d"] = patterns.detect_breakout(df, lookback=20).astype(float)
        result["failed_breakout"] = patterns.detect_failed_breakout(df, lookback=20).astype(float)

        # ---- Structure ----
        result["hh_hl_score"] = patterns.detect_higher_highs_higher_lows(df, lookback=60)
        result["vol_contraction_pct"] = patterns.volatility_contraction(result["bb_bandwidth"], 126)

        # ---- Gap Analysis ----
        gap_df = patterns.gap_analysis(df, fill_lookahead_bars=10)
        result["gap_pct"] = gap_df["gap_pct"]
        result["gap_filled"] = gap_df["gap_filled"].astype(float)

        # ---- Gap continuation (market-signal adoption package item 2) ----
        # Does a LATER session open beyond a breakout/breakdown level with an
        # actual overnight gap, extending the move. See
        # ``patterns.gap_continuation`` for the causal confirmation-delay
        # mechanics (mirrors ``detect_failed_breakout``).
        result["gap_continuation_up"] = patterns.gap_continuation(
            df, direction="up", lookback=20
        ).astype(float)
        result["gap_continuation_down"] = patterns.gap_continuation(
            df, direction="down", lookback=20
        ).astype(float)

        # ---- Consecutive Bars ----
        cu_cd_df = indicators.consecutive_up_down(close)
        result["consecutive_up"] = cu_cd_df["up"]
        result["consecutive_down"] = cu_cd_df["down"]

        # ---- Relative Strength ----
        if benchmark_df is not None and not benchmark_df.empty:
            bench_close = benchmark_df["close"].astype(float)
            bench_close = bench_close.reindex(close.index, method="ffill")

            rs_20_df = relative_strength.relative_strength(close, bench_close, 20)
            result["rs_vs_benchmark_20"] = rs_20_df["rs_line"]
            result["rs_percentile"] = rs_20_df["rs_percentile"] * 100.0

            rs_60_df = relative_strength.relative_strength(close, bench_close, 60)
            result["rs_vs_benchmark_60"] = rs_60_df["rs_line"]

            result["beta_60"] = relative_strength.beta(close, bench_close, 60)
            result["corr_benchmark_60"] = relative_strength.correlation(close, bench_close, 60)
        else:
            result["rs_vs_benchmark_20"] = np.nan
            result["rs_vs_benchmark_60"] = np.nan
            result["rs_percentile"] = 50.0
            result["beta_60"] = 1.0
            result["corr_benchmark_60"] = 0.5

        # ---- Sector Relative Strength ----
        if sector_dfs:
            sector_closes = {
                sname: sdf["close"].astype(float).reindex(close.index, method="ffill")
                for sname, sdf in sector_dfs.items()
            }
            sector_rs = relative_strength.sector_relative_strength(sector_closes, 20)
            if not sector_rs.empty:
                sector_rs = sector_rs.reindex(close.index)
                result["rs_vs_sector_20"] = (
                    sector_rs.iloc[:, 0] * 100.0 if len(sector_rs.columns) > 0 else np.nan
                )
            else:
                result["rs_vs_sector_20"] = np.nan
        else:
            result["rs_vs_sector_20"] = np.nan

        # ---- 52-week levels ----
        high_52w = high.rolling(window=252, min_periods=252).max()
        low_52w = low.rolling(window=252, min_periods=252).min()

        with np.errstate(divide="ignore", invalid="ignore"):
            result["dist_from_52w_high_pct"] = (
                (close - high_52w) / high_52w * 100.0
            )
            result["dist_from_52w_low_pct"] = (
                (close - low_52w) / low_52w * 100.0
            )

        # Days since 52-week high
        result["days_since_52w_high"] = high.rolling(window=252, min_periods=252).apply(
            lambda x: len(x) - 1 - int(np.nanargmax(x))
            if ~np.isnan(x).all()
            else np.nan,
            raw=False,
        )

        # ---- Self-history percentiles (ADR-0007 Decision 2) ----
        # Causal by construction (see PERCENTILE_WINDOW docstring above);
        # NaN for the warm-up period like every other rolling feature here,
        # which ``StrategyContext.feature()`` treats as "missing" and
        # defaults to a neutral 0.5 rather than crashing a scan.
        result["rel_volume_pctl_120"] = indicators.rolling_percentile(
            result["rel_volume_20"], PERCENTILE_WINDOW
        )
        result["roc_20_pctl_120"] = indicators.rolling_percentile(
            result["roc_20"], PERCENTILE_WINDOW
        )
        result["adx_pctl_120"] = indicators.rolling_percentile(
            result["adx_14"], PERCENTILE_WINDOW
        )
        result["rsi_pctl_120"] = indicators.rolling_percentile(
            result["rsi_14"], PERCENTILE_WINDOW
        )
        result["dist_sma50_pctl_120"] = indicators.rolling_percentile(
            result["dist_from_sma50_pct"], PERCENTILE_WINDOW
        )

        return result


def build_features(
    symbol: str,
    bars: list[Bar] | pd.DataFrame,
    benchmark_bars: list[Bar] | pd.DataFrame | None = None,
    sector_bars: dict[str, list[Bar] | pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Convenience function to build features with a single call."""
    builder = FeatureBuilder(symbol=symbol)
    return builder.build(symbol, bars, benchmark_bars, sector_bars)
