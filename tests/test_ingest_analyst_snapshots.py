"""Tests for ``DataIngestor.ingest_analyst_snapshots`` (``data.ingest``).

Mirrors ``tests/test_ingest_market_cap.py``'s shape: a minimal fake provider
duck-typing only the one capability under test, driven directly against
``DataIngestor`` -- no real TipRanks/HTTP involved (that boundary is covered
by ``tests/test_tipranks_provider.py`` and
``tests/test_tipranks_analyst_parsing.py``).
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import AnalystSnapshotRow, Security
from claudetrade.domain import AnalystSnapshot

SESSION = dt.date(2026, 7, 30)


def _snapshot(symbol: str, *, buy: int = 5, hold: int = 10, sell: int = 1) -> AnalystSnapshot:
    return AnalystSnapshot(
        symbol=symbol,
        as_of_session=SESSION,
        consensus_rating=3,
        buy_count=buy,
        hold_count=hold,
        sell_count=sell,
        analyst_count=buy + hold + sell,
        price_target_mean=100.0,
        price_target_currency="USD",
        fetched_at=dt.datetime.combine(SESSION, dt.time(20, 0), tzinfo=dt.UTC),
    )


class _AnalystProvider:
    """Duck-types the one TipRanks-specific capability under test."""

    name = "tipranks"

    def __init__(self, snapshots: dict[str, AnalystSnapshot] | None = None, *, raises: bool = False):
        self._snapshots = snapshots or {}
        self._raises = raises
        self.calls: list[tuple[list[str], dt.date | None]] = []

    def get_analyst_snapshots(
        self, symbols: list[str], *, as_of_session: dt.date | None = None
    ) -> dict[str, AnalystSnapshot]:
        self.calls.append((list(symbols), as_of_session))
        if self._raises:
            raise RuntimeError("boom")
        return {s: self._snapshots[s] for s in symbols if s in self._snapshots}


class _NoAnalystProvider:
    """A provider (e.g. synthetic) that does not implement this capability
    at all -- the default shape for every non-TipRanks adapter."""

    name = "synthetic"


def _ingestor(config: AppConfig, db, *, earnings=None, market=None) -> DataIngestor:
    return DataIngestor(config, db, market_provider=market, earnings_provider=earnings)


def _seed_securities(db, symbols: list[str]) -> None:
    with db.session() as session:
        for symbol in symbols:
            session.add(Security(symbol=symbol, name=symbol))


class TestIngestAnalystSnapshots:
    def test_stores_a_row_per_known_symbol(self, memory_db):
        _seed_securities(memory_db, ["AAA", "BBB"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA"), "BBB": _snapshot("BBB", buy=1)})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA", "BBB"], SESSION, report)

        assert written == 2
        assert report.analyst_snapshots_upserted == 2
        assert not report.provider_failures
        with memory_db.read_session() as session:
            rows = session.query(AnalystSnapshotRow).all()
        assert {r.symbol for r in rows} == {"AAA", "BBB"}

    def test_symbol_not_in_tracked_universe_is_skipped(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA"), "ZZZ": _snapshot("ZZZ")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA", "ZZZ"], SESSION, report)

        assert written == 1
        with memory_db.read_session() as session:
            symbols = {r.symbol for r in session.query(AnalystSnapshotRow).all()}
        assert symbols == {"AAA"}

    def test_re_ingesting_the_same_session_replaces_not_duplicates(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA", buy=5, hold=10, sell=1)})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()
        ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        provider._snapshots["AAA"] = _snapshot("AAA", buy=7, hold=23, sell=2)
        ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        with memory_db.read_session() as session:
            rows = session.query(AnalystSnapshotRow).filter_by(symbol="AAA").all()
        assert len(rows) == 1
        assert (rows[0].buy_count, rows[0].hold_count, rows[0].sell_count) == (7, 23, 2)

    def test_no_snapshots_returned_is_an_honest_zero(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({})  # e.g. no analyst coverage for anything requested
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert report.analyst_snapshots_upserted == 0
        assert not report.provider_failures

    def test_provider_exception_degrades_without_raising(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider(raises=True)
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert "tipranks_analyst" in report.provider_failures

    def test_no_configured_provider_is_a_silent_zero(self, memory_db):
        ingestor = _ingestor(AppConfig(), memory_db, earnings=None, market=None)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert not report.provider_failures

    def test_provider_without_the_capability_is_a_silent_zero(self, memory_db):
        """A non-TipRanks earnings provider (e.g. synthetic) has no
        ``get_analyst_snapshots`` at all -- must degrade quietly, not raise
        an ``AttributeError``."""
        _seed_securities(memory_db, ["AAA"])
        ingestor = _ingestor(AppConfig(), memory_db, earnings=_NoAnalystProvider())
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert not report.provider_failures

    def test_session_date_is_threaded_through_to_the_provider_call(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert provider.calls == [(["AAA"], SESSION)]

    def test_falls_back_to_the_market_provider_when_earnings_is_not_tipranks(self, memory_db):
        """``self.market``/``self.earnings`` are two SEPARATE ``TipRanksProvider``
        instances in a real deployment (``providers.registry``); this method
        must find the capability through whichever slot actually carries
        it, not assume it is always ``self.earnings``."""
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=_NoAnalystProvider(), market=provider)
        report = IngestReport()

        written = ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert written == 1
        assert provider.calls == [(["AAA"], SESSION)]

    def test_summary_reports_the_aggregate_counter(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _AnalystProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        ingestor.ingest_analyst_snapshots(["AAA"], SESSION, report)

        assert report.summary()["analyst_snapshots"] == 1
