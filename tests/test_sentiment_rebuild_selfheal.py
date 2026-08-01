"""The stored-aggregate self-heal (QA F25's durable fix).

``claudetrade db rebuild-sentiment`` always existed, but QA v3 proved the
operator never ran it: the trending list was still serving AS/YOU/DAY junk and
all-neutral ``bull_bear_ratio == 1.0`` rows written by pre-fix extraction a
week after both fixes shipped. These tests pin the mechanism that removes the
runbook from the loop:

* ``sentiment.EXTRACTION_VERSION`` names the extraction code generation;
* the version stored aggregates were last built with is stamped into the
  existing ``settings_kv`` table;
* ``db.migrations.init_database`` -- the seam every entry point's
  ``Pipeline.bootstrap`` passes through -- compares the two and rebuilds the
  aggregates from stored posts automatically when they are stale.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.db.migrations import init_database
from claudetrade.db.models import Security, SocialPostRow, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.sentiment import EXTRACTION_VERSION
from claudetrade.sentiment import rebuild as rebuild_module
from claudetrade.sentiment.rebuild import (
    RebuildUnavailableError,
    ensure_extraction_version,
    rebuild_sentiment,
    record_extraction_version,
    stored_extraction_version,
)


def _fresh_db() -> Database:
    db = Database("sqlite:///:memory:")
    init_database(db)
    return db


def _seed_stale_state(db: Database, *, securities: bool = True) -> None:
    """A database as the QA session found it: real posts on disk, junk
    aggregates written by the pre-fix extractor, no version stamp."""
    with db.session() as session:
        if securities:
            session.merge(Security(symbol="AMZN", name="Amazon.com Inc"))
        texts = [
            "$AMZN crushed earnings, very bullish. Buying more calls!",
            "$AMZN beat expectations, guidance raised. Loading up here.",
            "Great earnings from $AMZN, this is going higher.",
            "$AMZN to the moon after that blowout quarter.",
            "$AMZN printing money, growth is back. Long term hold.",
            "Undervalued even after the pop, adding $AMZN.",
            "$AMZN best stock in my portfolio right now.",
            "I was bearish but $AMZN proved me wrong, crushed it.",
            "Solid quarter from $AMZN, staying long.",
            "$AMZN breaking out on strong volume today.",
        ]
        for i, text in enumerate(texts):
            session.add(
                SocialPostRow(
                    source="reddit",
                    external_id=f"heal{i}",
                    # Naive on purpose: SQLite returns DateTime(timezone=True)
                    # columns naive, and the heal must survive exactly that.
                    created_at=dt.datetime.now(tz=dt.UTC).replace(tzinfo=None)
                    - dt.timedelta(hours=30 + i),
                    text=text,
                    author_hash=f"author{i}",
                    score=25,
                )
            )
        # The QA fingerprint rows: a stopword "ticker" with huge volume and
        # the all-neutral 1.0 ratio the legacy classifier left behind.
        session.add(
            SymbolSentimentDaily(
                symbol="YOU",
                session=dt.date.today() - dt.timedelta(days=2),
                source="all",
                post_count=1851,
                bull_bear_ratio=1.0,
            )
        )
        session.add(
            SymbolSentimentDaily(
                symbol="AS",
                session=dt.date.today() - dt.timedelta(days=3),
                source="all",
                post_count=1741,
                bull_bear_ratio=1.0,
            )
        )


def _aggregate_symbols(db: Database) -> set[str]:
    with db.read_session() as session:
        return {
            r.symbol for r in session.execute(select(SymbolSentimentDaily)).scalars()
        }


class TestVersionStamp:
    def test_fresh_database_records_the_current_version_without_config(self):
        """conftest-style ``init_database(db)`` on an empty database: nothing
        to rebuild, so the current version is simply recorded and the check
        stays a no-op forever after."""
        db = _fresh_db()
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        db.dispose()

    def test_unstamped_database_reads_as_version_zero(self):
        db = Database("sqlite:///:memory:")
        # Migrate WITHOUT the auto-stamp path interfering: seed posts first
        # via a bare metadata create, then check the raw read.
        from claudetrade.db.models import Base

        Base.metadata.create_all(db.engine)
        assert stored_extraction_version(db) == 0
        db.dispose()

    def test_record_and_read_round_trip(self):
        db = _fresh_db()
        record_extraction_version(db, 99)
        assert stored_extraction_version(db) == 99
        db.dispose()


class TestBootstrapSelfHeal:
    def test_stale_aggregates_are_rebuilt_and_version_recorded(self, tmp_app_config: AppConfig):
        db = _fresh_db()
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)  # pre-fix stamp

        init_database(db, tmp_app_config)

        symbols = _aggregate_symbols(db)
        assert "YOU" not in symbols and "AS" not in symbols  # junk cleared
        assert "AMZN" in symbols  # rebuilt from stored posts with current code
        with db.read_session() as session:
            amzn = (
                session.execute(
                    select(SymbolSentimentDaily).where(SymbolSentimentDaily.symbol == "AMZN")
                )
                .scalars()
                .all()
            )
        # The rebuilt rows carry the repaired classifier's output -- real
        # polarity, not the all-neutral 1.0 fingerprint.
        assert any(r.bull_bear_ratio != pytest.approx(1.0) for r in amzn)
        assert any(r.raw_sentiment > 0.1 for r in amzn)
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        db.dispose()

    def test_never_stamped_database_heals_too(self, tmp_app_config: AppConfig):
        """A legacy database has no stamp at all (reads as 0) -- the exact
        state the owner's production database is in."""
        db = _fresh_db()
        _seed_stale_state(db)
        with db.session() as session:  # simulate "no stamp ever written"
            from claudetrade.db.models import SettingKV

            row = session.get(SettingKV, rebuild_module.EXTRACTION_VERSION_KEY)
            if row is not None:
                session.delete(row)

        init_database(db, tmp_app_config)

        assert "YOU" not in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        db.dispose()

    def test_current_version_short_circuits_without_touching_rows(
        self, tmp_app_config: AppConfig
    ):
        db = _fresh_db()
        _seed_stale_state(db)  # junk present, but...
        record_extraction_version(db, EXTRACTION_VERSION)  # ...stamp says current

        init_database(db, tmp_app_config)

        # Nothing was rebuilt: the stamp is trusted (rebuilds are driven by
        # code-generation changes, not by row inspection).
        assert "YOU" in _aggregate_symbols(db)
        db.dispose()

    def test_posts_without_config_defer_unstamped(self):
        """A bare ``init_database(db)`` (no config seam) over a stale
        database must neither rebuild nor stamp -- the next config-carrying
        bootstrap heals."""
        db = _fresh_db()
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)

        init_database(db)  # no config

        assert "YOU" in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION - 1
        db.dispose()

    def test_posts_without_securities_defer_unstamped(self, tmp_app_config: AppConfig):
        db = _fresh_db()
        _seed_stale_state(db, securities=False)
        record_extraction_version(db, EXTRACTION_VERSION - 1)

        init_database(db, tmp_app_config)

        # Nothing to resolve against -- aggregates untouched, stamp untouched,
        # so the first bootstrap after a refresh stores securities will heal.
        assert "YOU" in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION - 1
        db.dispose()

    def test_rebuild_failure_is_swallowed_and_leaves_stamp_behind(
        self, tmp_app_config: AppConfig, monkeypatch
    ):
        """A failing rebuild must not take bootstrap (and with it every CLI
        command) down: stale aggregates are the pre-existing condition, not a
        startup-fatal error. The stamp stays behind so the next start-up
        retries."""
        db = _fresh_db()
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic rebuild failure")

        monkeypatch.setattr(rebuild_module, "rebuild_sentiment", _boom)
        init_database(db, tmp_app_config)  # must not raise

        assert stored_extraction_version(db) == EXTRACTION_VERSION - 1
        assert "YOU" in _aggregate_symbols(db)
        db.dispose()

    def test_pipeline_bootstrap_runs_the_heal(self, tmp_app_config: AppConfig):
        """End-to-end through the seam production actually uses: seed the
        stale state into the process database, then ``Pipeline.bootstrap`` --
        the owner's next command after `git pull` -- heals it."""
        from claudetrade.db.session import get_database, reset_database_cache
        from claudetrade.pipeline import Pipeline

        reset_database_cache()
        db = get_database(tmp_app_config)
        init_database(db)
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)

        Pipeline.bootstrap(tmp_app_config)

        assert "YOU" not in _aggregate_symbols(db)
        assert "AMZN" in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        reset_database_cache()

    def test_bootstrap_can_defer_the_heal_without_skipping_it(
        self, tmp_app_config: AppConfig
    ):
        """``allow_data_fixes=False`` -- what ``mcp_server.run_stdio`` passes.

        Integration hazard this pins: the self-heal landed on the seam every
        entry point shares, but on a real database it is a minute-scale
        rebuild, and the MCP server runs its bootstrap *inside* the client's
        initialize handshake (before ``server.run()`` answers anything). A
        client that launches the server as a subprocess would sit through the
        whole rebuild and can time out first -- and the per-tool watchdog
        cannot help, because no tool exists yet.

        Deferred must not mean skipped: the schema is still migrated, the
        stamp is deliberately NOT advanced, and the next ordinary bootstrap
        still heals -- otherwise declining once would strand the junk
        aggregates forever.
        """
        from claudetrade.db.session import get_database, reset_database_cache
        from claudetrade.pipeline import Pipeline

        reset_database_cache()
        db = get_database(tmp_app_config)
        init_database(db)
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)

        Pipeline.bootstrap(tmp_app_config, allow_data_fixes=False)

        # Untouched, and still flagged as stale so a later bootstrap retries.
        assert "YOU" in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION - 1

        Pipeline.bootstrap(tmp_app_config)  # a CLI/UI start-up heals it

        assert "YOU" not in _aggregate_symbols(db)
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        reset_database_cache()

    def test_migrations_still_run_when_data_fixes_are_deferred(self) -> None:
        """Deferring data fixes must never defer *schema* work -- the code
        assumes the current schema the moment it runs a query."""
        from claudetrade.db.migrations import LATEST_VERSION, current_version, verify_schema

        db = Database("sqlite:///:memory:")
        init_database(db, None, allow_data_fixes=False)

        assert current_version(db) == LATEST_VERSION
        assert verify_schema(db) == []
        db.dispose()


class TestRebuildCore:
    """The importable core the CLI command now wraps."""

    def test_summary_dict_shape_and_version_stamp(self, tmp_app_config: AppConfig):
        db = _fresh_db()
        _seed_stale_state(db)

        summary = rebuild_sentiment(tmp_app_config, db, days=90)

        assert summary["posts_considered"] == 10
        assert summary["sentiment_aggregates_deleted"] >= 2  # both junk rows
        assert summary["sentiment_rows_rebuilt"] >= 1
        assert summary["symbols_affected"] >= 1
        assert summary["window_start"] < summary["window_end"]
        # An explicit successful rebuild also brings the stamp current.
        assert stored_extraction_version(db) == EXTRACTION_VERSION
        db.dispose()

    def test_refuses_before_deleting_when_no_securities_stored(
        self, tmp_app_config: AppConfig
    ):
        db = _fresh_db()
        _seed_stale_state(db, securities=False)

        with pytest.raises(RebuildUnavailableError):
            rebuild_sentiment(tmp_app_config, db)

        # The abort happened before any delete: the junk rows survive.
        assert {"YOU", "AS"} <= _aggregate_symbols(db)
        db.dispose()

    def test_ensure_is_idempotent_after_heal(self, tmp_app_config: AppConfig):
        db = _fresh_db()
        _seed_stale_state(db)
        record_extraction_version(db, EXTRACTION_VERSION - 1)
        first = ensure_extraction_version(db, tmp_app_config)
        assert first is not None and first["sentiment_rows_rebuilt"] >= 1

        second = ensure_extraction_version(db, tmp_app_config)
        assert second is None  # already current: two point reads, no rebuild
        db.dispose()
