"""`claudetrade refresh` and `claudetrade init` default behaviour.

Covers two of the out-of-box-experience fixes:

* ``refresh`` with no ``--start``/``--end`` must cover the last 90 calendar
  days ending today, not a multi-year window -- a fresh install pointed at a
  real provider should not have to pull years of history for hundreds of
  symbols before it shows anything.
* ``init`` must say which universe is active and confirm that live Stooq data
  is enabled by default rather than fabricated synthetic tickers.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.session import reset_database_cache
from claudetrade.pipeline import PipelineResult

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()
    yield
    reset_config_cache()
    reset_database_cache()


def test_refresh_defaults_to_a_90_day_window(cli_env, monkeypatch):
    captured: dict[str, object] = {}

    class _FakePipeline:
        @classmethod
        def bootstrap(cls, config):
            return cls()

        def refresh(self, *, start, end, symbols=None):
            captured["start"] = start
            captured["end"] = end
            captured["symbols"] = symbols
            return PipelineResult()

    monkeypatch.setattr("claudetrade.pipeline.Pipeline", _FakePipeline)

    result = runner.invoke(app, ["refresh"])

    assert result.exit_code == 0, result.output
    assert captured["symbols"] is None
    start: dt.date = captured["start"]  # type: ignore[assignment]
    end: dt.date = captured["end"]  # type: ignore[assignment]
    assert (end - start).days == 90


def test_refresh_explicit_dates_are_not_overridden(cli_env, monkeypatch):
    captured: dict[str, object] = {}

    class _FakePipeline:
        @classmethod
        def bootstrap(cls, config):
            return cls()

        def refresh(self, *, start, end, symbols=None):
            captured["start"] = start
            captured["end"] = end
            return PipelineResult()

    monkeypatch.setattr("claudetrade.pipeline.Pipeline", _FakePipeline)

    result = runner.invoke(
        app, ["refresh", "--start", "2024-01-01", "--end", "2024-12-31"]
    )

    assert result.exit_code == 0, result.output
    assert captured["start"] == dt.date(2024, 1, 1)
    assert captured["end"] == dt.date(2024, 12, 31)


def test_init_reports_active_universe_and_live_provider(cli_env):
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "universe:" in result.output
    assert "us_default" in result.output
    assert "ca_default" in result.output
    assert "provider=stooq" in result.output
    assert "live; Stooq is the default" in result.output


def test_stooq_is_the_default_market_provider(cli_env):
    """A fresh install must not silently populate fabricated market tickers."""
    from claudetrade.config import get_config

    runner.invoke(app, ["init"])
    config = get_config(reload=True)
    assert config.market_data.provider == "stooq"
    assert config.market_data.fallbacks == ["yahoo", "csv"]
