"""Shared fixtures for the test suite."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from claudetrade.config import AppConfig, reset_config_cache
from claudetrade.db.migrations import init_database
from claudetrade.db.session import Database, reset_database_cache
from claudetrade.domain import Bar, SocialPost, SocialSource


@pytest.fixture
def tmp_app_config(tmp_path: Path, monkeypatch) -> AppConfig:
    """AppConfig pointed at a temporary directory.

    Logging is disabled for quiet tests. Every test gets an isolated config.
    """
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()

    config = AppConfig()
    config.logging.console = False
    config.paths.app_dir = tmp_path

    return config


@pytest.fixture
def tmp_db(tmp_app_config: AppConfig) -> Database:
    """Fresh in-memory SQLite database with migrations applied.

    Each test gets an isolated database. The database is disposed after the test.
    """
    reset_database_cache()

    # Use a file-based SQLite in the temp directory for isolation
    db_path = tmp_app_config.paths.app_dir / "test.db"
    db = Database(f"sqlite:///{db_path}", config=tmp_app_config)
    init_database(db)

    yield db

    db.dispose()
    reset_database_cache()


@pytest.fixture
def memory_db() -> Database:
    """Lightweight in-memory SQLite database with migrations applied."""
    db = Database("sqlite:///:memory:")
    init_database(db)
    yield db
    db.dispose()


@pytest.fixture
def unmigrated_db() -> Database:
    """In-memory SQLite database with **no** migrations applied.

    Migration tests need a virgin database: they assert that version starts at
    zero, that the first run reports applied migrations, and that a fresh
    schema is detected as incomplete. ``memory_db`` has already been migrated,
    which makes all three vacuously false.
    """
    db = Database("sqlite:///:memory:")
    yield db
    db.dispose()


@pytest.fixture
def sample_bars() -> list[Bar]:
    """Deterministic list of 100 trading bars.

    Price climbs slowly upward with realistic OHLCV structure.
    """
    bars = []
    base_date = dt.date(2023, 1, 3)
    price = 100.0
    volume = 1_000_000

    for i in range(100):
        session = base_date + dt.timedelta(days=i)
        # Skip weekends
        if session.weekday() >= 5:
            continue

        open_price = price
        high = price * 1.02
        low = price * 0.99
        close = price * 1.01
        adj_close = close

        bars.append(
            Bar(
                symbol="TEST",
                session=session,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                adj_close=adj_close,
            )
        )
        price = close

    return bars


@pytest.fixture
def make_bar():
    """Factory for creating Bar objects with sensible defaults."""
    counter = [0]

    def factory(
        symbol: str = "TEST",
        session: dt.date | None = None,
        open_: float = 100.0,
        high: float | None = None,
        low: float | None = None,
        close: float | None = None,
        volume: float = 1_000_000,
        adj_close: float | None = None,
    ) -> Bar:
        counter[0] += 1
        date = session or dt.date(2023, 1, 3) + dt.timedelta(days=counter[0])
        h = high if high is not None else open_ * 1.02
        l = low if low is not None else open_ * 0.98
        c = close if close is not None else open_ * 1.01
        ac = adj_close if adj_close is not None else c

        return Bar(
            symbol=symbol,
            session=date,
            open=open_,
            high=h,
            low=l,
            close=c,
            volume=volume,
            adj_close=ac,
        )

    return factory


@pytest.fixture
def make_post():
    """Factory for creating SocialPost objects with sensible defaults."""
    counter = [0]

    def factory(
        text: str = "Great stock idea!",
        symbol: str = "TEST",
        source: SocialSource = SocialSource.REDDIT,
        created_at: dt.datetime | None = None,
        score: int = 10,
        author_hash: str = "author1",
        engagement: int = 5,
    ) -> SocialPost:
        counter[0] += 1
        timestamp = created_at or dt.datetime(2023, 1, 15, 12, 0, 0, tzinfo=dt.UTC)

        return SocialPost(
            source=source,
            external_id=f"post_{counter[0]}",
            created_at=timestamp,
            text=text,
            score=score,
            author_hash=author_hash,
            num_comments=engagement,
            num_reposts=engagement,
            num_replies=engagement,
        )

    return factory
