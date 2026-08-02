"""Hourly social/attention collection, and how ready the resulting history is.

**Why this exists at all.** Every other data source in this application can be
backfilled: price bars, corporate actions and earnings all have history
endpoints, so a gap is an inconvenience. The social sources have none. Reddit
serves ``/new`` and stops paging after a few hundred posts; X's recent-search
window is days, not months; ApeWisdom publishes a *rolling current 24h*
snapshot with no history endpoint at all. The 120-session baseline this
application's premise needs -- "is this name being talked about more than is
normal *for it*" -- therefore cannot be fetched. It can only be accumulated
forward, one collection at a time, and an hour that passes without a
collection is an hour permanently missing from that baseline. That is the
whole justification for running a loop inside the server process rather than
telling the operator to set up cron.

**What one tick does, and deliberately does not.** A tick calls
``Pipeline.collect_social`` -- social posts, ticker-mention resolution,
daily-sentiment aggregation, and the ApeWisdom attention snapshot. It never
runs the market pass. A full ``Pipeline.refresh`` spends ~20 rate-limit-bound
minutes on per-symbol price/earnings fetches for data that changes once a day;
doing that hourly would exhaust the market provider's budget to re-fetch bars
that have not moved, and would make the cheap-but-perishable half hostage to
the expensive-but-recoverable half.

**Single-flight, cross-process.** The tick acquires the same
``db.refresh_state_store`` lock (the ``refresh_runs`` table, migration 005)
that the CLI, the web API's ``POST /api/system/refresh`` and the MCP server's
``trigger_refresh`` acquire, under its own ``entry_point`` value
(:data:`SCHEDULER_ENTRY_POINT`). Two consequences, both intended:

* A scheduled collection can never race an operator-triggered refresh's
  writes. When the lock is held it **skips** -- logged, counted, and gone.
  It is not queued: an hour's collection that could not run has already
  missed the window it was for, and running two of them back to back would
  just fetch the same posts twice.
* A scheduled run is visible everywhere a refresh is, with
  ``entry_point == "scheduler"`` naming it, through
  ``GET /api/system/refresh/status`` and ``mcp_server.get_refresh_status``
  -- see :func:`is_scheduled_run`. A background job nobody can see is a
  background job nobody trusts.

Crash safety comes from the same place: the lock row heartbeats through the
ordinary progress callback, and a holder whose process died is taken over once
its heartbeat goes stale, so a killed server never wedges future collections.
The one gap worth naming is the social *fetch* itself, which reports no
progress while it is talking to the network (the same is true of
``run_full_refresh``); a fetch longer than ``STALE_AFTER_SECONDS`` can have its
lock taken over by another process, after which this tick's ``finish`` is a
guarded no-op and the run is reported as failed. That is honest -- from every
other process's point of view it *had* stopped heartbeating -- and it costs a
collection, not correctness.

**Readiness is a label, never a gate.** The second half of this module answers
"how much should I trust a trend?" from real stored coverage
(``sentiment.history.coverage_summary``), and answers it in tiers --
``warming_up`` / ``provisional`` / ``partial`` / ``ready``. Nothing anywhere
is blocked by a tier. A hard 120-session gate was explicitly rejected:
attention inflection is a days-to-two-weeks phenomenon, and a baseline only
has to establish what normal chatter looks like for a name, which twenty
sessions already does roughly and sixty does well. The tier rides along with
the results so the operator knows how much weight to put on them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import TYPE_CHECKING, Any

from claudetrade.config import AppConfig
from claudetrade.db import refresh_state_store
from claudetrade.logging_setup import get_logger
from claudetrade.utils.timeutils import current_trading_session, to_display, utc_now

if TYPE_CHECKING:
    from claudetrade.db.session import Database
    from claudetrade.pipeline import Pipeline

log = get_logger(__name__)

#: ``refresh_runs.entry_point`` value for a collection this loop started.
#: Distinct from ``"cli"`` / ``"webapi"`` / ``"mcp"`` precisely so an operator
#: reading a status payload can tell "the app did this on its own" from "I
#: asked for this", which are very different things to see mid-session.
SCHEDULER_ENTRY_POINT = "scheduler"

#: Delay before the FIRST collection after start-up. Short on purpose: the
#: point of collecting hourly is that short sessions still contribute, so
#: waiting a full interval before the first tick would mean an app opened for
#: forty minutes gathered nothing. Not zero, because ``scripts/setup`` starts
#: the server and immediately triggers a full refresh against it -- one minute
#: lets that grab the lock first, so the first tick skips cleanly instead of
#: losing a race it was never meant to enter.
STARTUP_DELAY_SECONDS = 60.0

#: Cap on the doubling applied per consecutive failure, before the configured
#: ceiling is applied. Bounds the shift arithmetic; the real limit is
#: ``SchedulerConfig.social_collection_max_backoff_minutes``.
MAX_BACKOFF_DOUBLINGS = 10


# --------------------------------------------------------------------------
# Readiness tiers
# --------------------------------------------------------------------------

#: Tier name -> minimum sessions of collected history, richest first. Read in
#: order; the first threshold met wins. These are the owner's SOFTENED model:
#: a label, not a gate.
READINESS_THRESHOLDS: tuple[tuple[str, int], ...] = (
    ("ready", 120),
    ("partial", 60),
    ("provisional", 20),
)

#: What everything below the lowest threshold is called.
WARMING_UP = "warming_up"

#: Calendar days ``coverage_summary`` is asked about when computing a tier.
#: 400 calendar days is ~275 trading sessions, comfortably past the 120-session
#: top tier, so a mature installation is never labelled ``partial`` merely
#: because the question was asked over too short a window.
READINESS_WINDOW_DAYS = 400


def readiness_tier(sessions_collected: int) -> str:
    """Tier name for a session count. Pure; the thresholds live in one place."""
    for name, minimum in READINESS_THRESHOLDS:
        if sessions_collected >= minimum:
            return name
    return WARMING_UP


def _next_tier(sessions_collected: int) -> tuple[str | None, int | None]:
    """The next tier up and how many more sessions reach it, or ``(None, None)``."""
    for name, minimum in reversed(READINESS_THRESHOLDS):
        if sessions_collected < minimum:
            return name, minimum - sessions_collected
    return None, None


def collection_readiness(
    db: Database,
    config: AppConfig | None = None,
    *,
    as_of: dt.date | None = None,
    window_days: int = READINESS_WINDOW_DAYS,
) -> dict[str, Any]:
    """How much collected history exists, as a tier -- computed, never asserted.

    Every number here is read back out of the database
    (``sentiment.history.coverage_summary``) rather than inferred from "how
    long has this been installed" or "how many refreshes did we start". A
    refresh that ran and stored nothing must not raise the tier, and the only
    way to guarantee that is to count stored sessions.

    ``degraded_sources`` names the social/attention providers that are
    configured but currently unavailable (omitted entirely without a
    ``config``), because a ``ready`` tier assembled while Reddit was blocked
    for the last month means something different from one assembled with every
    source healthy.

    Takes a ``Database`` rather than a ``Pipeline`` on purpose: this is
    embedded in status surfaces that already hold one, and it must stay usable
    from a caller that has no business constructing providers.

    Never raises: a status endpoint that 500s because a diagnostic sub-read
    failed is strictly worse than one that reports what it could.
    """
    session_date = as_of or current_trading_session()
    coverage: dict[str, Any] = {}
    try:
        from claudetrade.sentiment.history import coverage_summary

        coverage = dict(coverage_summary(db, as_of=session_date, days=window_days))
    except Exception:
        log.debug("readiness coverage read failed; reporting unknown", exc_info=True)

    sessions = int(coverage.get("sessions_with_data") or 0)
    tier = readiness_tier(sessions)
    next_name, sessions_to_next = _next_tier(sessions)

    degraded: list[str] = []
    if config is not None:
        try:
            from claudetrade.providers.registry import provider_status_report

            degraded = sorted(
                status.name
                for status in provider_status_report(config)
                if status.kind in {"social", "attention"}
                and status.configured
                and not status.available
            )
        except Exception:
            log.debug("readiness provider probe failed; reporting none", exc_info=True)

    return {
        "tier": tier,
        "sessions_collected": sessions,
        "thresholds": dict(READINESS_THRESHOLDS),
        "next_tier": next_name,
        "sessions_to_next_tier": sessions_to_next,
        "earliest_session": coverage.get("earliest_session"),
        "latest_session": coverage.get("latest_session"),
        "symbols_with_history": coverage.get("symbols_with_history"),
        "symbols_with_attention_data": coverage.get("symbols_with_attention_data"),
        "window_days": window_days,
        "as_of": session_date.isoformat(),
        "degraded_sources": degraded,
        # Stated in the payload rather than only in the docs: the tier is
        # advisory, and every consumer of this block should behave that way.
        "blocking": False,
        "note": _READINESS_NOTES[tier],
    }


_READINESS_NOTES: dict[str, str] = {
    WARMING_UP: (
        "Fewer than 20 sessions of collected social history. Trends are computable but "
        "have almost no baseline to stand against; read them as anecdotes. Nothing is "
        "blocked -- history only accumulates forward, so leave the app open."
    ),
    "provisional": (
        "20+ sessions collected: enough to know roughly what normal chatter looks like "
        "for an active name, not enough to trust a marginal one. Nothing is blocked."
    ),
    "partial": (
        "60+ sessions collected: baselines are meaningful for most names that get "
        "discussed at all. Nothing is blocked."
    ),
    "ready": (
        "120+ sessions collected: the full baseline this application's premise assumes. "
        "Quiet names now have a real 'normal' to be measured against."
    ),
}


def is_scheduled_run(status_payload: dict[str, Any]) -> bool:
    """Whether a refresh-status payload describes an automatic collection.

    One definition, shared by the web API and the MCP server, so the two
    surfaces can never disagree about whether the operator started this.
    """
    return status_payload.get("entry_point") == SCHEDULER_ENTRY_POINT


# --------------------------------------------------------------------------
# The collector
# --------------------------------------------------------------------------


class SocialCollectionScheduler:
    """Runs ``Pipeline.collect_social`` on a timer for as long as the app runs.

    Owns exactly three concerns -- when to fire, whether it may fire, and
    never dying -- and delegates the actual work to the pipeline.

    Injection points exist for testing only: ``sleep``, ``jitter``, ``clock``
    and ``to_thread`` are replaced in tests so scheduling *decisions* can be
    asserted without any wall-clock time passing or any thread-scheduling
    race. Production always gets ``asyncio.sleep`` / ``random.random`` /
    ``utc_now`` / ``asyncio.to_thread``.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        config: AppConfig,
        *,
        sleep: Any = None,
        jitter: Any = None,
        clock: Any = None,
        to_thread: Any = None,
    ) -> None:
        self.pipeline = pipeline
        self.config = config
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random
        self._clock = clock or utc_now
        self._to_thread = to_thread or asyncio.to_thread
        self._task: asyncio.Task[None] | None = None

        self.ticks = 0
        self.collections = 0
        self.skips = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.last_tick_at: dt.datetime | None = None
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_outcome: dict[str, Any] | None = None
        self.next_run_at: dt.datetime | None = None

    # --- configuration views ---------------------------------------------

    @property
    def settings(self):
        return self.config.scheduler

    @property
    def enabled(self) -> bool:
        return bool(self.settings.social_collection_enabled)

    @property
    def lookback_hours(self) -> int:
        return max(1, int(self.settings.social_collection_lookback_hours))

    def interval_minutes(self, now: dt.datetime | None = None) -> int:
        """Cadence right now, honouring the optional quieter overnight window.

        The overnight window is half-open ``[start, end)`` in the configured
        display timezone and may wrap past midnight (22 -> 04 is the default
        shape). Zero overnight minutes -- the default -- means one cadence
        around the clock, which is the honest default for sources that never
        close.
        """
        base = max(1, int(self.settings.social_collection_interval_minutes))
        overnight = int(self.settings.social_collection_overnight_interval_minutes)
        if overnight <= 0:
            return base
        instant = now or self._clock()
        try:
            hour = to_display(instant, self.settings.timezone).hour
        except Exception:
            log.debug("overnight window timezone conversion failed; using UTC", exc_info=True)
            hour = instant.hour
        start = int(self.settings.social_collection_overnight_start_hour) % 24
        end = int(self.settings.social_collection_overnight_end_hour) % 24
        if start == end:
            return base  # a zero-width window is not an all-day window
        # The wrapping branch (22 -> 04) is the normal shape, not the edge case.
        inside = start <= hour < end if start < end else (hour >= start or hour < end)
        return max(1, overnight) if inside else base

    # --- one tick ---------------------------------------------------------

    def run_tick(self, *, entry_point: str = SCHEDULER_ENTRY_POINT) -> dict[str, Any]:
        """Collect once, synchronously. Blocking -- callers off the event loop.

        Also the on-demand path behind ``claudetrade sentiment collect``, which
        is why ``entry_point`` is a parameter: the same body, honestly
        labelled as operator-triggered when a human asked for it.

        Returns a status dict and never raises: a collection is best-effort by
        construction, and the loop above must not have to reason about
        exceptions to stay alive.
        """
        started = self._clock()
        outcome = refresh_state_store.try_acquire(self.pipeline.db, entry_point)
        if not outcome.acquired:
            holder = outcome.holder
            reason = (
                holder.describe() if holder else "another process holds the refresh lock"
            )
            # INFO, not WARNING: skipping because a refresh is running is the
            # single-flight rule working, not a fault. It is logged because a
            # silent skip is indistinguishable from a dead scheduler.
            log.info("social collection skipped -- %s", reason)
            return {
                "status": "skipped",
                "reason": reason,
                "entry_point": entry_point,
                "started_at": started,
                "finished_at": self._clock(),
            }

        handle = outcome.handle
        if handle is None:  # pragma: no cover - acquired always carries a handle
            log.error("refresh lock acquired without a handle; skipping this collection")
            return {
                "status": "failed",
                "error": "refresh lock acquired without a handle",
                "entry_point": entry_point,
                "started_at": started,
                "finished_at": self._clock(),
            }
        try:
            result = self.pipeline.collect_social(
                lookback_hours=self.lookback_hours,
                progress_callback=handle.update_progress,
            )
        except Exception as exc:
            handle.finish("failed", error=str(exc))
            log.exception("social collection failed")
            return {
                "status": "failed",
                "error": str(exc),
                "entry_point": entry_point,
                "started_at": started,
                "finished_at": self._clock(),
            }
        handle.finish("done")

        report = result.ingest
        return {
            "status": "collected",
            "entry_point": entry_point,
            "started_at": started,
            "finished_at": self._clock(),
            "lookback_hours": self.lookback_hours,
            "posts_fetched": len(report.posts) if report else 0,
            "posts_stored": report.posts_inserted if report else 0,
            "mentions_stored": report.mentions_inserted if report else 0,
            "sentiment_rows": result.sentiment_rows,
            "degraded_sources": dict(result.degraded_sources),
            "warnings": list(result.warnings),
        }

    async def tick(self) -> dict[str, Any]:
        """One tick, off the event loop, with the outcome recorded.

        ``run_tick`` blocks on network and SQLite for as long as the providers
        take; running it on the loop thread would stall every HTTP request the
        server is serving, so it goes to a worker thread. The broad ``except``
        is the last line of defence behind ``run_tick``'s own: a scheduler
        that dies silently is worse than no scheduler, so nothing short of
        cancellation is allowed out of here.
        """
        try:
            outcome = await self._to_thread(self.run_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - run_tick already catches
            log.exception("social collection tick raised; the loop continues")
            outcome = {
                "status": "failed",
                "error": str(exc),
                "entry_point": SCHEDULER_ENTRY_POINT,
                "finished_at": self._clock(),
            }
        self._record(outcome)
        return outcome

    def _record(self, outcome: dict[str, Any]) -> None:
        self.ticks += 1
        status = str(outcome.get("status") or "unknown")
        self.last_status = status
        self.last_tick_at = outcome.get("finished_at") or self._clock()
        self.last_outcome = outcome
        if status == "collected":
            self.collections += 1
            self.consecutive_failures = 0
            self.last_error = None
        elif status == "skipped":
            # A skip is a healthy outcome, not a failure: it means the
            # single-flight lock did its job. Backing off after one would
            # punish the app for the operator running a refresh.
            self.skips += 1
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = str(outcome.get("error") or status)

    # --- the loop ---------------------------------------------------------

    def _next_delay(self) -> float:
        """Seconds to wait before the next tick, including jitter and back-off."""
        base = self.interval_minutes() * 60.0
        if self.consecutive_failures:
            doublings = min(self.consecutive_failures, MAX_BACKOFF_DOUBLINGS)
            ceiling = max(1, int(self.settings.social_collection_max_backoff_minutes)) * 60.0
            base = min(base * (2**doublings), ceiling)
        jitter = max(0, int(self.settings.social_collection_jitter_seconds))
        return base + self._jitter() * jitter

    async def run(self) -> None:
        """Sleep/collect forever until cancelled. Never returns on its own."""
        log.info(
            "hourly social collection started: every %d min (+ up to %ds jitter), "
            "%dh lookback -- social and attention only, never the market pass",
            self.interval_minutes(),
            max(0, int(self.settings.social_collection_jitter_seconds)),
            self.lookback_hours,
        )
        delay = STARTUP_DELAY_SECONDS + self._jitter() * max(
            0, int(self.settings.social_collection_jitter_seconds)
        )
        try:
            while True:
                self.next_run_at = self._clock() + dt.timedelta(seconds=delay)
                await self._sleep(delay)
                await self.tick()
                delay = self._next_delay()
        except asyncio.CancelledError:
            log.info(
                "hourly social collection stopping (%d tick(s): %d collected, %d skipped, "
                "%d failed)",
                self.ticks, self.collections, self.skips, self.failures,
            )
            self.next_run_at = None
            raise

    def start(self) -> asyncio.Task[None] | None:
        """Create the loop task on the running event loop.

        Returns ``None`` (and logs why) when collection is switched off, so
        the caller never has to duplicate the enabled check.
        """
        if not self.enabled:
            log.info(
                "hourly social collection is disabled (scheduler.social_collection_enabled "
                "= false); social history will only advance when a refresh is run by hand, "
                "and hours the app was open but idle are not recoverable"
            )
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run(), name="claudetrade-social-collection")
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and wait for it to unwind. Idempotent.

        Awaited rather than fire-and-forget so shutdown cannot race a tick
        into a database handle the process is about to dispose.
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - run() only exits by cancellation
            log.exception("social collection loop exited with an error during shutdown")

    # --- introspection ----------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Loop state for the status surfaces. Cheap; touches no database."""
        return {
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "entry_point": SCHEDULER_ENTRY_POINT,
            "interval_minutes": self.interval_minutes(),
            "lookback_hours": self.lookback_hours,
            "ticks": self.ticks,
            "collections": self.collections,
            "skips": self.skips,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_tick_at": self.last_tick_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "next_run_at": self.next_run_at,
        }


__all__ = [
    "MAX_BACKOFF_DOUBLINGS",
    "READINESS_THRESHOLDS",
    "READINESS_WINDOW_DAYS",
    "SCHEDULER_ENTRY_POINT",
    "STARTUP_DELAY_SECONDS",
    "WARMING_UP",
    "SocialCollectionScheduler",
    "collection_readiness",
    "is_scheduled_run",
    "readiness_tier",
]
