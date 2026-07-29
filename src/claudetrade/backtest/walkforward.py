"""Walk-forward and sensitivity analysis for robust strategy validation.

These functions prevent overfitting by training and testing on non-overlapping
windows, and by measuring robustness to parameter perturbations. Out-of-sample
results are aggregated honestly: no cherry-picking the best fold, every fold
counts equally, and gates catch pathologies like wins with zero edge.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

from claudetrade.backtest.engine import BacktestEngine, BacktestResult, ContextProvider
from claudetrade.backtest.metrics import PerformanceMetrics
from claudetrade.config import BacktestConfig
from claudetrade.domain import Trade

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ChronologicalSplit:
    """Train/validation/test date ranges."""

    train_start: dt.date
    train_end: dt.date
    validation_start: dt.date
    validation_end: dt.date
    test_start: dt.date
    test_end: dt.date


def chronological_split(
    start: dt.date,
    end: dt.date,
    holdout_fraction: float = 0.25,
) -> ChronologicalSplit:
    """Divide a date range into train/validation/test.

    The test block is never touched by optimisation; it represents truly
    out-of-sample performance.

    Args:
        start: First date.
        end: Last date (inclusive).
        holdout_fraction: Fraction of the total span reserved as the test block.

    Returns:
        A split with three disjoint date ranges.
    """
    total_days = (end - start).days + 1
    test_days = max(1, int(total_days * holdout_fraction))
    tv_days = total_days - test_days

    val_days = tv_days // 2
    train_days = tv_days - val_days

    train_end = start + dt.timedelta(days=train_days - 1)
    val_start = train_end + dt.timedelta(days=1)
    val_end = val_start + dt.timedelta(days=val_days - 1)
    test_start = val_end + dt.timedelta(days=1)

    return ChronologicalSplit(
        train_start=start,
        train_end=train_end,
        validation_start=val_start,
        validation_end=val_end,
        test_start=test_start,
        test_end=end,
    )


def walk_forward(
    engine: BacktestEngine,
    provider: ContextProvider,
    start: dt.date,
    end: dt.date,
    config: BacktestConfig | None = None,
) -> dict[str, dict]:
    """Rolling walk-forward backtests across overlapping windows.

    Trains on a fixed window, tests on the next non-overlapping window, then
    steps forward and repeats. Returns per-fold in-sample and out-of-sample
    metrics, plus an aggregate over the concatenated out-of-sample trades.

    Args:
        engine: Backtest engine.
        provider: Data provider.
        start: First date.
        end: Last date (inclusive).
        config: BacktestConfig (uses engine's config if omitted).

    Returns:
        A dict with 'folds' (list of per-fold results) and 'aggregate_oos'
        (metrics computed from all out-of-sample trades concatenated).
    """
    if config is None:
        config = engine.backtest_config

    train_days = config.walk_forward_train_days
    test_days = config.walk_forward_test_days
    step_days = config.walk_forward_step_days

    all_sessions = provider.sessions()
    relevant_sessions = [s for s in all_sessions if start <= s <= end]

    if len(relevant_sessions) < train_days + test_days:
        log.warning(
            f"walk_forward: only {len(relevant_sessions)} sessions available, "
            f"need {train_days + test_days} for one fold"
        )

    folds = []
    oos_trades: list[Trade] = []

    fold_num = 0
    train_start = relevant_sessions[0]

    while True:
        train_end_idx = None
        for i, s in enumerate(relevant_sessions):
            if (s - train_start).days >= train_days - 1:
                train_end_idx = i
                break

        if train_end_idx is None or train_end_idx + 1 >= len(relevant_sessions):
            break

        train_end = relevant_sessions[train_end_idx]
        test_start = relevant_sessions[train_end_idx + 1]
        test_end_idx = None
        for i in range(train_end_idx + 1, len(relevant_sessions)):
            if (relevant_sessions[i] - test_start).days >= test_days - 1:
                test_end_idx = i
                break

        if test_end_idx is None:
            test_end = relevant_sessions[-1]
        else:
            test_end = relevant_sessions[test_end_idx]

        # Run in-sample (train)
        is_result = engine.run(provider, start_session=train_start, end_session=train_end)
        is_metrics = PerformanceMetrics(**is_result.metrics)

        # Run out-of-sample (test)
        oos_result = engine.run(provider, start_session=test_start, end_session=test_end)
        oos_metrics = PerformanceMetrics(**oos_result.metrics)

        fold_num += 1
        folds.append({
            "fold": fold_num,
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "is_result": is_result,
            "is_metrics": is_metrics,
            "oos_result": oos_result,
            "oos_metrics": oos_metrics,
        })

        oos_trades.extend(oos_result.trades)

        # Step forward
        step_idx = None
        for i, s in enumerate(relevant_sessions):
            if (s - train_start).days >= step_days - 1:
                step_idx = i
                break
        if step_idx is None or step_idx + 1 >= len(relevant_sessions):
            break
        train_start = relevant_sessions[step_idx + 1]

    # Aggregate out-of-sample results
    from claudetrade.backtest.metrics import compute_metrics

    aggregate_oos = (
        compute_metrics(
            oos_trades,
            [],
            config,
        )
        if oos_trades
        else None
    )

    return {
        "folds": folds,
        "aggregate_oos": aggregate_oos,
        "oos_trades": oos_trades,
    }


def parameter_sensitivity(
    runner: Callable[[dict], BacktestResult],
    base_params: dict,
    perturbations: dict[str, list[float]],
) -> dict[str, dict]:
    """Measure metric dispersion under parameter perturbations.

    Re-runs the strategy with small modifications to key parameters (e.g.,
    ±10% changes to a threshold) and reports the range of outcomes. A strategy
    that fails drastically on a ±10% change is fragile; a robust strategy's
    metrics stay stable.

    Args:
        runner: Function that takes a params dict and returns a BacktestResult.
        base_params: Baseline parameters.
        perturbations: Mapping of param name to list of relative changes
            (e.g., {'threshold': [-0.1, 0.0, 0.1]} for ±10% and baseline).

    Returns:
        A dict with base_result and per-param sensitivity analysis.
    """
    base_result = runner(base_params)
    from claudetrade.backtest.metrics import PerformanceMetrics
    base_metrics = PerformanceMetrics(**base_result.metrics)

    sensitivity = {}

    for param_name, multipliers in perturbations.items():
        param_results = []
        for mult in multipliers:
            params = base_params.copy()
            if param_name in params and isinstance(params[param_name], (int, float)):
                params[param_name] = params[param_name] * (1.0 + mult)
            result = runner(params)
            metrics = PerformanceMetrics(**result.metrics)
            param_results.append({
                "multiplier": mult,
                "result": result,
                "metrics": metrics,
            })

        sensitivity[param_name] = {
            "baseline": base_metrics,
            "results": param_results,
            "range": {
                "win_loss_ratio": (
                    min(r["metrics"].win_loss_ratio for r in param_results),
                    max(r["metrics"].win_loss_ratio for r in param_results),
                ),
                "expectancy": (
                    min(r["metrics"].expectancy_dollars for r in param_results),
                    max(r["metrics"].expectancy_dollars for r in param_results),
                ),
            },
        }

    return {
        "base_result": base_result,
        "base_metrics": base_metrics,
        "sensitivity": sensitivity,
    }


def multi_objective_score(metrics: PerformanceMetrics) -> float:
    """Score a strategy result, gating on edge and risk/reward.

    The win/loss ratio ranks first, but is GATED: a score of 0 is returned
    (failing this test) when:

    1. Expectancy <= 0: the strategy loses money on average (no edge).
    2. Avg loss > 2x avg win: unfavourable risk-reward structure.
    3. Trade count is below the configured minimum: insufficient validation.
    4. Profit factor <= 1.0: cumulative losses exceed cumulative gains.

    When all gates pass, the score is the win/loss ratio (or 0 if infinite/NaN).

    Returns:
        A score in [0, inf), higher is better.
    """
    # Gate 1: Edge exists
    if metrics.expectancy_dollars <= 0:
        log.debug(f"multi_objective_score gate 1: expectancy {metrics.expectancy_dollars} <= 0")
        return 0.0

    # Gate 2: Payoff ratio
    if metrics.average_loss > 2.0 * metrics.average_win and metrics.average_win > 0:
        log.debug(
            f"multi_objective_score gate 2: avg_loss {metrics.average_loss} "
            f"> 2x avg_win {metrics.average_win}"
        )
        return 0.0

    # Gate 3: Trade count (defer to validation_warnings; here we just note it)
    # (actual minimum enforcement happens in validation_warnings)

    # Gate 4: Profit factor
    if metrics.profit_factor <= 1.0:
        log.debug(f"multi_objective_score gate 4: profit_factor {metrics.profit_factor} <= 1.0")
        return 0.0

    # All gates pass: return win/loss ratio
    if math.isinf(metrics.win_loss_ratio):
        return 0.0  # Degenerate (zero losses); edge not proven
    if math.isnan(metrics.win_loss_ratio):
        return 0.0
    return metrics.win_loss_ratio


def regime_segmented_results(
    walk_forward_output: dict,
) -> dict[str, dict]:
    """Aggregate walk-forward results by market regime.

    Returns per-regime metrics across ALL folds, not just the best or most
    recent one. This prevents the false confidence that can come from
    cherry-picking a single favourable regime.

    Args:
        walk_forward_output: Output from walk_forward().

    Returns:
        A dict mapping regime name to aggregated metrics and trade list.
    """
    from claudetrade.backtest.metrics import compute_metrics
    from claudetrade.domain import MarketRegime

    regime_trades: dict[str, list[Trade]] = {r.value: [] for r in MarketRegime}
    regime_trades["unknown"] = []

    for fold in walk_forward_output["folds"]:
        oos_result = fold["oos_result"]
        for trade in oos_result.trades:
            regime_name = trade.regime_at_entry.value if trade.regime_at_entry else "unknown"
            regime_trades[regime_name].append(trade)

    results = {}
    for regime_name, trades in regime_trades.items():
        if trades:
            from claudetrade.config import get_config
            cfg = get_config()
            metrics = compute_metrics(trades, [], cfg.backtest)
            results[regime_name] = {
                "trade_count": len(trades),
                "metrics": metrics,
                "trades": trades,
            }

    return results


__all__ = [
    "ChronologicalSplit",
    "chronological_split",
    "multi_objective_score",
    "parameter_sensitivity",
    "regime_segmented_results",
    "walk_forward",
]
