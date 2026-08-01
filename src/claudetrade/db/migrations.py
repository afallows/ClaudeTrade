"""Versioned schema migrations.

A deliberately small runner rather than Alembic (ADR-0004): the schema ships
with the application, migrations are linear and forward-only, and a single
self-contained runner keeps the PyInstaller bundle simple. Each migration is a
numbered callable; applied versions are recorded in ``schema_version`` and the
runner is idempotent, so ``migrate()`` is safe to call at every start-up.

Adding a migration:

1. Append a ``Migration`` to ``MIGRATIONS`` with the next version number.
2. Never edit an already-released migration -- add a new one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from claudetrade.db.models import Base, SchemaVersion
from claudetrade.db.session import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Session], None]
    description: str = ""


# --------------------------------------------------------------------------
# Migration bodies
# --------------------------------------------------------------------------


def _m001_create_schema(session: Session) -> None:
    """Create all ORM-declared tables and indexes."""
    Base.metadata.create_all(session.get_bind())


def _m002_immutability_triggers(session: Session) -> None:
    """Make the signal ledger append-only at the storage layer.

    Application code already refuses to mutate signals, but a trigger means an
    operator poking at the database with a SQL client cannot quietly delete a
    losing signal and improve the reported win/loss ratio.

    SQLite only; on PostgreSQL the equivalent is a rule or a revoked DELETE
    grant, which is documented in ``docs/architecture.md`` rather than applied
    here (it needs role management outside this tool's remit).
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        log.info("immutability triggers skipped on %s; see docs/architecture.md", bind.dialect.name)
        return

    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS trg_signals_no_update
        BEFORE UPDATE ON signals
        BEGIN
            SELECT RAISE(ABORT,
                'signals are immutable: append a row to signal_revisions instead');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_signals_no_delete
        BEFORE DELETE ON signals
        BEGIN
            SELECT RAISE(ABORT,
                'signals are immutable: deleting signals would corrupt performance history');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_signal_revisions_no_update
        BEFORE UPDATE ON signal_revisions
        BEGIN
            SELECT RAISE(ABORT, 'signal revisions are append-only');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_signal_revisions_no_delete
        BEFORE DELETE ON signal_revisions
        BEGIN
            SELECT RAISE(ABORT, 'signal revisions are append-only');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit log is append-only');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit log is append-only');
        END;
        """,
        # A closed paper trade may not be reopened or have its outcome rewritten.
        """
        CREATE TRIGGER IF NOT EXISTS trg_paper_trade_no_reopen
        BEFORE UPDATE ON paper_trades
        WHEN OLD.exit_session IS NOT NULL
         AND (NEW.exit_session IS NULL
              OR NEW.exit_price IS NOT OLD.exit_price
              OR NEW.outcome IS NOT OLD.outcome)
        BEGIN
            SELECT RAISE(ABORT,
                'closed paper trades are final: outcome and exit cannot be rewritten');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_paper_trade_no_delete_closed
        BEFORE DELETE ON paper_trades
        WHEN OLD.exit_session IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT,
                'closed paper trades cannot be deleted: history must stay complete');
        END;
        """,
    ]
    for stmt in statements:
        session.execute(text(stmt))


def _m003_performance_indexes(session: Session) -> None:
    """Indexes that only matter once the tables have real volume."""
    bind = session.get_bind()
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_bars_session_symbol ON price_bars (session, symbol)",
        "CREATE INDEX IF NOT EXISTS ix_mentions_symbol_conf ON ticker_mentions (symbol, confidence)",
        "CREATE INDEX IF NOT EXISTS ix_bt_trades_outcome ON backtest_trades (run_id, outcome)",
        "CREATE INDEX IF NOT EXISTS ix_paper_trades_outcome ON paper_trades (outcome, exit_session)",
    ]
    for stmt in statements:
        try:
            session.execute(text(stmt))
        except Exception as exc:  # pragma: no cover - dialect variance
            log.warning("index creation skipped (%s): %s", bind.dialect.name, exc)


def _m004_add_flair_column(session: Session) -> None:
    """Add the nullable ``social_posts.flair`` column.

    A brand-new database created via ``_m001_create_schema`` already has this
    column -- ``Base.metadata.create_all`` always reflects the *current* ORM
    model, ``flair`` included -- so this migration only does real work when
    applied to a database that reached version 3 before the column existed.
    Guarded by an inspector check so it is a no-op either way (fresh schema
    or already-migrated database), matching the idempotence ``migrate()``
    promises everywhere else.
    """
    bind = session.get_bind()
    insp = inspect(bind)
    if "social_posts" not in insp.get_table_names():
        return
    existing_columns = {col["name"] for col in insp.get_columns("social_posts")}
    if "flair" in existing_columns:
        return
    session.execute(text("ALTER TABLE social_posts ADD COLUMN flair VARCHAR(80)"))


def _m005_refresh_runs(session: Session) -> None:
    """Create the cross-process refresh-run table plus its single-flight guard.

    A brand-new database already has ``refresh_runs`` from
    ``_m001_create_schema`` (``create_all`` reflects the current ORM model),
    so the targeted ``create_all`` here only does real work on a database
    that reached version 4 before the table existed -- same idempotence
    posture as ``_m004_add_flair_column``.

    The partial unique index is the actual concurrency mechanism (see
    ``db.refresh_state_store.try_acquire``): at most one ``status='running'``
    row may exist, so of two processes racing to start a refresh exactly one
    INSERT succeeds and the loser gets a constraint violation it can report
    -- an atomicity guarantee a check-then-insert alone cannot give under
    SQLite's deferred write locking. The identical statement is valid on
    PostgreSQL; any dialect that rejects it degrades to check-then-insert
    (a small race window, logged) rather than failing the migration.
    """
    from claudetrade.db.models import RefreshRunRow

    bind = session.get_bind()
    RefreshRunRow.metadata.create_all(bind, tables=[RefreshRunRow.__table__])
    try:
        session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_refresh_running "
                "ON refresh_runs (status) WHERE status = 'running'"
            )
        )
    except Exception as exc:  # pragma: no cover - dialect variance
        log.warning(
            "partial unique index for refresh_runs skipped (%s): %s", bind.dialect.name, exc
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "create_schema", _m001_create_schema, "Initial tables, indexes and constraints"),
    Migration(2, "immutability_triggers", _m002_immutability_triggers, "Append-only ledger guards"),
    Migration(3, "performance_indexes", _m003_performance_indexes, "Query indexes for scale"),
    Migration(4, "add_flair_column", _m004_add_flair_column, "Nullable social_posts.flair column"),
    Migration(5, "refresh_runs", _m005_refresh_runs, "Cross-process refresh state + lock (F27)"),
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def current_version(db: Database) -> int:
    """Highest applied migration version, or 0 on a fresh database."""
    insp = inspect(db.engine)
    if "schema_version" not in insp.get_table_names():
        return 0
    with db.read_session() as session:
        rows = session.execute(select(SchemaVersion.version)).scalars().all()
    return max(rows) if rows else 0


def migrate(db: Database, *, target: int | None = None) -> list[int]:
    """Apply outstanding migrations in order.

    Args:
        db: Database handle.
        target: Highest version to apply; defaults to the latest.

    Returns:
        The versions applied by this call (empty when already up to date).
    """
    target = LATEST_VERSION if target is None else target
    applied: list[int] = []
    start = current_version(db)
    if start >= target:
        log.debug("database schema already at version %d", start)
        return applied

    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= start or migration.version > target:
            continue
        log.info("applying migration %03d %s", migration.version, migration.name)
        with db.session() as session:
            migration.apply(session)
            session.add(
                SchemaVersion(
                    version=migration.version,
                    name=migration.name,
                    checksum=f"{migration.version:03d}:{migration.name}",
                )
            )
        applied.append(migration.version)
    return applied


def verify_schema(db: Database) -> list[str]:
    """Report tables the ORM expects but the database lacks."""
    insp = inspect(db.engine)
    present = set(insp.get_table_names())
    expected = set(Base.metadata.tables)
    return sorted(expected - present)


def init_database(db: Database) -> list[int]:
    """Convenience: migrate to latest and assert the schema is complete."""
    applied = migrate(db)
    missing = verify_schema(db)
    if missing:
        raise RuntimeError(f"schema incomplete after migration; missing tables: {missing}")
    return applied
