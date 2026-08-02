"""Causal price-action pattern detection.

Several patterns here are, by their nature, only knowable in hindsight: a
swing high only "held" once enough later bars have printed; a breakout only
"failed" once price actually falls back through the level. Where that is
true, this module handles it explicitly and documents it rather than papering
over it: the output is marked True on the CONFIRMATION bar (the bar on which
the fact became knowable), never on the earlier bar where the pattern first
looked plausible. Marking it earlier would be exactly the kind of look-ahead
bias this application is built to prevent.

Everything else here (breakout detection, pullback detection, consolidation
ranges, gap size) is knowable the same day it happens and needs no
confirmation delay.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from claudetrade.features.indicators import roc, rolling_percentile, slope

# --------------------------------------------------------------------------
# Swing points
# --------------------------------------------------------------------------


def find_swing_highs(high: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    """Confirmed swing-high (fractal) markers.

    A swing high at bar ``t`` is a local peak: ``high[t]`` exceeds every high
    in the ``left`` bars before it and every high in the ``right`` bars after
    it. That definition is inherently non-causal at ``t`` itself -- nobody
    can know a peak held until ``right`` further bars have printed. This
    function handles that look-ahead trap explicitly: the boolean is True at
    bar ``t + right`` (the first bar on which the peak is actually
    confirmed), never at bar ``t``.

    Returns a boolean Series aligned to ``high``'s index. The first ``left``
    and last ``right`` bars can never be confirmed swing points and are
    always False.
    """
    values = high.to_numpy(dtype=float)
    n = values.size
    confirmed = np.zeros(n, dtype=bool)
    for t in range(left, n - right):
        pivot = values[t]
        if np.isnan(pivot):
            continue
        left_window = values[t - left : t]
        right_window = values[t + 1 : t + right + 1]
        if np.any(np.isnan(left_window)) or np.any(np.isnan(right_window)):
            continue
        if pivot > left_window.max(initial=-np.inf) and pivot > right_window.max(initial=-np.inf):
            confirmed[t + right] = True
    return pd.Series(confirmed, index=high.index, name="swing_high_confirmed")


def find_swing_lows(low: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    """Confirmed swing-low (fractal) markers -- the mirror of ``find_swing_highs``.

    A swing low at bar ``t`` is confirmed, and marked, at bar ``t + right``,
    for the same reason a swing high cannot be marked at ``t`` itself.
    """
    values = low.to_numpy(dtype=float)
    n = values.size
    confirmed = np.zeros(n, dtype=bool)
    for t in range(left, n - right):
        pivot = values[t]
        if np.isnan(pivot):
            continue
        left_window = values[t - left : t]
        right_window = values[t + 1 : t + right + 1]
        if np.any(np.isnan(left_window)) or np.any(np.isnan(right_window)):
            continue
        if pivot < left_window.min(initial=np.inf) and pivot < right_window.min(initial=np.inf):
            confirmed[t + right] = True
    return pd.Series(confirmed, index=low.index, name="swing_low_confirmed")


def recent_swing_level(price: pd.Series, confirmed_mask: pd.Series) -> pd.Series:
    """Most recently CONFIRMED swing price, carried forward.

    Forward-fills (never back-fills) the swing price at each confirmation
    bar, so at any bar ``t`` this reflects the latest swing level that had
    already been confirmed by ``t`` -- never one confirmed later.
    """
    return price.where(confirmed_mask).ffill()


# --------------------------------------------------------------------------
# Support / resistance clustering
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Cluster:
    level: float
    touches: int
    last_index: int


def _cluster_levels(prices: np.ndarray, positions: np.ndarray, tolerance_pct: float) -> list[_Cluster]:
    """Group ``prices`` (with their bar ``positions``) into clusters within
    ``tolerance_pct`` percent of a running cluster mean. Pure numerical
    grouping, no time-ordering assumption other than what ``positions``
    already encodes.
    """
    order = np.argsort(prices)
    sorted_prices = prices[order]
    sorted_positions = positions[order]
    clusters: list[_Cluster] = []
    members: list[float] = [float(sorted_prices[0])]
    member_positions: list[int] = [int(sorted_positions[0])]
    for price, pos in zip(sorted_prices[1:], sorted_positions[1:], strict=True):
        ref = sum(members) / len(members)
        if ref != 0 and abs(price - ref) / abs(ref) * 100.0 <= tolerance_pct:
            members.append(float(price))
            member_positions.append(int(pos))
        else:
            clusters.append(_Cluster(sum(members) / len(members), len(members), max(member_positions)))
            members = [float(price)]
            member_positions = [int(pos)]
    clusters.append(_Cluster(sum(members) / len(members), len(members), max(member_positions)))
    return clusters


def support_resistance_levels(
    bars: pd.DataFrame,
    lookback: int = 100,
    tolerance_pct: float = 1.5,
    swing_left: int = 3,
    swing_right: int = 3,
) -> pd.DataFrame:
    """Clustered support/resistance levels, recomputed causally at every bar.

    At each session ``t``: takes every swing high/low CONFIRMED at or before
    ``t`` (see ``find_swing_highs``/``find_swing_lows`` -- a swing at raw
    pivot ``p`` only becomes visible here once its own confirmation bar has
    occurred) within the trailing ``lookback`` bars, clusters swing prices
    within ``tolerance_pct`` of each other, and reports the most-touched
    cluster as the resistance (from swing highs) and support (from swing
    lows) level, with a touch count and how many bars ago it was last
    touched.

    This is a genuine per-bar recomputation, not a vectorised rolling
    operation, since clustering is not linear. For the symbol-level history
    sizes this application works with (roughly 1-2k daily bars) it runs in a
    fraction of a second; it should not be invoked in a tight per-bar loop
    across a large universe without batching.

    Requires ``bars`` to have ``high`` and ``low`` columns. Returns a
    DataFrame indexed like ``bars`` with columns: ``resistance_level``,
    ``resistance_touches``, ``resistance_recency_days``, ``support_level``,
    ``support_touches``, ``support_recency_days``.
    """
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    swing_high_mask = find_swing_highs(high, left=swing_left, right=swing_right)
    swing_low_mask = find_swing_lows(low, left=swing_left, right=swing_right)
    hi_vals = high.where(swing_high_mask).to_numpy()
    lo_vals = low.where(swing_low_mask).to_numpy()

    n = len(bars)
    res_level = np.full(n, np.nan)
    res_touches = np.zeros(n, dtype=float)
    res_recency = np.full(n, np.nan)
    sup_level = np.full(n, np.nan)
    sup_touches = np.zeros(n, dtype=float)
    sup_recency = np.full(n, np.nan)

    for t in range(n):
        start = max(0, t - lookback + 1)

        hi_window = hi_vals[start : t + 1]
        hi_idx = np.where(~np.isnan(hi_window))[0]
        if hi_idx.size:
            clusters = _cluster_levels(hi_window[hi_idx], hi_idx + start, tolerance_pct)
            best = max(clusters, key=lambda c: (c.touches, c.last_index))
            res_level[t] = best.level
            res_touches[t] = best.touches
            res_recency[t] = t - best.last_index

        lo_window = lo_vals[start : t + 1]
        lo_idx = np.where(~np.isnan(lo_window))[0]
        if lo_idx.size:
            clusters = _cluster_levels(lo_window[lo_idx], lo_idx + start, tolerance_pct)
            best = max(clusters, key=lambda c: (c.touches, c.last_index))
            sup_level[t] = best.level
            sup_touches[t] = best.touches
            sup_recency[t] = t - best.last_index

    return pd.DataFrame(
        {
            "resistance_level": res_level,
            "resistance_touches": res_touches,
            "resistance_recency_days": res_recency,
            "support_level": sup_level,
            "support_touches": sup_touches,
            "support_recency_days": sup_recency,
        },
        index=bars.index,
    )


# --------------------------------------------------------------------------
# Breakouts
# --------------------------------------------------------------------------


def detect_breakout(bars: pd.DataFrame, lookback: int = 20, volume_mult: float = 1.5) -> pd.Series:
    """Breakout above the PRIOR ``lookback``-day high, confirmed by volume.

    Compares today's close against the Donchian-style upper channel computed
    from the ``lookback`` bars STRICTLY BEFORE today (the channel is shifted
    by one bar before the comparison -- ``indicators.donchian_channels``
    intentionally includes the current bar in its own window; using it
    unshifted here would let today's own high count toward "the resistance
    it broke", which is a tautology, not a breakout). Volume confirmation
    requires the day's volume to exceed ``volume_mult`` times the prior
    ``lookback``-day average volume (also computed on strictly prior bars).

    This event is knowable the same day it happens -- no forward-looking
    confirmation delay is needed here (contrast ``detect_failed_breakout``).

    Requires ``bars`` to have ``high``, ``close`` and ``volume`` columns.
    Warm-up: False for the first ``lookback + 1`` rows.
    """
    high = bars["high"].astype(float)
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    prior_high = high.shift(1).rolling(window=lookback, min_periods=lookback).max()
    prior_avg_volume = volume.shift(1).rolling(window=lookback, min_periods=lookback).mean()
    price_break = close > prior_high
    volume_confirmed = volume > volume_mult * prior_avg_volume
    return (price_break & volume_confirmed).rename("breakout")


def detect_failed_breakout(
    bars: pd.DataFrame, lookback: int = 20, volume_mult: float = 1.5, confirm_within_bars: int = 5
) -> pd.Series:
    """Failed breakout: a ``detect_breakout`` day whose close falls back
    below the breakout level within ``confirm_within_bars`` subsequent bars.

    Like a swing high, "this breakout failed" cannot be known on the
    breakout day itself -- it can only be known once (and if) price actually
    falls back below the level. This function marks the boolean True on the
    bar where the failure is CONFIRMED (the day price closes back below the
    breakout level), never on the original breakout day. A breakout that is
    never invalidated within the window is not marked at all.

    Warm-up: same as ``detect_breakout``, plus the confirmation delay.
    """
    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    breakout = detect_breakout(bars, lookback=lookback, volume_mult=volume_mult)
    prior_high = high.shift(1).rolling(window=lookback, min_periods=lookback).max()

    n = len(bars)
    close_vals = close.to_numpy()
    level_vals = prior_high.to_numpy()
    failed = np.zeros(n, dtype=bool)
    for b in np.where(breakout.to_numpy())[0]:
        level = level_vals[b]
        end = min(n, b + 1 + confirm_within_bars)
        for j in range(b + 1, end):
            if close_vals[j] < level:
                failed[j] = True
                break
    return pd.Series(failed, index=bars.index, name="failed_breakout")


# --------------------------------------------------------------------------
# Structure / volatility
# --------------------------------------------------------------------------


def detect_higher_highs_higher_lows(
    bars: pd.DataFrame, lookback: int = 60, swing_left: int = 3, swing_right: int = 3, n_pivots: int = 3
) -> pd.Series:
    """Structure score in ``[-1, 1]`` from the sequence of the last
    ``n_pivots`` CONFIRMED swing highs and swing lows within a trailing
    ``lookback`` window.

    ``+1``: every recent swing high is higher than the one before it AND
    every recent swing low is higher than the one before it (a clean uptrend
    structure). ``-1`` is the symmetric downtrend (lower highs, lower lows).
    Values in between reflect mixed structure. NaN when fewer than two of
    each pivot type have been confirmed within the lookback window.

    Uses only swings already confirmed by bar ``t`` (see
    ``find_swing_highs``), so it never anticipates a pivot before its
    confirmation bar.
    """
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    swing_high_mask = find_swing_highs(high, left=swing_left, right=swing_right)
    swing_low_mask = find_swing_lows(low, left=swing_left, right=swing_right)
    hi_vals = high.where(swing_high_mask).to_numpy()
    lo_vals = low.where(swing_low_mask).to_numpy()

    n = len(bars)
    score = np.full(n, np.nan)
    for t in range(n):
        start = max(0, t - lookback + 1)
        highs = hi_vals[start : t + 1]
        highs = highs[~np.isnan(highs)][-n_pivots:]
        lows = lo_vals[start : t + 1]
        lows = lows[~np.isnan(lows)][-n_pivots:]
        if highs.size < 2 or lows.size < 2:
            continue
        hh = (np.diff(highs) > 0).astype(float) * 2 - 1
        hl = (np.diff(lows) > 0).astype(float) * 2 - 1
        score[t] = float(np.mean(np.concatenate([hh, hl])))
    return pd.Series(score, index=bars.index, name="hh_hl_score")


def volatility_contraction(bandwidth: pd.Series, window: int = 126) -> pd.Series:
    """Volatility-contraction score in ``[0, 100]``.

    ``100`` minus the trailing-window percentile rank of a volatility proxy
    (typically Bollinger bandwidth or ATR%; caller's choice). High values
    mean today's volatility sits low relative to its own recent history -- a
    classic pre-breakout coiling pattern. Built entirely from
    ``indicators.rolling_percentile``, so it inherits that function's
    trailing-window (not whole-sample) causality.

    Warm-up: same as ``rolling_percentile`` (NaN for the first ``window``
    rows).
    """
    pct = rolling_percentile(bandwidth, window)
    return (1.0 - pct) * 100.0


def consolidation_range(bars: pd.DataFrame, window: int = 10, max_range_pct: float = 5.0) -> pd.DataFrame:
    """Tight trading-range detection with a running duration count.

    At each bar ``t``, the trailing ``window``-bar high/low range as a
    percent of its mean is compared against ``max_range_pct``; ``in_range``
    is True when that trailing range is tight enough to call a
    consolidation. ``duration`` is the running count of consecutive
    ``in_range`` bars (reset on the first bar that breaks out of the tight
    range), built the same reset-on-break way as
    ``indicators.consecutive_up_down``.

    Warm-up: NaN/False for the first ``window - 1`` rows.
    """
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    rolling_high = high.rolling(window=window, min_periods=window).max()
    rolling_low = low.rolling(window=window, min_periods=window).min()
    rolling_mean = ((high + low) / 2.0).rolling(window=window, min_periods=window).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        range_pct = (rolling_high - rolling_low) / rolling_mean * 100.0
    in_range = range_pct <= max_range_pct
    in_range_filled = in_range.fillna(False)
    break_group = (~in_range_filled).cumsum()
    duration = in_range_filled.groupby(break_group).cumsum().astype(float)
    duration = duration.where(range_pct.notna())
    return pd.DataFrame({"in_range": in_range, "range_pct": range_pct, "duration": duration})


# --------------------------------------------------------------------------
# Trend-following setups
# --------------------------------------------------------------------------


def detect_pullback_to_ma(
    bars: pd.DataFrame,
    ma: pd.Series,
    trend_ma: pd.Series,
    pullback_pct: float = 3.0,
    volume_window: int = 10,
) -> pd.Series:
    """Healthy pullback-to-moving-average setup.

    True when, as of bar ``t``: the longer trend MA (``trend_ma``) is rising
    (via ``indicators.slope``), price sits above it (uptrend intact), price
    has pulled back to within ``pullback_pct`` percent of the shorter ``ma``,
    and volume on down days over the trailing ``volume_window`` is trending
    down (a declining-selling-pressure tell). Every input is
    trailing/instantaneous, so this needs no forward-looking confirmation
    delay -- it is knowable same-day.

    Warm-up: bounded by whichever of ``ma``/``trend_ma`` warms up last, plus
    ``volume_window`` for the down-volume trend.
    """
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    trend_slope = slope(trend_ma, window=20)
    uptrend_intact = (close > trend_ma) & (trend_slope > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        distance_pct = (close - ma).abs() / ma * 100.0
    near_ma = distance_pct <= pullback_pct
    down_day = close.diff() < 0
    down_volume = volume.where(down_day, 0.0)
    down_volume_avg = down_volume.rolling(window=volume_window, min_periods=volume_window).mean()
    declining_down_volume = slope(down_volume_avg, window=volume_window) <= 0
    return (uptrend_intact & near_ma & declining_down_volume).rename("pullback_to_ma")


def detect_reclaim(bars: pd.DataFrame, support_level: pd.Series, lookback: int = 10) -> pd.Series:
    """False-breakdown reclaim.

    True when price closed below ``support_level`` at least once in the
    trailing ``lookback`` bars STRICTLY BEFORE today, and has now closed back
    above it. The reclaim itself is knowable the day it happens -- no
    forward-looking confirmation is needed here (unlike
    ``detect_failed_breakout``, which must wait to see whether a breakout
    DOES fail).

    Warm-up: NaN/False until ``support_level`` itself is available, plus
    ``lookback`` rows for the breakdown-history window.
    """
    close = bars["close"].astype(float)
    broke_below = close < support_level
    broke_below_recently = (
        broke_below.shift(1).rolling(window=lookback, min_periods=1).max().fillna(0.0).astype(bool)
    )
    reclaimed_today = close > support_level
    return (broke_below_recently & reclaimed_today).rename("reclaim")


def gap_analysis(bars: pd.DataFrame, fill_lookahead_bars: int = 10) -> pd.DataFrame:
    """Overnight gap size, and whether it later filled.

    ``gap_pct`` is knowable the instant the bar prints (today's open versus
    yesterday's close) and is causal immediately -- no delay needed.
    ``gap_filled``, however, describes something only confirmable AFTER the
    fact: a gap fills when price later trades back through the prior close.
    This function marks ``gap_filled`` True on the bar where the fill
    actually happens (checking each subsequent day's high/low for an
    intraday touch of the prior close, within ``fill_lookahead_bars``
    bars) -- never on the gap day itself unless the fill happens intraday on
    that same day. A gap that never fills within the window stays False.

    Warm-up: row 0 has no prior close, so ``gap_pct`` is NaN there.
    """
    open_ = bars["open"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        gap_pct = (open_ - prev_close) / prev_close * 100.0

    n = len(bars)
    gap_filled = np.zeros(n, dtype=bool)
    prev_close_vals = prev_close.to_numpy()
    high_vals = high.to_numpy()
    low_vals = low.to_numpy()
    gap_vals = gap_pct.to_numpy()
    for t in range(n):
        if np.isnan(gap_vals[t]) or gap_vals[t] == 0.0:
            continue
        target = prev_close_vals[t]
        end = min(n, t + 1 + fill_lookahead_bars)
        for j in range(t, end):
            if low_vals[j] <= target <= high_vals[j]:
                gap_filled[j] = True
                break
    return pd.DataFrame({"gap_pct": gap_pct, "gap_filled": gap_filled}, index=bars.index)


def gap_continuation(
    bars: pd.DataFrame,
    direction: str = "up",
    lookback: int = 20,
    volume_mult: float = 1.5,
    confirm_within_bars: int = 3,
) -> pd.Series:
    """Gap-and-go confirmation: does a LATER session open beyond the broken
    level with an actual overnight gap, extending a breakout ("up") or a
    failed-breakout breakdown ("down") rather than merely drifting through it.

    Idea from daily_stock_analysis ``strategies/volume_breakout.yaml`` (MIT):
    "next day's open should be above the breakout level, to separate real
    breakouts from fake ones" -- reimplemented here from scratch, causally,
    and generalised to the mirror-image breakdown case. Nothing beyond that
    one-sentence rule is reused; the confirmation-delay mechanics below mirror
    this module's own :func:`detect_failed_breakout`, not the source repo.

    ``direction="up"`` anchors on :func:`detect_breakout` days: the level is
    the same strictly-prior ``lookback``-day high ``detect_breakout`` itself
    compares against. ``direction="down"`` anchors on
    :func:`detect_failed_breakout` days -- a breakout that already failed
    back below the level -- and re-derives "the level" as the strictly-prior
    ``lookback``-day high AS OF the failure-confirmation bar (a simplification
    documented here: this can differ slightly from the original breakout
    day's level if the rolling window has moved on in the few bars between
    breakout and failure, but with the small default ``confirm_within_bars``
    detect_failed_breakout itself uses, the two are almost always identical).

    For each event day ``b``, later sessions ``j`` in
    ``(b, b + confirm_within_bars]`` are checked in order; the first ``j``
    whose open clears the level in ``direction`` AND whose ``gap_pct`` (see
    :func:`gap_analysis`) has the matching sign is marked True -- a level
    cleared by drift alone, with no overnight gap, does not count. This is
    exactly :func:`detect_failed_breakout`'s confirmation-delay shape: the
    event day is knowable same-day, but whether a continuation gap follows it
    can only be known once (and if) a later session actually opens that way.
    The boolean is marked True on the CONFIRMATION session, never on the
    event day itself; an event never followed by a continuation gap within
    the window is not marked at all.

    Requires ``bars`` to have ``open``, ``high``, ``close`` and ``volume``
    columns. Warm-up: False for at least the first ``lookback + 1`` rows
    (inherited from the underlying event detector).
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")

    open_ = bars["open"].astype(float)
    high = bars["high"].astype(float)
    gap_pct = gap_analysis(bars)["gap_pct"]
    prior_high = high.shift(1).rolling(window=lookback, min_periods=lookback).max()

    event = (
        detect_breakout(bars, lookback=lookback, volume_mult=volume_mult)
        if direction == "up"
        else detect_failed_breakout(bars, lookback=lookback, volume_mult=volume_mult)
    )

    n = len(bars)
    open_vals = open_.to_numpy()
    gap_vals = gap_pct.to_numpy()
    level_vals = prior_high.to_numpy()
    event_vals = event.to_numpy()

    continuation = np.zeros(n, dtype=bool)
    for b in np.where(event_vals)[0]:
        lvl = level_vals[b]
        if np.isnan(lvl):
            continue
        end = min(n, b + 1 + confirm_within_bars)
        for j in range(b + 1, end):
            if np.isnan(open_vals[j]) or np.isnan(gap_vals[j]):
                continue
            if direction == "up":
                confirmed = open_vals[j] > lvl and gap_vals[j] > 0
            else:
                confirmed = open_vals[j] < lvl and gap_vals[j] < 0
            if confirmed:
                continuation[j] = True
                break
    return pd.Series(continuation, index=bars.index, name="gap_continuation")


# --------------------------------------------------------------------------
# Pivot points / Fibonacci / round-number levels
# --------------------------------------------------------------------------


def pivot_points(bars: pd.DataFrame) -> pd.DataFrame:
    """Classic floor-trader pivot points, computed from the PRIOR session's
    high/low/close only (every column below is built with ``.shift(1)``, so
    today's pivot/support/resistance levels are fully determined before
    today's bar prints -- no look-ahead is possible by construction).

    Standard formulas (public domain, not any vendor's IP)::

        pivot = (prior_high + prior_low + prior_close) / 3
        r1 = 2 * pivot - prior_low
        s1 = 2 * pivot - prior_high
        r2 = pivot + (prior_high - prior_low)
        s2 = pivot - (prior_high - prior_low)

    Idea/aggregation shape (using pivots as one of several candidate S/R
    inputs) from tradingview-mcp ``core/services/indicators.py::
    compute_trade_setup`` (MIT); the pivot formulas themselves are standard
    floor-trader arithmetic, not TradingView's IP, and are reimplemented here
    from scratch.

    Requires ``bars`` to have ``high``, ``low`` and ``close`` columns.
    Warm-up: row 0 has no prior session and is NaN throughout.
    """
    prior_high = bars["high"].astype(float).shift(1)
    prior_low = bars["low"].astype(float).shift(1)
    prior_close = bars["close"].astype(float).shift(1)
    pivot = (prior_high + prior_low + prior_close) / 3.0
    return pd.DataFrame(
        {
            "pivot": pivot,
            "pivot_r1": 2.0 * pivot - prior_low,
            "pivot_s1": 2.0 * pivot - prior_high,
            "pivot_r2": pivot + (prior_high - prior_low),
            "pivot_s2": pivot - (prior_high - prior_low),
        },
        index=bars.index,
    )


#: Standard Fibonacci retracement ratios (public domain).
FIBONACCI_RATIOS: tuple[float, ...] = (0.236, 0.382, 0.500, 0.618, 0.786)


def fibonacci_levels(swing_high: pd.Series, swing_low: pd.Series) -> pd.DataFrame:
    """Fibonacci retracement levels spanning ``swing_high``/``swing_low``.

    Callers are expected to pass already CONFIRMED, forward-filled swing
    series (e.g. ``recent_swing_level(high, find_swing_highs(high))`` and its
    low-side mirror, or the ``swing_high_recent``/``swing_low_recent``
    features built from them) -- this function itself performs no temporal
    lookups of any kind, only row-wise arithmetic on its inputs, so it adds no
    look-ahead of its own beyond whatever its inputs already carry.

    Levels are anchored at ``swing_low`` and expressed as a fraction of the
    ``swing_high - swing_low`` range (public-domain Fibonacci ratios, not any
    vendor's IP): ``fib_23_6`` .. ``fib_78_6``, ascending as the ratio rises,
    regardless of whether the high or the low was confirmed more recently --
    a retracement level is a property of the RANGE, not of trade direction.

    Idea from tradingview-mcp ``core/services/indicators.py::
    compute_fibonacci_levels`` (MIT) -- using retracement levels as candidate
    S/R inputs; the ratios and the arithmetic are standard and reimplemented
    here from scratch.

    Returns a DataFrame with one column per ratio, aligned to
    ``swing_high``'s index. NaN wherever either input is NaN (i.e. before any
    swing has been confirmed).
    """
    rng = swing_high - swing_low
    return pd.DataFrame(
        {
            "fib_23_6": swing_low + FIBONACCI_RATIOS[0] * rng,
            "fib_38_2": swing_low + FIBONACCI_RATIOS[1] * rng,
            "fib_50_0": swing_low + FIBONACCI_RATIOS[2] * rng,
            "fib_61_8": swing_low + FIBONACCI_RATIOS[3] * rng,
            "fib_78_6": swing_low + FIBONACCI_RATIOS[4] * rng,
        },
        index=swing_high.index,
    )


#: Round-number rounding increments by price tier -- a classic, cheap S/R
#: proxy (round-dollar levels attract resting limit orders). Config-free
#: constants: this is a fixed price-scale convention, not a tunable strategy
#: parameter, so it lives here rather than in ``config.calibration``.
_ROUND_NUMBER_LOW_PRICE = 20.0
_ROUND_NUMBER_MID_PRICE = 100.0
_ROUND_NUMBER_LOW_INCREMENT = 1.0
_ROUND_NUMBER_MID_INCREMENT = 5.0
_ROUND_NUMBER_HIGH_INCREMENT = 10.0


def round_number_level(price: pd.Series) -> pd.Series:
    """Nearest 'round' price level to ``price`` -- a classic, cheap S/R proxy.

    The rounding increment scales with price magnitude so a $3 stock and a
    $300 stock each get a sensibly-sized round level rather than the same
    flat $1 grid: increments are $1 below $20, $5 from $20-100, $10 above
    $100 (see the module-level constants above). Pure same-bar arithmetic on
    ``price`` alone -- no window, no look-ahead of any kind.
    """
    price_vals = price.to_numpy(dtype=float)
    increment = np.where(
        price_vals < _ROUND_NUMBER_LOW_PRICE,
        _ROUND_NUMBER_LOW_INCREMENT,
        np.where(price_vals < _ROUND_NUMBER_MID_PRICE, _ROUND_NUMBER_MID_INCREMENT, _ROUND_NUMBER_HIGH_INCREMENT),
    )
    with np.errstate(invalid="ignore"):
        rounded = np.round(price_vals / increment) * increment
    return pd.Series(rounded, index=price.index, name="round_number_level")


def level_confluence_count(
    price: pd.Series,
    levels: dict[str, list[pd.Series]],
    tolerance_pct: float = 1.0,
) -> pd.Series:
    """How many independent METHODS place a candidate level within
    ``tolerance_pct`` percent of ``price``, at each bar.

    ``levels`` maps a method name (e.g. ``"swings"``, ``"pivots"``, ``"fib"``,
    ``"round_number"``, ``"ma"``) to the list of candidate level series that
    method offers simultaneously (a pivot method typically offers several:
    pivot/R1/S1/R2/S2). A method counts ONCE per bar if ANY of its candidate
    levels is within tolerance -- this counts agreeing METHODS, not raw
    levels, so a method that happens to emit five nearby numbers cannot
    outweigh a method that emits one.

    Pure per-bar arithmetic on already-computed level series -- introduces no
    temporal logic of its own, so it is causal at bar ``t`` whenever every
    series passed in ``levels`` already is (clustered support/resistance,
    floor pivots, Fibonacci retracements off confirmed swings, moving
    averages, and the round-number proxy are all already causal features
    computed elsewhere in this module / ``feature_builder``).

    Idea/aggregation shape from tradingview-mcp ``core/services/
    indicators.py::compute_trade_setup`` (MIT), which deduplicates several
    S/R methods (pivots, EMAs, Bollinger bands, Parabolic SAR) into one
    supports/resistances list; the specific methods and the counting
    mechanics here are our own.

    Returns a float Series (bar-level agreement count) aligned to ``price``'s
    index.
    """
    price_vals = price.to_numpy(dtype=float)
    n = len(price)
    count = np.zeros(n, dtype=float)
    for candidates in levels.values():
        method_hit = np.zeros(n, dtype=bool)
        for level_series in candidates:
            level_vals = level_series.to_numpy(dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                pct_dist = np.abs(price_vals - level_vals) / np.abs(price_vals) * 100.0
            hit = np.nan_to_num(pct_dist, nan=np.inf) <= tolerance_pct
            method_hit |= hit
        count += method_hit.astype(float)
    return pd.Series(count, index=price.index, name="level_confluence_count")


# --------------------------------------------------------------------------
# Volume / price relationship
# --------------------------------------------------------------------------

#: Elevated-volume threshold for the divergence flag: relative volume (see
#: ``indicators.relative_volume`` -- today's volume over the MEAN of the
#: prior 20 sessions, today excluded) at or above this multiple counts as
#: "loud". Matches ``detect_breakout``'s own ``volume_mult`` default, so
#: "loud" means the same thing here as it does everywhere else in this
#: module. Config-free constant, documented here rather than pulled from
#: ``config.calibration``, since this is a fixed feature-level convention
#: shared by every strategy that reads the flag, not a per-strategy tunable.
VOLUME_DIVERGENCE_REL_VOLUME_THRESHOLD = 1.5
#: "No follow-through" ceiling: an absolute same-day (close-vs-prior-close)
#: price change below this percent, on volume that cleared the threshold
#: above, is the absorption/distribution warning -- heavy participation that
#: did not move the price.
VOLUME_DIVERGENCE_PRICE_CHANGE_PCT = 1.0


def volume_divergence(
    bars: pd.DataFrame,
    rel_volume: pd.Series,
    rel_volume_threshold: float = VOLUME_DIVERGENCE_REL_VOLUME_THRESHOLD,
    price_change_threshold_pct: float = VOLUME_DIVERGENCE_PRICE_CHANGE_PCT,
) -> pd.Series:
    """Volume-divergence warning: elevated relative volume with little
    same-day price follow-through -- a possible absorption/distribution
    signature (heavy participation that did not move the price).

    True when ``rel_volume`` is at or above ``rel_volume_threshold`` AND the
    day's own close-vs-prior-close percent change (``indicators.roc`` with a
    1-bar window) is smaller in magnitude than ``price_change_threshold_pct``.
    Both inputs are knowable the instant the bar prints, so this needs no
    forward-looking confirmation delay -- it is causal same-bar.

    Idea from tradingview-mcp ``core/services/scanner_service.py::
    volume_confirmation_analyze`` (MIT): ``vol_ratio>=1.5 AND
    abs(price_change)<1.0``. Reimplemented from scratch against this
    codebase's own ``rel_volume`` convention (mean-based 20-day baseline that
    explicitly EXCLUDES today via ``.shift(1)`` -- see
    ``indicators.relative_volume``), not TradingView's own today-inclusive
    ``volume.SMA20`` ratio, which the market-signal assessment flags as a
    biased baseline.

    Requires ``bars`` to have a ``close`` column. ``rel_volume`` is passed in
    (not recomputed) so callers reuse the same series already computed
    elsewhere -- mirrors ``volatility_contraction``'s ``bandwidth`` parameter.

    Warm-up: False wherever ``rel_volume`` or the price-change series is NaN
    -- degrades to "no divergence flagged" rather than raising or crashing.
    """
    close = bars["close"].astype(float)
    price_change_pct = roc(close, 1)
    loud = rel_volume >= rel_volume_threshold
    quiet_move = price_change_pct.abs() < price_change_threshold_pct
    return (loud & quiet_move).fillna(False).rename("volume_divergence")
