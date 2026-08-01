"""Cross-process refresh state and single-flight lock (QA handoff v3, F27).

The CLI, the web API server and the MCP server each bootstrap their own
``Pipeline`` (one process, one SQLite connection pool). Refresh progress used
to live only in a per-process ``webapi.refresh_state.RefreshState``, so a
refresh started from one entry point was invisible to the other two -- QA
observed MCP ``get_refresh_status`` reporting idle while a CLI refresh was
actively writing -- and nothing stopped a second concurrent refresh from
racing the first one's writes on the same database file. This module makes
the ``refresh_runs`` table (``db.models.RefreshRunRow``) the cross-process
truth:

* :func:`try_acquire` -- atomically claim the single refresh slot. At most one
  ``status='running'`` row can exist (partial unique index, migration 005);
  a losing racer gets the constraint violation and reports the holder instead
  of starting a duplicate. A "running" row whose heartbeat has gone stale
  (owner process crashed/killed) is taken over and marked failed rather than
  blocking refreshes forever.
* :class:`RefreshRunHandle` -- heartbeat/progress writes (throttled to about
  one write per :data:`HEARTBEAT_MIN_INTERVAL_S`, so wiring it into the
  per-symbol progress callback cannot turn the refresh into a write storm)
  and terminal :meth:`~RefreshRunHandle.finish`.
* :func:`merged_status` -- the DB row merged with a process's in-memory
  ``RefreshState`` snapshot, so every status surface reports a refresh run by
  ANY entry point while keeping the local run's finer-grained live detail.

Every operation here opens its own short transaction and commits immediately
-- this module must never hold a lock that a multi-minute refresh (or another
process's reads) could queue behind; that failure mode is exactly what F26
was about.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from claudetrade.db.models import RefreshRunRow
from claudetrade.db.session import Database
from claudetrade.logging_setup import get_logger
from claudetrade.utils.timeutils import utc_now

log = get_logger(__name__)

#: A "running" row whose heartbeat is older than this is presumed dead (its
#: process crashed or was killed mid-refresh) and may be taken over. Progress
#: writes land at least every ``HEARTBEAT_MIN_INTERVAL_S`` while a refresh is
#: genuinely alive, so 120s of silence is orders of magnitude past normal --
#: while still short enough that an operator who kills a stuck refresh is not
#: locked out for long.
STALE_AFTER_SECONDS = 120.0

#: Floor between consecutive progress/heartbeat writes for one handle. The
#: per-symbol progress callback can fire hundreds of times a minute; the DB
#: row only needs to prove liveness and show coarse progress, not mirror
#: every tick -- and each skipped write is one less writer contending with
#: the refresh's own bulk inserts.
HEARTBEAT_MIN_INTERVAL_S = 2.0


@dataclass(slots=True, frozen=True)
class RefreshRunInfo:
    """Detached snapshot of one ``refresh_runs`` row."""

    id: int
    entry_point: str
    status: str
    phase: str
    symbols_done: int
    symbols_total: int
    started_at: Any
    heartbeat_at: Any
    finished_at: Any
    last_error: str | None
    #: True when ``status='running'`` but the heartbeat is past the stale
    #: threshold -- the run is presumed dead, not actively refreshing.
    stale: bool

    def describe(self) -> str:
        """Operator-facing one-liner naming the holder, for refusal messages."""
        started = self.started_at.isoformat() if self.started_at else "unknown"
        progress = (
            f"{self.symbols_done}/{self.symbols_total}"
            if self.symbols_total
            else "starting"
        )
        return (
            f"a refresh started by the {self.entry_point} entry point is already running "
            f"(started {started}, phase {self.phase!r}, progress {progress})"
        )


def _snapshot(row: RefreshRunRow, *, stale_after_s: float) -> RefreshRunInfo:
    heartbeat = row.heartbeat_at
    stale = False
    if row.status == "running" and heartbeat is not None:
        # SQLite hands back naive datetimes; stored values are UTC by
        # construction (same rationale as ``signals.ledger._row_to_utc``).
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=dt.UTC)
        stale = (utc_now() - heartbeat).total_seconds() > stale_after_s
    return RefreshRunInfo(
        id=row.id,
        entry_point=row.entry_point,
        status=row.status,
        phase=row.phase,
        symbols_done=row.symbols_done,
        symbols_total=row.symbols_total,
        started_at=row.started_at,
        heartbeat_at=row.heartbeat_at,
        finished_at=row.finished_at,
        last_error=row.last_error,
        stale=stale,
    )


def current_run(db: Database, *, stale_after_s: float = STALE_AFTER_SECONDS) -> RefreshRunInfo | None:
    """The current ``status='running'`` row (stale or not), or ``None``."""
    with db.read_session() as session:
        row = session.execute(
            select(RefreshRunRow)
            .where(RefreshRunRow.status == "running")
            .order_by(RefreshRunRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _snapshot(row, stale_after_s=stale_after_s) if row else None


class RefreshRunHandle:
    """Write-side handle for the run this process acquired.

    ``update_progress`` matches ``data.ingest.ProgressCallback``'s
    ``(phase, done, total)`` shape so entry points can pass it (or compose it
    with their in-process ``RefreshState.update_progress``) straight into
    ``Pipeline.refresh``. Both write methods swallow their own database
    errors: a refresh must never die because its *progress reporting* hit a
    transient "database is locked" while the refresh itself is fine.
    """

    def __init__(self, db: Database, run_id: int):
        self.db = db
        self.run_id = run_id
        self._last_write = 0.0  # monotonic; 0 forces the first write through

    def update_progress(self, phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._last_write < HEARTBEAT_MIN_INTERVAL_S:
            return
        self._last_write = now
        try:
            with self.db.session() as session:
                row = session.get(RefreshRunRow, self.run_id)
                if row is None or row.status != "running":
                    return  # taken over as stale, or already finished
                row.phase = phase
                row.symbols_done = int(done)
                row.symbols_total = int(total)
                row.heartbeat_at = utc_now()
        except Exception:
            log.debug("refresh heartbeat write failed; ignored", exc_info=True)

    def finish(self, status: str = "done", *, error: str | None = None) -> None:
        """Terminal write. Guarded so a stale takeover is never resurrected:
        if another process already marked this row failed, that verdict
        stands (this process was, from everyone else's view, dead)."""
        try:
            with self.db.session() as session:
                row = session.get(RefreshRunRow, self.run_id)
                if row is None or row.status != "running":
                    return
                row.status = status
                row.last_error = error
                row.finished_at = utc_now()
                row.heartbeat_at = utc_now()
        except Exception:
            log.warning("refresh finish write failed; run %s left running", self.run_id,
                        exc_info=True)


@dataclass(slots=True, frozen=True)
class AcquireOutcome:
    """Result of :func:`try_acquire`: the handle when acquired, else the holder."""

    acquired: bool
    handle: RefreshRunHandle | None = None
    holder: RefreshRunInfo | None = None


def try_acquire(
    db: Database,
    entry_point: str,
    *,
    stale_after_s: float = STALE_AFTER_SECONDS,
) -> AcquireOutcome:
    """Claim the single cross-process refresh slot, or report who holds it.

    Three short transactions at most (check, optional stale takeover,
    insert); the atomicity against a concurrent acquirer comes from the
    partial unique index on ``status='running'`` (migration 005), not from
    the check -- the check only exists to produce a good refusal message and
    to take over stale rows. A racer that slips between check and insert
    loses at INSERT time with a constraint violation and is refused with the
    winner's details.
    """
    holder = current_run(db, stale_after_s=stale_after_s)
    if holder is not None:
        if not holder.stale:
            return AcquireOutcome(acquired=False, holder=holder)
        # Presumed-dead run: mark it failed (guarded on still-running so two
        # concurrent takeovers are idempotent) and fall through to insert.
        with db.session() as session:
            row = session.get(RefreshRunRow, holder.id)
            if row is not None and row.status == "running":
                row.status = "failed"
                row.last_error = "stale lock taken over"
                row.finished_at = utc_now()
        log.warning(
            "took over stale refresh run %d (entry_point=%s, last heartbeat %s)",
            holder.id, holder.entry_point, holder.heartbeat_at,
        )

    try:
        with db.session() as session:
            row = RefreshRunRow(
                entry_point=entry_point,
                status="running",
                phase="starting",
                started_at=utc_now(),
                heartbeat_at=utc_now(),
            )
            session.add(row)
            session.flush()  # assigns row.id; the unique index rejects a loser here
            run_id = row.id
    except IntegrityError:
        # Lost the race: another process committed its running row between
        # the check above and this insert. That process IS the refresh now.
        winner = current_run(db, stale_after_s=stale_after_s)
        return AcquireOutcome(acquired=False, holder=winner)
    except OperationalError:
        # busy_timeout expired acquiring the write lock -- the database is
        # under heavy write load, which in this application means a refresh
        # is almost certainly the writer. Refusing (with whatever holder row
        # is readable) is strictly safer than letting the caller retry into
        # a duplicate refresh.
        log.warning("refresh lock insert timed out; refusing to start", exc_info=True)
        winner = current_run(db, stale_after_s=stale_after_s)
        return AcquireOutcome(acquired=False, holder=winner)

    return AcquireOutcome(acquired=True, handle=RefreshRunHandle(db, run_id))


def merged_status(
    db: Database,
    local: dict[str, Any],
    local_entry_point: str,
    *,
    stale_after_s: float = STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Merge the cross-process DB row into a process-local status snapshot.

    Precedence, per F27's acceptance check ("a CLI-run refresh is visible
    from MCP"):

    * local run in progress -> the in-process snapshot wins for the live
      progress fields (it updates on every callback tick, the DB row only
      every ~2s), tagged ``source='local'``.
    * fresh remote running row -> its fields override the local (idle)
      snapshot, tagged ``source='db'`` and naming the owning entry point.
    * stale running row -> reported as NOT running (the owner is presumed
      dead) with the abandoned run's details under ``stale_run`` -- honest,
      rather than a "running" that never ends.
    * nothing in the DB -> the local snapshot as-is, tagged ``source='idle'``
      when idle.

    Never raises: status surfaces must answer even when the database is
    briefly unreadable mid-refresh -- degraded (local-only) beats a 500.
    """
    out: dict[str, Any] = dict(local)
    out.setdefault("entry_point", None)
    out["source"] = "local" if local.get("running") else "idle"

    if local.get("running"):
        out["entry_point"] = local_entry_point
        return out

    try:
        run = current_run(db, stale_after_s=stale_after_s)
    except Exception:
        log.debug("cross-process refresh status read failed; local-only", exc_info=True)
        return out

    if run is None:
        return out
    if run.stale:
        out["stale_run"] = {
            "entry_point": run.entry_point,
            "phase": run.phase,
            "started_at": run.started_at,
            "heartbeat_at": run.heartbeat_at,
            "note": "refresh run stopped heartbeating; its process likely died mid-run",
        }
        return out

    out.update(
        running=True,
        phase=run.phase,
        symbols_done=run.symbols_done,
        symbols_total=run.symbols_total,
        started_at=run.started_at,
        finished_at=None,
        last_error=run.last_error,
        entry_point=run.entry_point,
        heartbeat_at=run.heartbeat_at,
        source="db",
    )
    return out


__all__ = [
    "HEARTBEAT_MIN_INTERVAL_S",
    "STALE_AFTER_SECONDS",
    "AcquireOutcome",
    "RefreshRunHandle",
    "RefreshRunInfo",
    "current_run",
    "merged_status",
    "try_acquire",
]
