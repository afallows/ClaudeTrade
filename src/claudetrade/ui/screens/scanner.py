"""Scanner page: candidate universe with filtering and details.

Shows a sortable, filterable table of scanned candidates with per-component
scores, earnings dates, and rejection reasons for excluded symbols.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from claudetrade.ui.formatting import (
    format_confidence,
    format_status,
    show_disclaimer,
)
from claudetrade.ui.state import get_config, get_pipeline


def page_scanner() -> None:
    """Render the scanner page."""
    st.set_page_config(page_title="Scanner", layout="wide")
    st.title("🔍 Signal Scanner")
    show_disclaimer()

    config = get_config()
    pipeline = get_pipeline(config)

    # --- Refresh Control ---
    st.subheader("Data Control")
    col1, col2 = st.columns(2)

    with col1:
        if st.button("Refresh All Data", key="refresh_all"):
            with st.spinner("Fetching market data, social posts, and earnings..."):
                try:
                    today = dt.datetime.now(dt.UTC).date()
                    start = today - dt.timedelta(days=config.sentiment.lookback_days)
                    result = pipeline.refresh(start=start, end=today)
                    st.success(f"Refreshed: {result.universe_size} symbols, "
                              f"{result.sentiment_rows} sentiment rows")
                except Exception as e:
                    st.error(f"Refresh failed: {e}")

    with col2:
        if st.button("Run Scan", key="run_scan"):
            with st.spinner("Scanning universe for trading signals..."):
                try:
                    today = dt.datetime.now(dt.UTC).date()
                    result = pipeline.scan(None, lookback_days=config.sentiment.lookback_days)
                    if result and hasattr(result, 'signals'):
                        st.success(f"Scan complete: {len(result.signals)} signals generated")
                    else:
                        st.info("Scan complete (no signals generated)")
                except Exception as e:
                    st.error(f"Scan failed: {e}")

    # --- Filters ---
    st.subheader("Filters")
    col1, col2, col3 = st.columns(3)

    with col1:
        direction_filter = st.multiselect(
            "Direction",
            options=["Long", "Short"],
            default=["Long", "Short"],
        )

    with col2:
        min_score = st.slider(
            "Minimum Score",
            min_value=0.0,
            max_value=100.0,
            value=config.signals.min_overall_score,
            step=1.0,
        )

    with col3:
        min_confidence = st.slider(
            "Minimum Confidence",
            min_value=0.0,
            max_value=1.0,
            value=config.signals.min_confidence,
            step=0.05,
        )

    # --- Candidate Table ---
    st.subheader("Candidates")
    try:
        recent = pipeline.ledger.recent(limit=100)
        if recent:
            # Filter
            filtered = [
                s for s in recent
                if (s.overall_score >= min_score
                    and s.confidence >= min_confidence
                    and str(s.direction).capitalize() in direction_filter)
            ]

            if filtered:
                # Build display table
                data = []
                for sig in filtered:
                    status = pipeline.ledger.current_status(sig.signal_id)
                    data.append({
                        "Symbol": sig.symbol,
                        "Name": sig.company_name,
                        "Direction": "📈 LONG" if str(sig.direction) == "long" else "📉 SHORT",
                        "Score": f"{sig.overall_score:.0f}",
                        "Confidence": format_confidence(sig.confidence),
                        "Status": format_status(status.value if status else "unknown"),
                        "Entry": f"${sig.plan.entry_low:.2f}-${sig.plan.entry_high:.2f}",
                        "Target": f"${sig.plan.targets[0]:.2f}" if sig.plan.targets else "-",
                        "Stop": f"${sig.plan.stop_loss:.2f}",
                        "R:R": f"{sig.plan.reward_risk_ratio:.2f}:1",
                    })

                # Display table with selection
                st.dataframe(data, use_container_width=True)

                # Detail view on click
                st.subheader("Signal Detail")
                selected_symbol = st.selectbox(
                    "Select a signal to view details:",
                    options=[s.symbol for s in filtered],
                    key="scanner_symbol_select",
                )

                if selected_symbol:
                    symbol_signals = [s for s in filtered if s.symbol == selected_symbol]
                    if symbol_signals:
                        sig = symbol_signals[0]
                        col1, col2 = st.columns(2)
                        with col1:
                            st.write(f"**Signal ID**: {sig.signal_id}")
                            st.write(f"**Strategy**: {sig.strategy}")
                            st.write(f"**Created**: {format_datetime(sig.created_at, config)}")
                            st.write(f"**Thesis**: {sig.thesis}")
                        with col2:
                            st.write(f"**Invalidation**: {', '.join(sig.invalidation) if sig.invalidation else 'None'}")
                            st.write(f"**Risks**: {', '.join(sig.risks) if sig.risks else 'None'}")
                            st.write(f"**Evidence**: {', '.join(sig.evidence) if sig.evidence else 'None'}")

                        # Component scores
                        st.write("**Component Scores**:")
                        components = sig.components.as_dict()
                        cols = st.columns(4)
                        for i, (name, score) in enumerate(components.items()):
                            cols[i % 4].metric(name, f"{score:.0f}")

            else:
                st.info("No signals match the selected filters")
        else:
            st.info("No signals generated yet. Click 'Run Scan' to start.")
    except Exception as e:
        st.error(f"Error loading signals: {e}")


def format_datetime(dt_val, config):
    """Format a datetime."""
    if dt_val is None:
        return "-"
    return dt_val.strftime("%Y-%m-%d %H:%M")
