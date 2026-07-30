"""End-to-end smoke tests: each screen renders without raising.

Uses ``streamlit.testing.v1.AppTest`` to drive each ``page_*`` function
directly -- the harness has no browser, so this is the documented way to
"click around" a Streamlit screen from a test. Two fixtures are exercised:
an empty database (every empty-state path) and a small, realistic one
(candidate tables, the ticker chart with real bars/sentiment/earnings,
signal history).
"""

from __future__ import annotations

import datetime as dt

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from claudetrade.config import reset_config_cache
from claudetrade.db.models import EarningsEventRow, PriceBar, SymbolSentimentDaily
from claudetrade.db.session import reset_database_cache
from claudetrade.domain import (
    ComponentScores,
    Direction,
    MarketRegime,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.pipeline import Pipeline
from claudetrade.signals.ledger import SignalLedger

#: (module name under claudetrade.ui.screens, function name). AppTest needs a
#: real script with its own imports -- ``AppTest.from_function`` re-executes
#: only the function's own source lines with no module-level imports carried
#: over, so each screen is driven via a tiny generated wrapper script instead
#: (see ``_app_test_for``).
_SCREENS = [
    ("dashboard", "page_dashboard"),
    ("scanner", "page_scanner"),
    ("ticker_detail", "page_ticker_detail"),
    ("backtesting", "page_backtesting"),
    ("settings", "page_settings"),
]


def _app_test_for(module_name: str, func_name: str) -> AppTest:
    script = (
        "from claudetrade.ui.state import init_session_state\n"
        f"from claudetrade.ui.screens.{module_name} import {func_name}\n"
        "init_session_state()\n"
        f"{func_name}()\n"
    )
    return AppTest.from_string(script, default_timeout=30)


def _make_signal(symbol: str, strategy: str, direction: Direction, score: float) -> Signal:
    now = dt.datetime(2024, 3, 1, 15, 0, tzinfo=dt.UTC)
    return Signal(
        signal_id=f"test-{symbol}-{strategy}",
        created_at=now,
        session=dt.date(2024, 3, 1),
        symbol=symbol,
        company_name=f"{symbol} Inc",
        strategy=strategy,
        direction=direction,
        status=SignalStatus.ACTIONABLE,
        reference_price=25.0,
        price_as_of=now,
        overall_score=score,
        confidence=0.7,
        components=ComponentScores(),
        plan=TradePlan(
            entry_low=24.0, entry_high=26.0, stop_loss=22.0, targets=[28.0, 30.0], shares=100
        ),
        regime=MarketRegime.BULL_QUIET,
    )


@pytest.fixture
def empty_env(tmp_app_config, monkeypatch):
    """A bootstrapped pipeline over an otherwise-empty database."""
    st.cache_resource.clear()
    Pipeline.bootstrap(tmp_app_config)
    yield tmp_app_config
    st.cache_resource.clear()
    reset_config_cache()
    reset_database_cache()


@pytest.fixture
def populated_env(tmp_app_config, monkeypatch):
    """A small, realistic dataset: bars, two signals, sentiment, and an earnings date."""
    st.cache_resource.clear()
    pipeline = Pipeline.bootstrap(tmp_app_config)

    base = dt.date(2024, 1, 2)
    with pipeline.db.session() as session:
        session_date = base
        while session_date < dt.date(2024, 10, 1):
            if session_date.weekday() < 5:
                session.add(
                    PriceBar(
                        symbol="ACME",
                        session=session_date,
                        open=24.5,
                        high=25.8,
                        low=24.2,
                        close=25.1,
                        adj_close=25.1,
                        volume=1_000_000,
                        source="test",
                    )
                )
            session_date += dt.timedelta(days=1)
        session.add(
            SymbolSentimentDaily(
                symbol="ACME",
                session=dt.date(2024, 2, 20),
                source="all",
                post_count=14,
                unique_authors=6,
                bull_bear_ratio=1.8,
            )
        )
        session.add(
            EarningsEventRow(
                symbol="ACME", report_date=dt.date(2024, 4, 15), source="test"
            )
        )

    ledger = SignalLedger(pipeline.db)
    ledger.record(_make_signal("ACME", "sentiment_breakout", Direction.LONG, 72.0))
    ledger.record(_make_signal("ACME", "hype_failure_short", Direction.SHORT, 60.0))

    yield tmp_app_config
    st.cache_resource.clear()
    reset_config_cache()
    reset_database_cache()


@pytest.mark.parametrize("module_name,func_name", _SCREENS, ids=[s[0] for s in _SCREENS])
def test_screen_renders_on_empty_database(empty_env, module_name, func_name):
    at = _app_test_for(module_name, func_name)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


@pytest.mark.parametrize("module_name,func_name", _SCREENS, ids=[s[0] for s in _SCREENS])
def test_screen_renders_on_populated_database(populated_env, module_name, func_name):
    at = _app_test_for(module_name, func_name)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


def test_dashboard_shows_top_candidates(populated_env):
    at = _app_test_for("dashboard", "page_dashboard")
    at.run()
    assert not at.exception
    body = "\n".join(m.value for m in at.markdown) + "\n".join(str(df.value) for df in at.dataframe)
    assert "ACME" in body


def test_ticker_detail_defaults_to_top_scored_symbol(populated_env):
    at = _app_test_for("ticker_detail", "page_ticker_detail")
    at.run()
    assert not at.exception
    assert at.selectbox(key="ticker_symbol_select").value == "ACME"
