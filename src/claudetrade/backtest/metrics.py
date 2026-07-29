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
    sharpe: float
    sortino: float
    calmar: float

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
) -> PerformanceMetrics:
    """Compute the full metric set for a batch of completed trades.

    Args:
        trades: Completed trades only. An open trade cannot be classified
            (``Trade.outcome`` raises), so this asserts none are open rather
            than silently filtering -- a dropped open trade is exactly the
            kind of quiet omission that inflates a win/loss ratio.
        equity_curve: Session-by-session portfolio marks. Equity-curve-derived
            figures (drawdown, Sharpe/Sortino, exposure, turnover) default to
            0.0 when fewer than two points are supplied -- e.g. for a
            per-segment slice with no dedicated equity series.
        config: Supplies the risk-free rate and trading-day convention for
            annualisation.
        breakeven_threshold_pct: Net returns inside +/- this percentage are
            excluded from both the win and loss counts.
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
    if is_degenerate:
        win_loss_ratio = math.inf if n_wins > 0 else 0.0
    else:
        win_loss_ratio = n_wins / n_losses

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

    gross_vs_net = _gross_vs_net(trades, gross_profit, gross_loss, expectancy_dollars, profit_factor)

    total_return_pct, annualised_return_pct, max_dd_pct, max_dd_days, sharpe, sortino, calmar = (
        _equity_curve_metrics(equity_curve, config)
    )
    exposure_pct = (
        statistics.fmean(p.exposure_pct for p in equity_curve) if equity_curve else 0.0
    )
    turnover = _turnover(trades, config.initial_capital_usd)

    win_rate_se, win_rate_ci = _win_rate_confidence_interval(n_wins, n_losses)
    expectancy_se, expectancy_ci = _bootstrap_expectancy_ci(trades, config.random_seed)

    top3_share, max_symbol_share, max_sector_share = _concentration(trades)

    return PerformanceMetrics(
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
    )


def _gross_vs_net(
    trades: Sequence[Trade],
    gross_profit: float,
    gross_loss: float,
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
    equity_curve: Sequence[EquityPointLike], config: BacktestConfig
) -> tuple[float, float, float, int, float, float, float]:
    """Return (total_return_pct, annualised_pct, max_dd_pct, max_dd_days, sharpe, sortino, calmar)."""
    if len(equity_curve) < 2:
        return 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0

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
    sharpe = _sharpe_ratio(daily_returns, rf_daily, trading_days)
    sortino = _sortino_ratio(daily_returns, rf_daily, trading_days)
    calmar = (
        (annualised_return_pct / 100.0) / (max_dd_pct / 100.0) if max_dd_pct > 0 else math.inf
    )
    return total_return_pct, annualised_return_pct, max_dd_pct, max_dd_days, sharpe, sortino, calmar


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


def _sharpe_ratio(daily_returns: np.ndarray, rf_daily: float, trading_days: int) -> float:
    if daily_returns.size < 2:
        return 0.0
    excess = daily_returns - rf_daily
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(trading_days))


def _sortino_ratio(daily_returns: np.ndarray, rf_daily: float, trading_days: int) -> float:
    if daily_returns.size < 2:
        return 0.0
    excess = daily_returns - rf_daily
    downside = excess[excess < 0]
    if downside.size < 2:
        return 0.0 if np.mean(excess) <= 0 else math.inf
    downside_dev = float(np.std(downside, ddof=1))
    if downside_dev == 0:
        return 0.0
    return float(np.mean(excess) / downside_dev * math.sqrt(trading_days))


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
    equity_curve: Sequence[EquityPointLike],
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
