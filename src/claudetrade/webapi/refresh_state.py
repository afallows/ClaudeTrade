"""Shared state for the background data-refresh endpoints.

One instance lives on ``app.state.refresh_state`` per running server process
(see ``claudetrade.webapi.app.create_app``) and is read/written by
``claudetrade.webapi.routers.system``'s ``POST /api/system/refresh`` /
``GET /api/system/refresh/status``. This is what lets ``scripts/setup.ps1``
start the UI immediately and trigger the first data load in the background
(item 5) instead of blocking UI startup behind a multi-minute refresh, and
what lets the refresh loop's progress (item 6) reach the browser rather than
only the log file.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field


@dataclass
class RefreshState:
    """Progress of the background refresh started via the system router.

    ``lock`` guards every field below AND is what makes
    ``POST /api/system/refresh`` return 409 rather than starting a second
    overlapping refresh: the endpoint holds it just long enough to check
    ``running`` and flip it to ``True`` before releasing it and starting the
    background thread, so two near-simultaneous requests cannot both pass
    the check.
    """

    running: bool = False
    phase: str = "idle"
    symbols_done: int = 0
    symbols_total: int = 0
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    last_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, object]:
        """A plain, JSON-serialisable copy for the status endpoint."""
        with self.lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "symbols_done": self.symbols_done,
                "symbols_total": self.symbols_total,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_error": self.last_error,
            }

    def update_progress(self, phase: str, done: int, total: int) -> None:
        """``DataIngestor.progress_callback``-shaped hook."""
        with self.lock:
            self.phase = phase
            self.symbols_done = done
            self.symbols_total = total


__all__ = ["RefreshState"]
