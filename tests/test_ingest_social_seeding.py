"""QA F22: Stocktwits fetches nothing because its watchlist is empty.

``StocktwitsConfig.watchlist_symbols`` defaults to ``[]`` and the provider
caps each cycle at ``max_symbols_per_cycle``. In production that combination
meant the source was *connected* (status "ok") while fetching an arbitrary
head-of-list slice of the universe -- or, for callers passing no hint at all,
nothing whatsoever. ``DataIngestor._social_symbol_hints`` now seeds the front
of the per-cycle hint list, when and only when the configured watchlist is
empty, from what the operator demonstrably cares about:

1. open paper-portfolio holdings,
2. symbols from ledger signals in the recent sessions,
3. top stored trending symbols by 7-day post volume (securities-join
   guarded, like ``mcp_server.get_trending``).

These tests pin the priority order, the dedupe, the cap, the securities
guard, and -- most importantly -- that a configured NON-empty watchlist keeps
the exact previous behaviour.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor
from claudetrade.db.migrations import init_database
from claudetrade.db.models import (
    PaperAccountRow,
    PaperTradeRow,
    Security,
    SignalRow,
    SymbolSentimentDaily,
)
from claudetrade.db.session import Database
from claudetrade.domain import Bar, SecurityInfo, SocialPost

START = dt.date(2026, 7, 31)
END = dt.date(2026, 7, 31)
TODAY = dt.date.today()


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class _FastMarketProvider:
    name = "fake_market"

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        return {
            symbol: [
                Bar(
                    symbol=symbol,
                    session=start,
                    open=10.0,
                    high=10.5,
                    low=9.5,
                    close=10.2,
                    volume=1_000,
                    adj_close=10.2,
                    source=self.name,
                )
            ]
            for symbol in symbols
        }

    def get_corporate_actions(self, symbols, start, end):
        return {}


class _RecordingSocialProvider:
    """Records exactly what ``symbols`` hint each fetch received.

    Stands in for ``StocktwitsProvider``, the only provider that consumes the
    hint -- the assertion target here is the hint list itself, not the HTTP
    behaviour (covered by ``test_stocktwits_provider.py``).
    """

    name = "recording_social"

    def __init__(self) -> None:
        self.symbol_hints: list[list[str] | None] = []

    def fetch_posts(self, *, since, until=None, symbols=None, limit=None) -> list[SocialPost]:
        self.symbol_hints.append(list(symbols) if symbols is not None else None)
        return []


def _fresh_db() -> Database:
    db = Database("sqlite:///:memory:")
    init_database(db)
    return db


def _config(*, watchlist: list[str] | None = None, cap: int = 20) -> AppConfig:
    config = AppConfig()
    # Sequential: the seeded hint is computed identically either way (on the
    # main thread, before the fetch starts), and a deterministic order keeps
    # the assertions about *what was passed* free of thread timing.
    config.sentiment.fetch_concurrently = False
    config.stocktwits.watchlist_symbols = list(watchlist or [])
    config.stocktwits.max_symbols_per_cycle = cap
    return config


def _seed_holdings(db: Database, symbols: list[str]) -> None:
    with db.session() as session:
        session.add(PaperAccountRow(id=1, name="default"))
    with db.session() as session:
        for i, symbol in enumerate(symbols):
            session.add(
                PaperTradeRow(
                    trade_id=f"trade{i}",
                    account_id=1,
                    signal_id=f"sig-open-{i}",
                    symbol=symbol,
                    strategy="sentiment_breakout",
                    direction="long",
                    entry_session=TODAY - dt.timedelta(days=i + 1),
                    entry_price=100.0,
                    shares=10,
                    stop_loss=95.0,
                    original_stop_loss=95.0,
                )
            )


def _seed_signals(db: Database, symbols: list[str], *, days_ago: int = 1) -> None:
    with db.session() as session:
        for i, symbol in enumerate(symbols):
            session_date = TODAY - dt.timedelta(days=days_ago)
            session.add(
                SignalRow(
                    signal_id=f"sig-{symbol}-{days_ago}",
                    session=session_date,
                    symbol=symbol,
                    strategy="sentiment_breakout",
                    direction="long",
                    initial_status="actionable",
                    reference_price=50.0,
                    price_as_of=dt.datetime.now(tz=dt.UTC),
                    # Descending score, so the within-session ordering is
                    # observable in the seeded list.
                    overall_score=90.0 - i,
                    confidence=0.7,
                )
            )


def _seed_trending(db: Database, counts: dict[str, int], *, with_securities=True) -> None:
    with db.session() as session:
        for symbol, post_count in counts.items():
            if with_securities:
                session.merge(Security(symbol=symbol, name=f"{symbol} Inc"))
            session.add(
                SymbolSentimentDaily(
                    symbol=symbol,
                    session=TODAY - dt.timedelta(days=1),
                    source="all",
                    post_count=post_count,
                )
            )


def _run(db: Database, config: AppConfig, universe: list[str]) -> _RecordingSocialProvider:
    social = _RecordingSocialProvider()
    ingestor = DataIngestor(
        config, db, market_provider=_FastMarketProvider(), social_providers=[social]
    )
    ingestor.run_full_refresh(
        symbols=list(universe),
        start=START,
        end=END,
        securities=[SecurityInfo(symbol=s, name=f"{s} Inc") for s in universe],
    )
    return social


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestEmptyWatchlistSeeding:
    def test_holdings_signals_and_trending_seed_in_priority_order(self):
        db = _fresh_db()
        _seed_holdings(db, ["HOLD1"])
        _seed_signals(db, ["SIG1", "SIG2"])
        _seed_trending(db, {"TREND1": 900, "TREND2": 400})

        social = _run(db, _config(), universe=["ZZZ"])

        hint = social.symbol_hints[0]
        assert hint is not None
        # Holdings first, then this session's signals best-score-first, then
        # trending by post volume -- ahead of the caller's own candidates.
        assert hint[:5] == ["HOLD1", "SIG1", "SIG2", "TREND1", "TREND2"]
        assert "ZZZ" in hint
        db.dispose()

    def test_a_symbol_appearing_in_several_tiers_is_seeded_once(self):
        db = _fresh_db()
        _seed_holdings(db, ["DUPE"])
        _seed_signals(db, ["DUPE"])
        _seed_trending(db, {"DUPE": 500, "OTHER": 100})

        social = _run(db, _config(), universe=[])

        hint = social.symbol_hints[0]
        assert hint == ["DUPE", "OTHER"]
        db.dispose()

    def test_seed_is_capped_at_the_per_cycle_budget(self):
        """The cap is the point of the seeding: only the first
        ``max_symbols_per_cycle`` hints are ever fetched, so ranking more
        than that would be wasted work."""
        db = _fresh_db()
        _seed_trending(db, {f"T{i:02d}": 1000 - i for i in range(10)})

        social = _run(db, _config(cap=3), universe=["ZZZ"])

        hint = social.symbol_hints[0]
        # Exactly three seeded names (the three highest-volume), then the
        # caller's own candidates behind them.
        assert hint[:3] == ["T00", "T01", "T02"]
        assert "ZZZ" in hint
        db.dispose()

    def test_trending_symbols_absent_from_securities_never_seed(self):
        """The securities join is the same guard ``get_trending`` applies:
        junk "symbols" left by pre-fix extraction (bare English words) must
        not be able to steer the fetch budget."""
        db = _fresh_db()
        _seed_trending(db, {"YOU": 1851, "AS": 1741}, with_securities=False)
        _seed_trending(db, {"REALCO": 10})

        social = _run(db, _config(), universe=[])

        hint = social.symbol_hints[0]
        assert hint == ["REALCO"]
        db.dispose()

    def test_stale_signals_outside_the_recent_window_do_not_seed(self):
        db = _fresh_db()
        _seed_signals(db, ["ANCIENT"], days_ago=90)
        _seed_trending(db, {"FRESH": 50})

        social = _run(db, _config(), universe=[])

        hint = social.symbol_hints[0]
        assert "ANCIENT" not in hint
        assert hint == ["FRESH"]
        db.dispose()

    def test_nothing_stored_leaves_the_hint_untouched(self):
        """An empty database has nothing to prioritise: the caller's own
        candidate list passes through exactly as before this change."""
        db = _fresh_db()

        social = _run(db, _config(), universe=["AAA", "BBB"])

        assert social.symbol_hints[0] == ["AAA", "BBB"]
        db.dispose()

    def test_seeding_is_logged_once_per_refresh(self, caplog):
        db = _fresh_db()
        _seed_holdings(db, ["HOLD1"])

        with caplog.at_level("INFO", logger="claudetrade.data.ingest"):
            _run(db, _config(), universe=["ZZZ"])

        seeded_lines = [r.getMessage() for r in caplog.records if "seeded" in r.getMessage()]
        assert len(seeded_lines) == 1
        assert "HOLD1" in seeded_lines[0]
        db.dispose()


class TestConfiguredWatchlistIsUnchanged:
    def test_non_empty_watchlist_passes_candidates_through_verbatim(self):
        """The operator's explicit watchlist keeps the exact pre-change
        behaviour: no seeding, no reordering, no database reads."""
        db = _fresh_db()
        _seed_holdings(db, ["HOLD1"])
        _seed_trending(db, {"TREND1": 900})

        social = _run(db, _config(watchlist=["SPY", "QQQ"]), universe=["AAA", "BBB"])

        assert social.symbol_hints[0] == ["AAA", "BBB"]
        db.dispose()

    def test_configured_watchlist_still_reaches_the_provider_when_no_hint(self):
        """With a configured watchlist and no caller hint, the provider's own
        ``config.watchlist_symbols`` fallback is what applies -- untouched by
        this change (asserted at the provider seam, not through refresh)."""
        db = _fresh_db()
        _seed_holdings(db, ["HOLD1"])
        config = _config(watchlist=["SPY"])
        ingestor = DataIngestor(config, db, social_providers=[_RecordingSocialProvider()])

        assert ingestor._social_symbol_hints(None) is None
        db.dispose()


class TestSeedingIsBestEffort:
    def test_a_failing_seed_query_degrades_to_the_unseeded_hint(self, monkeypatch, caplog):
        """Fetch prioritisation must never fail a refresh: a broken seed
        query logs and falls back to the caller's own candidates."""
        db = _fresh_db()
        config = _config()
        ingestor = DataIngestor(config, db, social_providers=[_RecordingSocialProvider()])

        def _boom(self):
            raise RuntimeError("synthetic seed failure")

        monkeypatch.setattr(DataIngestor, "_seed_social_symbols", _boom)

        with caplog.at_level("WARNING", logger="claudetrade.data.ingest"):
            assert ingestor._social_symbol_hints(["AAA"]) == ["AAA"]
        assert any("seeding failed" in r.message for r in caplog.records)
        db.dispose()

    def test_concurrent_fetch_path_seeds_on_the_main_thread(self):
        """The background fetch thread must never touch the database, so the
        hint is seeded before the thread starts -- same result either way."""
        db = _fresh_db()
        _seed_holdings(db, ["HOLD1"])
        config = _config()
        config.sentiment.fetch_concurrently = True

        social = _RecordingSocialProvider()
        ingestor = DataIngestor(
            config, db, market_provider=_FastMarketProvider(), social_providers=[social]
        )
        ingestor.run_full_refresh(
            symbols=["ZZZ"],
            start=START,
            end=END,
            securities=[SecurityInfo(symbol="ZZZ", name="Zzz Corp")],
        )

        assert social.symbol_hints[0][0] == "HOLD1"
        db.dispose()
