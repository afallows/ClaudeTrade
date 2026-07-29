"""Tests for database migrations and backup/restore functionality."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from claudetrade.db.backup import create_backup, restore_backup
from claudetrade.db.migrations import (
    current_version,
    init_database,
    migrate,
    verify_schema,
)
from claudetrade.db.models import PriceBar, Security
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
