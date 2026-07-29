"""Ticker Detail page: technical analysis, sentiment, signals, and levels.

Shows candlestick chart with technical indicators, sentiment timeline,
earnings dates, and historical signals with outcomes.
"""

from __future__ import annotations

import streamlit as st

from claudetrade.ui.formatting import (
    format_confidence,
    format_date,
    format_price,
    format_status,
    show_disclaimer,
)
from claudetrade.ui.state import get_config, get_pipeline


def page_ticker_detail() -> None:
    """Render the ticker detail page."""
    st.set_page_config(page_title="Ticker Detail", layout="wide")
    st.title("📈 Ticker Detail")
    show_disclaimer()

    config = get_config()
    pipeline = get_pipeline(config)

    # --- Symbol Selection ---
    st.subheader("Select Symbol")
    symbol = st.text_input(
        "Enter ticker symbol",
        value="",
        placeholder="e.g., AAPL",
        key="ticker_input",
    ).upper()

    if not symbol:
        st.info("Enter a ticker symbol to view details")
        return

    # --- Fetch Symbol Signals ---
    try:
        signals = pipeline.ledger.for_symbol(symbol, limit=50)
    except Exception as e:
        st.error(f"Error loading signals: {e}")
        signals = []

    if not signals:
        st.warning(f"No signals found for {symbol}")
        return

    # --- Current Signal (if any) ---
    latest_signal = signals[0] if signals else None
    if latest_signal:
        st.subheader(f"Current Signal: {symbol}")
        col1, col2, col3 = st.columns(3)

        with col1:
            st.metric("Score", f"{latest_signal.overall_score:.0f}")
            st.metric("Confidence", format_confidence(latest_signal.confidence))

        with col2:
            status = pipeline.ledger.current_status(latest_signal.signal_id)
            st.metric("Status", format_status(status.value if status else "unknown"))
            st.metric("Strategy", latest_signal.strategy)

        with col3:
            st.metric("Entry Range",
                     f"${latest_signal.plan.entry_low:.2f} - ${latest_signal.plan.entry_high:.2f}")
            st.metric("Stop Loss", format_price(latest_signal.plan.stop_loss))

        # Proposed Levels
        col1, col2, col3 = st.columns(3)
        with col1:
            st.write("**Entry Target**")
            st.write(f"Low: {format_price(latest_signal.plan.entry_low)}")
            st.write(f"High: {format_price(latest_signal.plan.entry_high)}")

        with col2:
            st.write("**Stop Loss**")
            st.write(format_price(latest_signal.plan.stop_loss))
            st.write(f"Risk: {format_price(latest_signal.plan.risk_per_share)}")

        with col3:
            st.write("**Targets**")
            if latest_signal.plan.targets:
                for i, target in enumerate(latest_signal.plan.targets):
                    st.write(f"T{i+1}: {format_price(target)}")
            else:
                st.write("No targets")

        # Thesis & Invalidation
        col1, col2 = st.columns(2)
        with col1:
            st.write("**Thesis**")
            st.write(latest_signal.thesis if latest_signal.thesis else "No thesis available")

        with col2:
            st.write("**Invalidation Conditions**")
            if latest_signal.invalidation:
                for cond in latest_signal.invalidation:
                    st.write(f"- {cond}")
            else:
                st.write("No invalidation conditions")

        # Component Breakdown
        st.subheader("Component Scores")
        components = latest_signal.components.as_dict()
        cols = st.columns(4)
        for i, (name, score) in enumerate(components.items()):
            with cols[i % 4]:
                st.metric(name, f"{score:.0f}")

    # --- Signal History ---
    st.subheader("Signal History")
    try:
        if signals:
            history_data = []
            for sig in signals[:20]:
                status = pipeline.ledger.current_status(sig.signal_id)
                history_data.append({
                    "Date": format_date(sig.session),
                    "Strategy": sig.strategy,
                    "Direction": "📈 LONG" if str(sig.direction) == "long" else "📉 SHORT",
                    "Score": f"{sig.overall_score:.0f}",
                    "Status": format_status(status.value if status else "unknown"),
                    "Entry Range": f"${sig.plan.entry_low:.2f}-${sig.plan.entry_high:.2f}",
                })
            st.dataframe(history_data, use_container_width=True)
        else:
            st.info("No signal history available")
    except Exception as e:
        st.error(f"Error loading signal history: {e}")

    # --- Sentiment Timeline ---
    st.subheader("Sentiment Timeline")
    try:
        # Placeholder: sentiment data would come from the sentiment aggregation
        st.info("Sentiment timeline data not yet available in this view")
    except Exception as e:
        st.warning(f"Could not load sentiment data: {e}")

    # --- Earnings Dates ---
    st.subheader("Earnings Calendar")
    try:
        if latest_signal and latest_signal.next_earnings_date:
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Next Earnings**: {format_date(latest_signal.next_earnings_date)}")
            with col2:
                if latest_signal.days_to_earnings is not None:
                    st.write(f"**Days to Earnings**: {latest_signal.days_to_earnings}")
        else:
            st.info("No upcoming earnings date found")
    except Exception as e:
        st.warning(f"Could not load earnings data: {e}")

    # --- Technical Chart ---
    st.subheader("Price Action")
    try:
        st.info("Candlestick chart rendering requires market data integration")
    except Exception as e:
        st.error(f"Error loading chart data: {e}")
