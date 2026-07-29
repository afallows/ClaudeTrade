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


_CONFIGURED = False


def setup_logging(config: AppConfig, *, force: bool = False) -> logging.Logger:
    """Install handlers on the root logger. Idempotent unless ``force``."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)

    logs_dir: Path = config.paths.resolve("logs_dir")
    root.setLevel(getattr(logging, config.logging.level))
    redaction = RedactionFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / config.logging.filename,
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
    audit_handler = logging.handlers.RotatingFileHandler(
        logs_dir / config.logging.audit_filename,
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
