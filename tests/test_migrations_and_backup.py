"""Tests for database migrations and backup/restore functionality."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from claudetrade.db.backup import create_backup, restore_backup
from claudetrade.db.migrations import (
    current_version,
    init_database,
    migrate,
    verify_schema,
)
from claudetrade.db.models import Base, PriceBar, SchemaVersion, Security, SocialPostRow
from claudetrade.db.session import Database


class TestMigrationsIdempotent:
    """Running migrations twice should be idempotent."""

    def test_init_database_idempotent(self, unmigrated_db: Database):
        """Applying init_database twice is safe (idempotent)."""
        # First run
        applied1 = init_database(unmigrated_db)
        assert len(applied1) > 0

        # Second run
        applied2 = init_database(unmigrated_db)
        assert len(applied2) == 0  # Nothing to apply


class TestMigrationsVersioning:
    """Migrations are tracked and never re-applied."""

    def test_current_version_increments(self, unmigrated_db: Database):
        """current_version reflects applied migrations."""
        version1 = current_version(unmigrated_db)
        assert version1 == 0  # Fresh DB

        migrate(unmigrated_db)
        version2 = current_version(unmigrated_db)
        assert version2 > version1

    def test_migrate_to_target(self, unmigrated_db: Database):
        """migrate(target=N) applies up to version N."""
        migrate(unmigrated_db, target=1)
        assert current_version(unmigrated_db) == 1

        # Requesting target=1 again does nothing
        applied = migrate(unmigrated_db, target=1)
        assert len(applied) == 0


class TestSchemaVerification:
    """verify_schema detects missing tables."""

    def test_schema_complete_after_migration(self, memory_db: Database):
        """No missing tables after init_database."""
        init_database(memory_db)
        missing = verify_schema(memory_db)
        assert len(missing) == 0

    def test_schema_incomplete_fresh_db(self, unmigrated_db: Database):
        """Fresh DB reports missing tables."""
        missing = verify_schema(unmigrated_db)
        # Should report tables that exist in the model but not the DB
        # (since we haven't migrated yet)
        assert len(missing) > 0


class TestTableCreation:
    """Core tables are created correctly by migrations."""

    def test_price_bars_table_exists(self, memory_db: Database):
        """price_bars table exists after migration."""
        init_database(memory_db)

        with memory_db.session() as session:
            # Can insert a bar without error
            bar = PriceBar(
                symbol="TEST",
                session=dt.date(2023, 1, 3),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000_000.0,
                adj_close=101.0,
            )
            session.add(bar)

    def test_security_table_exists(self, memory_db: Database):
        """security table exists after migration."""
        init_database(memory_db)

        with memory_db.session() as session:
            security = Security(
                symbol="TEST",
                name="Test Company",
                exchange="NYSE",
            )
            session.add(security)


class TestBackupAndRestore:
    """Backup and restore functionality preserves data."""

    def test_create_backup(self, tmp_db: Database, tmp_path: Path):
        """create_backup creates a backup file."""
        # Add some data
        with tmp_db.session() as session:
            bar = PriceBar(
                symbol="TEST",
                session=dt.date(2023, 1, 3),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000_000.0,
                adj_close=101.0,
            )
            session.add(bar)

        backup_dir = tmp_path / "backups"
        backup_file = create_backup(tmp_db, backup_dir)

        assert backup_file.exists()
        assert backup_file.suffix == ".db"

    def test_restore_backup_roundtrip(self, tmp_db: Database, tmp_path: Path):
        """Backup and restore preserves data."""
        # Add test data
        with tmp_db.session() as session:
            bar = PriceBar(
                symbol="TEST",
                session=dt.date(2023, 1, 3),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000_000.0,
                adj_close=101.0,
            )
            session.add(bar)

        # Backup
        backup_dir = tmp_path / "backups"
        backup_file = create_backup(tmp_db, backup_dir)

        # Restore to a new database
        restore_db_path = tmp_path / "restored.db"
        restore_backup(backup_file, restore_db_path)

        # Verify data exists in restored DB
        from claudetrade.db.session import Database

        restored_db = Database(f"sqlite:///{restore_db_path}")
        with restored_db.read_session() as session:
            bars = session.query(PriceBar).filter_by(symbol="TEST").all()
            assert len(bars) == 1
            assert bars[0].open == 100.0

    def test_restore_without_force_raises(self, tmp_path: Path):
        """Restore to existing file without force=True raises."""
        backup_file = tmp_path / "backup.db"
        backup_file.touch()

        target_file = tmp_path / "existing.db"
        target_file.touch()

        with pytest.raises(FileExistsError):
            restore_backup(backup_file, target_file, force=False)

    def test_restore_with_force_overwrites(self, tmp_path: Path):
        """Restore with force=True overwrites existing file."""
        backup_file = tmp_path / "backup.db"
        backup_file.write_bytes(b"backup data")

        target_file = tmp_path / "existing.db"
        target_file.write_bytes(b"old data")

        restore_backup(backup_file, target_file, force=True)
        # Target should have backup's content
        assert target_file.read_bytes() == b"backup data"


class TestFlairColumnMigration:
    """Migration 004 adds the nullable ``social_posts.flair`` column.

    A brand-new database (``memory_db``, migrated fresh) already has the
    column because ``_m001_create_schema`` always reflects the *current* ORM
    model -- that path is covered by ``TestTableCreation`` implicitly. The
    migration only does real work against a database that reached version 3
    *before* the column existed, so that scenario is built by hand here
    (matching how a real pre-upgrade database on disk would look).
    """

    def test_fresh_schema_already_has_flair_column(self, memory_db: Database):
        """A brand-new database (created at the latest version) has the
        column without migration 004 needing to do anything."""
        insp = inspect(memory_db.engine)
        columns = {c["name"] for c in insp.get_columns("social_posts")}
        assert "flair" in columns

    def test_flair_column_insertable_on_fresh_schema(self, memory_db: Database):
        with memory_db.session() as session:
            session.add(
                SocialPostRow(
                    source="reddit",
                    external_id="t3_flairtest",
                    created_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
                    text="hello",
                    flair="DD",
                )
            )
        with memory_db.read_session() as session:
            row = session.query(SocialPostRow).filter_by(external_id="t3_flairtest").one()
            assert row.flair == "DD"

    def test_migrate_adds_flair_column_to_a_pre_existing_version_3_database(
        self, unmigrated_db: Database
    ):
        """A database that already reached version 3 -- built here with the
        old (pre-flair) table shape, exactly as a real on-disk database
        upgraded from an older release would look -- gains the nullable
        ``flair`` column when migrated to the latest version, without
        losing existing data."""
        # `schema_version` itself is unaffected by this migration, so it is
        # fine to create it via the current ORM model.
        Base.metadata.create_all(unmigrated_db.engine, tables=[SchemaVersion.__table__])

        with unmigrated_db.session() as session:
            # The pre-004 shape of social_posts: every column migration 004
            # doesn't touch, minus `flair`.
            session.execute(
                text(
                    """
                    CREATE TABLE social_posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source VARCHAR(20),
                        external_id VARCHAR(80),
                        created_at DATETIME,
                        fetched_at DATETIME,
                        text TEXT,
                        text_hash VARCHAR(64),
                        community VARCHAR(80),
                        score INTEGER,
                        num_comments INTEGER,
                        num_reposts INTEGER,
                        num_replies INTEGER,
                        author_hash VARCHAR(64),
                        author_age_days FLOAT,
                        author_karma FLOAT,
                        author_followers FLOAT,
                        is_comment BOOLEAN,
                        parent_id VARCHAR(80),
                        is_removed BOOLEAN,
                        is_crosspost BOOLEAN,
                        crosspost_parent VARCHAR(80),
                        duplicate_group VARCHAR(64),
                        injection_risk FLOAT,
                        raw_ref VARCHAR(200)
                    )
                    """
                )
            )
            session.execute(
                text(
                    "INSERT INTO social_posts (source, external_id, created_at, text) "
                    "VALUES ('reddit', 'abc123', '2024-01-01 00:00:00', 'hello')"
                )
            )
            session.add(
                SchemaVersion(version=3, name="performance_indexes", checksum="003:performance_indexes")
            )

        assert current_version(unmigrated_db) == 3

        applied = migrate(unmigrated_db)
        # 004 (flair) plus every later additive migration -- currently 005
        # (symbol_fetch_health); the point under test is that 004 ran.
        assert applied[0] == 4
        assert current_version(unmigrated_db) == max(applied)

        insp = inspect(unmigrated_db.engine)
        columns = {c["name"] for c in insp.get_columns("social_posts")}
        assert "flair" in columns
        # The 005 additive table arrived in the same upgrade pass.
        assert "symbol_fetch_health" in insp.get_table_names()

        with unmigrated_db.read_session() as session:
            row = session.execute(
                text("SELECT external_id, flair FROM social_posts WHERE external_id = 'abc123'")
            ).one()
            assert row.external_id == "abc123"
            # Pre-existing rows get NULL for the newly-added column.
            assert row.flair is None

        # And the column is now genuinely usable for new rows.
        with unmigrated_db.session() as session:
            session.execute(
                text(
                    "INSERT INTO social_posts (source, external_id, created_at, text, flair) "
                    "VALUES ('reddit', 'def456', '2024-01-02 00:00:00', 'hello again', 'YOLO')"
                )
            )
        with unmigrated_db.read_session() as session:
            row = session.execute(
                text("SELECT flair FROM social_posts WHERE external_id = 'def456'")
            ).one()
            assert row.flair == "YOLO"

    def test_migration_is_idempotent_when_rerun(self, unmigrated_db: Database):
        """Running migration 004 again (e.g. `init_database` called twice)
        is a safe no-op once the column already exists."""
        init_database(unmigrated_db)
        applied_again = migrate(unmigrated_db)
        assert applied_again == []
        insp = inspect(unmigrated_db.engine)
        columns = {c["name"] for c in insp.get_columns("social_posts")}
        assert "flair" in columns
