"""``claudetrade db fetch-health`` -- inspecting and clearing the per-symbol
fetch quarantine.

The quarantine (see ``data.ingest``'s ``_QUARANTINE_AFTER_FAILURES``) is
deliberately invisible during a normal refresh beyond one summary log line,
so this command is the operator's only way to answer "why is my symbol not
updating?" -- and its ``--clear`` escape hatch is the documented way to undo
a quarantine the operator disagrees with.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.models import SymbolFetchHealth
from claudetrade.db.session import get_database, reset_database_cache

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()
    runner.invoke(app, ["init"])
    yield tmp_path
    reset_config_cache()
    reset_database_cache()


def _db():
    from claudetrade.config import get_config

    return get_database(get_config(reload=True))


def _seed() -> None:
    now = dt.datetime.now(tz=dt.UTC)
    with _db().session() as session:
        session.add(
            SymbolFetchHealth(
                symbol="DEAD",
                consecutive_failures=4,
                last_failure_at=now,
                last_error="no bars from any configured market-data provider this refresh",
                quarantined_until=now + dt.timedelta(days=7),
            )
        )
        session.add(
            SymbolFetchHealth(
                symbol="FLAKY",
                consecutive_failures=1,
                last_failure_at=now,
                last_error="no bars from any configured market-data provider this refresh",
            )
        )


def _rows() -> list[SymbolFetchHealth]:
    with _db().read_session() as session:
        return list(session.execute(select(SymbolFetchHealth)).scalars())


def test_fetch_health_reports_a_clean_database(cli_env):
    result = runner.invoke(app, ["db", "fetch-health"])
    assert result.exit_code == 0, result.output
    assert "No failing symbols" in result.output


def test_fetch_health_lists_failing_and_quarantined_symbols(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "fetch-health"])
    assert result.exit_code == 0, result.output

    assert "DEAD" in result.output
    assert "FLAKY" in result.output
    # A symbol that is merely failing (1 strike) is listed but not counted as
    # quarantined -- the distinction is the whole point of the listing.
    assert "2 failing symbol(s), 1 currently quarantined" in result.output
    assert "--clear" in result.output


def test_fetch_health_clear_one_symbol(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "fetch-health", "--clear", "dead"])
    assert result.exit_code == 0, result.output
    assert "cleared DEAD" in result.output
    assert {r.symbol for r in _rows()} == {"FLAKY"}


def test_fetch_health_clear_unknown_symbol_is_not_an_error(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "fetch-health", "--clear", "NOSUCH"])
    assert result.exit_code == 0, result.output
    assert "no fetch-health record" in result.output
    assert len(_rows()) == 2


def test_fetch_health_clear_all(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "fetch-health", "--clear-all"])
    assert result.exit_code == 0, result.output
    assert "cleared 2 fetch-health record(s)" in result.output
    assert _rows() == []


def test_fetch_health_rejects_both_clear_flags(cli_env):
    _seed()
    result = runner.invoke(app, ["db", "fetch-health", "--clear", "DEAD", "--clear-all"])
    assert result.exit_code != 0
    assert len(_rows()) == 2  # nothing destroyed by the rejected invocation


def test_clearing_makes_the_next_refresh_fetch_the_symbol_again(cli_env):
    """The contract the command promises in its own help text."""
    from claudetrade.config import get_config
    from claudetrade.data.ingest import DataIngestor, IngestReport
    from claudetrade.domain import Bar

    _seed()

    class _Provider:
        name = "recording"

        def __init__(self):
            self.requested: list[str] = []

        def get_daily_bars(self, symbols, start, end, *, adjusted=True):
            self.requested.extend(symbols)
            return {
                s: [
                    Bar(
                        symbol=s, session=start, open=1.0, high=1.0, low=1.0,
                        close=1.0, volume=1.0, source=self.name,
                    )
                ]
                for s in symbols
            }

    config = get_config(reload=True)
    provider = _Provider()
    ingestor = DataIngestor(config, _db(), market_provider=provider)

    ingestor.ingest_prices(["DEAD"], dt.date(2026, 7, 29), dt.date(2026, 7, 30), IngestReport())
    assert "DEAD" not in provider.requested  # quarantined

    assert runner.invoke(app, ["db", "fetch-health", "--clear", "DEAD"]).exit_code == 0

    provider.requested.clear()
    ingestor.ingest_prices(["DEAD"], dt.date(2026, 7, 29), dt.date(2026, 7, 30), IngestReport())
    assert "DEAD" in provider.requested
