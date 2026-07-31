"""Cross-process persistence for the latest scan's rejection funnel.

``SignalEngine.scan()`` builds a :class:`~claudetrade.signals.engine.ScanFunnel`
that is rich enough to answer "why were there no signals" -- but, like the
rest of :class:`~claudetrade.signals.engine.ScanResult`, it only exists in the
process that ran the scan. The CLI, the web API server and the MCP server
each bootstrap their own ``Pipeline`` (see ``claudetrade.mcp_server``'s module
docstring: "each ... has its own ``Pipeline`` ... SQLite's WAL mode is what
makes concurrent *reads* ... safe, not a shared in-memory ... state"), so an
MCP client asking "why no picks today?" after a scan that ran somewhere else
-- most likely a scheduled ``claudetrade scan``, or a scan run from the web
UI -- has no in-memory ``ScanResult`` to read.

Rather than add a database table (which would need a migration, and
``db/migrations.py``/``db/models.py`` are off limits to this change -- see
``docs/decisions``) or store a full second copy of the scan's inputs, this
module writes the *already-bounded* funnel (aggregated counts plus a
top-N near-miss list -- both intentionally small, see ``ScanFunnel``'s own
docstring on memory-boundedness) as one small JSON file under
``PathsConfig.snapshots_dir`` -- an existing, previously-unused config field
already named for exactly this ("alongside the snapshot", in the same sense
``data.snapshot`` stores its manifests, but as a plain file rather than a
database row, so no schema change is needed anywhere). The file is
overwritten on every scan: only the most recent scan's funnel is kept,
matching ``ScanResult.rejected`` itself being in-memory-only and per-scan.

Writing and reading are both best-effort: a disk error here must never fail
the scan it is diagnosing, and a missing/corrupt file must never fail the
read -- it degrades to "no funnel data available" (see ``load_latest``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claudetrade.config import AppConfig
from claudetrade.logging_setup import get_logger
from claudetrade.signals.engine import ScanResult

log = get_logger(__name__)

#: Overwritten on every scan -- see the module docstring. Not versioned by
#: session/timestamp: cross-process readers (the MCP server) want "the most
#: recent scan", not an ever-growing directory of stale ones.
ARTIFACT_FILENAME = "last_scan_funnel.json"


def artifact_path(config: AppConfig) -> Path:
    """Where the funnel artifact for this installation lives."""
    return config.paths.resolve("snapshots_dir") / ARTIFACT_FILENAME


def save(config: AppConfig, scan_result: ScanResult) -> None:
    """Persist ``scan_result``'s funnel, overwriting any previous artifact.

    Best-effort: logs and returns on any I/O failure rather than raising --
    a scan that generated real signals (or a real "why zero" answer in
    memory for this process) must not fail because the diagnostic artifact
    for *other* processes could not be written.
    """
    payload: dict[str, Any] = {
        "session": scan_result.session.isoformat(),
        "generated_at": scan_result.generated_at.isoformat(),
        "evaluated_symbols": scan_result.evaluated_symbols,
        "signal_count": len(scan_result.signals),
        "rejected_count": len(scan_result.rejected),
        "warnings": list(scan_result.warnings),
        "funnel": scan_result.funnel.to_dict(),
    }
    path = artifact_path(config)
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError:
        log.warning("could not write scan funnel artifact to %s", path, exc_info=True)


def load_latest(config: AppConfig) -> dict[str, Any] | None:
    """Read the most recently persisted funnel artifact, or ``None``.

    ``None`` covers both "no scan has run yet on this installation" (the file
    does not exist) and "the file exists but could not be parsed" -- either
    way the caller has nothing usable and should say so rather than crash.
    """
    path = artifact_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("could not read scan funnel artifact at %s", path, exc_info=True)
        return None


__all__ = ["ARTIFACT_FILENAME", "artifact_path", "load_latest", "save"]
