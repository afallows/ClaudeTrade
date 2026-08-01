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


def _fake_pipeline_class(tmp_path, captured: dict[str, object]):
    """A ``Pipeline`` stand-in for the refresh command.

    Carries a real (migrated) throwaway ``Database`` because the refresh
    command now acquires the cross-process refresh lock through
    ``pipeline.db`` (``db.refresh_state_store``, F27) before calling
    ``refresh`` -- and accepts ``progress_callback``, which the command now
    passes for the lock's heartbeat.
    """
    from claudetrade.db.migrations import init_database
    from claudetrade.db.session import Database

    class _FakePipeline:
        @classmethod
        def bootstrap(cls, config):
            inst = cls()
            inst.db = Database(f"sqlite:///{tmp_path}/cli-refresh-test.db")
            init_database(inst.db)
            return inst

        def refresh(self, *, start, end, symbols=None, progress_callback=None):
            captured["start"] = start
            captured["end"] = end
            captured["symbols"] = symbols
            captured["progress_callback"] = progress_callback
            return PipelineResult()

    return _FakePipeline


def test_refresh_defaults_to_a_90_day_window(cli_env, tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "claudetrade.pipeline.Pipeline", _fake_pipeline_class(tmp_path, captured)
    )

    result = runner.invoke(app, ["refresh"])

    assert result.exit_code == 0, result.output
    assert captured["symbols"] is None
    # The refresh-lock heartbeat rides the ordinary progress plumbing.
    assert captured["progress_callback"] is not None
    start: dt.date = captured["start"]  # type: ignore[assignment]
    end: dt.date = captured["end"]  # type: ignore[assignment]
    assert (end - start).days == 90


def test_refresh_explicit_dates_are_not_overridden(cli_env, tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "claudetrade.pipeline.Pipeline", _fake_pipeline_class(tmp_path, captured)
    )

    result = runner.invoke(
        app, ["refresh", "--start", "2024-01-01", "--end", "2024-12-31"]
    )

    assert result.exit_code == 0, result.output
    assert captured["start"] == dt.date(2024, 1, 1)
    assert captured["end"] == dt.date(2024, 12, 31)


def test_refresh_refuses_when_another_entry_point_holds_the_lock(
    cli_env, tmp_path, monkeypatch
):
    """F27 single-flight: with a live webapi-held run in the shared database,
    the CLI refresh must refuse -- naming the holder -- and never call
    ``Pipeline.refresh`` at all."""
    from claudetrade.db import refresh_state_store

    captured: dict[str, object] = {}
    fake_cls = _fake_pipeline_class(tmp_path, captured)
    monkeypatch.setattr("claudetrade.pipeline.Pipeline", fake_cls)

    # Simulate the web API's running refresh in the same database file.
    holder_db = fake_cls.bootstrap(None).db
    outcome = refresh_state_store.try_acquire(holder_db, "webapi")
    assert outcome.acquired

    result = runner.invoke(app, ["refresh"])

    assert result.exit_code == 1
    assert "webapi" in result.output
    assert "already running" in result.output
    assert "start" not in captured  # Pipeline.refresh never ran


def test_init_reports_active_universe_and_live_provider(cli_env):
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "universe:" in result.output
    assert "us_default" in result.output
    assert "ca_default" in result.output
    assert "provider=tipranks" in result.output
    assert "live; TipRanks is the default" in result.output


def test_tipranks_is_the_default_market_provider(cli_env):
    """A fresh install must not silently populate fabricated market tickers."""
    from claudetrade.config import get_config

    runner.invoke(app, ["init"])
    config = get_config(reload=True)
    assert config.market_data.provider == "tipranks"
    assert config.market_data.fallbacks == ["yahoo", "csv"]
    assert config.earnings.provider == "tipranks"
