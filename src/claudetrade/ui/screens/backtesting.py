"""Backtesting page: strategy validation, walk-forward analysis, export.

Allows configuration and execution of backtests, displays metrics,
equity curves, trade lists, and validation warnings. Exports to CSV/Excel.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.strategies.registry import available_strategies
from claudetrade.ui.charts import (
    create_drawdown_chart,
    create_equity_curve_chart,
)
from claudetrade.ui.formatting import show_disclaimer
from claudetrade.ui.state import get_config


def page_backtesting() -> None:
    """Render the backtesting page."""
    st.set_page_config(page_title="Backtesting", layout="wide")
    st.title("📊 Backtesting")
    show_disclaimer()

    config = get_config()

    # --- Configuration ---
    st.subheader("Backtest Configuration")

    col1, col2 = st.columns(2)

    with col1:
        start_date = st.date_input(
            "Start Date",
            value=dt.datetime.now(dt.UTC).date() - dt.timedelta(days=252),
            key="bt_start_date",
        )

    with col2:
        end_date = st.date_input(
            "End Date",
            value=dt.datetime.now(dt.UTC).date(),
            key="bt_end_date",
        )

    # Strategy selection
    available = available_strategies()

    st.write("**Select Strategies**")
    selected_strategies = []
    cols = st.columns(3)
    for i, strategy in enumerate(available):
        with cols[i % 3]:
            if st.checkbox(strategy, value=strategy in config.signals.enabled_strategies):
                selected_strategies.append(strategy)

    if not selected_strategies:
        st.warning("Select at least one strategy to backtest")

    # Additional options
    st.write("**Options**")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.checkbox("Allow Shorts", value=config.backtest.allow_shorts)

    with col2:
        st.checkbox(
            "Force Close Open Positions",
            value=config.backtest.force_close_open_positions,
        )

    with col3:
        st.checkbox(
            "Walk-Forward Validation",
            value=True,
        )

    # --- Run Backtest ---
    if st.button("Run Backtest", key="run_backtest"):
        if not selected_strategies:
            st.error("Please select at least one strategy")
        elif start_date >= end_date:
            st.error("Start date must be before end date")
        else:
            with st.spinner("Running backtest... this may take a moment"):
                try:
                    # Note: Backtesting requires full integration with market data
                    # and the backtest engine. This is a placeholder structure.
                    st.info(
                        "Backtesting integration requires running against historical "
                        "market data. Set up market data first via Settings."
                    )

                    # Display sample metrics if available
                    st.subheader("Backtest Results (Placeholder)")
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("Total Trades", "-")
                    with col2:
                        st.metric("Win Rate", "-")
                    with col3:
                        st.metric("Profit Factor", "-")
                    with col4:
                        st.metric("Max Drawdown", "-")

                except Exception as e:
                    st.error(f"Backtest failed: {e}")

    # --- Results Display (if available) ---
    st.subheader("Backtest Results")

    # Tabs for different views
    tab1, tab2, tab3, tab4 = st.tabs(
        ["Metrics", "Equity Curve", "Trade List", "Export"]
    )

    with tab1:
        st.write("**Performance Metrics**")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Trades", "-")
            st.metric("Wins", "-")
            st.metric("Losses", "-")
        with col2:
            st.metric("Win Rate", "-")
            st.metric("Profit Factor", "-")
            st.metric("Expectancy", "-")
        with col3:
            st.metric("Total Return", "-")
            st.metric("Annual Return", "-")
            st.metric("Sharpe Ratio", "-")
        with col4:
            st.metric("Max Drawdown", "-")
            st.metric("Drawdown Duration", "-")
            st.metric("Recovery Factor", "-")

        st.write("**By Strategy**")
        st.info("Strategy breakdown would appear here after running a backtest")

    with tab2:
        st.write("**Equity Curve**")
        # Sample data
        try:
            import pandas as pd
            dates = pd.date_range(start_date, end_date, freq='D')
            equity = [100000.0] * len(dates)  # Placeholder: flat equity
            fig = create_equity_curve_chart(
                [d.date() for d in dates],
                equity,
                title="Portfolio Equity Over Time",
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.warning(f"Could not render equity curve: {e}")

        st.write("**Drawdown Chart**")
        try:
            import pandas as pd
            dates = pd.date_range(start_date, end_date, freq='D')
            drawdowns = [0.0] * len(dates)  # Placeholder: no drawdown
            fig = create_drawdown_chart(
                [d.date() for d in dates],
                drawdowns,
                title="Portfolio Drawdown",
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.warning(f"Could not render drawdown chart: {e}")

    with tab3:
        st.write("**Trade List**")
        st.info("Trade list would appear here after running a backtest")

        # Sample columns
        sample_data = {
            "Trade ID": [],
            "Symbol": [],
            "Entry Date": [],
            "Entry Price": [],
            "Exit Date": [],
            "Exit Price": [],
            "P&L": [],
            "Return %": [],
            "Holding Days": [],
        }
        if sample_data["Trade ID"]:
            st.dataframe(sample_data, use_container_width=True)
        else:
            st.info("No trades executed in this backtest")

    with tab4:
        st.write("**Export Results**")
        col1, col2 = st.columns(2)

        with col1:
            if st.button("Export as CSV", key="export_csv"):
                st.info("CSV export would contain trades.csv, equity_curve.csv, and metrics.csv")

        with col2:
            if st.button("Export as Excel", key="export_xlsx"):
                st.info("Excel export would contain all results in a formatted workbook")

    # --- Validation Warnings ---
    st.subheader("Validation Warnings")
    try:
        # This would be populated from a real backtest result
        warnings = []
        if warnings:
            for warning in warnings:
                st.warning(f"⚠️ {warning}")
        else:
            st.success("✅ No validation warnings (run a backtest to check)")
    except Exception as e:
        st.error(f"Error checking validation: {e}")
