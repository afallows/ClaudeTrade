"""Tests for the ADR-0008 Decision 3 market-cap acquisition/storage path:

``DataIngestor.enrich_market_caps`` establishes a real market cap for each
security via the market-data path at refresh time, stores it, and flags (but
never silently drops) a symbol whose cap could not be established by any
configured provider.
"""

from __future__ import annotations

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import Security
from claudetrade.domain import SecurityInfo
from claudetrade.providers.base import ProviderError


class _CapProvider:
    """Minimal fake MarketDataProvider exposing only get_market_caps."""

    name = "fake_cap_provider"

    def __init__(self, caps: dict[str, float] | None = None, *, raises: bool = False):
        self._caps = caps or {}
        self._raises = raises

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        if self._raises:
            raise ProviderError("boom", provider=self.name, retryable=True)
        return {s: self._caps[s] for s in symbols if s in self._caps}


class _NoCapProvider:
    """A provider that does not support get_market_caps at all (the default
    protocol behaviour -- not every real adapter overrides it)."""

    name = "no_cap_provider"

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        return {}


class _FallbackLike:
    """Duck-types providers.registry.FallbackMarketProvider's public shape
    (``.primary`` / ``.fallbacks``) without importing it, exactly as
    ``DataIngestor._market_cap_sources`` is documented to rely on."""

    name = "fallback"

    def __init__(self, primary, fallbacks):
        self.primary = primary
        self.fallbacks = fallbacks

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        # The real FallbackMarketProvider does not implement this itself --
        # calling into it directly must not be assumed; the ingestor reaches
        # through to .primary/.fallbacks instead. Returning {} here proves
        # that the enrichment result is NOT coming from this method.
        return {}


def _ingestor(config: AppConfig, db, market) -> DataIngestor:
    return DataIngestor(config, db, market_provider=market)


class TestEnrichMarketCaps:
    def test_resolves_cap_from_a_supporting_provider(self, memory_db):
        config = AppConfig()
        provider = _CapProvider({"AAPL": 2_800_000_000_000.0})
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        securities = [SecurityInfo(symbol="AAPL", name="Apple Inc.")]
        enriched = ingestor.enrich_market_caps(securities, report)

        assert enriched[0].market_cap_usd == 2_800_000_000_000.0
        assert not report.quality.issues

    def test_unresolved_symbol_is_flagged_not_dropped(self, memory_db):
        """The core data-quality rule: an unresolved cap is NEVER silently
        excluded here -- it stays in the returned list and is flagged."""
        config = AppConfig()
        provider = _CapProvider({})  # resolves nothing
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        securities = [SecurityInfo(symbol="TINY", name="Tiny Corp")]
        enriched = ingestor.enrich_market_caps(securities, report)

        assert len(enriched) == 1
        assert enriched[0].symbol == "TINY"
        assert enriched[0].market_cap_usd is None
        categories = {i.category for i in report.quality.issues}
        assert "unknown_market_cap" in categories
        assert report.quality.messages_for("TINY")

    def test_provider_with_no_cap_capability_is_a_silent_no_op(self, memory_db):
        """A provider that never overrides get_market_caps (the default:
        synthetic/csv/stooq) must degrade to 'unresolved and flagged', not
        an exception -- calling get_market_caps on it is always safe."""
        config = AppConfig()
        provider = _NoCapProvider()
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        securities = [SecurityInfo(symbol="AAPL", name="Apple Inc.")]
        enriched = ingestor.enrich_market_caps(securities, report)

        assert enriched[0].market_cap_usd is None
        assert any(i.category == "unknown_market_cap" for i in report.quality.issues)

    def test_provider_error_degrades_rather_than_raises(self, memory_db):
        config = AppConfig()
        provider = _CapProvider(raises=True)
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        securities = [SecurityInfo(symbol="AAPL", name="Apple Inc.")]
        enriched = ingestor.enrich_market_caps(securities, report)  # must not raise

        assert enriched[0].market_cap_usd is None
        assert any(i.category == "unknown_market_cap" for i in report.quality.issues)

    def test_existing_cap_is_not_cleared_when_provider_resolves_nothing_new(self, memory_db):
        """A security that already carries a market cap (e.g. from the
        provider's own get_security_info/list_universe) must not be wiped to
        None just because get_market_caps itself priced nothing new for it."""
        config = AppConfig()
        provider = _CapProvider({})
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        securities = [SecurityInfo(symbol="AAPL", name="Apple Inc.", market_cap_usd=3.0e12)]
        enriched = ingestor.enrich_market_caps(securities, report)

        assert enriched[0].market_cap_usd == 3.0e12
        assert not report.quality.issues, "an already-known cap is not 'unknown'"

    def test_reaches_through_fallback_shaped_wrapper_to_primary_and_fallbacks(self, memory_db):
        """DataIngestor._market_cap_sources must duck-type through a
        FallbackMarketProvider-shaped wrapper's .primary/.fallbacks -- the
        real FallbackMarketProvider (providers.registry) does not implement
        get_market_caps itself, so this is the only way cap enrichment
        reaches a capable adapter (e.g. yahoo) configured as a fallback."""
        config = AppConfig()
        primary = _NoCapProvider()
        yahoo_like = _CapProvider({"INTC": 413_002_720_000.0})
        wrapper = _FallbackLike(primary=primary, fallbacks=[yahoo_like])
        ingestor = _ingestor(config, memory_db, wrapper)
        report = IngestReport()

        securities = [SecurityInfo(symbol="INTC", name="Intel Corporation")]
        enriched = ingestor.enrich_market_caps(securities, report)

        assert enriched[0].market_cap_usd == 413_002_720_000.0
        assert not report.quality.issues

    def test_first_resolving_provider_wins(self, memory_db):
        """Matches the existing get_daily_bars/get_security_info fallback
        convention: the earliest source in the chain to price a symbol wins."""
        config = AppConfig()
        primary = _CapProvider({"AAPL": 1.0e12})
        secondary = _CapProvider({"AAPL": 999.0})
        wrapper = _FallbackLike(primary=primary, fallbacks=[secondary])
        ingestor = _ingestor(config, memory_db, wrapper)
        report = IngestReport()

        enriched = ingestor.enrich_market_caps([SecurityInfo(symbol="AAPL")], report)
        assert enriched[0].market_cap_usd == 1.0e12

    def test_no_market_provider_configured_flags_every_symbol(self, memory_db):
        config = AppConfig()
        ingestor = _ingestor(config, memory_db, None)
        report = IngestReport()

        enriched = ingestor.enrich_market_caps([SecurityInfo(symbol="AAPL")], report)
        assert enriched[0].market_cap_usd is None
        assert any(i.category == "unknown_market_cap" for i in report.quality.issues)


class TestIngestSecuritiesStoresResolvedCap:
    def test_ingest_securities_persists_the_enriched_cap(self, memory_db):
        config = AppConfig()
        provider = _CapProvider({"AAPL": 2_800_000_000_000.0})
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()

        ingestor.ingest_securities([SecurityInfo(symbol="AAPL", name="Apple Inc.")], report)

        with memory_db.read_session() as session:
            row = session.get(Security, "AAPL")
            assert row is not None
            assert row.market_cap_usd == 2_800_000_000_000.0


class TestHistoricalRequestMarketCapFloor:
    def test_current_cap_filters_history_request_universe(self, memory_db):
        config = AppConfig()
        provider = _CapProvider({"BIG": 2_000_000_000.0, "SMALL": 400_000_000.0})
        ingestor = _ingestor(config, memory_db, provider)
        report = IngestReport()
        enriched = ingestor.ingest_securities(
            [SecurityInfo(symbol="BIG"), SecurityInfo(symbol="SMALL")], report
        )

        assert ingestor.symbols_passing_market_cap_floor(
            ["BIG", "SMALL", config.market_data.benchmark_symbol], enriched
        ) == ["BIG", config.market_data.benchmark_symbol]

    def test_strict_unknown_policy_does_not_request_unresolved_symbol(self, memory_db):
        config = AppConfig()
        config.universe.unknown_cap_policy = "exclude"
        ingestor = _ingestor(config, memory_db, _CapProvider({}))

        assert ingestor.symbols_passing_market_cap_floor(
            ["UNKNOWN"], [SecurityInfo(symbol="UNKNOWN")]
        ) == []
