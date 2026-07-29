"""Streamlit session state management.

Provides caching and cached state access for expensive operations like
scanning, backtesting, and database queries. Implements the single-shot
pattern so expensive work runs once and persists across reruns.
"""

from __future__ import annotations

import streamlit as st

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline


def init_session_state() -> None:
    """Initialize session state variables on first page load."""
    defaults = {
        "pipeline": None,
        "config": None,
        "last_refresh": None,
        "last_scan": None,
        "selected_symbol": None,
        "backtest_in_progress": False,
        "backtest_result": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_config() -> AppConfig:
    """Retrieve or load the application config once per session."""
    if st.session_state.config is None:
        st.session_state.config = AppConfig.load()
    return st.session_state.config


@st.cache_resource
def get_pipeline(_config: AppConfig) -> Pipeline:
    """Bootstrap and cache the pipeline for the session.

    The underscore prefix on _config ensures the cache key includes config
    but doesn't try to hash the AppConfig object itself.
    """
    return Pipeline.bootstrap(_config)


def set_selected_symbol(symbol: str | None) -> None:
    """Record the user's selected ticker for detail view."""
    st.session_state.selected_symbol = symbol


def get_selected_symbol() -> str | None:
    """Retrieve the currently selected symbol."""
    return st.session_state.selected_symbol
