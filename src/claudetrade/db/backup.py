"""Backup and restore.

SQLite backups use the online backup API, so a consistent copy can be taken
while the scheduler or UI is holding the database open. Restores refuse to
overwrite unless explicitly forced, and always keep a timestamped copy of the
database being replaced.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3
from pathlib import Path

from claudetrade.db.session import Database

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".ctbak.db"


def _sqlite_path(url: str) -> Path:
    if not url.startswith("sqlite"):
        raise ValueError(f"backup currently supports SQLite only (got {url.split(':')[0]})")
    _, _, tail = url.partition(":///")
    if not tail:
        raise ValueError(f"could not parse SQLite path from URL: {url}")
    return Path(tail)


def create_backup(db: Database, destination_dir: Path, *, label: str = "") -> Path:
    """Write a consistent snapshot of the database.

    Returns:
        Path of the created backup file.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    target = destination_dir / f"claudetrade-{stamp}{suffix}{BACKUP_SUFFIX}"

    source_path = _sqlite_path(db.url)
    if not source_path.exists():
        raise FileNotFoundError(f"database file not found: {source_path}")

    src = sqlite3.connect(str(source_path))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    log.info("backup written: %s (%.1f KiB)", target, target.stat().st_size / 1024)
    return target


def list_backups(destination_dir: Path) -> list[Path]:
    """Backups in a directory, newest first."""
    if not destination_dir.exists():
        return []
    return sorted(
        destination_dir.glob(f"*{BACKUP_SUFFIX}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def restore_backup(backup_path: Path, db_url: str, *, force: bool = False) -> Path:
    """Replace the live database with a backup.

    The database currently in place is first copied aside with a
    ``.superseded-<timestamp>`` suffix, so a mistaken restore is recoverable.

    Raises:
        FileExistsError: if a database already exists and ``force`` is False.
    """
    target = _sqlite_path(db_url)
    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")

    if target.exists():
        if not force:
            raise FileExistsError(
                f"{target} already exists; pass force=True to replace it "
                "(the existing file will be preserved with a .superseded suffix)"
            )
        stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = target.with_suffix(target.suffix + f".superseded-{stamp}")
        shutil.copy2(target, aside)
        log.warning("existing database preserved at %s", aside)

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target)
    # WAL/SHM files from the replaced database would be inconsistent with the
    # restored main file.
    for side in (".wal", ".shm"):
        stale = Path(str(target) + side)
        if stale.exists():
            stale.unlink()
    log.info("database restored from %s", backup_path)
    return target


def prune_backups(destination_dir: Path, keep: int = 10) -> list[Path]:
    """Delete all but the ``keep`` most recent backups. Returns removed paths."""
    backups = list_backups(destination_dir)
    removed: list[Path] = []
    for path in backups[keep:]:
        path.unlink()
        removed.append(path)
    if removed:
        log.info("pruned %d old backups", len(removed))
    return removed
