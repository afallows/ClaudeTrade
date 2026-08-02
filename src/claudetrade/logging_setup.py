"""Structured logging with rotating files.

Two sinks:

* ``claudetrade.log`` -- application log, JSON lines by default so it can be
  ingested without parsing.
* ``audit.log`` -- security- and integrity-relevant events only, mirrored to the
  ``audit_log`` table.

A redaction filter runs on every record: anything that looks like an API key,
bearer token or password is replaced before it reaches disk. Logs are a common
accidental credential leak, and the operator's log directory is not necessarily
as protected as their credential store.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

from claudetrade.config import AppConfig
from claudetrade.version import CODE_VERSION

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"(sk-ant-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(Bearer\s+)[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)([^\s\",}]{8,})", re.I),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
]
REDACTED = "[REDACTED]"


def redact(message: str) -> str:
    """Replace credential-looking substrings with a placeholder."""
    out = message
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


class RedactionFilter(logging.Filter):
    """Scrub secrets from the formatted message and from string args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any ``extra=`` fields merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "code_version": CODE_VERSION,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable format for the terminal."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")


class ResilientRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A ``RotatingFileHandler`` that tolerates a rollover it cannot complete.

    On Windows, ``doRollover()``'s ``os.rename`` raises ``PermissionError``
    (``WinError 32``) when a second process has the same log file open --
    exactly the situation of running ``claudetrade refresh`` from a terminal
    while the desktop UI's own server is also running and logging to the
    same file. Left to the default handler, that exception is not silently
    swallowed: Python's logging module prints a full "--- Logging error
    ---" traceback to stderr for *every subsequent record* once a handler is
    left in this broken state -- a real owner log showed thousands of lines
    of exactly this, burying every other message.

    This subclass catches the rollover failure, logs one WARNING the first
    time (never per record), and keeps appending to the current file without
    rotating -- an oversized log file until the next successful rotation
    (typically the next process start, once the other process has released
    the file) is a far better failure mode than an unreadable console.
    Combined with per-entry-point log filenames (see ``setup_logging``),
    which remove the routine two-processes-one-file case entirely, this is
    belt-and-braces for whatever scenario still shares a file (e.g. two
    instances of the same entry point run concurrently by the operator).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._rollover_warned = False

    def doRollover(self) -> None:  # noqa: N802 - overrides logging.handlers.RotatingFileHandler's own camelCase method name
        try:
            super().doRollover()
        except OSError as exc:
            if not self._rollover_warned:
                self._rollover_warned = True
                logging.getLogger(__name__).warning(
                    "log rotation failed for %s (%s: %s) -- another process most likely has "
                    "this file open; continuing to append to the current file without "
                    "rotating until a future run can rotate it",
                    self.baseFilename,
                    type(exc).__name__,
                    exc,
                )
            # doRollover() may have already closed self.stream before the
            # rename failed; make sure the handler can still write.
            if self.stream is None or self.stream.closed:
                self.stream = self._open()


_CONFIGURED = False


def _entry_point_filename(base_filename: str, component: str | None) -> str:
    """Derive a per-entry-point log filename from the configured base name.

    ``claudetrade.log`` + ``component="cli"`` -> ``claudetrade-cli.log``;
    ``component=None`` leaves the configured name untouched (existing/other
    callers, and every pre-existing test, are unaffected). This is what
    stops the CLI and the desktop UI server -- two separate OS processes --
    from ever holding a handle to the same log file at the same time, which
    is the routine (not exceptional) cause of the Windows rollover
    contention ``ResilientRotatingFileHandler`` also guards against.
    """
    if not component:
        return base_filename
    path = Path(base_filename)
    return f"{path.stem}-{component}{path.suffix or '.log'}"


def setup_logging(
    config: AppConfig, *, force: bool = False, component: str | None = None
) -> logging.Logger:
    """Install handlers on the root logger. Idempotent unless ``force``.

    Args:
        component: Short tag identifying this entry point (``"cli"``,
            ``"web"``, ...), used to derive a per-entry-point log filename --
            see ``_entry_point_filename``. Omit to use the configured
            filename verbatim.
    """
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)

    logs_dir: Path = config.paths.resolve("logs_dir")
    root.setLevel(getattr(logging, config.logging.level))
    redaction = RedactionFilter()

    file_handler = ResilientRotatingFileHandler(
        logs_dir / _entry_point_filename(config.logging.filename, component),
        maxBytes=config.logging.rotate_max_bytes,
        backupCount=config.logging.rotate_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter() if config.logging.json_format else ConsoleFormatter())
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    if config.logging.console:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(ConsoleFormatter())
        console.addFilter(redaction)
        root.addHandler(console)

    audit = logging.getLogger("claudetrade.audit")
    audit.propagate = False
    audit_handler = ResilientRotatingFileHandler(
        logs_dir / _entry_point_filename(config.logging.audit_filename, component),
        maxBytes=config.logging.rotate_max_bytes,
        backupCount=config.logging.rotate_backup_count,
        encoding="utf-8",
    )
    audit_handler.setFormatter(JsonFormatter())
    audit_handler.addFilter(redaction)
    for handler in list(audit.handlers):
        audit.removeHandler(handler)
    audit.addHandler(audit_handler)
    audit.setLevel(logging.INFO)

    # Third-party libraries are chatty at INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "apscheduler.executors", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    root.info(
        "logging initialised",
        extra={"logs_dir": str(logs_dir), "level": config.logging.level},
    )
    return root


def get_logger(name: str) -> logging.Logger:
    """Module logger. Use ``get_logger(__name__)``."""
    return logging.getLogger(name)


def audit_event(action: str, **fields: Any) -> None:
    """Write an entry to the audit log.

    Use for anything that changes trading posture or touches credentials:
    mode switches, kill-switch toggles, credential reads, order submission,
    ledger writes, restores.
    """
    logging.getLogger("claudetrade.audit").info(action, extra={"action": action, **fields})
