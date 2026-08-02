"""Rolling aggregation from persisted history, and collection coverage.

Two defects, one theme: the application could not accumulate evidence.

``Pipeline.refresh`` aggregated only the posts one run had just fetched.
Providers look back ~72 hours, so every daily aggregate was built from a
single fetch and no rolling baseline could ever form, however long the
installation ran -- which breaks the premise the whole product rests on
(spotting rising mentions ahead of price). These tests pin the fix as a
behavioural property: a LATER batch's aggregate must contain an EARLIER
batch's eligible posts, without look-ahead and without rewriting history.

And a session with no stored row read as "0 mentions" whether collection
had run and found silence or had not run at all. The second case makes an
outage look like a quiet stretch, which sags every baseline and then
manufactures a universe-wide surge the day collection resumes. Coverage
records which sessions were really collected; the baseline divides by
those.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import (
    Security,
    SocialCoverageRow,
    SymbolSentimentDaily,
    TickerMentionRow,
)
from claudetrade.db.session import Database
from claudetrade.domain import SecurityInfo, SocialPost, SocialSource
from claudetrade.pipeline import Pipeline
from claudetrade.sentiment.history import coverage_summary, rising_symbols, symbol_series
from claudetrade.sentiment.store import (
    SourceCollection,
    coverage_window,
    load_stored_posts,
    record_collection_coverage,
    sessions_covered_by_fetch,
)
from claudetrade.utils.timeutils import (
    current_trading_session,
    previous_trading_day,
    session_close_utc,
    session_for_instant,
    trading_day_range,
    utc_now,
)

#: Anchored to the clock rather than hard-coded, because the refresh path's
#: history read is floored at ``now - history_window_days``: a fixed date
#: would quietly stop exercising the rolling read once it aged past that.
#: ``previous_trading_day(current_trading_session())`` is guaranteed to have
#: closed, which is what makes the session-window arithmetic below exact.
LAST_SESSION = previous_trading_day(current_trading_session())
PREV_SESSION = previous_trading_day(LAST_SESSION)

SYMBOL = "AAA"


def _post(idx: int, when: dt.datetime, *, symbol: str = SYMBOL) -> SocialPost:
    return SocialPost(
        source=SocialSource.REDDIT,
        external_id=f"{symbol}-{idx}-{when.date().isoformat()}",
        created_at=when,
        text=f"${symbol} is ripping today, very bullish breakout on huge volume",
        author_hash=f"author-{symbol}-{idx}",
        score=25,
    )


def _batch(count: int, when: dt.datetime, *, symbol: str = SYMBOL) -> list[SocialPost]:
    return [_post(i, when, symbol=symbol) for i in range(count)]


def _directory(*symbols: str) -> dict[str, SecurityInfo]:
    return {s: SecurityInfo(symbol=s, name=f"{s} Inc") for s in symbols or (SYMBOL,)}


def _refresh_batch(
    pipeline: Pipeline, posts: list[SocialPost], *, start: dt.date, end: dt.date
) -> int:
    """One refresh's sentiment work, wired exactly as ``Pipeline.refresh`` wires it.

    Persist first (the ingestor does), then aggregate the fresh posts over
    the stored history they need for context. Going through the real
    ``_stored_history_for`` is the point -- a test that handed
    ``build_sentiment`` a hand-picked history would pass while the bounded
    read that production uses returned nothing.
    """
    DataIngestor(pipeline.config, pipeline.db)._persist_posts(posts, IngestReport())
    return pipeline.build_sentiment(
        posts=posts,
        directory=_directory(SYMBOL),
        history=pipeline._stored_history_for(posts),
        start=start,
        end=end,
    )


def _row(db: Database, session_date: dt.date, symbol: str = SYMBOL):
    with db.read_session() as session:
        return session.execute(
            select(SymbolSentimentDaily).where(
                SymbolSentimentDaily.symbol == symbol,
                SymbolSentimentDaily.session == session_date,
                SymbolSentimentDaily.source == "all",
            )
        ).scalar_one_or_none()


# --------------------------------------------------------------------------
# The rolling baseline
# --------------------------------------------------------------------------


class TestAggregatesUsePersistedHistory:
    def test_a_later_batch_aggregate_includes_the_earlier_batch(
        self, tmp_app_config, tmp_db
    ):
        """The headline defect. Two refreshes separated in time: the second
        one's aggregate must count the first one's posts, because they are
        still inside the window it is measuring. Before the fix each refresh
        saw only its own fetch, so a symbol's "normal" was whatever the last
        72 hours happened to return and no baseline ever accumulated."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        first = _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        second = _batch(2, session_close_utc(LAST_SESSION) - dt.timedelta(hours=2))

        _refresh_batch(pipeline, first, start=PREV_SESSION, end=PREV_SESSION)
        _refresh_batch(pipeline, second, start=PREV_SESSION, end=LAST_SESSION)

        latest = _row(tmp_db, LAST_SESSION)
        assert latest is not None
        assert latest.post_count == 5, (
            "the second batch's aggregate must carry the first batch's posts too; "
            "counting only the fresh fetch is the defect this fixes"
        )

    def test_the_earlier_session_is_not_rewritten_by_the_later_batch(
        self, tmp_app_config, tmp_db
    ):
        """Incremental scope. The second batch's posts belong to the later
        session, so the earlier session's row must be left exactly as the
        first batch built it -- a refresh recomputes what it learned about,
        not every session in its calendar range."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        _refresh_batch(
            pipeline,
            _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2)),
            start=PREV_SESSION,
            end=PREV_SESSION,
        )
        before = _row(tmp_db, PREV_SESSION)
        assert before is not None
        before_count, before_computed = before.post_count, before.computed_at

        _refresh_batch(
            pipeline,
            _batch(2, session_close_utc(LAST_SESSION) - dt.timedelta(hours=2)),
            start=PREV_SESSION,
            end=LAST_SESSION,
        )

        after = _row(tmp_db, PREV_SESSION)
        assert after.post_count == before_count
        assert after.computed_at == before_computed  # not even touched

    def test_rerunning_a_batch_changes_no_stored_counts(self, tmp_app_config, tmp_db):
        """Idempotence, which is what makes 'commit as you go' safe: a refresh
        killed and restarted, or simply run twice, must re-cover the same
        ground rather than inflating what it already recorded."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        first = _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        second = _batch(2, session_close_utc(LAST_SESSION) - dt.timedelta(hours=2))
        _refresh_batch(pipeline, first, start=PREV_SESSION, end=PREV_SESSION)
        _refresh_batch(pipeline, second, start=PREV_SESSION, end=LAST_SESSION)

        snapshot = {
            (r.symbol, r.session, r.source): (r.post_count, r.raw_sentiment)
            for r in _all_rows(tmp_db)
        }

        _refresh_batch(pipeline, second, start=PREV_SESSION, end=LAST_SESSION)
        _refresh_batch(pipeline, first, start=PREV_SESSION, end=PREV_SESSION)

        assert {
            (r.symbol, r.session, r.source): (r.post_count, r.raw_sentiment)
            for r in _all_rows(tmp_db)
        } == snapshot

    def test_a_future_dated_post_never_reaches_an_earlier_session(
        self, tmp_app_config, tmp_db
    ):
        """No-look-ahead survives the union. A post from after a session's
        close is stored history for LATER sessions and must contribute
        nothing to one that already closed -- otherwise the rolling read
        would be a time machine."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        _refresh_batch(
            pipeline,
            _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2)),
            start=PREV_SESSION,
            end=PREV_SESSION,
        )
        # A batch dated after the LATER session's close, aggregated over a
        # range that still includes the earlier session.
        _refresh_batch(
            pipeline,
            _batch(4, session_close_utc(LAST_SESSION) + dt.timedelta(hours=3)),
            start=PREV_SESSION,
            end=LAST_SESSION,
        )

        assert _row(tmp_db, PREV_SESSION).post_count == 3

    def test_a_post_after_the_close_lands_on_the_next_session(
        self, tmp_app_config, tmp_db
    ):
        """Refreshes run in the evening, so most gathered posts are dated
        after the close. Such a post is early information about the NEXT
        session -- attributing it to the one that already closed would be the
        real look-ahead, and dropping it (the older behaviour) silently threw
        away most of every evening's fetch."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        after_close = session_close_utc(LAST_SESSION) + dt.timedelta(hours=3)
        next_session = session_for_instant(after_close)
        assert next_session > LAST_SESSION

        _refresh_batch(
            pipeline, _batch(4, after_close), start=LAST_SESSION, end=LAST_SESSION
        )

        assert _row(tmp_db, LAST_SESSION) is None
        landed = _row(tmp_db, next_session)
        assert landed is not None and landed.post_count == 4

    def test_history_alone_writes_nothing(self, tmp_app_config, tmp_db):
        """Stored posts are evidence, never scope. A refresh that fetched
        nothing new must not resurrect rows for sessions it learned nothing
        about -- that was the 'fabricated freshness' failure, where a static
        post set produced a fresh-looking row for every session in range."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        stored = _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        DataIngestor(tmp_app_config, tmp_db)._persist_posts(stored, IngestReport())

        written = pipeline.build_sentiment(
            posts=[],
            directory=_directory(SYMBOL),
            history=load_stored_posts(
                tmp_db, since=session_close_utc(PREV_SESSION) - dt.timedelta(days=30)
            ),
            start=PREV_SESSION,
            end=LAST_SESSION,
        )

        assert written == 0
        assert _all_rows(tmp_db) == []

    def test_a_restated_post_wins_over_its_stored_copy(self, tmp_app_config, tmp_db):
        """Deduped by ``(source, external_id)`` with the FRESH copy winning:
        a re-fetched post carries updated engagement, and the persist path
        deliberately never rewrites an existing row, so the stored copy is
        always the staler one. Counting it twice would also be an easy way
        to invent a mention surge out of one post."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        when = session_close_utc(LAST_SESSION) - dt.timedelta(hours=2)
        stale = _post(0, when)
        stale.score = 1
        DataIngestor(tmp_app_config, tmp_db)._persist_posts([stale], IngestReport())

        restated = _post(0, when)
        restated.score = 9_000
        _refresh_batch(pipeline, [restated], start=LAST_SESSION, end=LAST_SESSION)

        row = _row(tmp_db, LAST_SESSION)
        assert row.post_count == 1  # one post, not one per copy
        assert row.total_engagement == pytest.approx(9_000.0)


def _all_rows(db: Database) -> list[SymbolSentimentDaily]:
    with db.read_session() as session:
        return list(session.execute(select(SymbolSentimentDaily)).scalars().all())


# --------------------------------------------------------------------------
# The loader stays non-destructive
# --------------------------------------------------------------------------


class TestTheSharedLoaderDeletesNothing:
    def test_reading_stored_history_leaves_mentions_and_aggregates_alone(
        self, tmp_app_config, tmp_db
    ):
        """The read path was extracted from ``rebuild_sentiment`` precisely
        because that function DELETES every ticker mention and every
        aggregate in its window first. Calling it per refresh would have been
        a data-loss bug; this pins that the extracted half destroys nothing."""
        pipeline = Pipeline(tmp_app_config, tmp_db)
        posts = _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        ingestor = DataIngestor(tmp_app_config, tmp_db)
        ingestor._persist_posts(posts, IngestReport())
        ingestor.resolve_and_persist_mentions(posts, _directory(SYMBOL), IngestReport())
        _refresh_batch(pipeline, posts, start=PREV_SESSION, end=PREV_SESSION)

        with tmp_db.read_session() as session:
            mentions_before = len(
                session.execute(select(TickerMentionRow)).scalars().all()
            )
        assert mentions_before > 0

        loaded = load_stored_posts(
            tmp_db, since=session_close_utc(PREV_SESSION) - dt.timedelta(days=30)
        )

        assert len(loaded) == 3
        with tmp_db.read_session() as session:
            assert (
                len(session.execute(select(TickerMentionRow)).scalars().all())
                == mentions_before
            )
        assert _row(tmp_db, PREV_SESSION) is not None

    def test_the_loader_still_works_after_a_rebuild_cleared_the_mentions(
        self, tmp_app_config, tmp_db
    ):
        """Why stored mentions are NOT used to narrow the read: a rebuild
        clears ``ticker_mentions`` wholesale while the posts survive. A
        loader that filtered on that table would return nothing here and the
        rolling baseline would vanish with no error anywhere."""
        posts = _batch(3, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        DataIngestor(tmp_app_config, tmp_db)._persist_posts(posts, IngestReport())
        with tmp_db.session() as session:
            session.query(TickerMentionRow).delete()  # what a rebuild does

        loaded = load_stored_posts(
            tmp_db, since=session_close_utc(PREV_SESSION) - dt.timedelta(days=30)
        )

        assert len(loaded) == 3

    def test_the_read_window_is_bounded(self, tmp_app_config, tmp_db):
        """An unbounded read of ``social_posts`` is the largest scan in the
        database, and a refresh does it every run."""
        old = _post(99, session_close_utc(PREV_SESSION) - dt.timedelta(days=400))
        recent = _post(1, session_close_utc(PREV_SESSION) - dt.timedelta(hours=2))
        DataIngestor(tmp_app_config, tmp_db)._persist_posts([old, recent], IngestReport())

        loaded = Pipeline(tmp_app_config, tmp_db)._stored_history_for([recent])

        assert {p.external_id for p in loaded} == {recent.external_id}


# --------------------------------------------------------------------------
# Collection coverage
# --------------------------------------------------------------------------


class TestCollectionCoverageIsRecorded:
    def test_a_refresh_records_which_sessions_it_covered(
        self, tmp_app_config, tmp_db, monkeypatch
    ):
        """Wired into the real refresh, and recorded even though the fetch
        returned nothing -- 'we ran and found silence' is the confirmed zero
        the whole table exists to make expressible."""
        pipeline = _pipeline_with_stub_ingest(
            tmp_app_config, tmp_db, monkeypatch, posts=[]
        )

        pipeline.refresh(
            start=LAST_SESSION - dt.timedelta(days=5),
            end=LAST_SESSION,
            social_lookback_hours=72,
        )

        with tmp_db.read_session() as session:
            rows = session.execute(select(SocialCoverageRow)).scalars().all()
        assert rows, "a successful collection that found nothing must still be recorded"
        assert {r.source for r in rows} == {"reddit"}
        assert all(r.status == "ok" and r.posts_collected == 0 for r in rows)
        # Never a session that has not closed yet -- see
        # ``sessions_covered_by_fetch``.
        assert all(session_close_utc(r.session) <= utc_now() for r in rows)

    def test_a_failed_source_is_recorded_as_an_outage_not_as_silence(
        self, tmp_app_config, tmp_db, monkeypatch
    ):
        pipeline = _pipeline_with_stub_ingest(
            tmp_app_config,
            tmp_db,
            monkeypatch,
            posts=[],
            failures={"reddit": "429 rate limited"},
        )

        pipeline.refresh(
            start=LAST_SESSION - dt.timedelta(days=5),
            end=LAST_SESSION,
            social_lookback_hours=72,
        )

        with tmp_db.read_session() as session:
            rows = session.execute(select(SocialCoverageRow)).scalars().all()
        assert rows and all(r.status == "failed" for r in rows)
        assert all("429" in r.error for r in rows)

    def test_a_later_failure_never_erases_an_earlier_success(
        self, tmp_app_config, tmp_db
    ):
        """The session genuinely WAS collected. Downgrading it would delete
        real coverage and turn an honest zero back into missing data."""
        record_collection_coverage(
            tmp_db,
            sessions=[LAST_SESSION],
            outcomes=[SourceCollection(source="reddit", ok=True)],
        )
        record_collection_coverage(
            tmp_db,
            sessions=[LAST_SESSION],
            outcomes=[SourceCollection(source="reddit", ok=False, error="down")],
        )

        assert coverage_window(
            tmp_db, start=LAST_SESSION, end=LAST_SESSION
        ).is_collected(LAST_SESSION)

    def test_recording_the_same_run_twice_adds_nothing(self, tmp_app_config, tmp_db):
        posts = _batch(4, session_close_utc(LAST_SESSION) - dt.timedelta(hours=2))
        for _ in range(2):
            record_collection_coverage(
                tmp_db,
                sessions=[LAST_SESSION],
                outcomes=[SourceCollection(source="reddit", ok=True)],
                posts=posts,
            )

        with tmp_db.read_session() as session:
            rows = session.execute(select(SocialCoverageRow)).scalars().all()
        assert len(rows) == 1
        assert rows[0].posts_collected == 4  # max, never a running sum

    def test_a_still_open_session_is_not_claimed_as_collected(self):
        """Its information is incomplete until it closes; claiming it as a
        fully collected zero would depress every baseline containing it."""
        close = session_close_utc(LAST_SESSION)

        covered = sessions_covered_by_fetch(
            close - dt.timedelta(days=3), close - dt.timedelta(hours=1)
        )

        assert LAST_SESSION not in covered

    def test_a_long_lookback_recovers_the_sessions_an_outage_skipped(self):
        """The ~72h provider lookback is what makes a missed day self-heal:
        the next successful refresh reaches back past both missed closes."""
        close = session_close_utc(LAST_SESSION)

        covered = sessions_covered_by_fetch(
            close - dt.timedelta(days=3), close + dt.timedelta(hours=2)
        )

        assert LAST_SESSION in covered and PREV_SESSION in covered


def _pipeline_with_stub_ingest(
    config,
    db,
    monkeypatch,
    *,
    posts: list[SocialPost],
    failures: dict[str, str] | None = None,
) -> Pipeline:
    """A pipeline whose ingest is canned, so ``refresh``'s own wiring is what
    is under test rather than the providers'."""
    import claudetrade.pipeline as pipeline_module

    class _StubIngestor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_full_refresh(self, **_kwargs) -> IngestReport:
            report = IngestReport()
            report.posts = list(posts)
            report.provider_failures.update(failures or {})
            DataIngestor(config, db)._persist_posts(list(posts), IngestReport())
            return report

    class _StubSocial:
        name = "reddit"
        source = SocialSource.REDDIT

    class _StubMarket:
        name = "stub"

        def list_universe(self):
            return [SecurityInfo(symbol=SYMBOL, name=f"{SYMBOL} Inc")]

    pipeline = Pipeline(config, db)
    pipeline.market = _StubMarket()
    pipeline.social = [_StubSocial()]
    pipeline.attention = []
    pipeline.adanos = []
    monkeypatch.setattr(pipeline_module, "DataIngestor", _StubIngestor)
    return pipeline


# --------------------------------------------------------------------------
# Coverage-aware baselines
# --------------------------------------------------------------------------

#: A Friday, so trading-day arithmetic is unambiguous.
AS_OF = dt.date(2026, 7, 31)


def _screen_sessions() -> list[dt.date]:
    """The 23 sessions a default 3-recent/20-baseline screen looks at."""
    return trading_day_range(AS_OF - dt.timedelta(days=45), AS_OF)[-23:]


def _seed_aggregates(db: Database, rows: list[tuple[str, dt.date, int]]) -> None:
    with db.session() as session:
        for symbol in {r[0] for r in rows}:
            session.merge(Security(symbol=symbol, name=f"{symbol} Inc"))
        for symbol, session_date, count in rows:
            session.add(
                SymbolSentimentDaily(
                    symbol=symbol,
                    session=session_date,
                    source="all",
                    post_count=count,
                    raw_sentiment=0.2,
                    confidence=0.6,
                )
            )


def _mark_collected(db: Database, sessions: list[dt.date]) -> None:
    record_collection_coverage(
        db, sessions=sessions, outcomes=[SourceCollection(source="reddit", ok=True)]
    )


class TestCoverageAwareBaseline:
    def test_a_collection_outage_does_not_inflate_mention_change(self, memory_db):
        """The defect a reviewer caught: the baseline divided mentions by the
        sessions in the window rather than by the sessions actually
        COLLECTED. Here nothing changes -- the symbol is mentioned at a flat
        rate throughout -- but half the baseline was never collected. Divided
        by elapsed sessions that flat rate reads as a doubling; divided by
        collected sessions it correctly reads as no change at all."""
        sessions = _screen_sessions()
        baseline, recent = sessions[:-3], sessions[-3:]
        collected = baseline[:10]  # the other 10 baseline sessions were an outage
        _seed_aggregates(
            memory_db, [("FLAT", s, 10) for s in collected + recent]
        )
        _mark_collected(memory_db, collected + recent)

        [trend] = rising_symbols(memory_db, as_of=AS_OF, min_recent_mentions=5)

        assert trend.symbol == "FLAT"
        assert trend.baseline_sessions_covered == 10
        assert trend.baseline_rate == pytest.approx(10.0)
        assert trend.mention_change == pytest.approx(0.0), (
            "a flat mention rate across a collection gap is not a surge; dividing by "
            "elapsed rather than collected sessions is what invented one"
        )
        assert trend.baseline_fully_covered is False

    def test_a_real_surge_still_ranks_when_coverage_is_complete(self, memory_db):
        """The correction must not blunt the screen: with every session
        collected the denominators are unchanged and a genuine surge is
        still a surge."""
        sessions = _screen_sessions()
        rows = [("QUIET", s, 1) for s in sessions[:-3]]
        rows += [("QUIET", s, 40) for s in sessions[-3:]]
        _seed_aggregates(memory_db, rows)
        _mark_collected(memory_db, sessions)

        [trend] = rising_symbols(memory_db, as_of=AS_OF)

        assert trend.baseline_fully_covered is True
        assert trend.baseline_sessions_covered == 20
        assert trend.mention_change > 5

    def test_a_baseline_too_thin_to_trust_is_dropped_rather_than_ranked(
        self, memory_db
    ):
        """Correct arithmetic over two surviving sessions is still a two-day
        'normal' that any ordinary day beats. Ranking on it fills the screen
        with outage artefacts, so the honest answer is nothing at all."""
        sessions = _screen_sessions()
        collected = sessions[:2] + sessions[-3:]
        _seed_aggregates(memory_db, [("THIN", s, 10) for s in collected])
        _mark_collected(memory_db, collected)

        assert rising_symbols(memory_db, as_of=AS_OF) == []
        # ...and the caller can still ask for it explicitly.
        assert rising_symbols(memory_db, as_of=AS_OF, min_baseline_sessions=1) != []

    def test_a_deliberately_short_baseline_is_not_treated_as_an_outage(
        self, memory_db
    ):
        """Asking for a 2-session baseline is a different question, not a
        collection gap. The floor caps at the window actually requested, or
        every such call would answer 'nothing is rising' forever."""
        sessions = _screen_sessions()
        wanted = sessions[-5:]
        _seed_aggregates(memory_db, [("SHORT", s, 10) for s in wanted])
        _mark_collected(memory_db, wanted)

        ranked = rising_symbols(
            memory_db, as_of=AS_OF, recent_sessions=3, baseline_sessions=2
        )

        assert [t.symbol for t in ranked] == ["SHORT"]
        assert ranked[0].baseline_sessions_covered == 2

    def test_history_predating_coverage_tracking_keeps_its_baseline(self, memory_db):
        """Coverage recording started when the feature shipped. An
        installation upgraded yesterday has months of good aggregates and no
        coverage rows behind them; reading that as 'never collected' would
        void every baseline overnight -- far worse than the bug being
        fixed."""
        sessions = _screen_sessions()
        rows = [("OLD", s, 1) for s in sessions[:-3]]
        rows += [("OLD", s, 40) for s in sessions[-3:]]
        _seed_aggregates(memory_db, rows)
        _mark_collected(memory_db, sessions[-3:])  # tracking begins today

        [trend] = rising_symbols(memory_db, as_of=AS_OF)

        assert trend.baseline_sessions_covered == 20  # pre-tracking = assumed collected
        assert trend.mention_change > 5


class TestConfirmedZeroVersusNotCollected:
    def test_the_two_kinds_of_zero_are_distinguishable_in_a_series(self, memory_db):
        """A session with no stored row is 0 mentions either way. Only
        coverage says whether that zero is evidence."""
        sessions = _screen_sessions()
        collected_but_silent, never_collected = sessions[-2], sessions[-1]
        _mark_collected(memory_db, [collected_but_silent])

        points = {
            p.session: p
            for p in symbol_series(memory_db, "NVDA", as_of=AS_OF, days=10).points
        }

        assert points[collected_but_silent].mentions == 0
        assert points[collected_but_silent].collected is True  # confirmed zero
        assert points[never_collected].mentions == 0
        assert points[never_collected].collected is False  # no evidence at all

    def test_the_summary_separates_collected_sessions_from_sessions_with_data(
        self, memory_db
    ):
        """``sessions_with_data`` cannot tell an outage from silence -- both
        are the absence of a row. The coverage counters can, which is the
        whole reason both are reported."""
        sessions = _screen_sessions()
        window = trading_day_range(AS_OF - dt.timedelta(days=30), AS_OF)
        # Tracking starts 10 sessions back, then a 6-session outage, then the
        # last three sessions collected again.
        collected = [sessions[-10], *sessions[-3:]]
        _seed_aggregates(memory_db, [("NVDA", sessions[-1], 7)])
        _mark_collected(memory_db, collected)

        summary = coverage_summary(memory_db, as_of=AS_OF, days=30)

        # One session produced an aggregate row; six were never collected at
        # all. Neither number can be derived from the other.
        assert summary["sessions_with_data"] == 1
        assert summary["sessions_not_collected"] == 6
        assert summary["sessions_collected"] == len(window) - 6
        assert summary["collection_tracked"] is True
        assert summary["collection_tracked_from"] == sessions[-10].isoformat()
        assert summary["collection_sources"] == ["reddit"]

    def test_the_longest_outage_is_reported(self, memory_db):
        """One number an operator can act on: 'how long were we blind?'."""
        sessions = _screen_sessions()
        collected = [sessions[-9], *sessions[-3:]]  # a 5-session hole in between
        _mark_collected(memory_db, collected)

        summary = coverage_summary(memory_db, as_of=AS_OF, days=30)

        assert summary["max_consecutive_gap"] == 5
        assert sessions[-4].isoformat() in summary["uncollected_sessions"]

    def test_an_untracked_database_reports_no_manufactured_gaps(self, memory_db):
        """Nothing recorded means nothing is known, not that everything was
        missed."""
        summary = coverage_summary(memory_db, as_of=AS_OF, days=30)

        assert summary["collection_tracked"] is False
        assert summary["sessions_not_collected"] == 0
        assert summary["max_consecutive_gap"] == 0
