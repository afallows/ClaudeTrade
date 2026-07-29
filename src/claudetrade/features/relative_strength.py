"""Relative strength and sector-relative ranking features.

Every function here computes only from trailing windows: no look-ahead bias.
NaN warm-up periods are never back-filled.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from claudetrade.features.indicators import slope


def relative_strength(
    symbol_close: pd.Series,
    benchmark_close: pd.Series,
    window: int = 20,
) -> pd.DataFrame:
    """Relative strength ratio, slope, and trailing percentile rank.

    Computes the ratio of ``symbol_close / benchmark_close`` (normalized by
    scaling both by their own past value to give a ratio whose drift is
    meaningful), then the slope of that ratio over the window, and its
    percentile rank within the trailing window.

    Warm-up: NaN for the first ``window`` rows or until either series has
    a zero, which would cause division-by-zero in normalization.
    """
    # Normalize by prior close to get returns-like values
    with np.errstate(divide="ignore", invalid="ignore"):
        symbol_ratio = symbol_close / symbol_close.shift(1)
        bench_ratio = benchmark_close / benchmark_close.shift(1)
        rs_line = symbol_ratio / bench_ratio

    # Trailing percentile of the RS line
    def _pct_rank_last(values: np.ndarray) -> float:
        last = values[-1]
        if np.isnan(last):
            return np.nan
        valid = values[~np.isnan(values)]
        if valid.size == 0:
            return np.nan
        return float(np.sum(valid <= last) / valid.size)

    rs_percentile = rs_line.rolling(window=window, min_periods=window).apply(
        _pct_rank_last, raw=True
    )
    rs_slope = slope(rs_line, window=window)

    return pd.DataFrame(
        {
            "rs_line": rs_line,
            "rs_slope": rs_slope,
            "rs_percentile": rs_percentile,
        },
        index=symbol_close.index,
    )


def relative_strength_score(
    symbol_close: pd.Series,
    benchmark_close: pd.Series,
    window: int = 20,
) -> pd.Series:
    """Relative strength score in [0, 100].

    Maps the percentile rank (0-1) to a 0-100 score.
    """
    rs_data = relative_strength(symbol_close, benchmark_close, window)
    return rs_data["rs_percentile"] * 100.0


def beta(
    symbol_close: pd.Series,
    benchmark_close: pd.Series,
    window: int = 60,
) -> pd.Series:
    """Beta: covariance(symbol_returns, benchmark_returns) / variance(benchmark_returns).

    Computed over a trailing window.

    Warm-up: NaN for the first ``window`` rows.
    """
    symbol_ret = symbol_close.pct_change()
    bench_ret = benchmark_close.pct_change()

    beta_vals = np.full(len(symbol_ret), np.nan)

    for i in range(window - 1, len(symbol_ret)):
        start = i - window + 1
        sym_window = symbol_ret.iloc[start : i + 1].to_numpy()
        ben_window = bench_ret.iloc[start : i + 1].to_numpy()

        valid = ~(np.isnan(sym_window) | np.isnan(ben_window))
        if valid.sum() < 2:
            continue

        sym_valid = sym_window[valid]
        ben_valid = ben_window[valid]

        bench_var = np.var(ben_valid, ddof=1)
        if bench_var == 0:
            continue

        beta_vals[i] = float(np.cov(sym_valid, ben_valid)[0, 1] / bench_var)

    return pd.Series(beta_vals, index=symbol_close.index)


def correlation(
    symbol_close: pd.Series,
    benchmark_close: pd.Series,
    window: int = 60,
) -> pd.Series:
    """Trailing-window correlation of returns.

    Warm-up: NaN for the first ``window`` rows.
    """
    symbol_ret = symbol_close.pct_change()
    bench_ret = benchmark_close.pct_change()

    corr_vals = np.full(len(symbol_ret), np.nan)

    for i in range(window - 1, len(symbol_ret)):
        start = i - window + 1
        sym_window = symbol_ret.iloc[start : i + 1].to_numpy()
        ben_window = bench_ret.iloc[start : i + 1].to_numpy()

        valid = ~(np.isnan(sym_window) | np.isnan(ben_window))
        if valid.sum() < 2:
            continue

        sym_valid = sym_window[valid]
        ben_valid = ben_window[valid]

        sym_std = np.std(sym_valid, ddof=1)
        ben_std = np.std(ben_valid, ddof=1)
        if sym_std == 0 or ben_std == 0:
            continue

        corr_vals[i] = float(
            np.cov(sym_valid, ben_valid)[0, 1] / (sym_std * ben_std)
        )

    return pd.Series(corr_vals, index=symbol_close.index)


def sector_relative_strength(
    sector_closes: dict[str, pd.Series],
    window: int = 20,
) -> pd.DataFrame:
    """Rank sectors by relative strength over a trailing window.

    Takes a dict mapping sector names to their close-price Series (typically
    ETF prices like XLY, XLP, etc.) and returns a DataFrame indexed by session
    with one column per sector, showing that sector's percentile rank among
    all sectors in that window.

    The percentile is computed as: how many other sectors' RS ratios lag
    behind this one's, expressed as a 0-1 fraction.

    Warm-up: NaN rows until all sectors have enough history.
    """
    if not sector_closes:
        return pd.DataFrame()

    # Align all series to the common index
    all_indices = set()
    for s in sector_closes.values():
        all_indices.update(s.index)
    common_index = sorted(all_indices)

    aligned = {}
    for name, series in sector_closes.items():
        aligned[name] = series.reindex(common_index)

    # Compute RS line for each sector (self-relative, normalized by shift)
    rs_lines = {}
    for name, series in aligned.items():
        with np.errstate(divide="ignore", invalid="ignore"):
            rs_line = series / series.shift(1)
        rs_lines[name] = rs_line

    # Rank each sector's RS line against the others in a trailing window
    def _rank_sectors(window_values: dict[str, np.ndarray]) -> dict[str, float]:
        """Compute percentile rank for each sector in window_values."""
        ranks = {}
        for name in window_values:
            vals = window_values[name]
            last = vals[-1]
            if np.isnan(last):
                ranks[name] = np.nan
                continue
            valid = vals[~np.isnan(vals)]
            if valid.size == 0:
                ranks[name] = np.nan
                continue
            ranks[name] = float(np.sum(valid <= last) / valid.size)
        return ranks

    # Build the output frame row by row
    output = {}
    for name in sector_closes:
        output[name] = []

    for i in range(len(common_index)):
        start = max(0, i - window + 1)
        window_data = {name: rs_lines[name].iloc[start : i + 1].to_numpy() for name in rs_lines}
        ranks = _rank_sectors(window_data)
        for name in sector_closes:
            output[name].append(ranks.get(name, np.nan))

    return pd.DataFrame(output, index=common_index)
