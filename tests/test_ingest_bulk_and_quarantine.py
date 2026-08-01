"""``ingest_prices``: bulk-provider window narrowing and the per-symbol fetch
quarantine (QA handoff v3 F23 items 3 and 4).

Two independent refresh-speed fixes share this module because they share one
call site:

* **Window narrowing.** When the configured PRIMARY provider fetches by DATE
  (``PolygonProvider.bulk_daily``), re-requesting sessions the database
  already stores re-downloads the entire market for each of them. The window
  narrows to what is actually missing, so a normal daily refresh is one
  grouped call. Every other configuration keeps the full window.
* **Fetch quarantine.** A symbol whose full provider chain yields nothing for
  three refreshes running is skipped for a week, instead of burning a
  dataForTicker probe plus a Yahoo chart call on every refresh forever (the
  owner's "many symbol failures" retry burn).

Both are deliberately conservative, and the tests below pin the *negative*
cases (no narrowing without a configured bulk primary; no quarantine from a
one-off outage) as hard as the positive ones.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.ingest import (
    _QUARANTINE_AFTER_FAILURES,
    _QUARANTINE_DAYS,
    DataIngestor,
    IngestReport,
)
from claudetrade.db.models import PriceBar, SymbolFetchHealth
from claudetrade.domain import Bar
from claudetrade.providers.base import ProviderError, ProviderStatus

START = dt.date(2026, 5, 1)
END = dt.date(2026, 7, 30)


def _config() -> AppConfig:
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    return config


class _RecordingProvider:
    """Bars provider that records the exact (symbols, start, end) of every
    call and serves one bar per requested symbol dated at ``start``."""

    name = "recording"

    def __init__(self, *, bulk_daily=False, configured=True, serves=None):
        self.calls: list[tuple[list[str], dt.date, dt.date]] = []
        if bulk_daily:
            self.bulk_daily = True
        self._configured = configured
        #: When given, only these symbols get bars; everything else is empty.
        self._serves = serves

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, kind="market", available=self._configured,
            configured=self._configured,
        )

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        self.calls.append((list(symbols), start, end))
        out = {}
        for symbol in symbols:
            if self._serves is not None and symbol not in self._serves:
                out[symbol] = []
                continue
            out[symbol] = [
                Bar(
                    symbol=symbol, session=start, open=100.0, high=101.0, low=99.0,
                    close=100.5, volume=1_000_000, adj_close=100.5, source=self.name,
                )
            ]
        return out


class _Cascade:
    """Minimal ``FallbackMarketProvider``-shaped wrapper (``.primary`` /
    ``.fallbacks``) -- the duck-typed shape ``DataIngestor`` already reaches
    through, matching ``tests/test_ingest_prices.py``'s ``_CascadeLike``."""

    name = "fallback"

    def __init__(self, primary, fallbacks=()):
        self.primary = primary
        self.fallbacks = list(fallbacks)

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        out: dict[str, list[Bar]] = {}
        unfilled = set(symbols)
        for provider in [self.primary, *self.fallbacks]:
            if not unfilled:
                break
            result = provider.get_daily_bars(list(unfilled), start, end, adjusted=adjusted)
            for symbol, bars in result.items():
                if bars and symbol not in out:
                    out[symbol] = bars
                    unfilled.discard(symbol)
        for symbol in symbols:
            out.setdefault(symbol, [])
        return out


def _store_bar(db, symbol: str, session_date: dt.date, *, source: str = "seed") -> None:
    with db.session() as session:
        session.add(
            PriceBar(
                symbol=symbol, session=session_date, open=1.0, high=1.0, low=1.0,
                close=1.0, adj_close=1.0, volume=1.0, source=source,
            )
        )


# --------------------------------------------------------------------------
# window narrowing (F23 item 3)
# --------------------------------------------------------------------------


def test_bulk_primary_narrows_the_window_to_missing_sessions(memory_db, caplog):
    """The headline fix: with a per-date primary and bars already stored
    through 2026-07-28, a 3-month refresh window collapses to the sessions
    the database is actually missing."""
    provider = _RecordingProvider(bulk_daily=True)
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))
    _store_bar(memory_db, "AAPL", dt.date(2026, 7, 28))

    with caplog.at_level(logging.INFO, logger="claudetrade.data.ingest"):
        ingestor.ingest_prices(["AAPL"], START, END, IngestReport())

    starts = {start for _symbols, start, _end in provider.calls}
    # The latest stored session itself is re-fetched (a provisional
    # current-session bar merged by an earlier run today must get repaired),
    # but nothing older than it.
    assert starts == {dt.date(2026, 7, 28)}
    assert all(end == END for _symbols, _start, end in provider.calls)
    assert any("narrowed the price fetch window" in r.getMessage() for r in caplog.records)


def test_no_narrowing_without_a_bulk_provider(memory_db):
    """A per-symbol provider pays per SYMBOL, not per date -- narrowing would
    only shrink coverage repair without saving a single call."""
    provider = _RecordingProvider(bulk_daily=False)
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))
    _store_bar(memory_db, "AAPL", dt.date(2026, 7, 28))

    ingestor.ingest_prices(["AAPL"], START, END, IngestReport())

    assert all(start == START for _symbols, start, _end in provider.calls)


def test_no_narrowing_when_the_database_has_no_bars(memory_db):
    """A first-ever refresh must fetch the whole requested window -- this is
    exactly the empty-database case F23 exists to fill."""
    provider = _RecordingProvider(bulk_daily=True)
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    ingestor.ingest_prices(["AAPL"], START, END, IngestReport())

    assert all(start == START for _symbols, start, _end in provider.calls)


def test_no_narrowing_when_the_bulk_primary_is_unconfigured(memory_db):
    """An unconfigured bulk primary means the per-symbol fallbacks are doing
    the work; narrowing their window would shrink coverage for nothing."""
    primary = _RecordingProvider(bulk_daily=True, configured=False, serves=set())
    fallback = _RecordingProvider(bulk_daily=False)
    fallback.name = "fallback_provider"
    ingestor = DataIngestor(
        _config(), memory_db, market_provider=_Cascade(primary, [fallback])
    )
    _store_bar(memory_db, "AAPL", dt.date(2026, 7, 28))

    ingestor.ingest_prices(["AAPL"], START, END, IngestReport())

    assert all(start == START for _symbols, start, _end in fallback.calls)


def test_no_narrowing_for_an_explicitly_historical_window(memory_db):
    """A window ENDING before the latest stored session is an explicit
    historical request: the operator wants those exact dates, and "the DB has
    newer bars" says nothing about whether it has THOSE."""
    provider = _RecordingProvider(bulk_daily=True)
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))
    _store_bar(memory_db, "AAPL", dt.date(2026, 7, 28))

    ingestor.ingest_prices(["AAPL"], dt.date(2024, 1, 2), dt.date(2024, 3, 1), IngestReport())

    assert all(start == dt.date(2024, 1, 2) for _symbols, start, _end in provider.calls)


def _missing_bars_finding(report: IngestReport, symbol: str):
    return next(
        (
            i
            for i in report.quality.issues
            if i.category == "missing_bars" and i.symbol == symbol
        ),
        None,
    )


def test_narrowing_does_not_flag_unfetched_sessions_as_missing_bars(memory_db):
    """The quality check must use the NARROWED window: flagging the
    deliberately-unfetched older sessions would report the optimisation
    itself as a data defect.

    The unnarrowed control below is what keeps this from being vacuous --
    the same provider, same stored bar, no ``bulk_daily`` flag, does report
    the whole May-July window as missing.
    """
    narrowed_report = IngestReport()
    bulk = _RecordingProvider(bulk_daily=True)
    _store_bar(memory_db, "AAPL", dt.date(2026, 7, 28))
    DataIngestor(_config(), memory_db, market_provider=_Cascade(bulk)).ingest_prices(
        ["AAPL"], START, END, narrowed_report
    )

    control_report = IngestReport()
    per_symbol = _RecordingProvider(bulk_daily=False)
    DataIngestor(_config(), memory_db, market_provider=_Cascade(per_symbol)).ingest_prices(
        ["AAPL"], START, END, control_report
    )

    control = _missing_bars_finding(control_report, "AAPL")
    assert control is not None, "the control must genuinely flag the full window"
    assert control.detail["first_missing"] == "2026-05-04"  # first session after START

    narrowed = _missing_bars_finding(narrowed_report, "AAPL")
    # Either nothing is flagged, or only sessions inside the narrowed window.
    if narrowed is not None:
        assert narrowed.detail["first_missing"] >= "2026-07-28"


# --------------------------------------------------------------------------
# fetch quarantine (F23 item 4)
# --------------------------------------------------------------------------


def _health(db, symbol: str) -> SymbolFetchHealth | None:
    with db.read_session() as session:
        return session.execute(
            select(SymbolFetchHealth).where(SymbolFetchHealth.symbol == symbol)
        ).scalar_one_or_none()


def test_repeated_full_chain_failures_quarantine_the_symbol(memory_db, caplog):
    """Three consecutive refreshes with no bars from ANY provider quarantine
    the symbol; the fourth refresh does not request it at all."""
    provider = _RecordingProvider(serves={"AAPL", "SPY"})  # DEAD gets nothing
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    for expected in range(1, _QUARANTINE_AFTER_FAILURES + 1):
        ingestor.ingest_prices(["AAPL", "DEAD"], START, END, IngestReport())
        row = _health(memory_db, "DEAD")
        assert row is not None
        assert row.consecutive_failures == expected

    row = _health(memory_db, "DEAD")
    assert row.quarantined_until is not None
    assert row.last_failure_at is not None
    assert row.last_error

    provider.calls.clear()
    with caplog.at_level(logging.INFO, logger="claudetrade.data.ingest"):
        ingestor.ingest_prices(["AAPL", "DEAD"], START, END, IngestReport())

    requested = {s for symbols, _start, _end in provider.calls for s in symbols}
    assert "DEAD" not in requested
    assert "AAPL" in requested  # healthy symbols are unaffected
    assert any(
        "skipped 1 quarantined symbol" in r.getMessage() for r in caplog.records
    )


def test_quarantine_is_recorded_as_a_visible_quality_finding(memory_db):
    provider = _RecordingProvider(serves={"AAPL", "SPY"})
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    report = None
    for _ in range(_QUARANTINE_AFTER_FAILURES):
        report = IngestReport()
        ingestor.ingest_prices(["AAPL", "DEAD"], START, END, report)

    findings = [i for i in report.quality.issues if i.category == "symbol_quarantined"]
    assert findings, "quarantining must never be silent"
    assert "DEAD" in findings[0].message
    assert str(_QUARANTINE_DAYS) in findings[0].message
    assert "fetch-health" in findings[0].message


def test_one_success_clears_the_failure_record(memory_db):
    """Success deletes the row outright -- the table only ever holds
    currently-failing names, which is what keeps it (and the CLI listing)
    small."""
    provider = _RecordingProvider(serves={"AAPL", "SPY"})
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    ingestor.ingest_prices(["AAPL", "FLAKY"], START, END, IngestReport())
    assert _health(memory_db, "FLAKY") is not None

    provider._serves = {"AAPL", "SPY", "FLAKY"}
    ingestor.ingest_prices(["AAPL", "FLAKY"], START, END, IngestReport())
    assert _health(memory_db, "FLAKY") is None


def test_a_chunk_wide_provider_error_never_counts_against_symbols(memory_db):
    """An outage (one ProviderError covering the whole chunk) is not a
    per-symbol failure -- otherwise three bad refreshes would quarantine the
    entire universe."""

    class _Outage:
        name = "outage"

        def get_daily_bars(self, symbols, start, end, *, adjusted=True):
            raise ProviderError("simulated outage", provider=self.name, retryable=True)

    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(_Outage()))

    for _ in range(_QUARANTINE_AFTER_FAILURES + 2):
        ingestor.ingest_prices(["AAPL", "MSFT"], START, END, IngestReport())

    with memory_db.read_session() as session:
        assert session.execute(select(SymbolFetchHealth)).scalars().all() == []


def test_expired_quarantine_is_retried_automatically(memory_db):
    provider = _RecordingProvider(serves={"AAPL", "SPY"})
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    for _ in range(_QUARANTINE_AFTER_FAILURES):
        ingestor.ingest_prices(["AAPL", "DEAD"], START, END, IngestReport())

    with memory_db.session() as session:
        row = session.execute(
            select(SymbolFetchHealth).where(SymbolFetchHealth.symbol == "DEAD")
        ).scalar_one()
        row.quarantined_until = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1)

    provider.calls.clear()
    ingestor.ingest_prices(["AAPL", "DEAD"], START, END, IngestReport())
    requested = {s for symbols, _start, _end in provider.calls for s in symbols}
    assert "DEAD" in requested


def test_the_benchmark_is_never_quarantined_out_of_a_fetch(memory_db):
    """Regime classification depends on the benchmark, so it always gets its
    attempt no matter what its health row says."""
    provider = _RecordingProvider(serves={"AAPL"})  # SPY never resolves
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))

    for _ in range(_QUARANTINE_AFTER_FAILURES + 1):
        provider.calls.clear()
        ingestor.ingest_prices(["AAPL"], START, END, IngestReport())
        requested = {s for symbols, _start, _end in provider.calls for s in symbols}
        assert "SPY" in requested


def test_quarantined_symbols_are_skipped_by_market_cap_enrichment(memory_db, caplog):
    """The per-symbol dataForTicker fallback inside ``get_market_caps`` is
    exactly the doomed-retry spend the quarantine exists to stop."""
    from claudetrade.domain import SecurityInfo

    class _CapProvider:
        name = "caps"

        def __init__(self):
            self.requested: list[list[str]] = []

        def get_market_caps(self, symbols):
            self.requested.append(sorted(symbols))
            return dict.fromkeys(symbols, 5000000000.0)

    with memory_db.session() as session:
        session.add(
            SymbolFetchHealth(
                symbol="DEAD",
                consecutive_failures=_QUARANTINE_AFTER_FAILURES,
                quarantined_until=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1),
            )
        )

    caps = _CapProvider()
    ingestor = DataIngestor(_config(), memory_db, market_provider=caps)
    report = IngestReport()

    with caplog.at_level(logging.INFO, logger="claudetrade.data.ingest"):
        enriched = ingestor.enrich_market_caps(
            [SecurityInfo(symbol="AAPL"), SecurityInfo(symbol="DEAD")], report
        )

    assert caps.requested == [["AAPL"]]
    assert any("skipped 1 quarantined symbol" in r.getMessage() for r in caplog.records)
    # "we deliberately did not ask" must not masquerade as "no provider could
    # answer" -- the quarantined name is not flagged unknown_market_cap.
    assert not any(
        i.category == "unknown_market_cap" and i.symbol == "DEAD"
        for i in report.quality.issues
    )
    assert {s.symbol for s in enriched} == {"AAPL", "DEAD"}


def test_quarantine_never_touches_stored_history_or_listing_status(memory_db):
    """This gates FETCHING only: stored bars and ``Security.delisted_date``
    are untouched, so a backtest spanning the period still sees the name."""
    from claudetrade.db.models import Security

    with memory_db.session() as session:
        session.add(Security(symbol="DEAD", name="Dead Co"))
    _store_bar(memory_db, "DEAD", dt.date(2026, 6, 1))

    provider = _RecordingProvider(serves={"AAPL", "SPY"})
    ingestor = DataIngestor(_config(), memory_db, market_provider=_Cascade(provider))
    for _ in range(_QUARANTINE_AFTER_FAILURES + 1):
        ingestor.ingest_prices(["AAPL", "DEAD"], START, END, IngestReport())

    with memory_db.read_session() as session:
        assert session.get(Security, "DEAD").delisted_date is None
        rows = session.execute(
            select(PriceBar).where(PriceBar.symbol == "DEAD")
        ).scalars().all()
    assert len(rows) == 1  # stored history intact


def test_quarantine_is_inert_without_a_database(memory_db):
    """The quarantine is an optimisation and must never be able to fail a
    refresh -- an ingestor with no database simply skips it."""
    provider = _RecordingProvider()
    ingestor = DataIngestor(_config(), None, market_provider=_Cascade(provider))
    assert ingestor._quarantined_symbols() == set()
    # And recording an outcome against no database is a no-op, not a crash.
    ingestor._record_fetch_outcomes(
        attempted=["AAPL"], chain_failed=set(), collected={"AAPL": []}, report=IngestReport()
    )
