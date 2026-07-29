"""Engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from claudetrade.config import AppConfig

log = logging.getLogger(__name__)


class Database:
    """Thin wrapper around a SQLAlchemy engine plus session factory.

    Holds the SQLite-specific tuning in one place (WAL journaling, busy timeout,
    foreign-key enforcement) so the rest of the code stays engine-agnostic.
    """

    def __init__(self, url: str, *, echo: bool = False, config: AppConfig | None = None):
        self.url = url
        self.config = config
        self._is_sqlite = url.startswith("sqlite")
        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if self._is_sqlite:
            # check_same_thread=False lets the Streamlit UI and the scheduler
            # share one engine; WAL keeps their reads/writes from blocking.
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = (config.database.busy_timeout_ms / 1000.0) if config else 10.0
        else:
            engine_kwargs["pool_size"] = config.database.pool_size if config else 5
            engine_kwargs["pool_pre_ping"] = True

        self.engine: Engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
        if self._is_sqlite:
            self._install_sqlite_pragmas(config)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def _install_sqlite_pragmas(self, config: AppConfig | None) -> None:
        wal = config.database.sqlite_wal if config else True
        busy_ms = config.database.busy_timeout_ms if config else 10_000

        @event.listens_for(self.engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={busy_ms}")
            if wal:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope. Commits on success, rolls back on exception."""
        sess = self._session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        """Read-only scope; never commits."""
        sess = self._session_factory()
        try:
            yield sess
        finally:
            sess.close()

    def execute_raw(self, statement: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(statement))

    def vacuum(self) -> None:
        """Reclaim space (SQLite only; a no-op elsewhere)."""
        if not self._is_sqlite:
            return
        with self.engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")

    def dispose(self) -> None:
        self.engine.dispose()

    @property
    def is_sqlite(self) -> bool:
        return self._is_sqlite


_DB: Database | None = None


def get_database(config: AppConfig, *, reload: bool = False) -> Database:
    """Process-wide database handle."""
    global _DB
    if _DB is None or reload:
        if _DB is not None:
            _DB.dispose()
        _DB = Database(config.database_url(), echo=config.database.echo, config=config)
        log.info("database connected", extra={"url_scheme": config.database_url().split(":")[0]})
    return _DB


def reset_database_cache() -> None:
    """Drop the cached handle (used by tests)."""
    global _DB
    if _DB is not None:
        _DB.dispose()
    _DB = None
