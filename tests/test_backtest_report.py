"""Tests for ``claudetrade.backtest.report`` -- the owner-facing backtest REPORT.

Three layers, cheapest first:

* Pure data-shaping helpers (``stitch_oos_equity_curve``, ``sum_funnels``) --
  no engine, no database.
* Rendering (``render_report_markdown``) against hand-built
  ``StrategyReportSection``s -- exercises both the "significant, walk-forward"
  and the "0 trades" / "insufficient evidence" pathways deterministically.
* End-to-end ``generate_backtest_report`` against a real ``Pipeline`` backed
  by the synthetic market provider (``tmp_app_config``/``tmp_db`` already
  default to it) -- a short window (forces the in-sample fallback for every
  strategy) and a long window (long enough for at least one real
  walk-forward fold). Numbers are never asserted beyond "did not crash and
  the shape is honest" -- synthetic data's *outcomes* are meaningless by
  design (see ``providers.market.synthetic``'s module docstring); only the
  reporting machinery is under test here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.backtest.engine import BacktestResult, RejectionFunnel
from claudetrade.backtest.metrics import compute_metrics
from claudetrade.backtest.portfolio import EquityPoint
from claudetrade.backtest.report import (
    INSUFFICIENT_EVIDENCE_HEADLINE,
    SIGNIFICANT_HEADLINE,
    BacktestReport,
    DataCoverage,
    EquityCurveSummary,
    StrategyReportSection,
    find_latest_report_json,
    generate_backtest_report,
    load_latest_report,
    render_report_markdown,
    save_report,
    stitch_oos_equity_curve,
    sum_funnels,
)
from claudetrade.config import AppConfig, BacktestConfig
from claudetrade.db.session import Database
from claudetrade.domain import Direction, ExitReason, Trade
from claudetrade.pipeline import Pipeline
from claudetrade.version import CODE_VERSION, DISCLAIMER

# --------------------------------------------------------------------------
# Small factories (mirrors test_metrics.py's make_closed_trade)
# --------------------------------------------------------------------------


def _equity_point(session: dt.date, equity: float) -> EquityPoint:
    return EquityPoint(
        session=session,
        equity=equity,
        cash=equity,
        open_positions=0,
        exposure_pct=0.0,
        portfolio_heat_pct=0.0,
        drawdown_pct=0.0,
    )


def _result(equity_curve: list[EquityPoint], trades: list[Trade] = ()) -> BacktestResult:
    return BacktestResult(
        run_id="",
        config=BacktestConfig(),
        start_session=equity_curve[0].session if equity_curve else dt.date(2024, 1, 1),
        end_session=equity_curve[-1].session if equity_curve else dt.date(2024, 1, 1),
        strategy_names=["stub"],
        universe_size=1,
        trades=list(trades),
        equity_curve=list(equity_curve),
        metrics={},
        segment_metrics={},
        warnings=[],
        funnel=RejectionFunnel(),
        code_version=CODE_VERSION,
        config_hash="",
        data_snapshot_hash="",
    )


def make_closed_trade(trade_id: str, net_pnl: float, holding_days: int = 5) -> Trade:
    commission = 10.0
    gross_pnl = net_pnl + commission
    shares = 100
    return Trade(
        trade_id=trade_id,
        signal_id=trade_id,
        symbol="TEST",
        strategy="stub",
        direction=Direction.LONG,
        entry_session=dt.date(2023, 1, 3),
        entry_price=100.0,
        shares=shares,
        stop_loss=95.0,
        exit_session=dt.date(2023, 1, 3) + dt.timedelta(days=holding_days),
        exit_price=100.0 + gross_pnl / shares,
        exit_reason=ExitReason.TARGET if net_pnl > 0 else ExitReason.STOP_LOSS,
        commission_total=commission,
    )


# --------------------------------------------------------------------------
# stitch_oos_equity_curve
# --------------------------------------------------------------------------


def test_stitch_oos_equity_curve_chains_folds_without_a_fake_reset() -> None:
    """Each fold's own equity curve starts fresh at initial_capital (a new
    portfolio per engine.run) -- the stitched series must continue smoothly
    across the boundary, not drop back to initial_capital."""
    fold1 = _result(
        [
            _equity_point(dt.date(2024, 1, 1), 100_000.0),
            _equity_point(dt.date(2024, 1, 2), 110_000.0),
        ]
    )
    fold2 = _result(
        [
            _equity_point(dt.date(2024, 2, 1), 100_000.0),  # fresh portfolio, same start capital
            _equity_point(dt.date(2024, 2, 2), 90_000.0),
        ]
    )
    stitched = stitch_oos_equity_curve([fold1, fold2], 100_000.0)
    assert [p.equity for p in stitched] == pytest.approx(
        [100_000.0, 110_000.0, 110_000.0, 99_000.0]
    )


def test_stitch_oos_equity_curve_recomputes_drawdown_across_the_whole_series() -> None:
    fold1 = _result(
        [
            _equity_point(dt.date(2024, 1, 1), 100_000.0),
            _equity_point(dt.date(2024, 1, 2), 120_000.0),
        ]
    )
    fold2 = _result(
        [
            _equity_point(dt.date(2024, 2, 1), 100_000.0),
            _equity_point(dt.date(2024, 2, 2), 80_000.0),
        ]
    )
    stitched = stitch_oos_equity_curve([fold1, fold2], 100_000.0)
    # peak is fold1's 120,000 (post-multiplier); fold2's last point is
    # 1.2 * 0.8 * 100,000 = 96,000 -- a drawdown *from the whole series'
    # peak*, not from fold2's own (independently-reset) high water mark.
    assert stitched[-1].equity == pytest.approx(96_000.0)
    assert stitched[-1].drawdown_pct == pytest.approx(100.0 * (120_000.0 - 96_000.0) / 120_000.0)


def test_stitch_oos_equity_curve_skips_a_fold_with_no_points_or_zero_start() -> None:
    empty_fold = _result([])
    fold = _result([_equity_point(dt.date(2024, 1, 1), 50_000.0)])
    stitched = stitch_oos_equity_curve([empty_fold, fold], 50_000.0)
    assert len(stitched) == 1
    assert stitched[0].equity == pytest.approx(50_000.0)


# --------------------------------------------------------------------------
# sum_funnels
# --------------------------------------------------------------------------


def test_sum_funnels_adds_counts_and_merges_strategy_declines() -> None:
    f1 = RejectionFunnel(universe_candidates=10, gate_rejected=2)
    f1.strategy_declined["sentiment_breakout"] = {"illiquid": 3}
    f2 = RejectionFunnel(universe_candidates=5, gate_rejected=1)
    f2.strategy_declined["sentiment_breakout"] = {"illiquid": 2, "no_setup": 1}

    total = sum_funnels([f1, f2])

    assert total.universe_candidates == 15
    assert total.gate_rejected == 3
    assert total.strategy_declined["sentiment_breakout"] == {"illiquid": 5, "no_setup": 1}


# --------------------------------------------------------------------------
# render_report_markdown
# --------------------------------------------------------------------------


def _coverage() -> DataCoverage:
    return DataCoverage(
        symbol_count=5,
        session_start=dt.date(2024, 1, 2),
        session_end=dt.date(2024, 6, 28),
        total_sessions=120,
        sentiment_row_count=37,
        config_hash="abc123",
        code_version=CODE_VERSION,
    )


def test_render_report_markdown_zero_trades_section_has_no_metrics_table() -> None:
    section = StrategyReportSection(
        strategy="hype_failure_short",
        evidence_basis="out_of_sample_walk_forward",
        fold_count=2,
        trades_taken=0,
        headline=INSUFFICIENT_EVIDENCE_HEADLINE,
        is_statistically_significant=False,
        significance_reason="zero_completed_trades: 0 completed trades in this window",
        metrics=None,
        equity_summary=EquityCurveSummary(
            start_equity=100_000.0,
            end_equity=100_000.0,
            fold_count=2,
            reconciled_fold_count=2,
            reconciliation_ok=True,
            max_reconciliation_gap_usd=0.0,
            note="stitched",
        ),
        fold_details=[],
        aggregate_funnel_summary=["Universe candidates (symbol x session): 100", "Gate-rejected: 40"],
    )
    report = BacktestReport(
        generated_at=dt.datetime(2024, 6, 29, tzinfo=dt.UTC),
        window_start=dt.date(2024, 1, 1),
        window_end=dt.date(2024, 6, 28),
        coverage=_coverage(),
        sections=[section],
    )

    markdown = render_report_markdown(report)

    assert INSUFFICIENT_EVIDENCE_HEADLINE in markdown
    assert "0 completed trades in this window" in markdown
    assert "hype_failure_short" in markdown
    assert "Gate-rejected: 40" in markdown
    # No metrics table for a 0-trade section.
    assert "Win rate |" not in markdown
    assert DISCLAIMER in markdown
    assert "abc123" in markdown  # config hash in the coverage header


def test_render_report_markdown_significant_section_shows_point_estimates() -> None:
    # 2-in-3 win rate, small consistent losses -- low enough dispersion that
    # the bootstrap expectancy CI clears zero comfortably (this is a fixture
    # for exercising the renderer, not a claim about any real strategy).
    trades = [make_closed_trade(f"T{i}", 500.0 if i % 3 else -100.0, holding_days=6) for i in range(60)]
    curve = [
        _equity_point(dt.date(2024, 1, 1) + dt.timedelta(days=i), 100_000.0 + i * 200.0)
        for i in range(60)
    ]
    cfg = BacktestConfig(min_trades_for_validation=5, random_seed=7)
    metrics = compute_metrics(trades, curve, cfg)
    assert metrics.is_statistically_significant  # sanity: this fixture should clear the gate

    section = StrategyReportSection(
        strategy="sentiment_breakout",
        evidence_basis="out_of_sample_walk_forward",
        fold_count=1,
        trades_taken=metrics.trade_count,
        headline=SIGNIFICANT_HEADLINE,
        is_statistically_significant=True,
        significance_reason=None,
        metrics=metrics,
        equity_summary=EquityCurveSummary(
            start_equity=100_000.0,
            end_equity=curve[-1].equity,
            fold_count=1,
            reconciled_fold_count=1,
            reconciliation_ok=True,
            max_reconciliation_gap_usd=0.0,
            note="stitched",
        ),
        fold_details=[],
        aggregate_funnel_summary=["Universe candidates (symbol x session): 500"],
    )
    report = BacktestReport(
        generated_at=dt.datetime(2024, 6, 29, tzinfo=dt.UTC),
        window_start=dt.date(2024, 1, 1),
        window_end=dt.date(2024, 6, 28),
        coverage=_coverage(),
        sections=[section],
    )

    markdown = render_report_markdown(report)

    assert SIGNIFICANT_HEADLINE in markdown
    assert "Win rate |" in markdown
    assert "95% CI" in markdown
    assert "Expectancy per trade (after costs)" in markdown
    assert f"**Trades taken**: {metrics.trade_count}" in markdown
    assert "Profit factor |" in markdown
    assert "Max drawdown |" in markdown


def test_render_report_markdown_reconciliation_failure_is_flagged() -> None:
    section = StrategyReportSection(
        strategy="post_earnings_drift",
        evidence_basis="in_sample_single_pass_fallback",
        fold_count=0,
        trades_taken=0,
        headline=INSUFFICIENT_EVIDENCE_HEADLINE,
        is_statistically_significant=False,
        significance_reason="insufficient_history_for_walk_forward: only 40 stored session(s)",
        metrics=None,
        equity_summary=EquityCurveSummary(
            start_equity=100_000.0,
            end_equity=100_500.0,
            fold_count=0,
            reconciled_fold_count=0,
            reconciliation_ok=False,
            max_reconciliation_gap_usd=250.0,
            note="single pass",
        ),
        fold_details=[],
        aggregate_funnel_summary=["Universe candidates (symbol x session): 5"],
    )
    report = BacktestReport(
        generated_at=dt.datetime(2024, 6, 29, tzinfo=dt.UTC),
        window_start=dt.date(2024, 5, 20),
        window_end=dt.date(2024, 6, 28),
        coverage=_coverage(),
        sections=[section],
    )

    markdown = render_report_markdown(report)
    assert "FAIL" in markdown
    assert "insufficient_history_for_walk_forward" in markdown


# --------------------------------------------------------------------------
# save_report / find_latest_report_json / load_latest_report
# --------------------------------------------------------------------------


def test_save_and_load_latest_report_round_trips(tmp_path) -> None:
    section = StrategyReportSection(
        strategy="capitulation_reversal",
        evidence_basis="in_sample_single_pass_fallback",
        fold_count=0,
        trades_taken=0,
        headline=INSUFFICIENT_EVIDENCE_HEADLINE,
        is_statistically_significant=False,
        significance_reason="zero_completed_trades: 0 completed trades in this window",
        metrics=None,
        equity_summary=EquityCurveSummary(
            start_equity=100_000.0,
            end_equity=100_000.0,
            fold_count=0,
            reconciled_fold_count=1,
            reconciliation_ok=True,
            max_reconciliation_gap_usd=0.0,
            note="single pass",
        ),
        fold_details=[],
        aggregate_funnel_summary=[],
    )
    report = BacktestReport(
        generated_at=dt.datetime(2024, 6, 29, tzinfo=dt.UTC),
        window_start=dt.date(2024, 5, 20),
        window_end=dt.date(2024, 6, 28),
        coverage=_coverage(),
        sections=[section],
    )

    assert find_latest_report_json(tmp_path) is None
    assert load_latest_report(tmp_path) is None

    md_path, json_path = save_report(report, tmp_path, date_str="2024-06-29")
    assert md_path.exists()
    assert json_path.exists()
    assert md_path.name == "backtest-report-2024-06-29.md"

    assert find_latest_report_json(tmp_path) == json_path
    loaded = load_latest_report(tmp_path)
    assert loaded["coverage"]["config_hash"] == "abc123"
    assert loaded["sections"][0]["strategy"] == "capitulation_reversal"


def test_find_latest_report_json_picks_the_most_recent_date(tmp_path) -> None:
    for stamp in ("2024-01-01", "2024-06-15", "2024-03-10"):
        (tmp_path / f"backtest-report-{stamp}.json").write_text("{}")
    latest = find_latest_report_json(tmp_path)
    assert latest.name == "backtest-report-2024-06-15.json"


# --------------------------------------------------------------------------
# generate_backtest_report -- end to end against a real (synthetic) Pipeline
# --------------------------------------------------------------------------

#: A handful of ordinary (non-leveraged, non-ETF) synthetic tickers -- picked
#: from providers.market.synthetic's fixed, seeded universe so the ingest
#: below is fast (5 symbols, not the full ~120-name synthetic universe).
_SYNTHETIC_SYMBOLS = ["SIPH", "RECY", "SANE", "LIQU", "EMQU"]


@pytest.fixture
def synthetic_pipeline(tmp_app_config: AppConfig, tmp_db: Database) -> Pipeline:
    # tmp_app_config pins market/earnings/reddit to the synthetic providers
    # but -- like every other test that calls Pipeline.refresh (see
    # test_providers.py) -- news_rss defaults to a *live* adapter and must be
    # disabled explicitly, or refresh() spends its time on doomed outbound
    # requests instead of the synthetic ingest this test actually wants.
    # X also defaults on with its own synthetic generator; left off here too
    # -- Reddit alone is enough to exercise sentiment_row_count coverage
    # below without doubling the (expensive) post classification/aggregation
    # work this ingest already pays for.
    tmp_app_config.news.enabled = False
    tmp_app_config.x.enabled = False
    return Pipeline(tmp_app_config, tmp_db)


def test_generate_backtest_report_short_window_falls_back_to_in_sample_for_every_strategy(
    synthetic_pipeline: Pipeline,
) -> None:
    """A window far shorter than walk_forward_train_days + walk_forward_test_days
    (630 calendar days by default) cannot produce even one walk-forward fold --
    every strategy must fall back to the labelled in-sample single pass, never
    silently produce an empty report."""
    start = dt.date(2024, 1, 2)
    end = dt.date(2024, 3, 1)
    synthetic_pipeline.refresh(start=start, end=end, symbols=_SYNTHETIC_SYMBOLS)

    report = generate_backtest_report(synthetic_pipeline, synthetic_pipeline.config, start=start, end=end)

    assert report.coverage.symbol_count >= 1
    assert report.coverage.config_hash == synthetic_pipeline.config.config_hash
    assert report.coverage.code_version == CODE_VERSION
    assert {s.strategy for s in report.sections} == {
        "capitulation_reversal",
        "hype_failure_short",
        "post_earnings_drift",
        "sentiment_breakout",
        "sentiment_pullback",
    }
    for section in report.sections:
        assert section.evidence_basis == "in_sample_single_pass_fallback"
        assert section.fold_count == 0
        assert section.is_statistically_significant is False
        assert "insufficient_history_for_walk_forward" in section.significance_reason
        # Headline is never a bare point estimate.
        assert section.headline == INSUFFICIENT_EVIDENCE_HEADLINE

    # Must render and JSON-round-trip without raising, even though every
    # section took this fallback path.
    markdown = render_report_markdown(report)
    assert INSUFFICIENT_EVIDENCE_HEADLINE in markdown
    assert DISCLAIMER in markdown

    md_path, json_path = save_report(report, _exports_dir_for(synthetic_pipeline))
    assert md_path.exists() and json_path.exists()
    reloaded = load_latest_report(md_path.parent)
    assert reloaded["coverage"]["symbol_count"] == report.coverage.symbol_count


def test_generate_backtest_report_can_produce_a_real_walk_forward_fold(
    tmp_app_config: AppConfig, tmp_db: Database
) -> None:
    """With a window comfortably longer than train+test, at least one
    strategy must reach real out-of-sample walk-forward evidence --
    exercising the fold/stitching/per-window-funnel code paths against real
    (if fabricated) data, not just hand-built fixtures.

    ``walk_forward_train_days``/``test_days``/``step_days`` are shrunk to a
    couple of weeks each (rather than the 504/126/126-day production
    defaults) purely so this test stays fast: the point is exercising the
    walk-forward *machinery* end to end, not producing a realistic fold
    size, and a 630-calendar-day minimum window (with the full symbol/
    strategy fan-out this report generates) would make this test far too
    slow to run routinely. Reddit is disabled too (unlike ``synthetic_pipeline``)
    -- this test is not about sentiment coverage, and the synthetic Reddit
    source fabricates a large volume of posts to run the real classification/
    aggregation pipeline over regardless of how small the requested window is,
    which otherwise dominates this test's runtime. Both must be set *before*
    ``Pipeline`` is constructed -- it builds its provider list once, at
    ``__init__``.
    """
    tmp_app_config.news.enabled = False
    tmp_app_config.reddit.enabled = False
    tmp_app_config.x.enabled = False
    tmp_app_config.backtest.walk_forward_train_days = 20
    tmp_app_config.backtest.walk_forward_test_days = 10
    tmp_app_config.backtest.walk_forward_step_days = 10
    pipeline = Pipeline(tmp_app_config, tmp_db)

    start = dt.date(2024, 1, 2)
    end = dt.date(2024, 4, 1)  # ~90 calendar days: several 30-day folds fit
    pipeline.refresh(start=start, end=end, symbols=_SYNTHETIC_SYMBOLS[:2])

    report = generate_backtest_report(
        pipeline,
        tmp_app_config,
        start=start,
        end=end,
        strategy_names=["sentiment_breakout", "capitulation_reversal"],
    )

    assert any(s.fold_count >= 1 for s in report.sections)
    for section in report.sections:
        if section.fold_count >= 1:
            assert section.evidence_basis == "out_of_sample_walk_forward"
            for fold in section.fold_details:
                assert fold.fold is not None
                assert fold.train_start is not None and fold.test_start is not None
                assert isinstance(fold.funnel_summary, list) and fold.funnel_summary
        # Whether or not this strategy took any trades, the equity summary
        # must always reconcile against the trade ledger (or say clearly
        # that it doesn't) -- never silently omitted.
        assert isinstance(section.equity_summary.reconciliation_ok, bool)

    # Must not raise regardless of which strategies happened to trade.
    render_report_markdown(report)


def test_generate_backtest_report_raises_a_clear_error_with_no_stored_data(
    synthetic_pipeline: Pipeline,
) -> None:
    with pytest.raises(ValueError, match="no price-bar data stored"):
        generate_backtest_report(synthetic_pipeline, synthetic_pipeline.config)


def _exports_dir_for(pipeline: Pipeline):
    """The exports directory for this pipeline's config."""
    return pipeline.config.paths.resolve("exports_dir")
