"""Tests for the Windows log-rotation-contention fix.

A real owner refresh ran ``claudetrade refresh`` from a terminal while the
desktop UI's own server process was also running, both writing to the same
``claudetrade.log``. When ``RotatingFileHandler.doRollover()``'s
``os.rename`` hit the file the other process still had open, Windows raised
``PermissionError`` (WinError 32) -- and, because that exception was never
caught, every subsequent log record printed a full "--- Logging error ---"
traceback to the console, drowning out everything else.

These tests exercise the two independent halves of the fix: a
``RotatingFileHandler`` subclass that survives a failed rollover
(``ResilientRotatingFileHandler``), and per-entry-point log filenames
(``_entry_point_filename`` / ``setup_logging(..., component=...)``) that
remove the routine two-processes-one-file case entirely.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claudetrade.config import AppConfig
from claudetrade.logging_setup import (
    ResilientRotatingFileHandler,
    _entry_point_filename,
    setup_logging,
)


@pytest.fixture
def handler(tmp_path: Path) -> ResilientRotatingFileHandler:
    h = ResilientRotatingFileHandler(
        tmp_path / "test.log", maxBytes=10, backupCount=3, encoding="utf-8"
    )
    yield h
    h.close()


class TestResilientRotatingFileHandler:
    def test_rollover_failure_does_not_raise(self, handler, monkeypatch):
        def _raise(*args, **kwargs):
            raise PermissionError("WinError 32: file in use by another process")

        monkeypatch.setattr("os.rename", _raise)
        handler.doRollover()  # must not raise

    def test_handler_keeps_writing_after_a_failed_rollover(self, handler, monkeypatch, tmp_path):
        def _raise(*args, **kwargs):
            raise PermissionError("in use")

        monkeypatch.setattr("os.rename", _raise)
        handler.doRollover()

        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "still writable", (), None
        )
        handler.emit(record)
        handler.flush()
        assert "still writable" in (tmp_path / "test.log").read_text(encoding="utf-8")

    def test_warns_once_not_per_rollover_attempt(self, handler, monkeypatch, caplog):
        def _raise(*args, **kwargs):
            raise OSError("locked")

        monkeypatch.setattr("os.rename", _raise)
        with caplog.at_level("WARNING", logger="claudetrade.logging_setup"):
            handler.doRollover()
            handler.doRollover()
            handler.doRollover()

        warnings = [r for r in caplog.records if "log rotation failed" in r.message]
        assert len(warnings) == 1

    def test_successful_rollover_is_unaffected(self, tmp_path):
        """A handler that CAN rotate still does -- this subclass only
        changes behaviour on a rollover failure."""
        h = ResilientRotatingFileHandler(
            tmp_path / "ok.log", maxBytes=1, backupCount=3, encoding="utf-8"
        )
        try:
            for i in range(5):
                h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, f"line {i}", (), None))
                h.flush()
                h.doRollover()
            assert (tmp_path / "ok.log.1").exists()
        finally:
            h.close()


class TestEntryPointFilename:
    def test_component_suffixes_the_stem(self):
        assert _entry_point_filename("claudetrade.log", "cli") == "claudetrade-cli.log"
        assert _entry_point_filename("claudetrade.log", "web") == "claudetrade-web.log"

    def test_none_component_leaves_filename_untouched(self):
        assert _entry_point_filename("claudetrade.log", None) == "claudetrade.log"
        assert _entry_point_filename("claudetrade.log", "") == "claudetrade.log"

    def test_preserves_a_non_log_suffix(self):
        assert _entry_point_filename("audit.log", "cli") == "audit-cli.log"


class TestSetupLoggingPerComponentFiles:
    def test_cli_and_web_components_write_to_different_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
        config = AppConfig()
        config.paths.app_dir = tmp_path
        config.logging.console = False

        setup_logging(config, component="cli", force=True)
        logging.getLogger("test.cli").info("from cli")

        setup_logging(config, component="web", force=True)
        logging.getLogger("test.web").info("from web")

        logs_dir = config.paths.resolve("logs_dir")
        assert (logs_dir / "claudetrade-cli.log").exists()
        assert (logs_dir / "claudetrade-web.log").exists()
