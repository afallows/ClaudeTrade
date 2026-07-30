"""Backtesting: runs the real ``BacktestEngine`` over stored history.

Wiring mirrors ``claudetrade.cli``'s ``backtest`` command exactly: build the
universe for the end date, build a context provider over the requested
window, and run ``BacktestEngine.run`` -- the same engine the CLI and the
scheduler use. Nothing here is simulated or sampled; a 0-trade result shows
the same rejection funnel the CLI prints, and every export button writes the
same CSVs ``claudetrade.backtest.reporting.export_csv`` produces.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.backtest.engine import BacktestEngine, BacktestResult
from claudetrade.backtest.metrics import PerformanceMetrics
from claudetrade.backtest.reporting import (
    equity_to_dataframe,
    metrics_to_dataframe,
    render_markdown_report,
    trades_to_dataframe,
)
from claudetrade.backtest.walkforward import walk_forward
from claudetrade.pipeline import Pipeline
from claudetrade.strategies.registry import available_strategies
from claudetrade.ui import theme
from claudetrade.ui.charts import (
    create_drawdown_chart,
    create_equity_curve_chart,
    create_funnel_bar_chart,
)
from claudetrade.ui.components.layout import page_header
from claudetrade.ui.components.stats import metric_tile, significance_caveat
from claudetrade.ui.components.tables import empty_state
from claudetrade.ui.state import get_backtest_result, get_config, set_backtest_result

#: Rows behind the "Rejection Funnel" table, in pipeline order. Each maps a
#: display label to the ``RejectionFunnel`` attribute (or callable) it reads.
_FUNNEL_ROWS: list[tuple[str, str]] = [
    ("Universe candidates (symbol x session)", "universe_candidates"),
    ("Symbols never producing a usable context", "universe_filtered_symbols"),
    ("(symbol, session) pairs with no context", "no_context"),
    ("Strategy declines (total)", "strategy_decline_total"),
    ("Strategy errors / invalid proposals", "strategy_errors"),
    ("Gate-rejected (hard filters)", "gate_rejected"),
    ("Score-rejected (below threshold)", "score_rejected"),
    ("Risk sizing produced zero shares", "sizing_zero"),
    ("Portfolio-limit rejected", "limits_rejected"),
    ("Signals generated (orders queued)", "signals_generated"),
    ("Entries filled", "entries_filled"),
    ("Entries expired unfilled", "entries_expired_unfilled"),
    ("Entries still queued at run end", "entries_carried_to_end"),
]


def funnel_rows(funnel) -> list[dict[str, object]]:
    """Structured funnel rows for the table/bar chart. Pure -- no Streamlit."""
    rows = []
    for label, attr in _FUNNEL_ROWS:
        value = getattr(funnel, attr)
        rows.append({"Stage": label, "Count": value() if callable(value) else value})
    return rows


def page_backtesting() -> None:
    """Render the backtesting page."""
    config = get_config()
    theme.inject_css()
    page_header("🧪", "Backtesting", "Runs the real engine over stored history -- no placeholders.")

    selected_strategies, start_date, end_date, allow_shorts, force_close = _render_config_form(config)

    run_col, wf_col = st.columns(2)
    with run_col:
        run_clicked = st.button("Run Backtest", type="primary", key="run_backtest")
    with wf_col:
        wf_clicked = st.button(
            "Run Walk-Forward Validation",
            key="run_walk_forward",
            help="Rolling train/test folds using the same strategies and window. "
            "More expensive than a single run -- several backtests execute in sequence.",
        )

    if run_clicked:
        if not selected_strategies:
            st.error("Select at least one strategy.")
        elif start_date >= end_date:
            st.error("Start date must be before end date.")
        else:
            result = _run_backtest(config, selected_strategies, start_date, end_date, allow_shorts, force_close)
            set_backtest_result(result)

    if wf_clicked:
        if not selected_strategies:
            st.error("Select at least one strategy.")
        elif start_date >= end_date:
            st.error("Start date must be before end date.")
        else:
            _run_and_render_walk_forward(config, selected_strategies, start_date, end_date, allow_shorts)

    result = get_backtest_result()
    if result is None:
        empty_state(
            "No backtest has been run in this session yet. Configure strategies and a "
            "date range above, then click Run Backtest.",
        )
        return

    _render_results(result)


def _render_config_form(config):
    st.subheader("Configuration")
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "Start Date",
            value=dt.datetime.now(dt.UTC).date() - dt.timedelta(days=730),
            key="bt_start_date",
        )
    with col2:
        end_date = st.date_input(
            "End Date", value=dt.datetime.now(dt.UTC).date(), key="bt_end_date"
        )

    selected_strategies = st.multiselect(
        "Strategies",
        options=available_strategies(),
        default=[s for s in config.signals.enabled_strategies if s in available_strategies()],
        key="bt_strategies",
    )

    opt1, opt2 = st.columns(2)
    with opt1:
        allow_shorts = st.checkbox(
            "Allow shorts",
            value=config.signals.allow_shorts,
            help="Sets `config.signals.allow_shorts` for this run -- the field strategies "
            "and risk checks actually consult.",
        )
    with opt2:
        force_close = st.checkbox(
            "Force-close open positions at run end",
            value=config.backtest.force_close_open_positions,
            help="Sets `config.backtest.force_close_open_positions`. Every position must be "
            "classified as a win/loss/breakeven -- leaving one open would silently "
            "exclude it from every metric.",
        )
    return selected_strategies, start_date, end_date, allow_shorts, force_close


def _build_run_config(config, selected_strategies, allow_shorts, force_close):
    cfg = config.model_copy(deep=True)
    cfg.signals.enabled_strategies = list(selected_strategies)
    cfg.signals.allow_shorts = allow_shorts
    cfg.backtest.force_close_open_positions = force_close
    return cfg


def _run_backtest(config, selected_strategies, start_date, end_date, allow_shorts, force_close):
    cfg = _build_run_config(config, selected_strategies, allow_shorts, force_close)
    with st.status("Running backtest...", expanded=True) as status:
        try:
            status.write("Bootstrapping pipeline for this configuration...")
            pipeline = Pipeline.bootstrap(cfg)
            universe = pipeline.universe.for_session(end_date)
            if not universe.symbols:
                status.update(label="Universe is empty", state="error")
                empty_state(
                    "The universe is empty for this end date -- there is no market data to "
                    "backtest against.",
                    "claudetrade refresh",
                )
                return None
            status.write(f"Building point-in-time contexts for {len(universe.symbols)} symbols...")
            provider = pipeline.make_context_provider(
                symbols=universe.symbols, start=start_date, end=end_date
            )
            n_sessions = len(provider.sessions())
            status.write(f"Running the engine over {n_sessions} sessions...")
            engine = BacktestEngine(cfg)
            result = engine.run(provider, start_session=start_date, end_session=end_date)
            status.update(
                label=f"Backtest complete: {len(result.trades)} trade(s)", state="complete"
            )
            return result
        except Exception as exc:
            status.update(label="Backtest failed", state="error")
            st.error(f"Backtest failed: {exc}")
            return None


def _run_and_render_walk_forward(config, selected_strategies, start_date, end_date, allow_shorts):
    cfg = _build_run_config(config, selected_strategies, allow_shorts, True)
    with st.status("Running walk-forward validation...", expanded=True) as status:
        try:
            pipeline = Pipeline.bootstrap(cfg)
            universe = pipeline.universe.for_session(end_date)
            if not universe.symbols:
                status.update(label="Universe is empty", state="error")
                empty_state("The universe is empty for this end date.", "claudetrade refresh")
                return
            provider = pipeline.make_context_provider(
                symbols=universe.symbols, start=start_date, end=end_date
            )
            train_days = cfg.backtest.walk_forward_train_days
            test_days = cfg.backtest.walk_forward_test_days
            span_days = (end_date - start_date).days
            if span_days < train_days + test_days:
                status.update(label="Window too short for one fold", state="error")
                st.warning(
                    f"The selected window ({span_days} days) is shorter than one "
                    f"train+test fold ({train_days + test_days} days) -- widen the date "
                    "range or lower `walk_forward_train_days`/`walk_forward_test_days`."
                )
                return
            status.write(f"Rolling train={train_days}d / test={test_days}d folds...")
            engine = BacktestEngine(cfg)
            output = walk_forward(engine, provider, start_date, end_date, cfg.backtest)
            status.update(label=f"{len(output['folds'])} fold(s) complete", state="complete")
        except Exception as exc:
            status.update(label="Walk-forward failed", state="error")
            st.error(f"Walk-forward validation failed: {exc}")
            return

    st.session_state["bt_walk_forward_output"] = output
    _render_walk_forward(output)


def _render_results(result: BacktestResult) -> None:
    st.subheader("Results")
    metrics = PerformanceMetrics(**result.metrics)

    tab_overview, tab_trades, tab_segments, tab_walkforward, tab_export = st.tabs(
        ["Overview", "Trade List", "Segments", "Walk-Forward", "Export"]
    )

    with tab_overview:
        _render_overview(result, metrics)
    with tab_trades:
        _render_trades(result)
    with tab_segments:
        _render_segments(result)
    with tab_walkforward:
        wf_output = st.session_state.get("bt_walk_forward_output")
        if wf_output is None:
            empty_state(
                "Walk-forward validation has not been run in this session.",
            )
            st.caption("Click 'Run Walk-Forward Validation' above to compute rolling out-of-sample folds.")
        else:
            _render_walk_forward(wf_output)
    with tab_export:
        _render_export(result)

    for warning in result.warnings:
        st.caption(f"⚠️ {warning}")


def _render_overview(result: BacktestResult, metrics: PerformanceMetrics) -> None:
    significance_caveat(
        is_significant=metrics.is_statistically_significant,
        reason=metrics.significance_reason,
        trade_count=metrics.trade_count,
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_tile("Trades", str(metrics.trade_count))
        metric_tile("Win Rate", f"{100 * metrics.win_rate:.1f}%")
    with c2:
        wl = "inf" if metrics.win_loss_ratio_is_degenerate else f"{metrics.win_loss_ratio:.2f}"
        metric_tile("Win/Loss Ratio", wl)
        metric_tile("Expectancy", f"${metrics.expectancy_dollars:,.2f}")
    with c3:
        metric_tile("Profit Factor", f"{metrics.profit_factor:.2f}")
        metric_tile("Total Return", f"{metrics.total_return_pct:.2f}%")
    with c4:
        metric_tile("Max Drawdown", f"{metrics.max_drawdown_pct:.2f}%")
        metric_tile(
            "Sharpe",
            None if "sharpe" in metrics.unavailable_reasons else f"{metrics.sharpe:.2f}",
            unavailable_reason=metrics.unavailable_reasons.get("sharpe"),
        )

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        metric_tile(
            "Sortino",
            None if "sortino" in metrics.unavailable_reasons else f"{metrics.sortino:.2f}",
            unavailable_reason=metrics.unavailable_reasons.get("sortino"),
        )
    with c6:
        metric_tile(
            "Calmar",
            None if "calmar" in metrics.unavailable_reasons else f"{metrics.calmar:.2f}",
            unavailable_reason=metrics.unavailable_reasons.get("calmar"),
        )
    with c7:
        metric_tile("Avg Holding Days", f"{metrics.average_holding_days:.1f}")
    with c8:
        metric_tile("Exposure", f"{metrics.exposure_pct:.1f}%")

    if result.equity_curve:
        sessions = [p.session for p in result.equity_curve]
        st.plotly_chart(
            create_equity_curve_chart(sessions, [p.equity for p in result.equity_curve]),
            theme=None,
        )
        st.plotly_chart(
            create_drawdown_chart(sessions, [p.drawdown_pct for p in result.equity_curve]),
            theme=None,
        )
    else:
        empty_state("No equity curve recorded -- the run produced no sessions with a position open.")

    st.write("**Rejection Funnel**")
    rows = funnel_rows(result.funnel)
    fc1, fc2 = st.columns([2, 3])
    with fc1:
        st.dataframe(rows, hide_index=True, width="stretch")
    with fc2:
        st.plotly_chart(
            create_funnel_bar_chart([r["Stage"] for r in rows], [r["Count"] for r in rows]),
            theme=None,
        )
    if result.funnel.strategy_declined:
        with st.expander("Strategy decline reasons"):
            for strat, reasons in sorted(result.funnel.strategy_declined.items()):
                st.write(f"**{strat}**: " + ", ".join(f"{r}={c}" for r, c in sorted(reasons.items(), key=lambda kv: -kv[1])))


def _render_trades(result: BacktestResult) -> None:
    if not result.trades:
        empty_state(
            "No completed trades in this run -- see the Rejection Funnel on the "
            "Overview tab for exactly where candidates fell out."
        )
        return
    df = trades_to_dataframe(result)
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "entry_price": st.column_config.NumberColumn(format="$%.2f"),
            "exit_price": st.column_config.NumberColumn(format="$%.2f"),
            "stop_loss": st.column_config.NumberColumn(format="$%.2f"),
            "net_pnl": st.column_config.NumberColumn(format="$%.2f"),
            "gross_pnl": st.column_config.NumberColumn(format="$%.2f"),
            "net_return_pct": st.column_config.NumberColumn(format="%.2f%%"),
            "r_multiple": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def _render_segments(result: BacktestResult) -> None:
    if not result.segment_metrics:
        empty_state("No segment metrics available for this run.")
        return
    dimension = st.selectbox("Segment by", options=sorted(result.segment_metrics.keys()))
    segments = result.segment_metrics.get(dimension) or {}
    if not segments:
        empty_state(f"No completed trades to segment by {dimension}.")
        return
    rows = []
    for bucket, seg in sorted(segments.items()):
        wl = "inf" if seg.win_loss_ratio_is_degenerate else f"{seg.win_loss_ratio:.2f}"
        rows.append(
            {
                "Segment": bucket,
                "Trades": seg.trade_count,
                "Win/Loss": wl,
                "Expectancy": seg.expectancy_dollars,
                "Profit Factor": seg.profit_factor,
                "Win Rate %": 100 * seg.win_rate,
            }
        )
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "Expectancy": st.column_config.NumberColumn(format="$%.2f"),
            "Profit Factor": st.column_config.NumberColumn(format="%.2f"),
            "Win Rate %": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )


def _render_walk_forward(output: dict) -> None:
    folds = output.get("folds", [])
    if not folds:
        empty_state("No folds fit inside the selected window.")
        return
    rows = []
    for fold in folds:
        oos = fold["oos_metrics"]
        wl = "inf" if oos.win_loss_ratio_is_degenerate else f"{oos.win_loss_ratio:.2f}"
        rows.append(
            {
                "Fold": fold["fold"],
                "Train": f"{fold['train_start']} -> {fold['train_end']}",
                "Test (OOS)": f"{fold['test_start']} -> {fold['test_end']}",
                "OOS Trades": oos.trade_count,
                "OOS Win/Loss": wl,
                "OOS Expectancy": oos.expectancy_dollars,
            }
        )
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={"OOS Expectancy": st.column_config.NumberColumn(format="$%.2f")},
    )

    aggregate = output.get("aggregate_oos")
    if aggregate is None:
        empty_state("No out-of-sample trades were produced across any fold.")
        return
    st.write("**Aggregate out-of-sample performance (all folds' test trades concatenated)**")
    significance_caveat(
        is_significant=aggregate.is_statistically_significant,
        reason=aggregate.significance_reason,
        trade_count=aggregate.trade_count,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_tile("OOS Trades", str(aggregate.trade_count))
    with c2:
        wl = "inf" if aggregate.win_loss_ratio_is_degenerate else f"{aggregate.win_loss_ratio:.2f}"
        metric_tile("OOS Win/Loss", wl)
    with c3:
        metric_tile("OOS Expectancy", f"${aggregate.expectancy_dollars:,.2f}")


def _render_export(result: BacktestResult) -> None:
    st.write("Every export below is generated from this run's actual result object via "
             "`claudetrade.backtest.reporting` -- the same functions `claudetrade backtest "
             "--export <dir>` uses from the CLI.")

    trades_csv = trades_to_dataframe(result).to_csv(index=False).encode("utf-8")
    equity_csv = equity_to_dataframe(result).to_csv(index=False).encode("utf-8")
    metrics_csv = metrics_to_dataframe(result.metrics).to_csv(index=False).encode("utf-8")
    report_md = render_markdown_report(result).encode("utf-8")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.download_button(
            "Download trades.csv", data=trades_csv, file_name=f"trades_{result.run_id}.csv",
            mime="text/csv", disabled=not result.trades,
        )
    with c2:
        st.download_button(
            "Download equity_curve.csv", data=equity_csv,
            file_name=f"equity_curve_{result.run_id}.csv", mime="text/csv",
            disabled=not result.equity_curve,
        )
    with c3:
        st.download_button(
            "Download metrics.csv", data=metrics_csv,
            file_name=f"metrics_{result.run_id}.csv", mime="text/csv",
        )
    with c4:
        st.download_button(
            "Download report.md", data=report_md,
            file_name=f"report_{result.run_id}.md", mime="text/markdown",
        )
    if not result.trades:
        st.caption("trades.csv is disabled: this run produced zero completed trades.")
