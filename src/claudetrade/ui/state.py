"""Streamlit session state management.

Provides caching and cached state access for expensive operations like
scanning, backtesting, and database queries. Implements the single-shot
pattern so expensive work runs once and persists across reruns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline

if TYPE_CHECKING:
    from claudetrade.backtest.engine import BacktestResult
    from claudetrade.pipeline import PipelineResult
    from claudetrade.signals.engine import ScanResult


def init_session_state() -> None:
    """Initialize session state variables on first page load."""
    defaults = {
        "pipeline": None,
        "config": None,
        "last_refresh": None,
        "last_scan": None,
        "last_scan_result": None,
        "last_refresh_result": None,
        "backtest_result": None,
        "backtest_in_progress": False,
        "selected_symbol": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_config() -> AppConfig:
    """Retrieve or load the application config once per session."""
    if st.session_state.config is None:
        st.session_state.config = AppConfig.load()
    return st.session_state.config


@st.cache_resource(hash_funcs={AppConfig: lambda c: c.config_hash})
def get_pipeline(config: AppConfig) -> Pipeline:
    """Bootstrap and cache the pipeline for this configuration.

    Hashed by ``config.config_hash`` (a content digest, see
    ``AppConfig.config_hash``) rather than Streamlit's default object hasher,
    so two different configurations -- e.g. two ``CLAUDETRADE_HOME`` values in
    two test runs, or a real config change -- correctly get distinct cached
    pipelines instead of silently reusing the first one ever built.
    """
    return Pipeline.bootstrap(config)


def set_selected_symbol(symbol: str | None) -> None:
    """Record the user's selected ticker for detail view."""
    st.session_state.selected_symbol = symbol


def get_selected_symbol() -> str | None:
    """Retrieve the currently selected symbol."""
    return st.session_state.selected_symbol


def set_last_scan_result(result: ScanResult) -> None:
    """Cache the most recent in-session scan, including its rejected list.

    ``ScanResult.rejected`` is never persisted to the database (only the
    signals that cleared every gate are written to the ledger), so the
    Scanner's near-miss expander can only show it for the scan just run in
    this session -- there is no path to recover it after a rerun that didn't
    also re-scan.
    """
    st.session_state.last_scan_result = result


def get_last_scan_result() -> ScanResult | None:
    return st.session_state.get("last_scan_result")


def set_last_refresh_result(result: PipelineResult) -> None:
    st.session_state.last_refresh_result = result


def get_last_refresh_result() -> PipelineResult | None:
    return st.session_state.get("last_refresh_result")


def set_backtest_result(result: BacktestResult | None) -> None:
    st.session_state.backtest_result = result


def get_backtest_result() -> BacktestResult | None:
    return st.session_state.get("backtest_result")
