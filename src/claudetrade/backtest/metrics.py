"""Performance metrics: report everything, especially the inconvenient parts.

The app's headline number is the win/loss ratio (winning trades / losing
trades). That single ratio is trivial to game -- take profits early and let
losers run, and it climbs while the account bleeds. Every function in this
module exists to make that manipulation visible rather than possible:

* Win/loss ratio is **always** reported next to expectancy, profit factor,
  average win and average loss, so "many small wins, one huge loss" cannot be
  presented without its own refutation sitting beside it.
* Breakeven trades are excluded from both the win and the loss count (they can
  neither pad the numerator nor hide in the denominator).
* When there are zero losing trades, the ratio is reported as ``inf`` -- not
  silently capped or hidden -- and flagged as degenerate; a "perfect" win/loss
  ratio from a handful of trades is a red flag, not an achievement.
* ``validation_warnings`` names the exact "tiny wins, huge losses" pathology
  explicitly, rather than leaving a reader to infer it from raw numbers.

Only ``Trade``s that are fully closed are graded. An open trade cannot be
classified into win/loss/breakeven (see ``Trade.outcome``), and letting one
sit uncounted would itself inflate the ratio -- so this module asserts none
are open rather than silently skipping them.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from claudetrade.config import BacktestConfig
from claudetrade.domain import Trade, TradeOutcome

log = logging.getLogger(__name__)

_Z_95 = 1.959963985  # two-sided 95% normal quantile
_BOOTSTRAP_RESAMPLES = 2000

# ADR-0007 Decision 3(a): a ratio metric computed over too few return
# observations, or over returns with ~zero variance, is not a number -- it is
# noise wearing a number's clothes (the historical failure mode here was a
# reported Sharpe of -9.2e16 from a two-point equity curve). Below these
# floors, compute_metrics returns None with a machine-readable reason instead
# of a numeric fallback (0.0, inf, or NaN all silently pass for "a result").
# Guard shape adopted from ROT `src/rot/backtest/metrics.py:48-95`
# (Mattbusel/Reddit-Options-Trader-ROT-, MIT licensed) -- that module returns
# None below 5 daily-return observations or a return stdev under 1e-10; this
# module reuses both thresholds against its own numpy/session-return
# representation rather than ROT's plain-list implementation.
_MIN_RETURN_OBSERVATIONS_FOR_RATIOS = 5
_ZERO_VARIANCE_FLOOR = 1e-10


class EquityPointLike(Protocol):
    """The subset of ``backtest.portfolio.EquityPoint`` metrics needs.

    Kept as a structural Protocol (rather than importing ``portfolio.py``) so
    this module has no dependency in that direction; segment metrics can also
    be computed against a synthetic/empty curve.
    """

    equity: float
    exposure_pct: float
    drawdown_pct: float


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ConfidenceInterval:
    """A two-sided interval plus the method used to derive it."""

    low: float
    high: float
    method: str = "normal_approximation"


@dataclass(slots=True)
class GrossNetMetrics:
    """The same headline figures before and after transaction costs.

    A strategy that is profitable gross but unprofitable net looks identical
    to a profitable strategy if you only ever look at net numbers with no
    gross comparison -- which is exactly the comparison this holds.
    """

    gross_total_return_pct: float
    net_total_return_pct: float
    gross_expectancy: float
    net_expectancy: float
    gross_profit_factor: float
    net_profit_factor: float


@dataclass(slots=True)
class PerformanceMetrics:
    """Everything needed to judge a set of completed trades honestly."""

    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float

    #: Wins / losses. ``inf`` when there are zero losses (see
    #: ``win_loss_ratio_is_degenerate``); never silently clamped.
    win_loss_ratio: float
    win_loss_ratio_is_degenerate: bool

    average_win: float
    average_loss: float  # positive magnitude
    avg_win_over_avg_loss: float
    payoff_ratio: float  # same ratio expressed in R-multiples, not dollars

    profit_factor: float
    expectancy_dollars: float
    expectancy_r: float
    r_expectancy: float  # alias of expectancy_r, kept for naming parity with the spec

    median_return_pct: float
    total_return_pct: float
    annualised_return_pct: float

    max_drawdown_pct: float
    max_drawdown_duration_days: int

    #: None (never 0.0/inf/NaN as a stand-in) when the sample is too small or
    #: has ~zero variance to support the ratio; see ``unavailable_reasons``
    #: for the machine-readable "why". See ADR-0007 Decision 3(a).
    sharpe: float | None
    sortino: float | None
    calmar: float | None

    largest_win: float
    largest_loss: float  # negative
    average_holding_days: float
    exposure_pct: float
    turnover: float

    gross_vs_net: GrossNetMetrics

    win_rate_se: float
    win_rate_ci: ConfidenceInterval
    expectancy_se: float
    expectancy_ci: ConfidenceInterval

    #: Profit concentration diagnostics, consumed by ``validation_warnings``.
    top3_profit_share_pct: float
    max_symbol_profit_share_pct: float
    max_sector_profit_share_pct: float

    #: ADR-0007 Decision 3(c): a minimum-trade-count *and* statistical-test
    #: gate, computed inside ``compute_metrics`` rather than left to a caller
    #: to remember to apply. True only when the sample both clears
    #: ``BacktestConfig.min_trades_for_validation`` and the bootstrap 95% CI
    #: for expectancy excludes zero -- a count floor alone says "enough data
    #: to ask the question", not "the answer is yes".
    is_statistically_significant: bool
    #: Machine-readable reason when the above is False; None when it's True.
    significance_reason: str | None

    #: Reasons a ratio metric above (``sharpe``/``sortino``/``calmar``) came
    #: out None instead of a number, keyed by field name. Never populated for
    #: a metric that has a real value. See ADR-0007 Decision 3(a).
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Core computation
# --------------------------------------------------------------------------


def compute_metrics(
    trades: Sequence[Trade],
    equity_curve: Sequence[EquityPointLike],
    config: BacktestConfig,
    *,
    breakeven_threshold_pct: float = 0.05,
    min_return_observations_for_ratios: int = _MIN_RETURN_OBSERVATIONS_FOR_RATIOS,
) -> PerformanceMetrics:
    """Compute the full metric set for a batch of completed trades.

    Args:
        trades: Completed trades only. An open trade cannot be classified
            (``Trade.outcome`` raises), so this asserts none are open rather
            than silently filtering -- a dropped open trade is exactly the
            kind of quiet omission that inflates a win/loss ratio.
        equity_curve: Session-by-session portfolio marks. Drawdown/exposure/
            turnover default to 0.0 when fewer than two points are supplied
            -- e.g. for a per-segment slice with no dedicated equity series.
            Sharpe/Sortino/Calmar instead come out ``None`` in that case (see
            ``min_return_observations_for_ratios`` below) rather than
            defaulting to 0.0, because 0.0 reads as "measured and flat", not
            "unmeasurable".
        config: Supplies the risk-free rate, trading-day convention and the
            ``min_trades_for_validation`` significance floor.
        breakeven_threshold_pct: Net returns inside +/- this percentage are
            excluded from both the win and loss counts.
        min_return_observations_for_ratios: Floor on return observations (and,
            for Calmar, on measurable drawdown) below which Sharpe/Sortino/
            Calmar come out ``None`` with a reason in ``unavailable_reasons``
            instead of a number. Mirrors the count-floor shape of
            ``BacktestConfig.min_trades_for_validation`` but is exposed as a
            keyword here (like ``breakeven_threshold_pct`` above) rather than
            added to ``BacktestConfig``, since it governs a purely
            statistical property of the equity curve, not a trading-account
            policy. Default matches ROT's guard (see module-level constant).
    """
    assert all(not t.is_open for t in trades), (
        "compute_metrics received an open trade; force-close it first "
        "(time stop / end-of-backtest / delisting) -- an uncounted open "
        "trade would silently inflate the win/loss ratio"
    )

    outcomes = [t.outcome(breakeven_threshold_pct) for t in trades]
    wins = [t for t, o in zip(trades, outcomes, strict=True) if o is TradeOutcome.WIN]
    losses = [t for t, o in zip(trades, outcomes, strict=True) if o is TradeOutcome.LOSS]
    breakeven = [t for t, o in zip(trades, outcomes, strict=True) if o is TradeOutcome.BREAKEVEN]

    trade_count = len(trades)
    n_wins, n_losses, n_breakeven = len(wins), len(losses), len(breakeven)
    win_rate = n_wins / trade_count if trade_count else 0.0

    is_degenerate = n_losses == 0
    win_loss_ratio = (math.inf if n_wins > 0 else 0.0) if is_degenerate else n_wins / n_losses

    average_win = statistics.fmean(t.net_pnl for t in wins) if wins else 0.0
    average_loss = statistics.fmean(-t.net_pnl for t in losses) if losses else 0.0
    avg_win_over_avg_loss = (
        (average_win / average_loss) if average_loss > 0 else (math.inf if average_win > 0 else 0.0)
    )

    win_r = statistics.fmean(t.r_multiple for t in wins) if wins else 0.0
    loss_r = statistics.fmean(-t.r_multiple for t in losses) if losses else 0.0
    payoff_ratio = (win_r / loss_r) if loss_r > 0 else (math.inf if win_r > 0 else 0.0)

    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = sum(t.net_pnl for t in trades if t.net_pnl < 0)
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else (
        math.inf if gross_profit > 0 else 0.0
    )

    expectancy_dollars = statistics.fmean(t.net_pnl for t in trades) if trades else 0.0
    expectancy_r = statistics.fmean(t.r_multiple for t in trades) if trades else 0.0
    median_return_pct = statistics.median(t.net_return_pct for t in trades) if trades else 0.0

    largest_win = max((t.net_pnl for t in trades), default=0.0)
    largest_win = max(largest_win, 0.0)
    largest_loss = min((t.net_pnl for t in trades), default=0.0)
    largest_loss = min(largest_loss, 0.0)
    average_holding_days = statistics.fmean(t.holding_days for t in trades) if trades else 0.0

    gross_vs_net = _gross_vs_net(trades, expectancy_dollars, profit_factor)

    (
        total_return_pct,
        annualised_return_pct,
        max_dd_pct,
        max_dd_days,
        sharpe,
        sortino,
        calmar,
        unavailable_reasons,
    ) = _equity_curve_metrics(equity_curve, config, min_return_observations_for_ratios)
    exposure_pct = (
        statistics.fmean(p.exposure_pct for p in equity_curve) if equity_curve else 0.0
    )
    turnover = _turnover(trades, config.initial_capital_usd)

    win_rate_se, win_rate_ci = _win_rate_confidence_interval(n_wins, n_losses)
    expectancy_se, expectancy_ci = _bootstrap_expectancy_ci(trades, config.random_seed)

    top3_share, max_symbol_share, max_sector_share = _concentration(trades)

    # ADR-0007 Decision 3(c): significance is a count floor AND a statistical
    # test, both evaluated here rather than left for a caller to opt into --
    # the same "attach it inside compute_metrics, not on request" principle
    # already used for `warnings` below. A trade count above the floor only
    # proves there is enough data to ask whether the edge is real; the
    # bootstrap CI on expectancy is what actually answers that question.
    count_floor_met = trade_count >= config.min_trades_for_validation
    edge_distinguishable_from_zero = bool(trades) and not (
        expectancy_ci.low <= 0.0 <= expectancy_ci.high
    )
    is_statistically_significant = count_floor_met and edge_distinguishable_from_zero
    if not count_floor_met:
        significance_reason = (
            f"trade_count_below_floor: {trade_count} completed trade(s), below the "
            f"{config.min_trades_for_validation}-trade minimum"
        )
    elif not edge_distinguishable_from_zero:
        significance_reason = (
            "expectancy_ci_includes_zero: the 95% bootstrap interval for per-trade "
            "expectancy spans zero, so this sample cannot yet rule out 'no edge at all'"
        )
    else:
        significance_reason = None

    metrics = PerformanceMetrics(
        trade_count=trade_count,
        winning_trades=n_wins,
        losing_trades=n_losses,
        breakeven_trades=n_breakeven,
        win_rate=win_rate,
        win_loss_ratio=win_loss_ratio,
        win_loss_ratio_is_degenerate=is_degenerate,
        average_win=average_win,
        average_loss=average_loss,
        avg_win_over_avg_loss=avg_win_over_avg_loss,
        payoff_ratio=payoff_ratio,
        profit_factor=profit_factor,
        expectancy_dollars=expectancy_dollars,
        expectancy_r=expectancy_r,
        r_expectancy=expectancy_r,
        median_return_pct=median_return_pct,
        total_return_pct=total_return_pct,
        annualised_return_pct=annualised_return_pct,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=max_dd_days,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        largest_win=largest_win,
        largest_loss=largest_loss,
        average_holding_days=average_holding_days,
        exposure_pct=exposure_pct,
        turnover=turnover,
        gross_vs_net=gross_vs_net,
        win_rate_se=win_rate_se,
        win_rate_ci=win_rate_ci,
        expectancy_se=expectancy_se,
        expectancy_ci=expectancy_ci,
        top3_profit_share_pct=top3_share,
        max_symbol_profit_share_pct=max_symbol_share,
        max_sector_profit_share_pct=max_sector_share,
        is_statistically_significant=is_statistically_significant,
        significance_reason=significance_reason,
        unavailable_reasons=unavailable_reasons,
    )
    # Attach the caveats here rather than leaving it to each caller. A warning
    # that depends on every consumer remembering to ask for it is a warning
    # that will eventually be missed, and the whole point of these is that a
    # flattering win/loss ratio never travels without its caveats.
    metrics.warnings = validation_warnings(metrics, config)
    return metrics


def _gross_vs_net(
    trades: Sequence[Trade],
    net_expectancy: float,
    net_profit_factor: float,
) -> GrossNetMetrics:
    """Before/after-cost comparison.

    Gross total return is approximated by adding total transaction costs back
    onto the net figure (additive, ignoring compounding order) -- there is no
    separate gross equity curve, only the net one the portfolio actually
    marked. This is a documented approximation, not a re-simulation.
    """
    if not trades:
        return GrossNetMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    total_costs = sum(
        t.commission_total + t.fees_total + t.borrow_cost_total + (t.gross_pnl - t.net_pnl - (
            t.commission_total + t.fees_total + t.borrow_cost_total
        ))
        for t in trades
    )
    # The above simplifies to sum(gross_pnl - net_pnl); written out for clarity.
    total_costs = sum(t.gross_pnl - t.net_pnl for t in trades)
    gross_expectancy = statistics.fmean(t.gross_pnl for t in trades)
    gross_wins = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_losses = sum(t.gross_pnl for t in trades if t.gross_pnl < 0)
    gross_profit_factor = (
        (gross_wins / abs(gross_losses)) if gross_losses != 0 else (math.inf if gross_wins > 0 else 0.0)
    )
    net_total_return_pct = sum(t.net_pnl for t in trades)  # placeholder overwritten by caller scale
    return GrossNetMetrics(
        gross_total_return_pct=net_total_return_pct + total_costs,  # dollars; caller rescales below
        net_total_return_pct=net_total_return_pct,
        gross_expectancy=gross_expectancy,
        net_expectancy=net_expectancy,
        gross_profit_factor=gross_profit_factor,
        net_profit_factor=net_profit_factor,
    )


def _equity_curve_metrics(
    equity_curve: Sequence[EquityPointLike],
    config: BacktestConfig,
    min_return_observations: int,
) -> tuple[float, float, float, int, float | None, float | None, float | None, dict[str, str]]:
    """Return (total_return_pct, annualised_pct, max_dd_pct, max_dd_days, sharpe, sortino, calmar, unavailable_reasons).

    ``sharpe``/``sortino``/``calmar`` are ``None`` -- never 0.0, ``inf`` or NaN
    standing in for "unmeasurable" -- whenever the sample is too small or has
    ~zero variance/drawdown to support the ratio; ``unavailable_reasons``
    explains why, keyed by field name. See ADR-0007 Decision 3(a).
    """
    if len(equity_curve) < 2:
        reason = "no_equity_curve: fewer than 2 marks to derive a return from"
        return 0.0, 0.0, 0.0, 0, None, None, None, {
            "sharpe": reason,
            "sortino": reason,
            "calmar": reason,
        }

    equities = np.array([p.equity for p in equity_curve], dtype=float)
    initial, final = equities[0], equities[-1]
    total_return_pct = 100.0 * (final / initial - 1.0) if initial > 0 else 0.0

    n_periods = len(equities) - 1
    trading_days = max(1, config.trading_days_per_year)
    if initial > 0 and final > 0 and n_periods > 0:
        annualised_return_pct = 100.0 * ((final / initial) ** (trading_days / n_periods) - 1.0)
    else:
        annualised_return_pct = 0.0

    max_dd_pct = max((p.drawdown_pct for p in equity_curve), default=0.0)
    max_dd_days = _max_drawdown_duration(equities)

    daily_returns = np.diff(equities) / np.where(equities[:-1] != 0, equities[:-1], np.nan)
    daily_returns = daily_returns[~np.isnan(daily_returns)]
    rf_daily = config.risk_free_rate_annual / trading_days

    unavailable: dict[str, str] = {}
    sharpe, sharpe_reason = _sharpe_ratio(daily_returns, rf_daily, trading_days, min_return_observations)
    if sharpe_reason is not None:
        unavailable["sharpe"] = sharpe_reason
    sortino, sortino_reason = _sortino_ratio(
        daily_returns, rf_daily, trading_days, min_return_observations
    )
    if sortino_reason is not None:
        unavailable["sortino"] = sortino_reason

    if daily_returns.size < min_return_observations:
        calmar = None
        unavailable["calmar"] = (
            f"insufficient_sample: {daily_returns.size} return observation(s), below the "
            f"{min_return_observations}-observation floor"
        )
    elif max_dd_pct < _ZERO_VARIANCE_FLOOR:
        # No measurable drawdown to divide by. The old behaviour (math.inf)
        # read as "infinitely good", which is exactly backwards for a ratio
        # that is actually undefined here -- there is no evidence about how
        # the strategy behaves in a drawdown at all.
        calmar = None
        unavailable["calmar"] = (
            f"no_drawdown: max drawdown {max_dd_pct:.2e}% is ~zero, so annual-return / "
            "drawdown is undefined rather than meaningfully infinite"
        )
    else:
        calmar = (annualised_return_pct / 100.0) / (max_dd_pct / 100.0)

    return (
        total_return_pct,
        annualised_return_pct,
        max_dd_pct,
        max_dd_days,
        sharpe,
        sortino,
        calmar,
        unavailable,
    )


def _max_drawdown_duration(equities: np.ndarray) -> int:
    """Longest run (in bars) spent below a prior high-water mark."""
    peak = equities[0]
    longest = 0
    current = 0
    for value in equities:
        if value >= peak:
            peak = value
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _sharpe_ratio(
    daily_returns: np.ndarray, rf_daily: float, trading_days: int, min_observations: int
) -> tuple[float | None, str | None]:
    """Annualised Sharpe ratio, or ``(None, reason)`` if the sample can't support one.

    Guard shape adopted from ROT `backtest/metrics.py::compute_sharpe_ratio`
    (lines 48-68, MIT licensed): None below a return-observation floor or
    below a near-zero-stdev floor, rather than a numeric fallback. The
    formula itself (mean excess return / stdev, annualised by sqrt(trading
    days)) is unchanged from this module's prior implementation.
    """
    n = daily_returns.size
    if n < min_observations:
        return None, (
            f"insufficient_sample: {n} return observation(s), below the "
            f"{min_observations}-observation floor"
        )
    excess = daily_returns - rf_daily
    std = float(np.std(excess, ddof=1))
    if std < _ZERO_VARIANCE_FLOOR:
        return None, (
            f"zero_variance: return stdev {std:.2e} is below the "
            f"{_ZERO_VARIANCE_FLOOR:.0e} floor"
        )
    return float(np.mean(excess) / std * math.sqrt(trading_days)), None


def _sortino_ratio(
    daily_returns: np.ndarray, rf_daily: float, trading_days: int, min_observations: int
) -> tuple[float | None, str | None]:
    """Annualised Sortino ratio (downside deviation only), or ``(None, reason)``.

    Same guard shape as ``_sharpe_ratio`` (see ROT `backtest/metrics.py::
    compute_sortino_ratio`, lines 71-95, MIT licensed). Previously a downside
    sample too thin to estimate a deviation from returned 0.0 or ``math.inf``
    depending on the sign of the mean excess return; both were numeric
    fallbacks standing in for "cannot be estimated", which is exactly what
    ADR-0007 Decision 3(a) rules out.
    """
    n = daily_returns.size
    if n < min_observations:
        return None, (
            f"insufficient_sample: {n} return observation(s), below the "
            f"{min_observations}-observation floor"
        )
    excess = daily_returns - rf_daily
    downside = excess[excess < 0]
    if downside.size < 2:
        return None, (
            f"insufficient_downside_sample: only {downside.size} negative-return period(s), "
            "too few to estimate a downside deviation"
        )
    downside_dev = float(np.std(downside, ddof=1))
    if downside_dev < _ZERO_VARIANCE_FLOOR:
        return None, (
            f"zero_variance: downside deviation {downside_dev:.2e} is below the "
            f"{_ZERO_VARIANCE_FLOOR:.0e} floor"
        )
    return float(np.mean(excess) / downside_dev * math.sqrt(trading_days)), None


def _turnover(trades: Sequence[Trade], initial_capital: float) -> float:
    """Gross dollar volume traded (entry + exit notional) / starting capital.

    Not annualised -- a raw multiple over the full test window. A strategy
    trading 3x its capital in gross volume over the period reports 3.0 here.
    """
    if initial_capital <= 0 or not trades:
        return 0.0
    gross_volume = sum(
        abs(t.entry_price * t.shares) + abs((t.exit_price or 0.0) * t.shares) for t in trades
    )
    return gross_volume / initial_capital


def _win_rate_confidence_interval(n_wins: int, n_losses: int) -> tuple[float, ConfidenceInterval]:
    """Normal-approximation 95% CI for the win rate, over wins+losses only.

    Breakeven trades are excluded from the denominator here too -- the
    interval describes uncertainty in the win/loss split, the same population
    the headline ratio is drawn from.
    """
    n = n_wins + n_losses
    if n == 0:
        return 0.0, ConfidenceInterval(0.0, 0.0)
    p = n_wins / n
    se = math.sqrt(p * (1 - p) / n)
    low = max(0.0, p - _Z_95 * se)
    high = min(1.0, p + _Z_95 * se)
    return se, ConfidenceInterval(low, high)


def _bootstrap_expectancy_ci(
    trades: Sequence[Trade], seed: int
) -> tuple[float, ConfidenceInterval]:
    """Bootstrap CI for per-trade expectancy.

    Trade P&L is typically skewed (many small wins, occasional large losses
    or vice versa), so a bootstrap of the empirical distribution is preferred
    over a normal approximation for the mean. Deterministic: seeded from
    ``BacktestConfig.random_seed`` so identical inputs reproduce identical
    intervals.
    """
    if not trades:
        return 0.0, ConfidenceInterval(0.0, 0.0, method="bootstrap")
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    if pnls.size < 2:
        return 0.0, ConfidenceInterval(float(pnls[0]), float(pnls[0]), method="bootstrap")
    rng = np.random.default_rng(seed)
    n = pnls.size
    resample_means = np.empty(_BOOTSTRAP_RESAMPLES, dtype=float)
    for i in range(_BOOTSTRAP_RESAMPLES):
        sample = rng.choice(pnls, size=n, replace=True)
        resample_means[i] = sample.mean()
    se = float(np.std(resample_means, ddof=1))
    low, high = np.percentile(resample_means, [2.5, 97.5])
    return se, ConfidenceInterval(float(low), float(high), method="bootstrap")


def _concentration(trades: Sequence[Trade]) -> tuple[float, float, float]:
    """Profit concentration: top-3 trades, and the most concentrated symbol/sector.

    Returns percentages of *total gross profit* (sum of positive net_pnl)
    attributable to the top 3 winning trades, the single most profitable
    symbol, and the single most profitable sector. A strategy whose entire
    return comes from one lucky name is not a strategy.
    """
    profits = [t.net_pnl for t in trades if t.net_pnl > 0]
    total_profit = sum(profits)
    if total_profit <= 0:
        return 0.0, 0.0, 0.0
    top3 = sum(sorted(profits, reverse=True)[:3])
    top3_share = 100.0 * top3 / total_profit

    by_symbol: dict[str, float] = defaultdict(float)
    by_sector: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.net_pnl > 0:
            by_symbol[t.symbol] += t.net_pnl
            by_sector[t.sector or "unknown"] += t.net_pnl
    max_symbol_share = 100.0 * max(by_symbol.values(), default=0.0) / total_profit
    max_sector_share = 100.0 * max(by_sector.values(), default=0.0) / total_profit
    return top3_share, max_symbol_share, max_sector_share


# --------------------------------------------------------------------------
# Validation warnings
# --------------------------------------------------------------------------


def validation_warnings(metrics: PerformanceMetrics, config: BacktestConfig) -> list[str]:
    """Plain-language warnings a reader must see before trusting the headline number."""
    warnings: list[str] = []

    # ADR-0007 Decision 3(c): an explicit, greppable marker -- distinct from
    # the softer "only N trades" wording below -- so a reader (or a test)
    # never has to infer significance from the trade count alone.
    if not metrics.is_statistically_significant:
        warnings.append(
            f"NOT STATISTICALLY SIGNIFICANT: {metrics.significance_reason}. Every ratio and "
            "point estimate above should be read as directional only, not as evidence of a "
            "durable edge."
        )

    if metrics.trade_count < config.min_trades_for_validation:
        warnings.append(
            f"only {metrics.trade_count} completed trades, below the "
            f"{config.min_trades_for_validation} minimum this app treats as validated; "
            "treat every metric here as preliminary."
        )
    elif metrics.trade_count < config.min_trades_for_confidence and metrics.win_loss_ratio > 1.0:
        warnings.append(
            f"win/loss ratio ({_fmt_ratio(metrics.win_loss_ratio)}) looks favourable but rests on "
            f"only {metrics.trade_count} trades, fewer than the {config.min_trades_for_confidence} "
            "needed for a reliable estimate -- see the confidence interval, not just the point estimate."
        )

    if metrics.expectancy_dollars <= 0 and metrics.win_loss_ratio > 1.0:
        warnings.append(
            "PATHOLOGY: win/loss ratio is above 1.0 (wins outnumber losses) but per-trade "
            f"expectancy is ${metrics.expectancy_dollars:,.2f} (<= 0) -- this is the classic "
            "'many tiny wins, occasional huge loss' failure mode. The win/loss ratio alone is "
            "actively misleading here; the strategy loses money on average despite winning "
            "more often than it loses."
        )

    if metrics.average_loss > 2.0 * metrics.average_win and metrics.average_win > 0:
        warnings.append(
            f"average loss (${metrics.average_loss:,.2f}) is more than double the average win "
            f"(${metrics.average_win:,.2f}); a single bad trade can erase several wins."
        )

    if metrics.top3_profit_share_pct > 50.0:
        warnings.append(
            f"the top 3 winning trades account for {metrics.top3_profit_share_pct:.1f}% of total "
            "gross profit; the result is not diversified across opportunities."
        )
    if metrics.max_symbol_profit_share_pct > 40.0:
        warnings.append(
            f"a single symbol accounts for {metrics.max_symbol_profit_share_pct:.1f}% of total "
            "gross profit; results may not generalise beyond that one name."
        )
    if metrics.max_sector_profit_share_pct > 40.0:
        warnings.append(
            f"a single sector accounts for {metrics.max_sector_profit_share_pct:.1f}% of total "
            "gross profit; results may be a sector-specific regime effect rather than edge."
        )

    gn = metrics.gross_vs_net
    if gn.gross_total_return_pct > 0 and gn.net_total_return_pct <= 0:
        warnings.append(
            f"profitable before costs (gross P&L ${gn.gross_total_return_pct:,.2f}) but "
            f"unprofitable after costs (net P&L ${gn.net_total_return_pct:,.2f}); the edge does "
            "not survive realistic transaction costs."
        )

    if metrics.win_loss_ratio_is_degenerate and metrics.trade_count > 0:
        warnings.append(
            "win/loss ratio is infinite (zero losing trades in the sample) -- this is a "
            "degenerate statistic, not a validated edge; it usually means the sample is too "
            "small or the time/stop rules have not yet been tested against an adverse move."
        )

    return warnings


def _fmt_ratio(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.2f}"


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------

SegmentDimension = str  # documented values below, kept as str to avoid import cycles

_DAYS_TO_EARNINGS_BUCKETS = (
    (0, 3, "0-3d"),
    (4, 7, "4-7d"),
    (8, 14, "8-14d"),
    (15, 30, "15-30d"),
)

_CONFIDENCE_BUCKETS = (
    (0.0, 0.5, "<0.50"),
    (0.5, 0.65, "0.50-0.65"),
    (0.65, 0.8, "0.65-0.80"),
    (0.8, 1.01, ">=0.80"),
)

_HOLDING_PERIOD_BUCKETS = (
    (0, 5, "0-5d"),
    (6, 10, "6-10d"),
    (11, 20, "11-20d"),
    (21, 10_000, ">20d"),
)


def _bucket(value: float | int | None, ranges: tuple[tuple[float, float, str], ...]) -> str:
    if value is None:
        return "unknown"
    for low, high, label in ranges:
        if low <= value <= high:
            return label
    return "other"


def _segment_key(trade: Trade, dimension: str) -> str:
    if dimension == "strategy":
        return trade.strategy or "unknown"
    if dimension == "direction":
        return trade.direction.value
    if dimension == "year":
        return str(trade.entry_session.year)
    if dimension == "regime":
        return trade.regime_at_entry.value
    if dimension == "sector":
        return trade.sector or "unknown"
    if dimension == "market_cap_bucket":
        return trade.market_cap_bucket or "unknown"
    if dimension == "sentiment_source":
        return trade.sentiment_source or "none"
    if dimension == "days_to_earnings_bucket":
        return _bucket(trade.days_to_earnings_at_entry, _DAYS_TO_EARNINGS_BUCKETS)
    if dimension == "confidence_bucket":
        return _bucket(trade.confidence_at_entry, _CONFIDENCE_BUCKETS)
    if dimension == "holding_period_bucket":
        return _bucket(trade.holding_days, _HOLDING_PERIOD_BUCKETS)
    raise ValueError(f"unknown segmentation dimension: {dimension!r}")


#: Every supported segmentation dimension, for callers that want to iterate all of them.
SEGMENT_DIMENSIONS: tuple[str, ...] = (
    "strategy",
    "direction",
    "year",
    "regime",
    "sector",
    "market_cap_bucket",
    "sentiment_source",
    "days_to_earnings_bucket",
    "confidence_bucket",
    "holding_period_bucket",
)


def segment_metrics(
    trades: Sequence[Trade],
    equity_curve: Sequence[EquityPointLike],  # noqa: ARG001 -- kept for API symmetry; see docstring
    config: BacktestConfig,
    dimension: str,
    *,
    breakeven_threshold_pct: float = 0.05,
) -> dict[str, PerformanceMetrics]:
    """Slice completed trades by ``dimension`` and compute metrics per bucket.

    Equity-curve-derived figures (Sharpe, drawdown, exposure) are portfolio-
    level and not meaningful sliced by trade attribute, so each bucket is
    computed against an empty equity curve -- those fields report 0.0 for
    segments (see ``compute_metrics``); only the trade-level figures (win/loss
    ratio, expectancy, profit factor, ...) are segment-specific.

    Reports every bucket that has at least one trade -- never just the best
    one. Cherry-picking a single favourable segment and presenting it as "the"
    result is exactly the failure mode ``walkforward.regime_segmented_results``
    guards against at the walk-forward level; this is the same discipline
    applied within a single run.
    """
    buckets: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        buckets[_segment_key(trade, dimension)].append(trade)
    return {
        bucket: compute_metrics(bucket_trades, [], config, breakeven_threshold_pct=breakeven_threshold_pct)
        for bucket, bucket_trades in sorted(buckets.items())
    }


__all__ = [
    "SEGMENT_DIMENSIONS",
    "ConfidenceInterval",
    "EquityPointLike",
    "GrossNetMetrics",
    "PerformanceMetrics",
    "compute_metrics",
    "segment_metrics",
    "validation_warnings",
]
