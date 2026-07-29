"""Dashboard page: market regime, candidates, recent outcomes, metrics.

Shows current market state, top signals, recent trades, provider status,
and key performance metrics with validation warnings.
"""

from __future__ import annotations

import streamlit as st

from claudetrade.ui.formatting import (
    format_confidence,
    format_currency,
    format_datetime,
    format_status,
    show_disclaimer,
)
from claudetrade.ui.state import get_config, get_pipeline


def page_dashboard() -> None:
    """Render the dashboard."""
    st.set_page_config(page_title="Dashboard", layout="wide")
    st.title("📊 Dashboard")
    show_disclaimer()

    config = get_config()
    pipeline = get_pipeline(config)

    # --- Market Regime and Status ---
    st.subheader("Market Status")
    col1, col2, col3 = st.columns(3)

    try:
        status = pipeline.provider_status()
        with col1:
            st.metric("Data Providers", f"{len(status)} configured")
        working = sum(1 for s in status if s.available)
        with col2:
            st.metric("Providers OK", f"{working}/{len(status)}")
        with col3:
            st.metric("Account Size", format_currency(config.risk.account_size_usd))
    except Exception as e:
        st.error(f"Error loading status: {e}")

    # --- Recent Signals ---
    st.subheader("Recent Signals")
    try:
        recent = pipeline.ledger.recent(limit=20)
        if recent:
            # Split by direction
            longs = [s for s in recent if str(s.direction) == "long"]
            shorts = [s for s in recent if str(s.direction) == "short"]

            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Long Candidates** ({len(longs)})")
                if longs:
                    long_data = []
                    for sig in longs[:10]:
                        status = pipeline.ledger.current_status(sig.signal_id)
                        long_data.append({
                            "Symbol": sig.symbol,
                            "Score": f"{sig.overall_score:.0f}",
                            "Confidence": format_confidence(sig.confidence),
                            "Status": format_status(status.value if status else "unknown"),
                            "Entry": f"${sig.plan.entry_low:.2f}-${sig.plan.entry_high:.2f}",
                        })
                    st.dataframe(long_data, use_container_width=True)
                else:
                    st.info("No long signals yet")

            with col2:
                st.write(f"**Short Candidates** ({len(shorts)})")
                if shorts:
                    short_data = []
                    for sig in shorts[:10]:
                        status = pipeline.ledger.current_status(sig.signal_id)
                        short_data.append({
                            "Symbol": sig.symbol,
                            "Score": f"{sig.overall_score:.0f}",
                            "Confidence": format_confidence(sig.confidence),
                            "Status": format_status(status.value if status else "unknown"),
                            "Entry": f"${sig.plan.entry_low:.2f}-${sig.plan.entry_high:.2f}",
                        })
                    st.dataframe(short_data, use_container_width=True)
                else:
                    st.info("No short signals yet")
        else:
            st.info("No signals generated yet. Run a scan to populate.")
    except Exception as e:
        st.error(f"Error loading signals: {e}")

    # --- Performance Snapshot ---
    st.subheader("Performance Snapshot")
    try:
        # Try to load recent trades from the database
        try:
            recent_signals = pipeline.ledger.recent(limit=100)
            if recent_signals:
                st.info("(Performance metrics require a backtest result)")
        except Exception:
            pass

        # Show a placeholder if no backtest result
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Trades", "-")
        with col2:
            st.metric("Win Rate", "-")
        with col3:
            st.metric("Expectancy", "-")
        with col4:
            st.metric("Max Drawdown", "-")

    except Exception as e:
        st.warning(f"Unable to compute performance metrics: {e}")

    # --- Data Provider Status ---
    st.subheader("Data Provider Status")
    try:
        status_report = pipeline.provider_status()
        if status_report:
            # provider_status() returns ProviderStatus dataclasses, not dicts.
            provider_data = [
                {
                    "Provider": item.name,
                    "Kind": item.kind,
                    "Status": "available" if item.available else "unavailable",
                    "Configured": "yes" if item.configured else "no",
                    "Point-in-time": "yes" if item.supports_point_in_time else "no",
                    "Delisted": "yes" if item.supports_delisted else "no",
                    "Message": item.message,
                    "Last success": format_datetime(item.last_success, config)
                    if item.last_success
                    else "-",
                }
                for item in status_report
            ]
            st.dataframe(provider_data, use_container_width=True)
        else:
            st.info("No provider status available")
    except Exception as e:
        st.error(f"Error loading provider status: {e}")

    # --- Settings Summary ---
    st.subheader("Configuration")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.write(f"**Market Data**: {config.market_data.provider}")
        st.write(f"**AI Provider**: {config.ai.provider}")
    with col2:
        st.write(f"**Reddit Enabled**: {'Yes' if config.reddit.enabled else 'No'}")
        st.write(f"**X Enabled**: {'Yes' if config.x.enabled else 'No'}")
    with col3:
        st.write(f"**Trading Mode**: {config.trading.mode}")
        st.write(f"**Allow Shorts**: {'Yes' if config.signals.allow_shorts else 'No'}")
