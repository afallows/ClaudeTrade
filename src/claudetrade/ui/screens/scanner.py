"""Scanner: filterable candidate universe, near-miss rejections, signal detail.

Refresh/scan run through the same ``Pipeline`` methods the CLI and scheduler
use. ``ScanResult.rejected`` (the near-miss candidates and their score
breakdown) is never persisted -- only signals that clear every gate reach the
ledger -- so it is only available for the scan just run in this session; the
expander says so rather than pretending to show history it does not have.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.ui import theme
from claudetrade.ui.components.layout import page_header
from claudetrade.ui.components.tables import (
    apply_candidate_filters,
    empty_state,
    rejected_dataframe,
    signals_column_config,
    signals_dataframe,
)
from claudetrade.ui.formatting import format_date, format_datetime, format_price
from claudetrade.ui.state import (
    get_config,
    get_last_refresh_result,
    get_last_scan_result,
    get_pipeline,
    set_last_refresh_result,
    set_last_scan_result,
)


def page_scanner() -> None:
    """Render the scanner page."""
    config = get_config()
    pipeline = get_pipeline(config)
    theme.inject_css()
    page_header("🔍", "Signal Scanner", "Filter, sort, and inspect the candidate universe.")

    _render_controls(config, pipeline)

    try:
        recent = pipeline.ledger.recent(limit=200)
    except Exception as exc:
        st.error(f"Error loading signals: {exc}")
        return

    if not recent:
        empty_state(
            "No signals generated yet -- the ledger is empty for this database.",
            "claudetrade scan",
        )
        return

    filtered = _render_filters_and_table(config, pipeline, recent)
    _render_rejected_expander()
    _render_detail_panel(config, pipeline, filtered)


def _render_controls(config, pipeline) -> None:
    st.subheader("Data Control")
    col1, col2 = st.columns(2)

    with col1:
        if st.button("Refresh All Data", key="refresh_all"):
            with st.spinner("Fetching market data, social posts, and earnings..."):
                try:
                    today = dt.datetime.now(dt.UTC).date()
                    start = today - dt.timedelta(days=config.sentiment.lookback_days)
                    result = pipeline.refresh(start=start, end=today)
                    set_last_refresh_result(result)
                    st.success(
                        f"Refreshed: {result.universe_size} symbols, "
                        f"{result.sentiment_rows} sentiment rows"
                    )
                    for warning in result.warnings:
                        st.warning(warning)
                except Exception as exc:
                    st.error(f"Refresh failed: {exc}")

    with col2:
        if st.button("Run Scan", key="run_scan"):
            with st.spinner("Scanning universe for trading signals..."):
                try:
                    today = dt.datetime.now(dt.UTC).date()
                    result = pipeline.scan(today, lookback_days=config.sentiment.lookback_days)
                    if result.scan is not None:
                        set_last_scan_result(result.scan)
                        st.success(
                            f"Scan complete: {len(result.scan.signals)} signal(s) from "
                            f"{result.scan.evaluated_symbols} symbols evaluated "
                            f"({len(result.scan.rejected)} rejected)"
                        )
                    for warning in result.warnings:
                        st.warning(warning)
                except Exception as exc:
                    st.error(f"Scan failed: {exc}")

    last_refresh = get_last_refresh_result()
    if last_refresh is not None and last_refresh.finished_at is not None:
        st.caption(f"Last refresh (this session): {format_datetime(last_refresh.finished_at, config)}")


def _render_filters_and_table(config, pipeline, recent):
    st.subheader("Filters")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        direction_filter = st.multiselect(
            "Direction", options=["long", "short"], default=["long", "short"],
            format_func=lambda d: d.upper(),
        )
    with col2:
        min_score = st.slider(
            "Minimum Score", min_value=0.0, max_value=100.0,
            value=float(config.signals.min_overall_score), step=1.0,
        )
    with col3:
        min_confidence = st.slider(
            "Minimum Confidence", min_value=0.0, max_value=1.0,
            value=float(config.signals.min_confidence), step=0.05,
        )
    with col4:
        max_days_to_earnings = st.slider(
            "Max Days to Earnings (0 = no limit)", min_value=0, max_value=90, value=0, step=1,
        )

    strategy_options = sorted({s.strategy for s in recent})
    selected_strategies = st.multiselect(
        "Strategy", options=strategy_options, default=strategy_options,
    )

    filtered = apply_candidate_filters(
        recent,
        directions=set(direction_filter) if direction_filter else set(),
        min_score=min_score,
        min_confidence=min_confidence,
        strategies=set(selected_strategies) if selected_strategies else set(),
        max_days_to_earnings=max_days_to_earnings or None,
    )

    st.subheader(f"Candidates ({len(filtered)})")
    if not filtered:
        empty_state("No signals match the selected filters. Loosen a filter above to see candidates.")
        return []

    status_by_id = {
        s.signal_id: (pipeline.ledger.current_status(s.signal_id) or None) for s in filtered
    }
    status_by_id = {k: (v.value if v else None) for k, v in status_by_id.items()}
    df = signals_dataframe(filtered, status_by_id)

    event = st.dataframe(
        df,
        column_order=[c for c in df.columns if c != "Signal ID"],
        column_config=signals_column_config(),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="scanner_candidates",
    )
    selected_rows = list(event.selection.get("rows", [])) if event else []
    st.session_state["scanner_selected_signal_ids"] = [
        df.iloc[i]["Signal ID"] for i in selected_rows if i < len(df)
    ]
    return filtered


def _render_rejected_expander() -> None:
    scan_result = get_last_scan_result()
    with st.expander("Near-miss / rejected candidates", expanded=False):
        if scan_result is None:
            empty_state(
                "Rejected candidates are only available for the scan just run in this "
                "session -- they are not persisted (only signals that clear every gate "
                "are written to the ledger). Run a scan to populate this list.",
                "claudetrade scan",
            )
            return
        if not scan_result.rejected:
            st.success("No rejected candidates in the last scan -- everything evaluated cleared every gate.")
            return
        st.caption(
            f"From the scan run at {scan_result.generated_at.isoformat(timespec='seconds')} "
            f"({scan_result.evaluated_symbols} symbols evaluated)."
        )
        st.dataframe(rejected_dataframe(scan_result.rejected), hide_index=True, width="stretch")


def _render_detail_panel(config, pipeline, filtered) -> None:
    st.subheader("Signal Detail")
    if not filtered:
        return

    selected_ids = st.session_state.get("scanner_selected_signal_ids") or []
    default_symbol = None
    if selected_ids:
        match = next((s for s in filtered if s.signal_id == selected_ids[0]), None)
        if match is not None:
            default_symbol = match.symbol

    symbols = [s.symbol for s in filtered]
    index = symbols.index(default_symbol) if default_symbol in symbols else 0
    selected_symbol = st.selectbox(
        "Symbol (select a table row above, or pick here)",
        options=symbols,
        index=index,
        key="scanner_symbol_select",
    )

    sig = next((s for s in filtered if s.symbol == selected_symbol), None)
    if sig is None:
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        st.write(f"**Signal ID**: `{sig.signal_id}`")
        st.write(f"**Strategy**: {sig.strategy}")
        st.write(f"**Created**: {format_datetime(sig.created_at, config)}")
        st.write(f"**Direction**: {theme.direction_label(str(sig.direction))}")
    with col2:
        st.write(f"**Entry**: {format_price(sig.plan.entry_low)} - {format_price(sig.plan.entry_high)}")
        st.write(f"**Stop**: {format_price(sig.plan.stop_loss)}")
        st.write(
            "**Targets**: "
            + (", ".join(format_price(t) for t in sig.plan.targets) if sig.plan.targets else "none")
        )
        st.write(f"**R:R**: {sig.plan.reward_risk_ratio:.2f}:1")
    with col3:
        st.write(f"**Thesis**: {sig.thesis or '-'}")
        st.write(f"**Next earnings**: {format_date(sig.next_earnings_date)}")

    with st.expander("Invalidation, risks and evidence"):
        st.write("**Invalidation**: " + (", ".join(sig.invalidation) if sig.invalidation else "-"))
        st.write("**Risks**: " + (", ".join(sig.risks) if sig.risks else "-"))
        st.write("**Evidence**: " + (", ".join(sig.evidence) if sig.evidence else "-"))

    st.write("**Component Scores**")
    components = sig.components.as_dict()
    cols = st.columns(4)
    for i, (name, score) in enumerate(components.items()):
        cols[i % 4].metric(name.replace("_", " ").title(), f"{score:.0f}")

    st.write("**Paper Trading**")
    if st.button(f"Open Paper Trade: {sig.symbol}", key=f"open_paper_{sig.signal_id}"):
        _open_paper_trade(pipeline, sig)


def _open_paper_trade(pipeline, sig) -> None:
    """Submit ``sig`` to the paper broker through the same seam the CLI uses.

    Mirrors `claudetrade paper open`: prices the entry on the first stored
    bar after the signal's session (never the signal bar itself -- that would
    be look-ahead) and reports a fill, a rejection, or "not fillable yet"
    honestly rather than pretending the order went through.
    """
    from claudetrade.brokers.base import OrderRequest, TradingHaltedError
    from claudetrade.paper.broker import PaperBroker

    try:
        broker = PaperBroker(pipeline.config, pipeline.db)
        next_bar = broker.next_bar_after(sig.symbol, sig.session)
        if next_bar is None:
            st.warning(
                f"Not fillable yet: no stored bar for {sig.symbol} after {sig.session}. "
                "Run `claudetrade refresh`, then try again."
            )
            return

        request = OrderRequest(
            signal=sig, next_bar=next_bar, marks=broker.marks_for_open_positions()
        )
        try:
            order = broker.submit_order(request)
        except TradingHaltedError as exc:
            st.error(f"Refused: {exc}")
            return

        if order.status.value == "filled":
            st.success(
                f"Filled: {order.symbol} {order.filled_shares} shares @ "
                f"{order.average_fill_price:.2f} on {next_bar.session} "
                f"(order {order.order_id})"
            )
        else:
            st.error(f"Rejected: {'; '.join(order.reasons) or 'no reason given'}")
    except Exception as exc:
        st.error(f"Could not submit paper order: {exc}")
