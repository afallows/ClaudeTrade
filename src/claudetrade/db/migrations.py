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
from typing import TYPE_CHECKING

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from claudetrade.db.models import Base, SchemaVersion
from claudetrade.db.session import Database

if TYPE_CHECKING:  # annotation-only; the hook imports lazily at call time
    from claudetrade.config import AppConfig

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

def _m006_symbol_fetch_health(session: Session) -> None:
    """Create the ``symbol_fetch_health`` table (per-symbol fetch quarantine).

    Same fresh-vs-migrated shape as ``_m004_add_flair_column``: a brand-new
    database already gets this table from ``_m001_create_schema``'s
    ``create_all`` (the ORM model exists now), so this migration only does
    real work on a database that reached version 4 before the model did.
    ``checkfirst=True`` keeps it idempotent either way.
    """
    from claudetrade.db.models import SymbolFetchHealth

    SymbolFetchHealth.__table__.create(session.get_bind(), checkfirst=True)


def _m007_social_coverage(session: Session) -> None:
    """Create the ``social_coverage`` table (collected vs. never-collected).

    Same fresh-vs-migrated shape as ``_m006_symbol_fetch_health``: a
    brand-new database already has the table from ``_m001_create_schema``,
    so this only does real work on a database that reached version 6 before
    the model existed. ``checkfirst=True`` keeps it idempotent either way.

    Deliberately no backfill. Coverage cannot be reconstructed after the
    fact -- a session with no aggregate rows is exactly the case that is
    unknowable in retrospect, which is why the table exists -- so inventing
    "collected" rows for stored history would be fabricating the evidence.
    ``sentiment.store.CoverageWindow`` handles the resulting pre-tracking
    history explicitly instead: sessions before the first recorded row are
    unknown, and unknown is read as collected (the assumption every reader
    already made), so an upgraded database keeps the baselines it has.
    """
    from claudetrade.db.models import SocialCoverageRow

    SocialCoverageRow.__table__.create(session.get_bind(), checkfirst=True)


def _m008_add_sentiment_prior_column(session: Session) -> None:
    """Add the nullable ``social_posts.sentiment_prior`` column.

    Same fresh-vs-migrated posture as ``_m004_add_flair_column``: a database
    created by ``_m001_create_schema`` already reflects the current ORM model
    and has the column, so the inspector guard makes this a no-op there.

    Existing rows stay NULL and are *not* backfilled. They cannot be: the tag
    lives on the Stocktwits message payload, which we do not retain, and
    inferring a tag from the stored text would be exactly the substitution
    this field exists to avoid. NULL correctly says "we never captured
    whether this author tagged the post".
    """
    bind = session.get_bind()
    insp = inspect(bind)
    if "social_posts" not in insp.get_table_names():
        return
    existing_columns = {col["name"] for col in insp.get_columns("social_posts")}
    if "sentiment_prior" in existing_columns:
        return
    session.execute(text("ALTER TABLE social_posts ADD COLUMN sentiment_prior VARCHAR(10)"))


def _m009_signal_research_revisions(session: Session) -> None:
    """Create ``signal_research_revisions`` plus its append-only triggers.

    Same fresh-vs-migrated shape as ``_m006_symbol_fetch_health``: a
    brand-new database already gets the table from ``_m001_create_schema``
    (the ORM model exists now), so ``checkfirst=True`` makes the table
    creation a no-op there. The triggers are always (re-)installed with
    ``IF NOT EXISTS``, exactly like ``_m002_immutability_triggers`` -- a
    fresh database created via ``create_all`` gets the table but not the
    trigger, since triggers are not part of the SQLAlchemy schema.
    """
    from claudetrade.db.models import SignalResearchRevisionRow

    bind = session.get_bind()
    SignalResearchRevisionRow.__table__.create(bind, checkfirst=True)

    if bind.dialect.name != "sqlite":
        log.info(
            "immutability triggers skipped on %s; see docs/architecture.md", bind.dialect.name
        )
        return

    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS trg_signal_research_revisions_no_update
        BEFORE UPDATE ON signal_research_revisions
        BEGIN
            SELECT RAISE(ABORT, 'research revisions are append-only');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_signal_research_revisions_no_delete
        BEFORE DELETE ON signal_research_revisions
        BEGIN
            SELECT RAISE(ABORT, 'research revisions are append-only');
        END;
        """,
    ]
    for stmt in statements:
        session.execute(text(stmt))


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "create_schema", _m001_create_schema, "Initial tables, indexes and constraints"),
    Migration(2, "immutability_triggers", _m002_immutability_triggers, "Append-only ledger guards"),
    Migration(3, "performance_indexes", _m003_performance_indexes, "Query indexes for scale"),
    Migration(4, "add_flair_column", _m004_add_flair_column, "Nullable social_posts.flair column"),
    Migration(5, "refresh_runs", _m005_refresh_runs, "Cross-process refresh state + lock (F27)"),
    Migration(
        6,
        "symbol_fetch_health",
        _m006_symbol_fetch_health,
        "Per-symbol fetch-failure quarantine table",
    ),
    Migration(
        7,
        "social_coverage",
        _m007_social_coverage,
        "Per-session social-collection coverage (confirmed zero vs not collected)",
    ),
    Migration(
        8,
        "add_sentiment_prior_column",
        _m008_add_sentiment_prior_column,
        "Nullable social_posts.sentiment_prior column (author's own bull/bear tag)",
    ),
    Migration(
        9,
        "signal_research_revisions",
        _m009_signal_research_revisions,
        "Append-only MCP research revisions (thesis/invalidation/score adjustments) + guards",
    ),
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


def init_database(
    db: Database, config: AppConfig | None = None, *, allow_data_fixes: bool = True
) -> list[int]:
    """Migrate to latest, assert the schema is complete, then self-heal data.

    Beyond schema migrations, this is also the seam for **stored-data fixes**
    that a code upgrade makes necessary: every entry point (``Pipeline.
    bootstrap`` -- CLI, web API, MCP server, UI -- and the bare CLI ``init``)
    passes through here, so a fix hooked in below runs on the owner's next
    command after a ``git pull`` with no runbook required.

    Args:
        db: Database handle.
        config: Application config, when the caller has one. Optional so
            schema-only callers (tests, ``claudetrade init``'s current call
            shape) keep working; config-dependent data fixes simply defer
            until a config-carrying bootstrap (``Pipeline.bootstrap`` passes
            it) when it is absent.
        allow_data_fixes: When ``False``, run migrations but defer the
            stored-data self-heal below. Schema work is always fast and must
            never be skipped; the data fixes are minute-scale and belong to
            whichever entry point can afford to wait (see
            ``Pipeline.bootstrap``, which the MCP server calls with ``False``
            so a rebuild cannot delay its protocol handshake).
    """
    applied = migrate(db)
    missing = verify_schema(db)
    if missing:
        raise RuntimeError(f"schema incomplete after migration; missing tables: {missing}")
    if not allow_data_fixes:
        return applied
    # Stored sentiment aggregates must have been built by the current
    # extraction code (QA F25: junk common-word symbols and all-neutral
    # ratios kept echoing out of symbol_sentiment_daily long after the
    # extractor/classifier were fixed). Lazy import: the sentiment package
    # (via pipeline) imports this module at load time.
    from claudetrade.sentiment.rebuild import ensure_extraction_version

    ensure_extraction_version(db, config)
    return applied
