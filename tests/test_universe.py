"""Universe loading: packaged seed universes and first-run seeding.

Covers what the out-of-box "very limited stock coverage" feedback asked for:
packaged US + Canadian seed universes that (a) actually parse and are
internally sane, and (b) are what a first real-data run sees before any
database of stored securities exists.
"""

from __future__ import annotations

import csv
import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.data.universe import (
    PACKAGED_UNIVERSE_DIR,
    PACKAGED_UNIVERSE_FILES,
    UniverseSelector,
    load_packaged_universe,
)
from claudetrade.db.models import Security
from claudetrade.domain import SecurityInfo

VALID_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "TSX", "TSXV"}


# --------------------------------------------------------------------------
# Packaged CSV sanity
# --------------------------------------------------------------------------


class TestPackagedUniverseFiles:
    def test_packaged_files_exist(self):
        for name, filename in PACKAGED_UNIVERSE_FILES.items():
            path = PACKAGED_UNIVERSE_DIR / filename
            assert path.exists(), f"packaged universe {name!r} missing at {path}"

    def test_us_default_parses_with_sane_row_count(self):
        """Roughly the S&P 500 plus liquid mid-caps -- the task calls ~500-700 fine."""
        securities = load_packaged_universe(["us_default"])
        assert 400 <= len(securities) <= 700, len(securities)

    def test_ca_default_parses_with_sane_row_count(self):
        """TSX 60 plus other liquid TSX names -- ~100-200 rows."""
        securities = load_packaged_universe(["ca_default"])
        assert 80 <= len(securities) <= 200, len(securities)

    def test_no_duplicate_symbols_within_us_default(self):
        securities = load_packaged_universe(["us_default"])
        symbols = [s.symbol for s in securities]
        assert len(symbols) == len(set(symbols)), "duplicate symbol(s) in us_default.csv"

    def test_no_duplicate_symbols_within_ca_default(self):
        securities = load_packaged_universe(["ca_default"])
        symbols = [s.symbol for s in securities]
        assert len(symbols) == len(set(symbols)), "duplicate symbol(s) in ca_default.csv"

    def test_no_symbol_collision_across_packaged_files(self):
        """The combined loader de-duplicates by first-seen symbol; assert that
        never silently drops a *different* real company because both files
        happen to use the same bare ticker."""
        us = {s.symbol for s in load_packaged_universe(["us_default"])}
        ca = {s.symbol for s in load_packaged_universe(["ca_default"])}
        assert not (us & ca), f"symbol(s) present in both packaged files: {sorted(us & ca)}"

    def test_all_exchanges_recognised(self):
        for name in PACKAGED_UNIVERSE_FILES:
            for security in load_packaged_universe([name]):
                assert security.exchange in VALID_EXCHANGES, (
                    f"{name}: {security.symbol} has unrecognised exchange {security.exchange!r}"
                )

    def test_ca_default_is_all_canadian_exchanges(self):
        for security in load_packaged_universe(["ca_default"]):
            assert security.exchange in {"TSX", "TSXV"}, security.symbol

    def test_us_default_is_all_us_exchanges(self):
        for security in load_packaged_universe(["us_default"]):
            assert security.exchange in {"NASDAQ", "NYSE", "AMEX"}, security.symbol

    def test_every_row_has_symbol_and_name(self):
        for name in PACKAGED_UNIVERSE_FILES:
            for security in load_packaged_universe([name]):
                assert security.symbol, f"{name}: row with no symbol"
                assert security.name, f"{name}: {security.symbol} has no name"

    def test_combined_default_includes_both_markets(self):
        combined = load_packaged_universe()  # default: us_default + ca_default
        exchanges = {s.exchange for s in combined}
        assert "NASDAQ" in exchanges or "NYSE" in exchanges
        assert "TSX" in exchanges

    def test_unknown_packaged_name_is_skipped_not_raised(self):
        """A typo in config degrades to fewer symbols, not a hard failure."""
        securities = load_packaged_universe(["no_such_universe"])
        assert securities == []

    def test_header_comment_lines_do_not_leak_into_data(self):
        """The leading '#'-prefixed banner must not be mistaken for a data row
        or corrupt the real header parsing."""
        for filename in PACKAGED_UNIVERSE_FILES.values():
            path = PACKAGED_UNIVERSE_DIR / filename
            with path.open(encoding="utf-8") as fh:
                first_lines = [next(fh) for _ in range(6)]
            assert all(line.startswith("#") for line in first_lines), (
                "expected a comment banner before the CSV header"
            )
            with path.open(newline="", encoding="utf-8") as fh:
                lines = [line for line in fh if not line.lstrip().startswith("#")]
            reader = csv.DictReader(lines)
            assert reader.fieldnames == [
                "symbol", "name", "exchange", "sector", "market_cap_bucket", "country",
            ]

    def test_market_cap_bucket_maps_to_a_market_cap_estimate(self):
        """Bucket labels feed FilterConfig.min_market_cap_usd; confirm the mapping
        actually produces a usable (non-None) figure for a bucketed row."""
        securities = load_packaged_universe(["us_default"])
        apple = next(s for s in securities if s.symbol == "AAPL")
        assert apple.market_cap_usd is not None
        assert apple.market_cap_usd > 1e9


# --------------------------------------------------------------------------
# UniverseSelector: first-run seeding + merge with stored securities
# --------------------------------------------------------------------------


class TestFirstRunSeeding:
    def test_empty_database_seeds_from_packaged_universe(self, memory_db):
        """Before any refresh, an empty securities table must not mean an empty
        scannable universe -- that was the reported bug."""
        config = AppConfig()
        selector = UniverseSelector(config, memory_db)
        securities = selector.load_all()

        assert len(securities) > 500  # us_default + ca_default combined
        symbols = {s.symbol for s in securities}
        assert "AAPL" in symbols
        assert "SHOP" in symbols  # Canadian (TSX) name

    def test_packaged_universes_disabled_by_empty_list(self, memory_db):
        """Setting packaged_universes = [] must restore a genuinely empty universe."""
        config = AppConfig()
        config.universe.packaged_universes = []
        selector = UniverseSelector(config, memory_db)
        assert selector.load_all() == []

    def test_stored_securities_take_precedence_over_packaged(self, memory_db):
        """A DB-stored AAPL (e.g. with real market_cap_usd from a refresh) must
        not be shadowed by the packaged stub."""
        with memory_db.session() as session:
            session.add(
                Security(symbol="AAPL", name="Apple Inc. (from refresh)", exchange="NASDAQ",
                          market_cap_usd=1.0)
            )

        config = AppConfig()
        selector = UniverseSelector(config, memory_db)
        securities = selector.load_all()
        by_symbol = {s.symbol: s for s in securities}

        assert by_symbol["AAPL"].name == "Apple Inc. (from refresh)"
        assert by_symbol["AAPL"].market_cap_usd == 1.0

    def test_packaged_symbols_not_yet_stored_are_still_merged_in(self, memory_db):
        """After a partial refresh (one symbol stored), the rest of the packaged
        seed must still be visible -- not narrowed down to only what's stored."""
        with memory_db.session() as session:
            session.add(Security(symbol="AAPL", name="Apple Inc.", exchange="NASDAQ"))

        config = AppConfig()
        selector = UniverseSelector(config, memory_db)
        symbols = {s.symbol for s in selector.load_all()}

        assert "AAPL" in symbols
        assert "MSFT" in symbols  # from the packaged seed, never stored
        assert len(symbols) > 500

    def test_no_db_handle_falls_back_to_packaged_universe(self):
        """Without a database at all, the packaged seed is still usable rather
        than an unconditional empty list."""
        config = AppConfig()
        selector = UniverseSelector(config, db=None)
        securities = selector.load_all()
        assert len(securities) > 500


class TestUniverseConfigDefaults:
    def test_packaged_universes_default(self):
        assert AppConfig().universe.packaged_universes == ["us_default", "ca_default"]

    def test_permitted_exchanges_include_tsx_and_tsxv(self):
        exchanges = AppConfig().universe.permitted_exchanges
        assert "TSX" in exchanges
        assert "TSXV" in exchanges
        # US exchanges must still be permitted -- this is additive, not a swap.
        assert "NASDAQ" in exchanges
        assert "NYSE" in exchanges

    def test_for_session_permits_tsx_by_default(self, memory_db):
        """A TSX security must clear the exchange-permission gate with default config."""
        config = AppConfig()
        selector = UniverseSelector(config, memory_db)
        shop = SecurityInfo(symbol="SHOP", name="Shopify Inc.", exchange="TSX",
                             market_cap_usd=50e9)
        report = selector.for_session(dt.date(2024, 6, 3), securities=[shop])
        assert "SHOP" in report.symbols
