"""Causal technical-indicator primitives over ``pandas`` Series/DataFrames.

Every function here obeys one rule: the value at index ``i`` depends only on
input rows ``0..i``. That means trailing/expanding windows only -- never
``center=True``, never ``.shift(-n)``, never a whole-sample ``min``/``max``/
``quantile`` applied to what is meant to be a point-in-time feature. Warm-up
periods are left as ``NaN`` (via ``min_periods``); they are never back-filled,
since a back-filled warm-up value would itself be look-ahead (a value from the
future silently standing in for a value that could not yet be known).

Several of these indicators (RSI, ATR, ADX) have more than one convention in
circulation. Each docstring below says explicitly which convention is used
and why, so a reviewer never has to guess.

``vwap`` is a *daily-bar* rolling approximation. A true session VWAP resets
every trading session and needs intraday trade prints; this application only
holds daily bars, so treat ``vwap`` as a multi-day mean-reversion reference
level, not an intraday execution benchmark.

``assert_causal`` is the reusable causality check the test suite uses against
every function in this module (and, by the same pattern, against
``features.patterns`` and ``features.relative_strength``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Moving averages
# --------------------------------------------------------------------------


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over a trailing window.

    Warm-up: NaN for the first ``window - 1`` rows.
    """
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Standard exponential moving average, alpha = 2 / (span + 1), recursive.

    ``adjust=False`` is required for causality in the sense traders mean by
    "EMA": with ``adjust=True`` pandas reweights using the *entire* prefix
    seen so far on every call, which is technically still causal but does not
    match the recursive definition every charting platform uses after the
    warm-up period. ``adjust=False`` reproduces that recursive definition.

    Warm-up: NaN for the first ``span - 1`` rows.
    """
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder_ema(series: pd.Series, window: int) -> pd.Series:
    """Wilder's smoothing: recursive EMA with alpha = 1 / window.

    This is the smoothing J. Welles Wilder used for RSI, ATR and ADX in *New
    Concepts in Technical Trading Systems* (1978) -- slower to react than a
    standard EMA of the same window, and the convention virtually every
    charting platform still uses for those three indicators. Using the
    standard EMA formula (``2 / (window + 1)``) here would silently change
    the RSI/ATR/ADX convention this codebase's strategies are tuned against.

    Warm-up: NaN for the first ``window - 1`` rows.
    """
    alpha = 1.0 / float(window)
    return series.ewm(alpha=alpha, adjust=False, min_periods=window).mean()


def slope(series: pd.Series, window: int) -> pd.Series:
    """Average per-bar change over a trailing window: (value - value[t-window]) / window.

    A simple, robust, causal proxy for local trend slope -- used for
    moving-average-slope trend scoring in the regime classifier. Deliberately
    not a rolling linear regression: at this granularity the two agree almost
    exactly and the regression costs far more to compute across a large
    universe.

    Warm-up: NaN for the first ``window`` rows.
    """
    return (series - series.shift(window)) / float(window)


# --------------------------------------------------------------------------
# Oscillators
# --------------------------------------------------------------------------


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI: ``100 - 100 / (1 + avg_gain / avg_loss)``.

    Gains and losses are smoothed with Wilder's own EMA (see ``wilder_ema``),
    which is Wilder's original 1978 convention -- as opposed to the
    SMA-seeded variant some platforms use for the first smoothed value only.
    The two converge within a handful of bars; using one formula throughout
    keeps the whole series internally consistent.

    Warm-up: NaN for the first ``window`` rows.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_ema(gain, window)
    avg_loss = wilder_ema(loss, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi_value = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 with avg_gain > 0 correctly saturates at 100 via the
    # 1/(1+inf) limit; only the genuine 0/0 case (no gains AND no losses,
    # i.e. a dead-flat price) needs an explicit, honest fallback.
    rsi_value = rsi_value.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return rsi_value


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD: EMA(fast) - EMA(slow), its signal EMA, and their difference.

    Standard (Appel) convention: both EMAs and the signal line use
    ``adjust=False`` recursion (see ``ema``).

    Warm-up: ``macd`` column NaN for the first ``slow - 1`` rows; ``signal``
    and ``hist`` NaN for ``slow - 1 + signal - 1`` rows (the signal EMA's own
    ``min_periods`` counts non-NaN observations of the macd line, so it only
    starts warming up once the macd line itself is defined).
    """
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def roc(series: pd.Series, window: int) -> pd.Series:
    """Rate of change: percent change versus the value ``window`` bars ago.

    Warm-up: NaN for the first ``window`` rows.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        return (series / series.shift(window) - 1.0) * 100.0


def money_flow_index(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, window: int = 14
) -> pd.Series:
    """Money Flow Index: an RSI-like oscillator built on typical-price money flow.

    Positive/negative flow is classified by the change in typical price
    versus the PRIOR bar (so the first bar contributes to neither), then
    summed over a trailing window with a plain rolling sum -- not
    Wilder-smoothed, matching the original Quong/Soudack definition.

    Warm-up: NaN for the first ``window`` rows.
    """
    typical = (high + low + close) / 3.0
    raw_flow = typical * volume
    change = typical.diff()
    positive_flow = raw_flow.where(change > 0, 0.0)
    negative_flow = raw_flow.where(change < 0, 0.0)
    pos_sum = positive_flow.rolling(window=window, min_periods=window).sum()
    neg_sum = negative_flow.rolling(window=window, min_periods=window).sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        money_ratio = pos_sum / neg_sum
        mfi = 100.0 - (100.0 / (1.0 + money_ratio))
    return mfi.where(~((pos_sum == 0.0) & (neg_sum == 0.0)), 50.0)


# --------------------------------------------------------------------------
# Volatility / range
# --------------------------------------------------------------------------


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: ``max(high-low, |high-prev_close|, |low-prev_close|)``.

    Warm-up: NaN for row 0 -- there is no previous close to compare against,
    and deliberately not back-filled with the bare high-low range, which
    would silently understate day-1 volatility relative to every later row.
    """
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1, skipna=True)
    return tr.where(prev_close.notna())


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's Average True Range: Wilder-smoothed true range.

    Warm-up: NaN for the first ``window`` rows (1 for true range's own
    warm-up, ``window - 1`` more for the smoothing).
    """
    return wilder_ema(true_range(high, low, close), window)


def atr_percent(atr_value: pd.Series, close: pd.Series) -> pd.Series:
    """ATR normalised by price, in percent -- comparable across differently-priced names."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return atr_value / close * 100.0


def historical_volatility(close: pd.Series, window: int = 20, trading_days_per_year: int = 252) -> pd.Series:
    """Annualised historical volatility: std-dev of trailing daily log returns,
    scaled by ``sqrt(trading_days_per_year)``. Uses ``ddof=1`` (sample std).

    Warm-up: NaN for the first ``window`` rows (a log return itself costs the
    first observation).
    """
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window=window, min_periods=window).std(ddof=1) * np.sqrt(trading_days_per_year)


def bollinger_bands(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: SMA mid-band, +/- ``num_std`` trailing sample std-dev bands.

    Uses ``ddof=0`` (population std), matching Bollinger's original
    convention. ``pct_b = (close - lower) / (upper - lower)``;
    ``bandwidth = (upper - lower) / mid``.

    Warm-up: NaN for the first ``window - 1`` rows.
    """
    mid = sma(close, window)
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    band_range = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_b = (close - lower) / band_range
        bandwidth = band_range / mid
    return pd.DataFrame({"upper": upper, "mid": mid, "lower": lower, "pct_b": pct_b, "bandwidth": bandwidth})


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_window: int = 20,
    atr_window: int = 10,
    mult: float = 2.0,
) -> pd.DataFrame:
    """Keltner Channel: EMA midline +/- ``mult`` * Wilder ATR.

    Warm-up: NaN until both the EMA and ATR legs are warm
    (``max(ema_window, atr_window + 1)`` rows, roughly).
    """
    mid = ema(close, ema_window)
    atr_value = atr(high, low, close, atr_window)
    upper = mid + mult * atr_value
    lower = mid - mult * atr_value
    return pd.DataFrame({"upper": upper, "mid": mid, "lower": lower})


def donchian_channels(high: pd.Series, low: pd.Series, window: int = 20) -> pd.DataFrame:
    """Donchian channel: trailing rolling max(high) / min(low), INCLUDING the current bar.

    For breakout detection you almost always want to compare today's close
    against YESTERDAY's channel -- shift this result by one bar before that
    comparison (see ``patterns.detect_breakout``, which does exactly that).
    This function intentionally returns the inclusive channel (matching how
    charting platforms plot it) rather than pre-shifting, to keep its own
    contract simple and unambiguous.

    Warm-up: NaN for the first ``window - 1`` rows.
    """
    upper = high.rolling(window=window, min_periods=window).max()
    lower = low.rolling(window=window, min_periods=window).min()
    mid = (upper + lower) / 2.0
    return pd.DataFrame({"upper": upper, "mid": mid, "lower": lower})


# --------------------------------------------------------------------------
# Trend strength
# --------------------------------------------------------------------------


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.DataFrame:
    """Average Directional Index with +DI/-DI, Wilder's original method.

    ``+DM_t = high_t - high_{t-1}`` when that exceeds ``low_{t-1} - low_t``
    and is positive, else 0. ``-DM_t = low_{t-1} - low_t`` when that exceeds
    ``high_t - high_{t-1}`` and is positive, else 0. Both DM series and true
    range are smoothed with Wilder's EMA (alpha = 1/window) -- the convention
    used by most charting platforms, as opposed to a plain SMA or a standard
    EMA (both also seen in the wild), which would shift early-history values.
    ``+DI = 100 * smoothed(+DM) / smoothed(TR)``; ``-DI`` likewise.
    ``DX = 100 * |+DI - -DI| / (+DI + -DI)``; ``ADX`` is the Wilder-smoothed DX.

    Warm-up: ``+DI``/``-DI`` need roughly ``window`` rows; ``adx`` needs a
    further ``window`` rows to smooth DX, so the ``adx`` column is NaN for
    roughly the first ``2 * window`` rows.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)
    tr = true_range(high, low, close)
    smoothed_tr = wilder_ema(tr, window)
    smoothed_plus_dm = wilder_ema(plus_dm, window)
    smoothed_minus_dm = wilder_ema(minus_dm, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * smoothed_plus_dm / smoothed_tr
        minus_di = 100.0 * smoothed_minus_dm / smoothed_tr
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_line = wilder_ema(dx, window)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line})


# --------------------------------------------------------------------------
# Volume
# --------------------------------------------------------------------------


def relative_volume(volume: pd.Series, window: int = 20) -> pd.Series:
    """Today's volume divided by the average of the PRIOR ``window`` days.

    Today's own volume is deliberately excluded from its own baseline --
    including it would mechanically pull an unusual-volume day's ratio toward
    1.0 and blunt the very signal this feature exists to surface.

    Warm-up: NaN for the first ``window`` rows.
    """
    baseline = volume.shift(1).rolling(window=window, min_periods=window).mean()
    return volume / baseline


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume signed by the direction of each
    day's close-to-close change (an unchanged close contributes zero).

    This is a cumulative indicator, not a windowed one -- the first row is
    simply seeded at a signed contribution of zero (no prior close to
    compare against), not a real "warm-up" in the rolling-window sense.
    """
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def accumulation_distribution(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Chaikin Accumulation/Distribution line: cumulative money-flow-multiplier * volume.

    ``MFM = ((close - low) - (high - close)) / (high - low)``, defined as 0
    on a zero-range bar (division-by-zero guard) rather than NaN, so the
    cumulative line does not gap.
    """
    range_ = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        mfm = ((close - low) - (high - close)) / range_
    mfm = mfm.where(range_ != 0, 0.0)
    return (mfm * volume).cumsum()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, window: int = 20) -> pd.Series:
    """Rolling (trailing-window) volume-weighted average price approximation.

    LIMITATION: a *true* session VWAP resets every trading session and is
    computed from intraday trade prints; this application only has daily
    bars, so this is a rolling multi-day approximation using each day's
    typical price ``(H+L+C)/3`` as a stand-in for that day's trade prices.
    Useful as a mean-reversion reference level, not an intraday execution
    benchmark -- do not treat it as a substitute for a real session VWAP feed.

    Warm-up: NaN for the first ``window - 1`` rows.
    """
    typical = (high + low + close) / 3.0
    pv = typical * volume
    return pv.rolling(window=window, min_periods=window).sum() / volume.rolling(
        window=window, min_periods=window
    ).sum()


# --------------------------------------------------------------------------
# Misc. price features
# --------------------------------------------------------------------------


def distance_from_ma_pct(close: pd.Series, ma: pd.Series) -> pd.Series:
    """Percent distance of price above/below a moving average: ``(close - ma) / ma * 100``."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return (close - ma) / ma * 100.0


def gap_percent(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Overnight gap: ``(today's open - yesterday's close) / yesterday's close * 100``.

    Warm-up: NaN for row 0.
    """
    prev_close = close.shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (open_ - prev_close) / prev_close * 100.0


def consecutive_up_down(close: pd.Series) -> pd.DataFrame:
    """Running streak length of consecutive up days / down days.

    Resets to 0 the day the direction breaks (an unchanged close breaks
    both streaks). Built entirely from ``close[0..i]`` via the classic
    "cumsum of streak-break flags as a group key" trick, so it is causal by
    construction -- no rolling window is involved.

    Returns a DataFrame with float-valued ``up`` and ``down`` columns
    (float, not int, to allow NaN on row 0, which has no prior close).
    """
    direction = np.sign(close.diff())  # NaN, then -1 / 0 / +1
    up_flag = direction > 0
    down_flag = direction < 0
    up_group = (~up_flag).cumsum()
    down_group = (~down_flag).cumsum()
    up_streak = up_flag.groupby(up_group).cumsum().astype(float)
    down_streak = down_flag.groupby(down_group).cumsum().astype(float)
    valid = direction.notna()
    return pd.DataFrame({"up": up_streak.where(valid), "down": down_streak.where(valid)})


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Trailing-window percentile rank (0-1) of each value among its own
    trailing ``window`` observations, inclusive of itself.

    This is NOT a whole-sample rank: only observations ``i - window + 1 .. i``
    are ever used to rank point ``i``, so nothing here depends on data that
    has not happened yet -- a full-series ``rank(pct=True)`` would leak
    exactly the kind of look-ahead bias this module exists to avoid.

    Warm-up: NaN until ``window`` observations are available.
    """

    def _pct_rank_last(values: np.ndarray) -> float:
        last = values[-1]
        if np.isnan(last):
            return np.nan
        valid = values[~np.isnan(values)]
        if valid.size == 0:
            return np.nan
        return float(np.sum(valid <= last) / valid.size)

    return series.rolling(window=window, min_periods=window).apply(_pct_rank_last, raw=True)


# --------------------------------------------------------------------------
# Causality verification
# --------------------------------------------------------------------------


def assert_causal(
    fn: Callable[..., pd.Series | pd.DataFrame],
    *args: pd.Series | pd.DataFrame,
    n_checks: int = 20,
    min_index: int = 5,
    atol: float = 1e-6,
    rtol: float = 1e-6,
    rng: np.random.Generator | None = None,
    **kwargs: Any,
) -> None:
    """Assert that ``fn(*args, **kwargs)`` is causal.

    Computing ``fn`` on a prefix of the data must reproduce the full-series
    value at that same position. This is the reusable check the test suite
    runs against every indicator/pattern/relative-strength function in this
    package. It works by:

    1. Computing ``fn`` once on the full-length ``args``.
    2. For ``n_checks`` random indices ``i``, truncating every positional
       Series/DataFrame argument to its first ``i + 1`` rows, recomputing
       ``fn``, and comparing the truncated run's LAST row against the full
       run's row ``i``.

    A mismatch means ``fn`` used data beyond row ``i`` to produce row ``i``'s
    value -- i.e. look-ahead bias. NaN in both runs at the same position is
    treated as a match (both "don't know yet"); NaN in only one run is a
    failure, since that means the two runs disagree about when the value
    becomes knowable, which is itself a causality bug.

    Args:
        fn: The causal function under test. May return a Series or a
            DataFrame; both are supported.
        *args: One or more equal-length Series/DataFrame positional
            arguments fed to ``fn``. All are truncated together at each
            checked index.
        n_checks: How many indices to sample (without replacement).
        min_index: Smallest index eligible for sampling -- keeps the check
            away from the very start of the warm-up period, where every
            value is legitimately NaN and thus uninformative.
        atol: Absolute tolerance passed to ``numpy.isclose``.
        rtol: Relative tolerance passed to ``numpy.isclose``.
        rng: Optional seeded generator for reproducible sampling.
        **kwargs: Passed unchanged to every call of ``fn`` (scalar
            configuration, not truncated).

    Raises:
        AssertionError: on the first index where the truncated and full
            results disagree.
        ValueError: if arguments are empty, mismatched in length, or the
            series is too short to sample ``min_index``.
    """
    if not args:
        raise ValueError("assert_causal requires at least one Series/DataFrame argument")
    length = len(args[0])
    for a in args:
        if len(a) != length:
            raise ValueError("all positional arguments passed to assert_causal must share a length")
    if length <= min_index + 1:
        raise ValueError(f"series of length {length} too short to check from min_index={min_index}")

    full_result = fn(*args, **kwargs)
    generator = rng if rng is not None else np.random.default_rng(0)
    candidates = np.arange(min_index, length - 1)
    if candidates.size == 0:
        raise ValueError("no valid indices to sample -- series too short relative to min_index")
    sample_size = min(n_checks, candidates.size)
    chosen = generator.choice(candidates, size=sample_size, replace=False)
    fn_name = getattr(fn, "__name__", repr(fn))

    for i in sorted(int(x) for x in chosen):
        truncated_args = [a.iloc[: i + 1] for a in args]
        truncated_result = fn(*truncated_args, **kwargs)
        _assert_same_row(full_result, truncated_result, i, atol=atol, rtol=rtol, fn_name=fn_name)


def _assert_same_row(
    full_result: pd.Series | pd.DataFrame,
    truncated_result: pd.Series | pd.DataFrame,
    i: int,
    *,
    atol: float,
    rtol: float,
    fn_name: str,
) -> None:
    full_row = full_result.iloc[i]
    trunc_row = truncated_result.iloc[-1]
    if isinstance(full_row, pd.Series):  # DataFrame result -> one scalar per column
        for col in full_row.index:
            _assert_same_value(full_row[col], trunc_row[col], i, f"{fn_name}[{col}]", atol=atol, rtol=rtol)
    else:
        _assert_same_value(full_row, trunc_row, i, fn_name, atol=atol, rtol=rtol)


def _assert_same_value(
    full_value: Any, trunc_value: Any, i: int, label: str, *, atol: float, rtol: float
) -> None:
    full_nan = full_value != full_value  # NaN check that also tolerates non-float scalars
    trunc_nan = trunc_value != trunc_value
    if full_nan and trunc_nan:
        return
    if full_nan != trunc_nan:
        raise AssertionError(
            f"{label}: look-ahead bias at index {i} -- full-series value is "
            f"{'NaN' if full_nan else full_value!r} but the prefix-computed value is "
            f"{'NaN' if trunc_nan else trunc_value!r} (the two runs disagree about whether "
            f"row {i} is knowable yet)"
        )
    if not np.isclose(float(full_value), float(trunc_value), atol=atol, rtol=rtol):
        raise AssertionError(
            f"{label}: look-ahead bias at index {i} -- full-series value {full_value!r} "
            f"!= prefix-computed value {trunc_value!r}"
        )
