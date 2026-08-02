"""Hourly social collection and tiered readiness (``claudetrade.scheduler``).

The failure this feature exists to prevent is silent and unrecoverable: the
social sources have no history endpoints, so an hour the app was open but not
collecting is an hour permanently missing from every baseline. That makes the
scheduling *decisions* the thing worth pinning -- does a tick run, does it
stand down when something else holds the lock, does one bad tick kill the
loop, does shutdown stop it cleanly -- rather than any wall-clock behaviour.

Nothing here sleeps for real and nothing here touches the network. The clock,
the sleep, the jitter and the thread offload are all injected, so every loop
test is fully deterministic: a test that proved the scheduler works by waiting
an hour would never be run, and one that proved it by polling a real worker
thread would be flaky for reasons that have nothing to do with scheduling.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from claudetrade.config import AppConfig
from claudetrade.db import refresh_state_store
from claudetrade.db.migrations import init_database
from claudetrade.db.models import RefreshRunRow, Security, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.pipeline import PipelineResult
from claudetrade.providers.base import ProviderStatus
from claudetrade.scheduler import (
    READINESS_THRESHOLDS,
    SCHEDULER_ENTRY_POINT,
    WARMING_UP,
    SocialCollectionScheduler,
    collection_readiness,
    is_scheduled_run,
    readiness_tier,
)

#: A Friday, so trading-day arithmetic in these tests is unambiguous.
AS_OF = dt.date(2026, 7, 31)


class StubPipeline:
    """The narrowest thing the scheduler actually uses: ``db`` + ``collect_social``.

    Deliberately not a real ``Pipeline``: constructing one wires up every
    configured provider, and this suite is about scheduling, not ingestion.
    """

    def __init__(self, db: Database, *, raises: Exception | None = None) -> None:
        self.db = db
        self.calls: list[dict[str, Any]] = []
        self.raises = raises
        self.result = PipelineResult(sentiment_rows=3)

    def collect_social(self, *, lookback_hours: int, progress_callback=None):
        self.calls.append({"lookback_hours": lookback_hours})
        if progress_callback is not None:
            progress_callback("social", 0, 3)
        if self.raises is not None:
            raise self.raises
        return self.result


async def _inline(fn, *args, **kwargs):
    """Stand-in for ``asyncio.to_thread`` that runs the body right here.

    Removes real thread scheduling from every loop assertion; the offload
    itself is pinned separately by
    ``test_production_offloads_the_blocking_body_to_a_thread``.
    """
    return fn(*args, **kwargs)


@pytest.fixture
def db() -> Database:
    database = Database("sqlite:///:memory:")
    init_database(database)
    yield database
    database.dispose()


@pytest.fixture
def config(tmp_app_config: AppConfig) -> AppConfig:
    return tmp_app_config


def _scheduler(pipeline, config: AppConfig, **kwargs) -> SocialCollectionScheduler:
    """A scheduler whose time never advances unless a test says so."""
    kwargs.setdefault("jitter", lambda: 0.0)
    kwargs.setdefault("clock", lambda: dt.datetime(2026, 7, 31, 14, 0, tzinfo=dt.UTC))
    kwargs.setdefault("to_thread", _inline)
    return SocialCollectionScheduler(pipeline, config, **kwargs)


# --------------------------------------------------------------------------
# One tick
# --------------------------------------------------------------------------


class TestOneTick:
    def test_a_tick_runs_a_collection_and_takes_the_lock(self, db, config) -> None:
        pipeline = StubPipeline(db)
        outcome = _scheduler(pipeline, config).run_tick()

        assert outcome["status"] == "collected"
        assert pipeline.calls == [
            {"lookback_hours": config.scheduler.social_collection_lookback_hours}
        ]
        assert outcome["sentiment_rows"] == 3

    def test_a_scheduled_tick_is_recorded_under_its_own_entry_point(self, db, config) -> None:
        """``entry_point`` is what tells an operator the app did this by
        itself -- the whole reason the scheduler does not reuse "webapi"."""
        _scheduler(StubPipeline(db), config).run_tick()

        with db.read_session() as session:
            rows = session.query(RefreshRunRow).all()
            entry_points = [r.entry_point for r in rows]
            statuses = [r.status for r in rows]

        assert entry_points == [SCHEDULER_ENTRY_POINT]
        assert statuses == ["done"]

    def test_the_lock_is_released_so_the_next_tick_can_run(self, db, config) -> None:
        scheduler = _scheduler(StubPipeline(db), config)
        assert scheduler.run_tick()["status"] == "collected"
        assert scheduler.run_tick()["status"] == "collected"

    def test_a_tick_skips_when_the_lock_is_held(self, db, config) -> None:
        """Single-flight: a manual refresh running anywhere means this tick
        stands down. Skipped, logged, and gone -- never queued, because the
        window it was collecting for has already passed."""
        held = refresh_state_store.try_acquire(db, "cli")
        assert held.acquired

        pipeline = StubPipeline(db)
        outcome = _scheduler(pipeline, config).run_tick()

        assert outcome["status"] == "skipped"
        assert "cli" in outcome["reason"]
        assert pipeline.calls == []

    def test_a_skip_leaves_the_holders_run_untouched(self, db, config) -> None:
        refresh_state_store.try_acquire(db, "cli")
        _scheduler(StubPipeline(db), config).run_tick()

        run = refresh_state_store.current_run(db)
        assert run is not None
        assert run.entry_point == "cli"

    def test_a_failing_collection_finishes_the_run_as_failed(self, db, config) -> None:
        """A crash must release the lock, or one bad tick wedges every future
        collection AND every manual refresh until the row goes stale."""
        pipeline = StubPipeline(db, raises=RuntimeError("provider exploded"))
        outcome = _scheduler(pipeline, config).run_tick()

        assert outcome["status"] == "failed"
        assert "provider exploded" in outcome["error"]
        assert refresh_state_store.current_run(db) is None

        # And the next tick starts cleanly rather than skipping forever.
        pipeline.raises = None
        assert _scheduler(pipeline, config).run_tick()["status"] == "collected"

    def test_an_on_demand_tick_is_labelled_as_operator_triggered(self, db, config) -> None:
        """``claudetrade sentiment collect`` runs the same body; the record
        must not claim the app decided to do it."""
        outcome = _scheduler(StubPipeline(db), config).run_tick(entry_point="cli")

        assert outcome["entry_point"] == "cli"
        assert is_scheduled_run(outcome) is False

    def test_a_scheduled_run_is_distinguishable_in_the_merged_status(
        self, db, config
    ) -> None:
        """The acceptance shape: while a scheduled collection holds the lock,
        every status surface must say so rather than implying a person did
        it. ``merged_status`` is what both the web API and MCP report."""
        outcome = refresh_state_store.try_acquire(db, SCHEDULER_ENTRY_POINT)
        outcome.handle.update_progress("social", 0, 3)

        status = refresh_state_store.merged_status(db, _idle_snapshot(), "webapi")

        assert status["running"] is True
        assert status["entry_point"] == SCHEDULER_ENTRY_POINT
        assert is_scheduled_run(status) is True

        outcome.handle.finish("done")
        assert is_scheduled_run(
            refresh_state_store.merged_status(db, _idle_snapshot(), "webapi")
        ) is False

    def test_production_offloads_the_blocking_body_to_a_thread(self, db, config) -> None:
        """The default must be a real offload: ``run_tick`` blocks on network
        and SQLite, and running that on the loop thread would stall every HTTP
        request the server is serving."""
        scheduler = SocialCollectionScheduler(StubPipeline(db), config)

        assert scheduler._to_thread is asyncio.to_thread


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class GatedSleep:
    """Stand-in for ``asyncio.sleep`` that hands out ticks one at a time.

    Records every requested delay (so back-off and jitter are assertable) and
    parks the loop until the test explicitly ``release``s it. Nothing waits on
    real time, and the test controls exactly how many ticks have happened at
    each point -- which is what lets "the loop survived a failing tick" be
    asserted as a *sequence* rather than a race.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._credits = 0
        self._gate = asyncio.Event()

    def release(self, ticks: int = 1) -> None:
        self._credits += ticks
        self._gate.set()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        while self._credits <= 0:
            self._gate.clear()
            await self._gate.wait()  # cancelled at shutdown
        self._credits -= 1


class TestTheLoop:
    def test_the_loop_collects_once_per_interval(self, db, config) -> None:
        pipeline = StubPipeline(db)
        sleep = GatedSleep()
        scheduler = _scheduler(pipeline, config, sleep=sleep)

        async def scenario():
            task = scheduler.start()
            sleep.release(3)
            await _settle()
            await scheduler.stop()
            return task

        task = asyncio.run(scenario())

        assert len(pipeline.calls) == 3
        assert scheduler.collections == 3
        assert task.done()

    def test_an_exception_in_one_tick_does_not_kill_the_loop(self, db, config) -> None:
        """The property that matters most: a scheduler that dies silently is
        worse than no scheduler, because the operator keeps believing history
        is accumulating."""
        pipeline = StubPipeline(db, raises=RuntimeError("transient"))
        sleep = GatedSleep()
        scheduler = _scheduler(pipeline, config, sleep=sleep)

        async def scenario():
            scheduler.start()
            sleep.release(1)
            await _settle()
            assert scheduler.failures == 1  # the tick did fail...
            pipeline.raises = None
            sleep.release(2)
            await _settle()  # ...and the loop was still there to try again
            await scheduler.stop()

        asyncio.run(scenario())

        assert scheduler.ticks == 3
        assert scheduler.failures == 1
        assert scheduler.collections == 2
        assert scheduler.consecutive_failures == 0

    def test_a_bug_in_run_tick_itself_does_not_kill_the_loop(self, db, config) -> None:
        """``run_tick`` promises not to raise; the loop must survive it
        breaking that promise anyway."""
        sleep = GatedSleep()
        scheduler = _scheduler(StubPipeline(db), config, sleep=sleep)
        calls = {"n": 0}

        def exploding_tick(**_kwargs):
            calls["n"] += 1
            raise ValueError("a bug, not a provider failure")

        scheduler.run_tick = exploding_tick

        async def scenario():
            scheduler.start()
            sleep.release(2)
            await _settle()
            await scheduler.stop()

        asyncio.run(scenario())

        assert calls["n"] == 2
        assert scheduler.failures == 2
        assert "a bug" in (scheduler.last_error or "")

    def test_shutdown_cancels_the_loop_cleanly(self, db, config) -> None:
        scheduler = _scheduler(StubPipeline(db), config, sleep=GatedSleep())
        observed: dict[str, Any] = {}

        async def scenario():
            task = scheduler.start()
            await _settle()
            observed["running_before"] = scheduler.state()["running"]
            await scheduler.stop()
            observed["running_after"] = scheduler.state()["running"]
            await scheduler.stop()  # idempotent
            return task

        task = asyncio.run(scenario())

        assert observed == {"running_before": True, "running_after": False}
        assert task.done()
        assert scheduler.next_run_at is None

    def test_a_disabled_scheduler_starts_nothing_but_stays_usable_on_demand(
        self, db, config
    ) -> None:
        config.scheduler.social_collection_enabled = False
        pipeline = StubPipeline(db)
        sleep = GatedSleep()
        scheduler = _scheduler(pipeline, config, sleep=sleep)

        async def scenario():
            started = scheduler.start()
            sleep.release(5)
            await _settle()
            return started

        assert asyncio.run(scenario()) is None
        assert pipeline.calls == []
        assert scheduler.state()["running"] is False
        # The CLI path deliberately does not consult the flag.
        assert scheduler.run_tick(entry_point="cli")["status"] == "collected"

    def test_the_first_tick_comes_soon_after_start_not_an_interval_later(
        self, db, config
    ) -> None:
        """An app opened for forty minutes must still contribute a collection."""
        sleep = GatedSleep()
        scheduler = _scheduler(StubPipeline(db), config, sleep=sleep)

        async def scenario():
            scheduler.start()
            sleep.release(1)
            await _settle()
            await scheduler.stop()

        asyncio.run(scenario())

        assert sleep.delays[0] < config.scheduler.social_collection_interval_minutes * 60
        # ...and the next wait is the configured cadence, not the start-up one.
        assert sleep.delays[1] == pytest.approx(
            config.scheduler.social_collection_interval_minutes * 60
        )


# --------------------------------------------------------------------------
# Cadence, jitter and back-off
# --------------------------------------------------------------------------


class TestCadence:
    def test_jitter_is_added_so_restarts_do_not_synchronise(self, db, config) -> None:
        base = config.scheduler.social_collection_interval_minutes * 60
        jitter = config.scheduler.social_collection_jitter_seconds

        earliest = _scheduler(StubPipeline(db), config, jitter=lambda: 0.0)._next_delay()
        latest = _scheduler(StubPipeline(db), config, jitter=lambda: 1.0)._next_delay()

        assert jitter > 0
        assert earliest == pytest.approx(base)
        assert latest == pytest.approx(base + jitter)

    def test_consecutive_failures_back_off_and_recovery_resets(self, db, config) -> None:
        scheduler = _scheduler(StubPipeline(db), config)
        interval_s = config.scheduler.social_collection_interval_minutes * 60

        scheduler._record({"status": "failed", "error": "down"})
        assert scheduler._next_delay() == pytest.approx(interval_s * 2)
        scheduler._record({"status": "failed", "error": "down"})
        assert scheduler._next_delay() == pytest.approx(interval_s * 4)

        scheduler._record({"status": "collected"})
        assert scheduler._next_delay() == pytest.approx(interval_s)

    def test_backoff_is_capped_so_a_bad_night_is_not_permanent(self, db, config) -> None:
        scheduler = _scheduler(StubPipeline(db), config)
        for _ in range(20):
            scheduler._record({"status": "failed", "error": "down"})

        ceiling = config.scheduler.social_collection_max_backoff_minutes * 60
        assert scheduler._next_delay() == pytest.approx(ceiling)

    def test_a_skip_is_not_treated_as_a_failure(self, db, config) -> None:
        """Backing off because the operator ran a refresh would punish the app
        for being used."""
        scheduler = _scheduler(StubPipeline(db), config)
        scheduler._record({"status": "skipped", "reason": "cli holds it"})

        assert scheduler.consecutive_failures == 0
        assert scheduler._next_delay() == pytest.approx(
            config.scheduler.social_collection_interval_minutes * 60
        )

    def test_the_default_cadence_is_the_same_around_the_clock(self, db, config) -> None:
        """Social flows 24/7; slowing down at night costs real samples, so the
        quiet window is opt-in rather than default."""
        scheduler = _scheduler(StubPipeline(db), config)

        assert config.scheduler.social_collection_overnight_interval_minutes == 0
        assert config.scheduler.social_collection_interval_minutes == 60
        for hour in (2, 9, 15, 23):
            instant = dt.datetime(2026, 7, 31, hour, tzinfo=dt.UTC)
            assert scheduler.interval_minutes(instant) == 60

    def test_the_optional_overnight_window_wraps_past_midnight(self, db, config) -> None:
        config.scheduler.social_collection_overnight_interval_minutes = 180
        config.scheduler.social_collection_overnight_start_hour = 22
        config.scheduler.social_collection_overnight_end_hour = 4
        scheduler = _scheduler(StubPipeline(db), config)

        def at_et(hour: int) -> int:
            # July, so ET is UTC-4; name the ET hour and convert back.
            instant = dt.datetime(2026, 7, 31, (hour + 4) % 24, tzinfo=dt.UTC)
            return scheduler.interval_minutes(instant)

        assert at_et(23) == 180
        assert at_et(2) == 180
        assert at_et(22) == 180  # the window is half-open [start, end)
        assert at_et(4) == 60
        assert at_et(10) == 60


# --------------------------------------------------------------------------
# Readiness tiers
# --------------------------------------------------------------------------


def _seed_sessions(db: Database, count: int, *, as_of: dt.date = AS_OF) -> None:
    """``count`` distinct sessions each carrying one stored sentiment row."""
    with db.session() as session:
        session.merge(Security(symbol="NVDA", name="NVDA Inc"))
    with db.session() as session:
        for offset in range(count):
            session.add(
                SymbolSentimentDaily(
                    symbol="NVDA",
                    session=as_of - dt.timedelta(days=offset),
                    source="all",
                    post_count=5,
                    raw_sentiment=0.2,
                )
            )


class TestReadinessTiers:
    @pytest.mark.parametrize(
        ("sessions", "expected"),
        [
            (0, WARMING_UP),
            (19, WARMING_UP),
            (20, "provisional"),
            (59, "provisional"),
            (60, "partial"),
            (119, "partial"),
            (120, "ready"),
            (500, "ready"),
        ],
    )
    def test_tier_boundaries(self, sessions: int, expected: str) -> None:
        assert readiness_tier(sessions) == expected

    def test_thresholds_are_the_owners_softened_model(self) -> None:
        assert dict(READINESS_THRESHOLDS) == {"provisional": 20, "partial": 60, "ready": 120}

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [(0, WARMING_UP), (20, "provisional"), (60, "partial"), (120, "ready")],
    )
    def test_the_tier_is_computed_from_real_stored_coverage(
        self, db, stored: int, expected: str
    ) -> None:
        """Computed, never asserted: the only thing that can raise a tier is a
        session actually landing in the database."""
        _seed_sessions(db, stored)

        readiness = collection_readiness(db, as_of=AS_OF)

        assert readiness["sessions_collected"] == stored
        assert readiness["tier"] == expected

    def test_nothing_is_blocked_at_any_tier(self, db) -> None:
        """The owner explicitly rejected a hard 120-session gate; the payload
        says so in-band so no consumer has to infer it."""
        assert collection_readiness(db, as_of=AS_OF)["blocking"] is False

    def test_the_next_tier_and_distance_to_it_are_reported(self, db) -> None:
        _seed_sessions(db, 25)

        readiness = collection_readiness(db, as_of=AS_OF)

        assert readiness["next_tier"] == "partial"
        assert readiness["sessions_to_next_tier"] == 35

    def test_a_fully_ready_installation_has_no_next_tier(self, db) -> None:
        _seed_sessions(db, 130)

        readiness = collection_readiness(db, as_of=AS_OF)

        assert readiness["next_tier"] is None
        assert readiness["sessions_to_next_tier"] is None

    def test_the_window_reaches_past_the_top_tier(self, db) -> None:
        """A 90-day window could never report 120 sessions, so the tier would
        be permanently capped below 'ready' no matter how much history exists."""
        _seed_sessions(db, 130)

        readiness = collection_readiness(db, as_of=AS_OF)

        assert readiness["window_days"] >= 200
        assert readiness["tier"] == "ready"

    def test_the_collected_session_range_is_reported(self, db) -> None:
        _seed_sessions(db, 5)

        readiness = collection_readiness(db, as_of=AS_OF)

        assert readiness["latest_session"] == AS_OF.isoformat()
        assert readiness["earliest_session"] == (AS_OF - dt.timedelta(days=4)).isoformat()

    def test_degraded_social_sources_are_named(self, db, config, monkeypatch) -> None:
        """A 'ready' tier assembled while Reddit was blocked all month means
        something different from one assembled with every source healthy."""
        import claudetrade.providers.registry as registry

        monkeypatch.setattr(
            registry,
            "provider_status_report",
            lambda _config: [
                ProviderStatus(name="reddit", kind="social", available=False, configured=True),
                ProviderStatus(name="news", kind="social", available=True, configured=True),
                ProviderStatus(
                    name="apewisdom", kind="attention", available=False, configured=True
                ),
                # Not a social source, and not configured -- neither belongs here.
                ProviderStatus(name="market", kind="market", available=False, configured=True),
                ProviderStatus(name="x", kind="social", available=False, configured=False),
            ],
        )

        readiness = collection_readiness(db, config, as_of=AS_OF)

        assert readiness["degraded_sources"] == ["apewisdom", "reddit"]

    def test_no_config_means_no_provider_probe(self, db) -> None:
        assert collection_readiness(db, as_of=AS_OF)["degraded_sources"] == []

    def test_readiness_never_raises_on_a_broken_database(self) -> None:
        """This block is embedded in status endpoints; a 500 because a
        diagnostic sub-read failed is worse than a degraded answer."""

        class BrokenDb:
            def read_session(self):
                raise RuntimeError("database is gone")

        readiness = collection_readiness(BrokenDb(), as_of=AS_OF)

        assert readiness["tier"] == WARMING_UP
        assert readiness["sessions_collected"] == 0
        assert readiness["blocking"] is False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _idle_snapshot() -> dict[str, object]:
    """Shape of ``webapi.refresh_state.RefreshState.snapshot()`` when idle."""
    return {
        "running": False,
        "phase": "idle",
        "symbols_done": 0,
        "symbols_total": 0,
        "started_at": None,
        "finished_at": None,
        "last_error": None,
    }


async def _settle() -> None:
    """Let the loop task make all the progress it can, without real time.

    Every await in the loop is either the injected sleep or the injected
    inline thread stand-in, so a bounded number of event-loop turns is enough
    to drive it to its next park -- no polling, no timeouts, no flakiness.
    """
    for _ in range(50):
        await asyncio.sleep(0)
