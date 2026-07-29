"""ClaudeTrade Streamlit application.

Main entry point for the Windows desktop UI. Implements five-screen navigation:
1. Dashboard - market regime, top candidates, recent outcomes
2. Scanner - filterable candidate universe
3. Ticker Detail - technical analysis and signal history
4. Backtesting - strategy validation and walk-forward analysis
5. Settings - configuration, secrets, risk limits, database

Run with: streamlit run app.py --server.port=8501
"""

from __future__ import annotations

import streamlit as st

from claudetrade.ui.screens import backtesting, dashboard, scanner, settings, ticker_detail
from claudetrade.ui.state import init_session_state
from claudetrade.version import CODE_VERSION


def main() -> None:
    """Main application entry point."""
    # Page configuration
    st.set_page_config(
        page_title="ClaudeTrade",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Initialize session state
    init_session_state()

    # Sidebar navigation
    st.sidebar.title("📊 ClaudeTrade")
    st.sidebar.write(f"v{CODE_VERSION}")

    page = st.sidebar.radio(
        "Navigation",
        options=[
            "Dashboard",
            "Scanner",
            "Ticker Detail",
            "Backtesting",
            "Settings",
        ],
        label_visibility="collapsed",
    )

    # Route to selected page
    if page == "Dashboard":
        dashboard.page_dashboard()
    elif page == "Scanner":
        scanner.page_scanner()
    elif page == "Ticker Detail":
        ticker_detail.page_ticker_detail()
    elif page == "Backtesting":
        backtesting.page_backtesting()
    elif page == "Settings":
        settings.page_settings()

    # Footer
    st.sidebar.divider()
    st.sidebar.write("**About**")
    st.sidebar.write(
        "ClaudeTrade is an automated swing-trading research tool. "
        "All signals are research only and not investment advice."
    )


if __name__ == "__main__":
    main()
