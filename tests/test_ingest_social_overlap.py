"""Social fetch/market-phase overlap.

The owner's direction: social providers (Reddit, news RSS, X, Stocktwits)
hit completely different hosts than the market-data provider (TipRanks/
Yahoo), so there is no reason ``DataIngestor.run_full_refresh`` should fetch
them strictly after the securities/prices/earnings phases instead of
overlapping the two. These tests cover:

(a) the overlap actually happens (timestamped fake providers prove the social
    fetch ran while the market phase was still in progress);
(b) posts collected in the background are persisted identically to the fully
    sequential path, for the same fake data;
(c) a social provider that raises in the background degrades exactly like the
    sequential path (``report.degraded`` / ``provider_failures`` parity);
(d) a hung social provider cannot hang the refresh -- a small configurable
    join timeout is honoured instead;
(e) mention resolution still sees the securities upserted earlier in the
    *same* refresh (the one hard ordering constraint that must survive the
    fetch moving earlier).
"""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.migrations import init_database
from claudetrade.db.models import SocialPostRow, TickerMentionRow
from claudetrade.db.session import Database
from claudetrade.domain import Bar, SecurityInfo, SocialPost, SocialSource
from claudetrade.providers.base import SourceBlockedError

START = dt.date(2024, 1, 2)
END = dt.date(2024, 1, 2)


class _EventLog:
    """Ordered event recorder. ``list.append`` is atomic under the GIL, so
    this needs no extra locking for the purposes of these tests."""

    def __init__(self) -> None:
        self._events: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        self._events.append((name, time.monotonic()))

    def at(self, name: str) -> float:
        for recorded_name, when in self._events:
            if recorded_name == name:
                return when
        raise AssertionError(f"event {name!r} was never recorded; got {self._events}")


class _SlowMarketProvider:
    """Fake market provider whose bar fetch takes measurable wall time, so a
    concurrently-running social fetch can be shown to overlap it."""

    name = "slow_market"

    def __init__(self, events: _EventLog, delay: float = 0.2):
        self.events = events
        self.delay = delay

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        self.events.mark("prices_start")
        time.sleep(self.delay)
        self.events.mark("prices_end")
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


class _FastMarketProvider(_SlowMarketProvider):
    """No artificial delay -- used by tests that only care about the final
    persisted state, not timing."""

    def __init__(self):
        super().__init__(_EventLog(), delay=0.0)


class _FakeSocialProvider:
    """Fake social provider: returns a fixed batch of posts, optionally after
    a delay, optionally raising, optionally hanging past any sane timeout."""

    def __init__(
        self,
        name: str,
        posts: list[SocialPost],
        *,
        events: _EventLog | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
        hang_s: float | None = None,
    ):
        self.name = name
        self._posts = posts
        self.events = events
        self.delay = delay
        self._raises = raises
        self._hang_s = hang_s
        self.calls = 0

    def fetch_posts(self, *, since, until=None, symbols=None, limit=None):
        self.calls += 1
        if self.events is not None:
            self.events.mark(f"{self.name}_start")
        if self._hang_s is not None:
            time.sleep(self._hang_s)
        if self.delay:
            time.sleep(self.delay)
        if self._raises is not None:
            raise self._raises
        if self.events is not None:
            self.events.mark(f"{self.name}_end")
        return list(self._posts)


def _post(
    external_id: str,
    text: str,
    *,
    source: SocialSource = SocialSource.REDDIT,
    flair: str | None = None,
) -> SocialPost:
    return SocialPost(
        source=source,
        external_id=external_id,
        created_at=dt.datetime(2024, 1, 2, 12, 0, tzinfo=dt.UTC),
        text=text,
        score=10,
        author_hash="author1",
        num_comments=1,
        flair=flair,
    )


def _fresh_db() -> Database:
    db = Database("sqlite:///:memory:")
    init_database(db)
    return db


def _config(*, fetch_concurrently: bool, join_timeout_s: float = 300.0) -> AppConfig:
    config = AppConfig()
    config.sentiment.fetch_concurrently = fetch_concurrently
    config.sentiment.fetch_join_timeout_s = join_timeout_s
    return config


class TestOverlapTiming:
    def test_social_fetch_overlaps_the_prices_phase(self, caplog):
        events = _EventLog()
        market = _SlowMarketProvider(events, delay=0.25)
        social = _FakeSocialProvider(
            "social_a", [_post("1", "$ZZZ to the moon")], events=events, delay=0.35
        )
        config = _config(fetch_concurrently=True)
        db = _fresh_db()
        ingestor = DataIngestor(
            config, db, market_provider=market, social_providers=[social]
        )
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        with caplog.at_level("INFO", logger="claudetrade.data.ingest"):
            ingestor.run_full_refresh(
                symbols=["ZZZ"], start=START, end=END, securities=securities
            )

        prices_start = events.at("prices_start")
        prices_end = events.at("prices_end")
        social_start = events.at("social_a_start")
        social_end = events.at("social_a_end")

        # True overlap: each interval starts before the other ends.
        assert social_start < prices_end
        assert prices_start < social_end

        messages = [r.message for r in caplog.records]
        assert any("social fetch started in background" in m for m in messages)
        assert any("social fetch complete" in m for m in messages)

    def test_sequential_mode_does_not_overlap(self):
        """Sanity check for the timing assertions above: with
        ``fetch_concurrently=False`` the social fetch starts only after the
        prices phase has entirely finished."""
        events = _EventLog()
        market = _SlowMarketProvider(events, delay=0.15)
        social = _FakeSocialProvider("social_a", [_post("1", "$ZZZ to the moon")], events=events)
        config = _config(fetch_concurrently=False)
        db = _fresh_db()
        ingestor = DataIngestor(
            config, db, market_provider=market, social_providers=[social]
        )
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        ingestor.run_full_refresh(symbols=["ZZZ"], start=START, end=END, securities=securities)

        assert events.at("social_a_start") >= events.at("prices_end")


class TestPersistenceParity:
    def test_concurrent_and_sequential_persist_identical_rows(self):
        posts = [
            _post("p1", "$ZZZ is breaking out today"),
            _post("p2", "Zzz Corp just announced a buyback", source=SocialSource.NEWS),
            _post("p3", "nothing tradeable here"),
        ]
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        def _run(*, fetch_concurrently: bool) -> tuple[IngestReport, Database]:
            db = _fresh_db()
            market = _FastMarketProvider()
            social = _FakeSocialProvider("social_a", list(posts))
            config = _config(fetch_concurrently=fetch_concurrently)
            ingestor = DataIngestor(
                config, db, market_provider=market, social_providers=[social]
            )
            report = ingestor.run_full_refresh(
                symbols=["ZZZ"], start=START, end=END, securities=list(securities)
            )
            return report, db

        concurrent_report, concurrent_db = _run(fetch_concurrently=True)
        sequential_report, sequential_db = _run(fetch_concurrently=False)

        assert concurrent_report.posts_inserted == sequential_report.posts_inserted == 3
        assert concurrent_report.mentions_inserted == sequential_report.mentions_inserted

        def _post_rows(db: Database) -> set[tuple[str, str, str]]:
            with db.read_session() as session:
                rows = session.execute(select(SocialPostRow)).scalars().all()
            return {(r.source, r.external_id, r.text) for r in rows}

        def _mention_rows(db: Database) -> set[tuple[str, float]]:
            with db.read_session() as session:
                posts_by_id = {
                    r.id: r.external_id
                    for r in session.execute(select(SocialPostRow)).scalars()
                }
                rows = session.execute(select(TickerMentionRow)).scalars().all()
            return {(posts_by_id[r.post_id], r.symbol) for r in rows}

        assert _post_rows(concurrent_db) == _post_rows(sequential_db)
        assert _mention_rows(concurrent_db) == _mention_rows(sequential_db)

        concurrent_db.dispose()
        sequential_db.dispose()


class TestProviderFailureIsolation:
    def test_background_provider_failure_degrades_like_sequential(self):
        posts_good = [_post("good1", "$ZZZ looking strong")]
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        def _run(*, fetch_concurrently: bool) -> IngestReport:
            db = _fresh_db()
            market = _FastMarketProvider()
            good = _FakeSocialProvider("good", list(posts_good))
            blocked = _FakeSocialProvider(
                "blocked",
                [],
                raises=SourceBlockedError("challenge page", provider="blocked"),
            )
            config = _config(fetch_concurrently=fetch_concurrently)
            ingestor = DataIngestor(
                config, db, market_provider=market, social_providers=[good, blocked]
            )
            return ingestor.run_full_refresh(
                symbols=["ZZZ"], start=START, end=END, securities=list(securities)
            )

        concurrent_report = _run(fetch_concurrently=True)
        sequential_report = _run(fetch_concurrently=False)

        assert concurrent_report.degraded is True
        assert sequential_report.degraded is True
        assert set(concurrent_report.provider_failures) == set(sequential_report.provider_failures)
        assert "blocked" in concurrent_report.provider_failures
        # The other provider's posts must still make it through -- one
        # provider's failure never blocks the rest, in either mode.
        assert concurrent_report.posts_inserted == sequential_report.posts_inserted == 1


class TestJoinTimeout:
    def test_a_hung_social_provider_does_not_hang_the_refresh(self, caplog):
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]
        market = _FastMarketProvider()
        hung = _FakeSocialProvider("hung", [_post("h1", "$ZZZ never arrives")], hang_s=5.0)
        config = _config(fetch_concurrently=True, join_timeout_s=0.1)
        db = _fresh_db()
        ingestor = DataIngestor(
            config, db, market_provider=market, social_providers=[hung]
        )

        started = time.monotonic()
        with caplog.at_level("WARNING", logger="claudetrade.data.ingest"):
            report = ingestor.run_full_refresh(
                symbols=["ZZZ"], start=START, end=END, securities=securities
            )
        elapsed = time.monotonic() - started

        # Nowhere near the provider's 5s hang -- the refresh proceeds after
        # the (tiny, test-configured) join timeout instead.
        assert elapsed < 3.0
        assert report.posts_inserted == 0
        assert any("timeout" in r.message for r in caplog.records)


class TestFlairPersistence:
    """``SocialPost.flair`` round-trips through ``_persist_posts`` onto
    ``SocialPostRow.flair`` -- checked against the *current* (concurrent
    -fetch-capable) ``DataIngestor``, since that persistence path was
    reworked for background social fetch and is the one that actually
    runs in production."""

    def test_flair_is_persisted_and_readable(self):
        db = _fresh_db()
        market = _FastMarketProvider()
        social = _FakeSocialProvider(
            "social_a",
            [_post("p1", "$ZZZ this is DD on the fundamentals", flair="DD")],
        )
        config = _config(fetch_concurrently=True)
        ingestor = DataIngestor(config, db, market_provider=market, social_providers=[social])
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        ingestor.run_full_refresh(symbols=["ZZZ"], start=START, end=END, securities=securities)

        with db.read_session() as session:
            row = session.execute(
                select(SocialPostRow).where(SocialPostRow.external_id == "p1")
            ).scalar_one()
        assert row.flair == "DD"

    def test_none_flair_is_persisted_as_null_not_dropped(self):
        db = _fresh_db()
        market = _FastMarketProvider()
        social = _FakeSocialProvider("social_a", [_post("p2", "$ZZZ nothing special today")])
        config = _config(fetch_concurrently=False)
        ingestor = DataIngestor(config, db, market_provider=market, social_providers=[social])
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        ingestor.run_full_refresh(symbols=["ZZZ"], start=START, end=END, securities=securities)

        with db.read_session() as session:
            row = session.execute(
                select(SocialPostRow).where(SocialPostRow.external_id == "p2")
            ).scalar_one()
        assert row.flair is None

    def test_flair_survives_the_concurrent_fetch_path_too(self):
        """Same assertion, but exercised the other way -- posts arriving
        via the background/concurrent fetch (not just sequential) still get
        their flair persisted; this is the exact path ``ingest.py`` was
        recently reworked around."""
        db = _fresh_db()
        market = _SlowMarketProvider(_EventLog(), delay=0.05)
        social = _FakeSocialProvider(
            "social_a", [_post("p3", "$ZZZ yolo into next week", flair="YOLO")], delay=0.05
        )
        config = _config(fetch_concurrently=True)
        ingestor = DataIngestor(config, db, market_provider=market, social_providers=[social])
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        ingestor.run_full_refresh(symbols=["ZZZ"], start=START, end=END, securities=securities)

        with db.read_session() as session:
            row = session.execute(
                select(SocialPostRow).where(SocialPostRow.external_id == "p3")
            ).scalar_one()
        assert row.flair == "YOLO"


class TestAliasDependency:
    def test_mention_resolution_sees_securities_upserted_this_refresh(self):
        """``ZZZ`` is not present in the database before this call -- it only
        becomes resolvable because ``ingest_securities`` runs (and commits)
        earlier in this same ``run_full_refresh`` call. Only the social
        *fetch* is allowed to move earlier than that; persistence/resolution
        must still come after."""
        db = _fresh_db()
        market = _FastMarketProvider()
        social = _FakeSocialProvider(
            "social_a", [_post("p1", "$ZZZ is a screaming buy today")]
        )
        config = _config(fetch_concurrently=True)
        ingestor = DataIngestor(
            config, db, market_provider=market, social_providers=[social]
        )
        securities = [SecurityInfo(symbol="ZZZ", name="Zzz Corp")]

        report = ingestor.run_full_refresh(
            symbols=["ZZZ"], start=START, end=END, securities=securities
        )

        assert report.mentions_inserted == 1
        with db.read_session() as session:
            rows = session.execute(
                select(TickerMentionRow).where(TickerMentionRow.symbol == "ZZZ")
            ).scalars().all()
        assert len(rows) == 1
