"""Tests for ``DataIngestor.ingest_institutional_snapshots`` (``data.ingest``).

Mirrors ``tests/test_ingest_analyst_snapshots.py`` exactly, one table over: a
minimal fake provider duck-typing only the one capability under test, driven
directly against ``DataIngestor`` -- no real TipRanks/HTTP involved.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import InstitutionalSnapshotRow, Security
from claudetrade.domain import (
    HedgeFundHoldingQuarter,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)

SESSION = dt.date(2026, 7, 30)


def _snapshot(symbol: str, *, net_flow: float = 500_000.0) -> InstitutionalSnapshot:
    return InstitutionalSnapshot(
        symbol=symbol,
        as_of_session=SESSION,
        insider_monthly=[InsiderTransactionMonth(month=SESSION.month, year=SESSION.year)],
        insider_net_3m_usd=net_flow,
        insider_net_3m_usd_vendor=net_flow,
        insider_confidence_stock_score=0.7,
        num_of_insiders=10,
        hedge_fund_sentiment=0.6,
        hedge_fund_holdings_by_quarter=[
            HedgeFundHoldingQuarter(date=SESSION, holding_amount=1_000_000)
        ],
        market_cap_usd=1_000_000_000.0,
        fetched_at=dt.datetime.combine(SESSION, dt.time(20, 0), tzinfo=dt.UTC),
    )


class _InstitutionalProvider:
    """Duck-types the one TipRanks-specific capability under test."""

    name = "tipranks"

    def __init__(
        self, snapshots: dict[str, InstitutionalSnapshot] | None = None, *, raises: bool = False
    ):
        self._snapshots = snapshots or {}
        self._raises = raises
        self.calls: list[tuple[list[str], dt.date | None]] = []

    def get_institutional_snapshots(
        self, symbols: list[str], *, as_of_session: dt.date | None = None
    ) -> dict[str, InstitutionalSnapshot]:
        self.calls.append((list(symbols), as_of_session))
        if self._raises:
            raise RuntimeError("boom")
        return {s: self._snapshots[s] for s in symbols if s in self._snapshots}


class _NoInstitutionalProvider:
    """A provider (e.g. synthetic) that does not implement this capability
    at all -- the default shape for every non-TipRanks adapter."""

    name = "synthetic"


def _ingestor(config: AppConfig, db, *, earnings=None, market=None) -> DataIngestor:
    return DataIngestor(config, db, market_provider=market, earnings_provider=earnings)


def _seed_securities(db, symbols: list[str]) -> None:
    with db.session() as session:
        for symbol in symbols:
            session.add(Security(symbol=symbol, name=symbol))


class TestIngestInstitutionalSnapshots:
    def test_stores_a_row_per_known_symbol(self, memory_db):
        _seed_securities(memory_db, ["AAA", "BBB"])
        provider = _InstitutionalProvider(
            {"AAA": _snapshot("AAA"), "BBB": _snapshot("BBB", net_flow=-100.0)}
        )
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA", "BBB"], SESSION, report)

        assert written == 2
        assert report.institutional_snapshots_upserted == 2
        assert not report.provider_failures
        with memory_db.read_session() as session:
            rows = session.query(InstitutionalSnapshotRow).all()
        assert {r.symbol for r in rows} == {"AAA", "BBB"}

    def test_score_is_computed_and_stored_at_ingest_time(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        with memory_db.read_session() as session:
            row = session.query(InstitutionalSnapshotRow).filter_by(symbol="AAA").one()
        assert row.score is not None
        assert row.insider_subscore is not None

    def test_symbol_not_in_tracked_universe_is_skipped(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA"), "ZZZ": _snapshot("ZZZ")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA", "ZZZ"], SESSION, report)

        assert written == 1
        with memory_db.read_session() as session:
            symbols = {r.symbol for r in session.query(InstitutionalSnapshotRow).all()}
        assert symbols == {"AAA"}

    def test_re_ingesting_the_same_session_replaces_not_duplicates(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA", net_flow=500_000.0)})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()
        ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        provider._snapshots["AAA"] = _snapshot("AAA", net_flow=-999_000.0)
        ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        with memory_db.read_session() as session:
            rows = session.query(InstitutionalSnapshotRow).filter_by(symbol="AAA").all()
        assert len(rows) == 1
        assert rows[0].insider_net_3m_usd == -999_000.0

    def test_no_snapshots_returned_is_an_honest_zero(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({})  # e.g. no institutional content for anything
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert report.institutional_snapshots_upserted == 0
        assert not report.provider_failures

    def test_provider_exception_degrades_without_raising(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider(raises=True)
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert "tipranks_institutional" in report.provider_failures

    def test_no_configured_provider_is_a_silent_zero(self, memory_db):
        ingestor = _ingestor(AppConfig(), memory_db, earnings=None, market=None)
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert not report.provider_failures

    def test_provider_without_the_capability_is_a_silent_zero(self, memory_db):
        """A non-TipRanks earnings provider (e.g. synthetic) has no
        ``get_institutional_snapshots`` at all -- must degrade quietly, not
        raise an ``AttributeError``."""
        _seed_securities(memory_db, ["AAA"])
        ingestor = _ingestor(AppConfig(), memory_db, earnings=_NoInstitutionalProvider())
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert written == 0
        assert not report.provider_failures

    def test_session_date_is_threaded_through_to_the_provider_call(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert provider.calls == [(["AAA"], SESSION)]

    def test_falls_back_to_the_market_provider_when_earnings_is_not_tipranks(self, memory_db):
        """``self.market``/``self.earnings`` are two SEPARATE ``TipRanksProvider``
        instances in a real deployment; this method must find the capability
        through whichever slot actually carries it."""
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(
            AppConfig(), memory_db, earnings=_NoInstitutionalProvider(), market=provider
        )
        report = IngestReport()

        written = ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert written == 1
        assert provider.calls == [(["AAA"], SESSION)]

    def test_summary_reports_the_aggregate_counter(self, memory_db):
        _seed_securities(memory_db, ["AAA"])
        provider = _InstitutionalProvider({"AAA": _snapshot("AAA")})
        ingestor = _ingestor(AppConfig(), memory_db, earnings=provider)
        report = IngestReport()

        ingestor.ingest_institutional_snapshots(["AAA"], SESSION, report)

        assert report.summary()["institutional_snapshots"] == 1
