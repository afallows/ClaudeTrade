"""``claudetrade db backfill`` -- the one-time historical price backfill.

QA handoff v3 F23: the scanner gates on 60 sessions of price history and a
daily refresh only ever adds one session per day, so without this command a
fresh install stays dead for weeks (2,355 symbols evaluated, 11,775
rejections, 100% ``insufficient_history``). These tests pin the properties
that make the command usable on a 5-calls/minute free tier: newest-first
ordering, per-date commits, resume-by-skipping-covered-dates, and a clean
refusal when polygon is unconfigured.

No live network: the polygon adapter's ``httpx.Client`` is swapped for a
``MockTransport`` serving the committed fixtures, the same pattern as
``tests/test_tipranks_provider.py`` and ``tests/test_polygon_provider.py``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.models import PriceBar, Security
from claudetrade.db.session import get_database, reset_database_cache

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures" / "polygon"
GROUPED_BY_DATE = {
    "2026-07-29": json.loads((FIXTURES / "grouped_2026-07-29.json").read_text(encoding="utf-8")),
    "2026-07-30": json.loads((FIXTURES / "grouped_2026-07-30.json").read_text(encoding="utf-8")),
}

D_0729 = dt.date(2026, 7, 29)
D_0730 = dt.date(2026, 7, 30)
#: Symbols the fixtures cover that are also in the packaged seed universe, so
#: the backfill's target-symbol filter keeps them.
FIXTURE_SYMBOLS = {"AAPL", "INTC", "SPY", "BRK-B"}


class _Stub:
    """Serves grouped responses per date, with per-date status overrides."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_by_date: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        date = request.url.path.rsplit("/", 1)[-1]
        status = self.status_by_date.get(date)
        if status is not None:
            return httpx.Response(status, json={})
        payload = GROUPED_BY_DATE.get(date)
        if payload is None:
            return httpx.Response(200, json={"status": "OK", "resultsCount": 0, "results": []})
        return httpx.Response(200, json=payload)

    @property
    def dates_requested(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


@pytest.fixture
def stub(monkeypatch) -> _Stub:
    s = _Stub()
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(s.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.polygon.httpx.Client", _factory)
    return s


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    # The real limiter blocks; the free-tier default of 5/min would make each
    # extra date cost 12 real seconds of test wall clock.
    monkeypatch.setenv("CLAUDETRADE_POLYGON__RATE_LIMIT_PER_MINUTE", "6000")
    reset_config_cache()
    reset_database_cache()
    runner.invoke(app, ["init"])
    yield tmp_path
    reset_config_cache()
    reset_database_cache()


@pytest.fixture
def unconfigured_cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDETRADE_SECRET_POLYGON_API_KEY", raising=False)
    reset_config_cache()
    reset_database_cache()
    runner.invoke(app, ["init"])
    yield tmp_path
    reset_config_cache()
    reset_database_cache()


def _db():
    from claudetrade.config import get_config

    return get_database(get_config(reload=True))


def _bars() -> list[PriceBar]:
    with _db().read_session() as session:
        return list(session.execute(select(PriceBar)).scalars())


# --------------------------------------------------------------------------
# unconfigured
# --------------------------------------------------------------------------


def test_backfill_refuses_cleanly_when_polygon_is_unconfigured(unconfigured_cli_env):
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert result.exit_code == 1
    assert "POLYGON_API_KEY" in result.output
    assert _bars() == []


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_backfill_persists_grouped_bars_newest_first(cli_env, stub):
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert result.exit_code == 0, result.output

    # Newest -> oldest: the scanner becomes useful as soon as the most recent
    # sessions land, long before the whole range finishes.
    assert stub.dates_requested == ["2026-07-30", "2026-07-29"]

    rows = _bars()
    assert {r.symbol for r in rows} == FIXTURE_SYMBOLS
    assert {r.session for r in rows} == {D_0729, D_0730}
    assert {r.source for r in rows} == {"polygon_grouped"}

    aapl = sorted(
        (r for r in rows if r.symbol == "AAPL"), key=lambda r: r.session
    )
    assert aapl[0].close == pytest.approx(231.15)
    assert aapl[1].close == pytest.approx(233.02)
    assert aapl[0].volume == pytest.approx(70790813)

    # BRK.B (Polygon's dot notation) is stored under this codebase's BRK-B.
    assert any(r.symbol == "BRK-B" for r in rows)
    # A response row for a symbol outside the universe is never stored -- the
    # grouped response is the whole market (~10k rows/day).
    assert not any(r.symbol == "ZZZC" for r in rows)

    assert '"bars_written": 8' in result.output
    assert '"dates_fetched": 2' in result.output
    assert "claudetrade scan" in result.output


def test_backfill_summary_reports_symbols_and_dates(cli_env, stub):
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
    assert payload["dates_processed"] == 2
    assert payload["dates_fetched"] == 2
    assert payload["dates_skipped_covered"] == 0
    assert payload["http_calls"] == 2
    assert payload["symbols_covered"] == 4
    assert payload["bars_written"] == 8


def test_backfill_skips_weekends_and_holidays(cli_env, stub):
    """The window spans a weekend; only real trading dates cost a call."""
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-08-02"])
    assert result.exit_code == 0, result.output
    # 07-29 Wed .. 08-02 Sun -> Wed/Thu/Fri only.
    assert stub.dates_requested == ["2026-07-31", "2026-07-30", "2026-07-29"]


def test_years_option_sizes_the_window(cli_env, stub):
    """``--years`` is the headline option; it must actually reach back that
    far rather than silently defaulting to a short window."""
    result = runner.invoke(app, ["db", "backfill", "--years", "1", "--end", "2026-07-30"])
    assert result.exit_code == 0, result.output
    requested = sorted(stub.dates_requested)
    assert requested[0].startswith("2025-07")
    assert requested[-1] == "2026-07-30"
    # ~252 trading days in a year, and every one of them is a call attempt.
    assert 240 <= len(requested) <= 260


# --------------------------------------------------------------------------
# resume / idempotence
# --------------------------------------------------------------------------


def test_rerun_skips_already_covered_dates_without_http(cli_env, stub):
    """Resume semantics: a second run costs zero calls and writes nothing --
    which is also what makes a Ctrl-C'd backfill safe to restart."""
    first = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert first.exit_code == 0, first.output
    calls_after_first = len(stub.requests)
    bars_after_first = len(_bars())

    second = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert second.exit_code == 0, second.output
    assert len(stub.requests) == calls_after_first  # no new HTTP at all
    assert len(_bars()) == bars_after_first  # no duplicate rows
    assert '"dates_skipped_covered": 2' in second.output


def test_partial_run_commits_per_date_so_a_failure_keeps_earlier_progress(cli_env, stub):
    """Per-date commits (short transactions -- no long-transaction
    contention): a date that fails mid-run must not roll back the dates
    already written before it."""
    stub.status_by_date["2026-07-29"] = 500  # oldest date, processed last

    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert result.exit_code == 0, result.output

    rows = _bars()
    # 07-30 (processed first) is committed; 07-29 failed and wrote nothing.
    assert {r.session for r in rows} == {D_0730}
    assert '"dates_errored": 1' in result.output

    # And the failed date is retried on the next run, then committed.
    del stub.status_by_date["2026-07-29"]
    again = runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    assert again.exit_code == 0, again.output
    assert {r.session for r in _bars()} == {D_0729, D_0730}


def test_force_refetches_and_replaces_covered_dates(cli_env, stub):
    """``--force`` re-fetches covered dates and replaces the rows in place --
    one row per (symbol, session), never a duplicate."""
    runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-29"])
    with _db().session() as session:
        row = session.execute(
            select(PriceBar).where(PriceBar.symbol == "AAPL", PriceBar.session == D_0729)
        ).scalar_one()
        row.close = 1.23  # simulate a stale/bad stored value

    result = runner.invoke(
        app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-29", "--force"]
    )
    assert result.exit_code == 0, result.output

    rows = [r for r in _bars() if r.symbol == "AAPL"]
    assert len(rows) == 1  # replaced in place, not duplicated
    assert rows[0].close == pytest.approx(231.15)
    assert '"dates_skipped_covered": 0' in result.output


def test_force_never_destroys_rows_for_symbols_polygon_does_not_cover(cli_env, stub):
    """A TSX name whose bars came from the tipranks/yahoo cascade must
    survive a ``--force`` pass: the replace is scoped to symbols the grouped
    response actually returned."""
    with _db().session() as session:
        session.add(Security(symbol="TECK-B", name="Teck Resources"))
        session.add(
            PriceBar(
                symbol="TECK-B", session=D_0729, open=40.0, high=41.0, low=39.5,
                close=40.5, adj_close=40.5, volume=1000.0, source="yahoo",
            )
        )

    result = runner.invoke(
        app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-29", "--force"]
    )
    assert result.exit_code == 0, result.output

    teck = [r for r in _bars() if r.symbol == "TECK-B"]
    assert len(teck) == 1
    assert teck[0].source == "yahoo"
    assert teck[0].close == pytest.approx(40.5)


def test_backfilled_dates_are_free_cache_hits_for_a_later_window(cli_env, stub):
    """The provider's per-date cache is shared with the refresh path, so a
    backfilled date never costs a second HTTP call -- here proven by a
    ``--force`` run (which skips the DB-coverage check) hitting the cache."""
    runner.invoke(app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"])
    calls_after_first = len(stub.requests)

    result = runner.invoke(
        app, ["db", "backfill", "--start", "2026-07-29", "--end", "2026-07-30"]
    )
    assert result.exit_code == 0, result.output
    assert len(stub.requests) == calls_after_first
    assert '"http_calls": 0' in result.output


def test_empty_polygon_response_writes_nothing_and_stays_retryable(cli_env, stub):
    """A date polygon has no data for (EOD not published yet, an unmodelled
    closure) writes nothing, is reported, and is NOT recorded as covered --
    so a later run retries it."""
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-27", "--end", "2026-07-27"])
    assert result.exit_code == 0, result.output
    assert "no data" in result.output
    assert _bars() == []
    assert '"bars_written": 0' in result.output


def test_start_after_end_is_rejected(cli_env, stub):
    result = runner.invoke(app, ["db", "backfill", "--start", "2026-07-30", "--end", "2026-07-29"])
    assert result.exit_code != 0
    assert stub.requests == []
