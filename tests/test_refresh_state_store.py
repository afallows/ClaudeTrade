"""Cross-process refresh state + single-flight lock (``db.refresh_state_store``).

QA handoff v3, F27: refresh progress lived only in a per-process dataclass,
so whichever entry point (CLI / web API / MCP) did not start a refresh was
blind to it -- and free to start a second concurrent one. These tests
exercise the DB-backed truth the fix introduces, including the exact QA
acceptance shape: a run acquired through ONE ``Database`` handle must be
visible (and blocking) through a SECOND handle on the same file, which is
what two separate processes look like to SQLite.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.db import refresh_state_store as store
from claudetrade.db.migrations import init_database
from claudetrade.db.models import RefreshRunRow
from claudetrade.db.session import Database
from claudetrade.utils.timeutils import utc_now


@pytest.fixture
def second_db(tmp_app_config, tmp_db) -> Database:
    """A second handle on the SAME database file ``tmp_db`` uses -- the
    closest a single test process can get to a second OS process."""
    db_path = tmp_app_config.paths.app_dir / "test.db"
    db = Database(f"sqlite:///{db_path}", config=tmp_app_config)
    yield db
    db.dispose()


def _idle_snapshot() -> dict[str, object]:
    """Shape of ``RefreshState.snapshot()`` for an idle process."""
    return {
        "running": False,
        "phase": "idle",
        "symbols_done": 0,
        "symbols_total": 0,
        "started_at": None,
        "finished_at": None,
        "last_error": None,
    }


# --------------------------------------------------------------------------
# try_acquire / finish
# --------------------------------------------------------------------------


def test_acquire_on_a_fresh_database(tmp_db: Database) -> None:
    outcome = store.try_acquire(tmp_db, "cli")
    assert outcome.acquired
    assert outcome.handle is not None

    run = store.current_run(tmp_db)
    assert run is not None
    assert run.entry_point == "cli"
    assert run.status == "running"
    assert run.stale is False


def test_second_acquire_is_refused_and_names_the_holder(tmp_db: Database) -> None:
    first = store.try_acquire(tmp_db, "cli")
    assert first.acquired
    first.handle.update_progress("prices", 120, 2400)

    second = store.try_acquire(tmp_db, "mcp")
    assert not second.acquired
    assert second.handle is None
    holder = second.holder
    assert holder is not None
    assert holder.entry_point == "cli"
    message = holder.describe()
    assert "cli" in message
    assert "120/2400" in message
    assert "prices" in message
    assert holder.started_at is not None


def test_acquire_is_blocked_across_database_handles(tmp_db: Database, second_db: Database) -> None:
    """The cross-process case itself: handle A (the 'CLI') holds the run;
    handle B (the 'MCP server') must be refused."""
    assert store.try_acquire(tmp_db, "cli").acquired

    outcome = store.try_acquire(second_db, "mcp")
    assert not outcome.acquired
    assert outcome.holder is not None
    assert outcome.holder.entry_point == "cli"


def test_finish_releases_the_slot(tmp_db: Database) -> None:
    outcome = store.try_acquire(tmp_db, "webapi")
    outcome.handle.finish("done")

    assert store.current_run(tmp_db) is None
    again = store.try_acquire(tmp_db, "cli")
    assert again.acquired


def test_finish_failed_records_the_error(tmp_db: Database) -> None:
    outcome = store.try_acquire(tmp_db, "cli")
    outcome.handle.finish("failed", error="boom")

    with tmp_db.read_session() as session:
        row = session.get(RefreshRunRow, outcome.handle.run_id)
        assert row.status == "failed"
        assert row.last_error == "boom"
        assert row.finished_at is not None


def test_stale_running_row_is_taken_over_and_marked_failed(tmp_db: Database) -> None:
    """A holder whose process died stops heartbeating; the next acquirer must
    take the lock over rather than being blocked forever."""
    dead = store.try_acquire(tmp_db, "cli")
    stale_instant = utc_now() - dt.timedelta(seconds=store.STALE_AFTER_SECONDS + 60)
    with tmp_db.session() as session:
        session.get(RefreshRunRow, dead.handle.run_id).heartbeat_at = stale_instant

    outcome = store.try_acquire(tmp_db, "mcp")
    assert outcome.acquired

    with tmp_db.read_session() as session:
        old = session.get(RefreshRunRow, dead.handle.run_id)
        assert old.status == "failed"
        assert old.last_error == "stale lock taken over"
    run = store.current_run(tmp_db)
    assert run.entry_point == "mcp"


def test_finish_never_resurrects_a_taken_over_run(tmp_db: Database) -> None:
    """The dead-looking process may in fact still be alive and eventually call
    ``finish`` -- the takeover verdict must stand."""
    dead = store.try_acquire(tmp_db, "cli")
    with tmp_db.session() as session:
        session.get(RefreshRunRow, dead.handle.run_id).heartbeat_at = utc_now() - dt.timedelta(
            seconds=store.STALE_AFTER_SECONDS + 60
        )
    assert store.try_acquire(tmp_db, "mcp").acquired

    dead.handle.finish("done")

    with tmp_db.read_session() as session:
        old = session.get(RefreshRunRow, dead.handle.run_id)
        assert old.status == "failed"
        assert old.last_error == "stale lock taken over"


def test_unique_index_refuses_a_racer_that_slipped_past_the_check(
    tmp_db: Database, monkeypatch
) -> None:
    """Atomicity does not rest on the check-then-insert: with the check
    blinded (simulating the race window), the partial unique index rejects
    the second running row and the racer is refused, not doubled."""
    assert store.try_acquire(tmp_db, "cli").acquired

    real_current_run = store.current_run
    monkeypatch.setattr(store, "current_run", lambda db, **kw: None)
    outcome = store.try_acquire(tmp_db, "mcp")
    monkeypatch.setattr(store, "current_run", real_current_run)

    assert not outcome.acquired
    with tmp_db.read_session() as session:
        from sqlalchemy import func, select

        running = session.execute(
            select(func.count()).select_from(RefreshRunRow).where(RefreshRunRow.status == "running")
        ).scalar()
    assert running == 1


# --------------------------------------------------------------------------
# update_progress (heartbeat)
# --------------------------------------------------------------------------


def test_update_progress_writes_and_heartbeats(tmp_db: Database) -> None:
    outcome = store.try_acquire(tmp_db, "cli")
    before = store.current_run(tmp_db).heartbeat_at

    outcome.handle._last_write = 0.0  # bypass the throttle for determinism
    outcome.handle.update_progress("securities", 5, 100)

    run = store.current_run(tmp_db)
    assert run.phase == "securities"
    assert run.symbols_done == 5
    assert run.symbols_total == 100
    assert run.heartbeat_at >= before


def test_update_progress_is_throttled(tmp_db: Database) -> None:
    """Per-symbol callbacks can fire hundreds of times a minute; only about
    one write per HEARTBEAT_MIN_INTERVAL_S may reach the database."""
    outcome = store.try_acquire(tmp_db, "cli")
    handle = outcome.handle
    handle._last_write = 0.0
    handle.update_progress("prices", 1, 100)  # lands (throttle bypassed above)
    handle.update_progress("prices", 2, 100)  # inside the interval -- skipped
    handle.update_progress("prices", 3, 100)  # skipped too

    run = store.current_run(tmp_db)
    assert run.symbols_done == 1


def test_update_progress_survives_database_errors(tmp_db: Database) -> None:
    """A hiccuping heartbeat write must never raise into the refresh."""
    outcome = store.try_acquire(tmp_db, "cli")
    handle = outcome.handle
    handle.db = None  # type: ignore[assignment] -- force the write to blow up internally
    handle._last_write = 0.0
    handle.update_progress("prices", 1, 10)  # must not raise


# --------------------------------------------------------------------------
# merged_status -- the F27 acceptance surface
# --------------------------------------------------------------------------


def test_merged_status_reports_a_remote_run(tmp_db: Database, second_db: Database) -> None:
    """THE QA acceptance check: a CLI-run refresh (handle A) must be visible
    through another process's status surface (handle B, local state idle)."""
    outcome = store.try_acquire(tmp_db, "cli")
    outcome.handle._last_write = 0.0
    outcome.handle.update_progress("prices", 7, 40)

    merged = store.merged_status(second_db, _idle_snapshot(), "mcp")
    assert merged["running"] is True
    assert merged["entry_point"] == "cli"
    assert merged["phase"] == "prices"
    assert merged["symbols_done"] == 7
    assert merged["symbols_total"] == 40
    assert merged["started_at"] is not None
    assert merged["source"] == "db"


def test_merged_status_prefers_local_detail_for_a_local_run(tmp_db: Database) -> None:
    store.try_acquire(tmp_db, "webapi")
    local = {
        "running": True,
        "phase": "sentiment",
        "symbols_done": 33,
        "symbols_total": 40,
        "started_at": utc_now(),
        "finished_at": None,
        "last_error": None,
    }
    merged = store.merged_status(tmp_db, local, "webapi")
    assert merged["source"] == "local"
    assert merged["entry_point"] == "webapi"
    assert merged["phase"] == "sentiment"
    assert merged["symbols_done"] == 33


def test_merged_status_idle_when_nothing_is_running(tmp_db: Database) -> None:
    merged = store.merged_status(tmp_db, _idle_snapshot(), "mcp")
    assert merged["running"] is False
    assert merged["phase"] == "idle"
    assert merged["entry_point"] is None
    assert merged["source"] == "idle"


def test_merged_status_reports_a_stale_run_as_not_running(tmp_db: Database) -> None:
    """An abandoned row (dead process) must not read as an eternal refresh."""
    dead = store.try_acquire(tmp_db, "cli")
    with tmp_db.session() as session:
        session.get(RefreshRunRow, dead.handle.run_id).heartbeat_at = utc_now() - dt.timedelta(
            seconds=store.STALE_AFTER_SECONDS + 60
        )

    merged = store.merged_status(tmp_db, _idle_snapshot(), "mcp")
    assert merged["running"] is False
    assert merged["stale_run"]["entry_point"] == "cli"
    assert "died" in merged["stale_run"]["note"]


def test_refresh_runs_table_exists_after_init(tmp_app_config, tmp_path) -> None:
    """Migration 005 wires the table into ``init_database`` on a fresh file."""
    from sqlalchemy import inspect

    db = Database(f"sqlite:///{tmp_path}/fresh-init.db", config=tmp_app_config)
    try:
        init_database(db)
        names = set(inspect(db.engine).get_table_names())
        assert "refresh_runs" in names
        indexes = {ix["name"] for ix in inspect(db.engine).get_indexes("refresh_runs")}
        assert "uq_refresh_running" in indexes
    finally:
        db.dispose()
