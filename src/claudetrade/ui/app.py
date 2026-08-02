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

from claudetrade.ui import theme
from claudetrade.ui.components.layout import render_footer, render_sidebar_status
from claudetrade.ui.screens import backtesting, dashboard, scanner, settings, ticker_detail
from claudetrade.ui.state import get_config, get_pipeline, init_session_state
from claudetrade.version import CODE_VERSION

_PAGES = {
    "Dashboard": ("📊", dashboard.page_dashboard),
    "Scanner": ("🔍", scanner.page_scanner),
    "Ticker Detail": ("📈", ticker_detail.page_ticker_detail),
    "Backtesting": ("🧪", backtesting.page_backtesting),
    "Settings": ("⚙️", settings.page_settings),
}


def main() -> None:
    """Main application entry point."""
    st.set_page_config(
        page_title="ClaudeTrade",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.inject_css()
    init_session_state()

    config = get_config()
    pipeline = get_pipeline(config)

    st.sidebar.title("📊 ClaudeTrade")
    st.sidebar.caption(f"Research terminal · v{CODE_VERSION}")

    page = st.sidebar.radio(
        "Navigation",
        options=list(_PAGES.keys()),
        format_func=lambda name: f"{_PAGES[name][0]}  {name}",
        label_visibility="collapsed",
    )

    render_sidebar_status(config, pipeline)

    _, render = _PAGES[page]
    render()

    render_footer()


if __name__ == "__main__":
    main()
