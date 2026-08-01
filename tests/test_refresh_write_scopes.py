"""Write-transaction scopes on the refresh path (QA handoff v3, F26).

The MCP lockup QA hit was not caused by any single slow query -- under WAL a
reader is never blocked by a writer. It was caused by *how long* the refresh
held its write transactions: ``Pipeline.build_sentiment`` wrapped its entire
symbol loop in ONE transaction, and the ingest persistence helpers wrapped
whole-universe loops in one each. A transaction that stays open for minutes
holds SQLite's write lock for minutes (so every other process's writes queue
behind it, and a second refresh's ``busy_timeout`` expiry is the *good* case)
and, because WAL only checkpoints at commit, lets the WAL grow unboundedly --
which degrades cross-process reads too.

These tests pin the fix as a behavioural property, not an implementation
detail: work committed earlier in a loop must be visible to a SEPARATE
database connection while the loop is still running. Each would fail against
the single-transaction versions, where nothing is visible until the very end.
Idempotence is asserted alongside, because "commit as you go" is only safe if
re-running covers the same ground without duplicating it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from claudetrade.data.ingest import PERSIST_CHUNK_ROWS, DataIngestor, IngestReport
from claudetrade.db.models import Security, SocialPostRow, SymbolSentimentDaily, TickerMentionRow
from claudetrade.db.session import Database
from claudetrade.domain import SecurityInfo, SocialPost, SocialSource
from claudetrade.pipeline import Pipeline

_CLOSE = dt.datetime(2026, 7, 31, 20, 0, tzinfo=dt.UTC)
_SESSION = dt.date(2026, 7, 31)


@pytest.fixture
def observer_db(tmp_app_config, tmp_db) -> Database:
    """A second connection to the same file -- stands in for another process
    (the MCP server) trying to read while a refresh writes."""
    db_path = tmp_app_config.paths.app_dir / "test.db"
    db = Database(f"sqlite:///{db_path}", config=tmp_app_config)
    yield db
    db.dispose()


def _post(symbol: str, idx: int) -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=f"{symbol}-{idx}",
        created_at=_CLOSE - dt.timedelta(hours=3),
        text=f"${symbol} is ripping today, very bullish breakout on huge volume",
        author_hash=f"author-{symbol}-{idx}",
        score=25,
    )


def _corpus(symbols: list[str], per_symbol: int = 3) -> list[SocialPost]:
    return [_post(sym, i) for sym in symbols for i in range(per_symbol)]


# --------------------------------------------------------------------------
# Pipeline.build_sentiment
# --------------------------------------------------------------------------


class TestBuildSentimentCommitsPerSymbol:
    def test_earlier_symbols_are_visible_to_another_connection_mid_loop(
        self, tmp_app_config, tmp_db, observer_db
    ):
        """The core F26 property. The progress hook fires after each symbol is
        persisted, so by the time the SECOND symbol is reported, the FIRST
        symbol's rows must already be committed and readable from a separate
        connection. Under the old single-transaction loop the observer sees
        zero rows until the whole pass finishes."""
        symbols = ["AAA", "BBB", "CCC"]
        directory = {s: SecurityInfo(symbol=s, name=f"{s} Inc") for s in symbols}
        pipeline = Pipeline(tmp_app_config, tmp_db)

        observed: list[int] = []

        def _observe(phase: str, done: int, total: int) -> None:
            with observer_db.read_session() as session:
                observed.append(
                    session.execute(
                        select(func.count()).select_from(SymbolSentimentDaily)
                    ).scalar()
                )

        written = pipeline.build_sentiment(
            posts=_corpus(symbols),
            directory=directory,
            start=_SESSION,
            end=_SESSION,
            progress_callback=_observe,
        )

        assert written == len(symbols)
        # One observation per symbol, and each already sees that symbol's own
        # committed row -- i.e. rows land incrementally, never all at the end.
        assert observed == [1, 2, 3]

    def test_progress_is_reported_once_per_symbol(self, tmp_app_config, tmp_db):
        """This is the hook that keeps the cross-process refresh heartbeat
        (F27) alive through a long aggregation pass."""
        symbols = ["AAA", "BBB", "CCC"]
        pipeline = Pipeline(tmp_app_config, tmp_db)
        calls: list[tuple[str, int, int]] = []

        pipeline.build_sentiment(
            posts=_corpus(symbols),
            directory={s: SecurityInfo(symbol=s, name=s) for s in symbols},
            start=_SESSION,
            end=_SESSION,
            progress_callback=lambda phase, done, total: calls.append((phase, done, total)),
        )

        assert calls == [
            ("sentiment_aggregate", 1, 3),
            ("sentiment_aggregate", 2, 3),
            ("sentiment_aggregate", 3, 3),
        ]

    def test_a_raising_progress_callback_never_breaks_the_run(self, tmp_app_config, tmp_db):
        """Matches ``DataIngestor._report_progress``: reporting is best-effort,
        a refresh must not die because its progress sink hiccuped."""
        pipeline = Pipeline(tmp_app_config, tmp_db)

        def _boom(phase: str, done: int, total: int) -> None:
            raise RuntimeError("heartbeat write failed")

        written = pipeline.build_sentiment(
            posts=_corpus(["AAA", "BBB"]),
            directory={s: SecurityInfo(symbol=s, name=s) for s in ("AAA", "BBB")},
            start=_SESSION,
            end=_SESSION,
            progress_callback=_boom,
        )
        assert written == 2

    def test_no_progress_callback_still_works(self, tmp_app_config, tmp_db):
        """Every pre-existing caller passes none (the CLI's rebuild-sentiment
        command among them); the parameter is purely additive."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        written = pipeline.build_sentiment(
            posts=_corpus(["AAA"]),
            directory={"AAA": SecurityInfo(symbol="AAA", name="AAA")},
            start=_SESSION,
            end=_SESSION,
        )
        assert written == 1

    def test_rerunning_upserts_rather_than_duplicating(self, tmp_app_config, tmp_db):
        """Committing per symbol is only safe because the writes are
        idempotent upserts -- a run interrupted between symbols re-covers the
        same ground on the next refresh instead of doubling rows."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        symbols = ["AAA", "BBB"]
        directory = {s: SecurityInfo(symbol=s, name=s) for s in symbols}
        posts = _corpus(symbols)

        pipeline.build_sentiment(
            posts=posts, directory=directory, start=_SESSION, end=_SESSION
        )
        second = pipeline.build_sentiment(
            posts=posts, directory=directory, start=_SESSION, end=_SESSION
        )

        assert second == 0  # nothing newly inserted the second time
        with tmp_db.read_session() as session:
            rows = session.execute(select(SymbolSentimentDaily)).scalars().all()
        assert len(rows) == 2
        assert {r.symbol for r in rows} == set(symbols)


# --------------------------------------------------------------------------
# DataIngestor persistence helpers
# --------------------------------------------------------------------------


def _ingestor(config, db) -> DataIngestor:
    return DataIngestor(config, db)


class TestIngestPersistenceIsChunked:
    def test_a_multi_chunk_persist_stores_every_post(
        self, tmp_app_config, tmp_db, observer_db
    ):
        """Chunking must not lose rows at a boundary: more posts than one
        chunk, all of them durable and visible from another connection."""
        ingestor = _ingestor(tmp_app_config, tmp_db)
        report = IngestReport()
        posts = [_post("AAA", i) for i in range(PERSIST_CHUNK_ROWS + 25)]

        ingestor._persist_posts(posts, report)

        assert report.posts_inserted == len(posts)
        with observer_db.read_session() as session:
            stored = session.execute(select(func.count()).select_from(SocialPostRow)).scalar()
        assert stored == len(posts)

    def test_posts_are_committed_incrementally(self, tmp_app_config, tmp_db, observer_db):
        """Directly observable: patch the chunk helper to check, from another
        connection, that everything before the current chunk is already
        durable. A single whole-loop transaction shows 0 until the end."""
        import claudetrade.data.ingest as ingest_module

        ingestor = _ingestor(tmp_app_config, tmp_db)
        posts = [_post("AAA", i) for i in range(3 * PERSIST_CHUNK_ROWS)]
        visible_before_chunk: list[int] = []

        real_chunks = ingest_module._chunks

        def _observing_chunks(items, size):
            for chunk in real_chunks(items, size):
                with observer_db.read_session() as session:
                    visible_before_chunk.append(
                        session.execute(
                            select(func.count()).select_from(SocialPostRow)
                        ).scalar()
                    )
                yield chunk

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ingest_module, "_chunks", _observing_chunks)
            ingestor._persist_posts(posts, IngestReport())

        # Chunk 1 starts with nothing stored; each later chunk starts with
        # every previous chunk already committed and visible cross-connection.
        assert visible_before_chunk == [0, PERSIST_CHUNK_ROWS, 2 * PERSIST_CHUNK_ROWS]

    def test_persisting_posts_twice_inserts_nothing_new(self, tmp_app_config, tmp_db):
        ingestor = _ingestor(tmp_app_config, tmp_db)
        posts = [_post("AAA", i) for i in range(5)]

        ingestor._persist_posts(posts, IngestReport())
        second_report = IngestReport()
        ingestor._persist_posts(posts, second_report)

        assert second_report.posts_inserted == 0
        with tmp_db.read_session() as session:
            assert session.execute(
                select(func.count()).select_from(SocialPostRow)
            ).scalar() == 5

    def test_securities_upsert_in_chunks_and_stay_idempotent(self, tmp_app_config, tmp_db):
        ingestor = _ingestor(tmp_app_config, tmp_db)
        securities = [
            SecurityInfo(symbol=f"S{i:04d}", name=f"Company {i}")
            for i in range(PERSIST_CHUNK_ROWS + 10)
        ]

        report = IngestReport()
        ingestor.ingest_securities(list(securities), report)
        assert report.securities_upserted == len(securities)

        # Re-running updates in place rather than duplicating rows/aliases.
        ingestor.ingest_securities(list(securities), IngestReport())
        with tmp_db.read_session() as session:
            assert session.execute(
                select(func.count()).select_from(Security)
            ).scalar() == len(securities)

    def test_mentions_persist_in_chunks_and_stay_idempotent(self, tmp_app_config, tmp_db):
        """``persist_mentions`` also stopped doing its id-map lookup inside the
        write transaction -- it used to load every post row and linear-scan
        that map per mention while holding the write lock."""
        from claudetrade.domain import TickerMention

        ingestor = _ingestor(tmp_app_config, tmp_db)
        posts = [_post("AAA", i) for i in range(5)]
        ingestor._persist_posts(posts, IngestReport())

        mentions_by_post = {
            p.external_id: [
                TickerMention(
                    post_external_id=p.external_id,
                    symbol="AAA",
                    confidence=0.9,
                    method="cashtag",
                    matched_text="$AAA",
                )
            ]
            for p in posts
        }

        first = IngestReport()
        ingestor.persist_mentions(mentions_by_post, first)
        assert first.mentions_inserted == 5

        second = IngestReport()
        ingestor.persist_mentions(mentions_by_post, second)
        assert second.mentions_inserted == 0

        with tmp_db.read_session() as session:
            assert session.execute(
                select(func.count()).select_from(TickerMentionRow)
            ).scalar() == 5
