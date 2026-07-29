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


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "create_schema", _m001_create_schema, "Initial tables, indexes and constraints"),
    Migration(2, "immutability_triggers", _m002_immutability_triggers, "Append-only ledger guards"),
    Migration(3, "performance_indexes", _m003_performance_indexes, "Query indexes for scale"),
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
