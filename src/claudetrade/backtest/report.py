"""The owner-facing backtest REPORT: per-strategy, walk-forward, significance-gated.

Everything this module needs already exists and is already tested: the
event-driven engine (``backtest.engine``), walk-forward splitting
(``backtest.walkforward``), and honest metrics with confidence intervals and
a significance gate (``backtest.metrics``). What did not exist is the piece
that turns those primitives into a single answer to "how much historical
weight should the owner give strategy X's recommendations" -- run every
registered strategy in isolation, out-of-sample, over whatever history is
locally stored, and say so plainly when the evidence isn't there yet.

Design choices, in order of how often a reader will hit them:

* **Per-strategy isolation.** Each strategy is backtested alone (a config
  clone with ``signals.enabled_strategies = [name]``) against the *same*
  ``ContextProvider`` -- built once and reused -- so five strategies don't
  compete for the same portfolio's risk budget and one symbol/session load
  isn't repeated five times.
* **Walk-forward, not one full-period pass.** ``walkforward.walk_forward``
  trains and tests on non-overlapping windows; only the concatenated
  out-of-sample trades ever back a headline number. When the available
  history is too short for even one train+test fold, this falls back to a
  single in-sample pass -- shown for information only, never as validated
  evidence (see ``StrategyReportSection.evidence_basis``).
* **Significance is a headline, not a footnote.** ``compute_metrics`` already
  computes ``is_statistically_significant``; this module ALSO forces the
  verdict to "insufficient evidence" whenever the evidence is an in-sample
  fallback, regardless of what the raw statistic says -- an in-sample number
  cannot be out-of-sample proof no matter how large the sample.
* **Zero trades is a complete answer, not a blank one.** A strategy with no
  completed trades in the window renders "0 trades" plus the rejection
  funnel's own top reasons -- never a table of 0.00/NaN cells standing in
  for "nothing happened".
* **The out-of-sample equity curve is stitched, not concatenated.** Every
  walk-forward fold's ``BacktestResult.equity_curve`` starts fresh at
  ``initial_capital_usd`` (a new ``BacktestPortfolio`` per ``engine.run``
  call); naively concatenating fold curves would show a fake reset to
  starting capital at every fold boundary. ``stitch_oos_equity_curve``
  instead expresses each fold as a return multiplier relative to its own
  first mark and compounds those multipliers across folds -- a documented
  approximation of what one continuous account would have shown trading
  each fold's test window in sequence, not a re-simulation of one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from claudetrade.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    ContextProvider,
    RejectionFunnel,
)
from claudetrade.backtest.metrics import PerformanceMetrics, compute_metrics
from claudetrade.backtest.walkforward import walk_forward
from claudetrade.config import AppConfig
from claudetrade.db.models import PriceBar, SymbolSentimentDaily
from claudetrade.logging_setup import get_logger
from claudetrade.strategies.registry import available_strategies
from claudetrade.ui.data_access import data_freshness
from claudetrade.utils.text import sanitize_for_export
from claudetrade.utils.timeutils import utc_now
from claudetrade.version import CODE_VERSION, DISCLAIMER

if TYPE_CHECKING:
    from claudetrade.pipeline import Pipeline

log = get_logger(__name__)

#: Filenames written under ``exports_dir`` (see :func:`save_report`). Dated
#: rather than overwritten-in-place like ``signals/funnel_store.py``'s single
#: artifact: a backtest report is a point-in-time evidentiary document an
#: owner may want to keep several of, not a "latest status" cache. Multiple
#: runs on the same calendar day overwrite each other -- "the report for
#: today", not an unbounded pile.
REPORT_FILENAME_PREFIX = "backtest-report-"

#: A per-window reconciliation gap above this is reported as a FAIL, not
#: silently rounded away. $1 tolerates float accumulation, nothing more --
#: see ``test_portfolio_reconciliation.py`` for the accounting invariant this
#: is checking (equity - initial_capital == sum of completed trades' net P&L).
_RECONCILIATION_TOLERANCE_USD = 1.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DataCoverage:
    """Exactly what data this report's numbers are drawn from.

    Printed once at the top of the report so re-running ``claudetrade
    backtest report`` against the same database, config and code version is a
    reproducibility check, not a hope.
    """

    symbol_count: int
    session_start: dt.date | None
    session_end: dt.date | None
    total_sessions: int
    sentiment_row_count: int
    config_hash: str
    code_version: str


@dataclass(slots=True)
class StitchedEquityPoint:
    """One point on a walk-forward-stitched out-of-sample equity series.

    Satisfies ``metrics.EquityPointLike`` (``equity``/``exposure_pct``/
    ``drawdown_pct``) so it can be fed straight into ``compute_metrics``.
    """

    session: dt.date
    equity: float
    exposure_pct: float
    drawdown_pct: float


@dataclass(slots=True)
class EquityCurveSummary:
    """Start/end equity and the reconciliation check the report shows per strategy."""

    start_equity: float
    end_equity: float
    #: 0 for the in-sample single-pass fallback (see ``evidence_basis``).
    fold_count: int
    reconciled_fold_count: int
    reconciliation_ok: bool
    max_reconciliation_gap_usd: float
    note: str


@dataclass(slots=True)
class FoldDetail:
    """One walk-forward window's dates, trade count and rejection funnel."""

    fold: int | None  # None for the in-sample single-pass fallback
    train_start: dt.date | None
    train_end: dt.date | None
    test_start: dt.date
    test_end: dt.date
    trades: int
    funnel_summary: list[str]


@dataclass(slots=True)
class StrategyReportSection:
    """Everything the report says about one strategy."""

    strategy: str
    #: "out_of_sample_walk_forward" | "in_sample_single_pass_fallback"
    evidence_basis: str
    fold_count: int
    trades_taken: int
    #: The prominent verdict a reader sees before any point estimate.
    headline: str
    is_statistically_significant: bool
    significance_reason: str | None
    #: None when trades_taken == 0 -- no metrics table is rendered for a
    #: 0-trade result (see the module docstring).
    metrics: PerformanceMetrics | None
    equity_summary: EquityCurveSummary
    fold_details: list[FoldDetail]
    #: Rejection funnel summed across every fold (or the single fallback
    #: run's funnel) -- a strategy-level "why" alongside the per-fold detail.
    aggregate_funnel_summary: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BacktestReport:
    """The complete multi-strategy report: coverage header plus one section per strategy."""

    generated_at: dt.datetime
    window_start: dt.date
    window_end: dt.date
    coverage: DataCoverage
    sections: list[StrategyReportSection]
    disclaimer: str = DISCLAIMER


# ---------------------------------------------------------------------------
# Equity-curve stitching
# ---------------------------------------------------------------------------


def stitch_oos_equity_curve(
    fold_results: list[BacktestResult], initial_capital: float
) -> list[StitchedEquityPoint]:
    """Chain walk-forward folds' out-of-sample equity paths into one compounding series.

    See the module docstring for why this cannot be a naive concatenation.
    Each fold contributes its own return path (relative to its own first
    mark); the running peak/drawdown is recomputed over the *stitched*
    series, not copied from each fold's own (independently-reset) drawdown
    column.
    """
    points: list[StitchedEquityPoint] = []
    cumulative_multiplier = 1.0
    peak = initial_capital if initial_capital > 0 else 0.0
    for result in fold_results:
        curve = result.equity_curve
        if not curve or curve[0].equity <= 0:
            continue
        fold_base = curve[0].equity
        for p in curve:
            relative = p.equity / fold_base
            equity = cumulative_multiplier * relative * initial_capital
            peak = max(peak, equity)
            drawdown = 0.0 if peak <= 0 else 100.0 * (peak - equity) / peak
            points.append(
                StitchedEquityPoint(
                    session=p.session, equity=equity, exposure_pct=p.exposure_pct, drawdown_pct=drawdown
                )
            )
        cumulative_multiplier *= curve[-1].equity / fold_base
    return points


def _fold_reconciliation_gap(result: BacktestResult, initial_capital: float) -> float:
    """abs(actual final equity - (initial capital + sum of completed trades' net P&L)).

    Each walk-forward fold (and the single-pass fallback) starts a fresh
    ``BacktestPortfolio`` at ``initial_capital`` -- see
    ``test_portfolio_reconciliation.py`` for the invariant this checks and the
    accounting defect it was written to catch.
    """
    if not result.equity_curve:
        return 0.0
    expected = initial_capital + sum(t.net_pnl for t in result.trades)
    actual = result.equity_curve[-1].equity
    return abs(actual - expected)


_STITCH_NOTE = (
    "end_equity is a stitched out-of-sample series: each fold's own equity path is "
    "expressed as a return relative to that fold's own starting capital, then compounded "
    "across folds -- an approximation of one continuous account trading each fold's test "
    "window in sequence, not a re-simulation of one (see stitch_oos_equity_curve)."
)
_SINGLE_PASS_NOTE = (
    "single in-sample pass over the whole window (walk-forward was not possible -- see "
    "the significance reason above); not out-of-sample evidence."
)


def _equity_summary_from_folds(
    fold_results: list[BacktestResult], stitched: list[StitchedEquityPoint], initial_capital: float
) -> EquityCurveSummary:
    gaps = [_fold_reconciliation_gap(r, initial_capital) for r in fold_results]
    reconciled = sum(1 for g in gaps if g <= _RECONCILIATION_TOLERANCE_USD)
    end_equity = stitched[-1].equity if stitched else initial_capital
    return EquityCurveSummary(
        start_equity=initial_capital,
        end_equity=end_equity,
        fold_count=len(fold_results),
        reconciled_fold_count=reconciled,
        reconciliation_ok=reconciled == len(fold_results),
        max_reconciliation_gap_usd=max(gaps, default=0.0),
        note=_STITCH_NOTE,
    )


def _equity_summary_single(result: BacktestResult, initial_capital: float) -> EquityCurveSummary:
    gap = _fold_reconciliation_gap(result, initial_capital)
    ok = gap <= _RECONCILIATION_TOLERANCE_USD
    end_equity = result.equity_curve[-1].equity if result.equity_curve else initial_capital
    return EquityCurveSummary(
        start_equity=initial_capital,
        end_equity=end_equity,
        fold_count=0,
        reconciled_fold_count=1 if ok else 0,
        reconciliation_ok=ok,
        max_reconciliation_gap_usd=gap,
        note=_SINGLE_PASS_NOTE,
    )


def sum_funnels(funnels: list[RejectionFunnel]) -> RejectionFunnel:
    """Add up rejection funnels from several runs (e.g. every walk-forward fold).

    Used for the strategy-level "why" alongside the per-fold detail already
    on each ``BacktestResult``.
    """
    total = RejectionFunnel()
    for f in funnels:
        total.universe_candidates += f.universe_candidates
        total.universe_filtered_symbols += f.universe_filtered_symbols
        total.no_context += f.no_context
        total.strategy_errors += f.strategy_errors
        total.gate_rejected += f.gate_rejected
        total.score_rejected += f.score_rejected
        total.sizing_zero += f.sizing_zero
        total.limits_rejected += f.limits_rejected
        total.signals_generated += f.signals_generated
        total.orders_queued += f.orders_queued
        total.entries_filled += f.entries_filled
        total.entries_expired_unfilled += f.entries_expired_unfilled
        total.entries_carried_to_end += f.entries_carried_to_end
        for strategy, reasons in f.strategy_declined.items():
            bucket = total.strategy_declined.setdefault(strategy, {})
            for reason, count in reasons.items():
                bucket[reason] = bucket.get(reason, 0) + count
    return total


# ---------------------------------------------------------------------------
# Per-strategy section
# ---------------------------------------------------------------------------

#: Headline shown whenever the evidence does not clear the significance gate
#: -- deliberately not a point estimate (a win rate, a ratio, anything that
#: could be mistaken for a claim of edge). See the module docstring.
INSUFFICIENT_EVIDENCE_HEADLINE = "INSUFFICIENT EVIDENCE"
SIGNIFICANT_HEADLINE = "STATISTICALLY SIGNIFICANT (walk-forward out-of-sample)"


def _build_section(
    name: str,
    engine: BacktestEngine,
    provider: ContextProvider,
    strategy_cfg: AppConfig,
    start: dt.date,
    end: dt.date,
) -> StrategyReportSection:
    wf = walk_forward(engine, provider, start, end, strategy_cfg.backtest)
    folds = wf["folds"]
    initial_capital = strategy_cfg.backtest.initial_capital_usd

    if folds:
        fold_results = [f["oos_result"] for f in folds]
        oos_trades = wf["oos_trades"]
        stitched = stitch_oos_equity_curve(fold_results, initial_capital)
        metrics = compute_metrics(oos_trades, stitched, strategy_cfg.backtest) if oos_trades else None
        evidence_basis = "out_of_sample_walk_forward"
        fold_details = [
            FoldDetail(
                fold=f["fold"],
                train_start=f["train_start"],
                train_end=f["train_end"],
                test_start=f["test_start"],
                test_end=f["test_end"],
                trades=len(f["oos_result"].trades),
                funnel_summary=f["oos_result"].funnel.summary_lines(),
            )
            for f in folds
        ]
        aggregate_funnel = sum_funnels([r.funnel for r in fold_results])
        equity_summary = _equity_summary_from_folds(fold_results, stitched, initial_capital)
    else:
        # Not enough stored history for even one train+test window. Run a
        # single in-sample pass over the whole span for information only --
        # this is exactly the "0 trades is a complete answer" principle
        # applied one level up: "insufficient history" gets a real number
        # (what happened, in-sample) rather than a blank section, but that
        # number is never allowed to read as validated evidence (see
        # evidence_basis handling below).
        single_result = engine.run(provider, start_session=start, end_session=end)
        evidence_basis = "in_sample_single_pass_fallback"
        metrics = (
            compute_metrics(single_result.trades, single_result.equity_curve, strategy_cfg.backtest)
            if single_result.trades
            else None
        )
        fold_details = [
            FoldDetail(
                fold=None,
                train_start=None,
                train_end=None,
                test_start=start,
                test_end=end,
                trades=len(single_result.trades),
                funnel_summary=single_result.funnel.summary_lines(),
            )
        ]
        aggregate_funnel = single_result.funnel
        equity_summary = _equity_summary_single(single_result, initial_capital)

    trades_taken = metrics.trade_count if metrics is not None else 0
    needed_days = strategy_cfg.backtest.walk_forward_train_days + strategy_cfg.backtest.walk_forward_test_days

    # Evidence-basis first: an in-sample fallback is never allowed to read as
    # validated evidence regardless of how many trades it found (a strategy
    # that traded plenty in-sample still has zero out-of-sample proof), so
    # that reason takes priority over the plain "zero trades" one below --
    # but still names the trade count so the two honest facts ("not enough
    # history for walk-forward" and "0 trades in the fallback pass") are
    # never left implicit.
    if evidence_basis != "out_of_sample_walk_forward":
        is_significant = False
        trade_note = (
            "0 completed trades in the fallback pass"
            if metrics is None
            else f"{trades_taken} completed trade(s) in the fallback pass, shown below for information"
        )
        significance_reason = (
            f"insufficient_history_for_walk_forward: only {len(provider.sessions())} stored "
            f"session(s) in [{start}, {end}], below the {needed_days} calendar day(s) "
            "(walk_forward_train_days + walk_forward_test_days) needed for one validated "
            f"out-of-sample fold ({trade_note})"
        )
        warnings = list(metrics.warnings) if metrics is not None else []
    elif metrics is None:
        is_significant = False
        significance_reason = (
            f"zero_completed_trades: 0 completed out-of-sample trades across {len(folds)} "
            "walk-forward fold(s)"
        )
        warnings = []
    else:
        is_significant = metrics.is_statistically_significant
        significance_reason = metrics.significance_reason
        warnings = list(metrics.warnings)

    headline = SIGNIFICANT_HEADLINE if is_significant else INSUFFICIENT_EVIDENCE_HEADLINE

    return StrategyReportSection(
        strategy=name,
        evidence_basis=evidence_basis,
        fold_count=len(folds),
        trades_taken=trades_taken,
        headline=headline,
        is_statistically_significant=is_significant,
        significance_reason=significance_reason,
        metrics=metrics,
        equity_summary=equity_summary,
        fold_details=fold_details,
        aggregate_funnel_summary=aggregate_funnel.summary_lines(),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Data coverage
# ---------------------------------------------------------------------------


def _compute_data_coverage(
    pipeline: Pipeline,
    config: AppConfig,
    provider: ContextProvider,
    symbols: list[str],
    start: dt.date,
    end: dt.date,
) -> DataCoverage:
    benchmark = config.market_data.benchmark_symbol
    bars_by_symbol = provider.bars_by_symbol() if hasattr(provider, "bars_by_symbol") else {}
    symbol_count = len([s for s in bars_by_symbol if s != benchmark]) or len(symbols)
    sessions = provider.sessions()

    with pipeline.db.read_session() as session:
        sentiment_row_count = session.execute(
            select(func.count())
            .select_from(SymbolSentimentDaily)
            .where(
                SymbolSentimentDaily.symbol.in_(symbols),
                SymbolSentimentDaily.session >= start,
                SymbolSentimentDaily.session <= end,
            )
        ).scalar()

    return DataCoverage(
        symbol_count=symbol_count,
        session_start=sessions[0] if sessions else None,
        session_end=sessions[-1] if sessions else None,
        total_sessions=len(sessions),
        sentiment_row_count=int(sentiment_row_count or 0),
        config_hash=config.config_hash,
        code_version=CODE_VERSION,
    )


# ---------------------------------------------------------------------------
# Top-level generation
# ---------------------------------------------------------------------------


def generate_backtest_report(
    pipeline: Pipeline,
    config: AppConfig,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    strategy_names: list[str] | None = None,
) -> BacktestReport:
    """Run every strategy, walk-forward, over the requested (or full) window.

    Args:
        pipeline: A bootstrapped ``Pipeline`` (owns the database and universe).
        config: The base configuration; a deep copy is made per strategy with
            ``signals.enabled_strategies`` narrowed to that one name, so
            strategies never compete for the same simulated portfolio.
        start: First session to include. Defaults to the earliest stored
            price bar -- "everything available".
        end: Last session to include. Defaults to the latest stored price
            bar.
        strategy_names: Strategies to report on. Defaults to every registered
            strategy (``strategies.registry.available_strategies()``), not
            just the ones currently enabled for live scanning -- this report
            answers "how much should any of these be trusted", which is a
            broader question than "what's live today".

    Raises:
        ValueError: no price-bar data at all, an empty universe, or an
            explicit ``start`` after ``end`` -- all caller errors that should
            stop the run with a clear message rather than produce an empty
            or nonsensical report.
    """
    names = strategy_names or available_strategies()
    if not names:
        raise ValueError("no strategies are registered")

    freshness = data_freshness(pipeline.db)
    if end is None:
        end = freshness.latest_session
    with pipeline.db.read_session() as session:
        earliest = session.execute(select(func.min(PriceBar.session))).scalar()
    if start is None:
        start = earliest

    if start is None or end is None:
        raise ValueError(
            "no price-bar data stored yet -- run `claudetrade refresh` (or `claudetrade "
            "db purge --synthetic` for an offline demo run) before generating a report"
        )
    if start > end:
        raise ValueError(f"start ({start}) is after end ({end})")

    universe = pipeline.universe.for_session(end)
    if not universe.symbols:
        raise ValueError(f"universe is empty as of {end} -- run `claudetrade refresh` first")

    provider = pipeline.make_context_provider(symbols=universe.symbols, start=start, end=end)
    coverage = _compute_data_coverage(pipeline, config, provider, universe.symbols, start, end)

    sections: list[StrategyReportSection] = []
    for name in sorted(names):
        strategy_cfg = config.model_copy(deep=True)
        strategy_cfg.signals.enabled_strategies = [name]
        engine = BacktestEngine(strategy_cfg)
        sections.append(_build_section(name, engine, provider, strategy_cfg, start, end))

    return BacktestReport(
        generated_at=utc_now(),
        window_start=start,
        window_end=end,
        coverage=coverage,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_ci(ci, *, pct: bool = False, dollars: bool = False) -> str:
    if pct:
        return f"[{100 * ci.low:.1f}%, {100 * ci.high:.1f}%] ({ci.method})"
    if dollars:
        return f"[${ci.low:,.2f}, ${ci.high:,.2f}] ({ci.method})"
    return f"[{ci.low:.3f}, {ci.high:.3f}] ({ci.method})"


def _fmt_ratio(value: float) -> str:
    return "inf (degenerate: zero losing trades)" if math.isinf(value) else f"{value:.2f}"


def _fmt_unavailable(value: float | None, reason: str | None, fmt: str = "{:.2f}") -> str:
    if value is None:
        return f"unavailable ({reason})" if reason else "unavailable"
    return fmt.format(value)


def _render_section_markdown(section: StrategyReportSection) -> str:
    lines: list[str] = [f"\n---\n\n## Strategy: `{sanitize_for_export(section.strategy)}`\n\n"]
    lines.append(f"### {section.headline}\n\n")
    if section.significance_reason:
        lines.append(f"> {section.significance_reason}\n\n")

    basis_label = {
        "out_of_sample_walk_forward": f"walk-forward, {section.fold_count} out-of-sample fold(s)",
        "in_sample_single_pass_fallback": "single in-sample pass (walk-forward not possible on this window)",
    }.get(section.evidence_basis, section.evidence_basis)
    lines.append(f"- **Evidence basis**: {basis_label}\n")
    lines.append(f"- **Trades taken**: {section.trades_taken}\n")

    if section.metrics is None:
        lines.append(
            "\n**0 completed trades in this window.** No metrics table is rendered here -- "
            "a table of NaN/0.00 cells would misrepresent 'nothing happened' as a measured "
            "result. Rejection funnel (why):\n\n```\n"
            + "\n".join(section.aggregate_funnel_summary)
            + "\n```\n"
        )
    else:
        m = section.metrics
        lines.append("\n| Metric | Value |\n|---|---|\n")
        lines.append(
            f"| Win rate | {100 * m.win_rate:.1f}% (95% CI {_fmt_ci(m.win_rate_ci, pct=True)}) |\n"
        )
        lines.append(
            f"| Expectancy per trade (after costs) | ${m.expectancy_dollars:,.2f} "
            f"(95% CI {_fmt_ci(m.expectancy_ci, dollars=True)}) |\n"
        )
        lines.append(f"| Profit factor | {_fmt_ratio(m.profit_factor)} |\n")
        lines.append(
            f"| Max drawdown | {m.max_drawdown_pct:.2f}% ({m.max_drawdown_duration_days} days) |\n"
        )
        lines.append(f"| Average hold | {m.average_holding_days:.1f} days |\n")
        lines.append(
            f"| Winning / losing / breakeven | {m.winning_trades} / {m.losing_trades} / "
            f"{m.breakeven_trades} |\n"
        )
        lines.append(f"| Sharpe | {_fmt_unavailable(m.sharpe, m.unavailable_reasons.get('sharpe'))} |\n")
        lines.append(
            f"| Sortino | {_fmt_unavailable(m.sortino, m.unavailable_reasons.get('sortino'))} |\n"
        )

        if m.warnings:
            lines.append("\n**Validation warnings:**\n\n")
            for w in m.warnings:
                lines.append(f"- {w}\n")

    eq = section.equity_summary
    lines.append("\n#### Equity Curve Summary\n\n")
    lines.append(f"- Start: ${eq.start_equity:,.0f}  ->  End: ${eq.end_equity:,.0f}\n")
    reconciliation_verdict = "PASS" if eq.reconciliation_ok else "FAIL -- investigate before trusting this report"
    lines.append(
        f"- Reconciliation check: {eq.reconciled_fold_count}/{max(eq.fold_count, 1)} window(s) "
        f"reconciled (max gap ${eq.max_reconciliation_gap_usd:,.2f}): **{reconciliation_verdict}**\n"
    )
    lines.append(f"- {eq.note}\n")

    lines.append("\n#### Rejection Funnel by Walk-Forward Window\n")
    for fold in section.fold_details:
        label = (
            f"Fold {fold.fold}: train {fold.train_start}..{fold.train_end}, "
            f"test {fold.test_start}..{fold.test_end}"
            if fold.fold is not None
            else f"Single pass: {fold.test_start}..{fold.test_end}"
        )
        lines.append(f"\n**{label}** -- {fold.trades} trade(s)\n\n```\n" + "\n".join(fold.funnel_summary) + "\n```\n")

    return "".join(lines)


def render_report_markdown(report: BacktestReport) -> str:
    """Render the complete multi-strategy report as Markdown."""
    c = report.coverage
    lines: list[str] = ["# ClaudeTrade Backtest Report\n\n"]
    lines.append(f"{DISCLAIMER}\n\n")
    lines.append(
        "This report exists to say how much historical weight each strategy's "
        "recommendations deserve -- not to sell any of them. A strategy without enough "
        f"out-of-sample evidence is headlined **{INSUFFICIENT_EVIDENCE_HEADLINE}**, never its "
        "best-looking point estimate.\n\n"
    )
    lines.append("## Data Coverage\n\n")
    lines.append(f"- **Generated**: {report.generated_at.isoformat()}\n")
    lines.append(f"- **Requested window**: {report.window_start} to {report.window_end}\n")
    lines.append(
        f"- **Sessions with data**: {c.total_sessions} ({c.session_start} to {c.session_end})\n"
    )
    lines.append(f"- **Symbols covered**: {c.symbol_count}\n")
    lines.append(f"- **Sentiment rows in window**: {c.sentiment_row_count}\n")
    lines.append(f"- **Config hash**: `{c.config_hash}`\n")
    lines.append(f"- **Code version**: `{c.code_version}`\n\n")
    lines.append(
        "Re-running `claudetrade backtest report` against the same database, with this "
        "config hash and code version, reproduces this report exactly.\n"
    )

    for section in report.sections:
        lines.append(_render_section_markdown(section))

    return "".join(lines)


def report_to_dict(report: BacktestReport) -> dict[str, Any]:
    """The report as a plain dict, suitable for ``json.dumps(..., default=str)``.

    Dates/datetimes are left as-is (``dataclasses.asdict`` does not touch
    them); the caller's ``json.dumps`` must pass ``default=str`` to serialise
    them, matching ``cli._echo_json``'s existing convention.
    """
    return dataclasses.asdict(report)


def save_report(
    report: BacktestReport, exports_dir: str | Path, *, date_str: str | None = None
) -> tuple[Path, Path]:
    """Write the Markdown report and its JSON twin under ``exports_dir``.

    Both files share one stem (``backtest-report-<date>``); a second run on
    the same calendar day overwrites both, matching "the report for today"
    rather than accumulating an unbounded pile of near-duplicates.

    Returns:
        ``(markdown_path, json_path)``.
    """
    exports_path = Path(exports_dir)
    exports_path.mkdir(parents=True, exist_ok=True)
    stamp = date_str or report.generated_at.date().isoformat()
    md_path = exports_path / f"{REPORT_FILENAME_PREFIX}{stamp}.md"
    json_path = exports_path / f"{REPORT_FILENAME_PREFIX}{stamp}.json"
    md_path.write_text(render_report_markdown(report) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(report_to_dict(report), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return md_path, json_path


def find_latest_report_json(exports_dir: str | Path) -> Path | None:
    """The most recently dated ``backtest-report-*.json`` under ``exports_dir``, or None.

    Filenames are ``backtest-report-YYYY-MM-DD.json`` -- lexicographic sort
    is chronological sort, so this needs no filesystem-mtime comparison.
    """
    exports_path = Path(exports_dir)
    if not exports_path.exists():
        return None
    candidates = sorted(exports_path.glob(f"{REPORT_FILENAME_PREFIX}*.json"))
    return candidates[-1] if candidates else None


def load_latest_report(exports_dir: str | Path) -> dict[str, Any] | None:
    """Read back the most recently generated report JSON, or None if there isn't one.

    Best-effort like ``signals/funnel_store.load_latest``: a missing or
    corrupt file degrades to "no report available" rather than raising, so a
    caller (notably the MCP ``get_backtest_report`` tool) can always return a
    clear message instead of crashing.
    """
    path = find_latest_report_json(exports_dir)
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("could not read backtest report artifact at %s", path, exc_info=True)
        return None


__all__ = [
    "INSUFFICIENT_EVIDENCE_HEADLINE",
    "SIGNIFICANT_HEADLINE",
    "BacktestReport",
    "DataCoverage",
    "EquityCurveSummary",
    "FoldDetail",
    "StitchedEquityPoint",
    "StrategyReportSection",
    "find_latest_report_json",
    "generate_backtest_report",
    "load_latest_report",
    "render_report_markdown",
    "report_to_dict",
    "save_report",
    "stitch_oos_equity_curve",
    "sum_funnels",
]
