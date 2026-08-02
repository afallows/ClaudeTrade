"""Windows Task Scheduler integration: keep sentiment collecting when the app is closed.

**Why this exists.** ``claudetrade.scheduler.SocialCollectionScheduler`` collects social
posts and attention every hour, but only while a process (the desktop UI / web API
server) is running. Social sources cannot be backfilled -- Reddit ``/new``, X
recent-search and ApeWisdom's rolling 24h snapshot have no history endpoint -- so every
hour the app is closed is an hour of baseline permanently lost. This module lets the
owner register two per-user Windows Scheduled Tasks (via ``schtasks.exe``) that keep
collection running around the clock without the app open at all:

* ``ClaudeTrade Sentiment Collect`` -- hourly, every day, running
  ``claudetrade sentiment collect`` (the same one-shot collection
  ``claudetrade sentiment collect`` always ran on demand; see ``scheduler.py``'s
  module docstring for why this is social-only and never the market pass).
* ``ClaudeTrade Daily Refresh`` -- weekdays at 18:30 local (after US market close and
  settle), running ``claudetrade schedule run-daily``. ``schtasks`` cannot chain two
  console-script invocations into one task, so ``schedule run-daily`` exists purely to
  run refresh-then-scan in-process under one task -- see that command's docstring.

**Design constraints, and why.**

* Per-user tasks, ``/RL LIMITED``: no administrator elevation and no stored password
  prompt (``/RU``/``/RP``), which a personal research tool has no business asking for.
* List-form ``subprocess.run`` args, never ``shell=True``: task names and paths are
  passed as literal argv elements, not through a shell that could reinterpret them.
* Idempotent install (``/F`` overwrites): running ``schedule install`` twice updates the
  existing tasks in place rather than erroring or duplicating them.
* ``schtasks`` gives a scheduled run no control over its working directory and discards
  its stdout. Neither matters here: every entry point already resolves paths from
  ``config.paths.app_dir`` rather than the current directory, and everything worth
  keeping is already written to the rotating log files under ``<app_dir>/logs`` (see
  ``logging_setup.setup_logging``) rather than depended on via stdout.
* Non-Windows: :func:`install`/:func:`uninstall`/:func:`status` all raise
  :class:`TaskSchedulerUnavailableError` naming cron as the alternative, rather than doing
  nothing silently or crashing on a missing ``schtasks.exe``.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from claudetrade.logging_setup import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Scheduled-task names. Kept as module constants (not config) -- these are an
#: installation detail of this machine's Task Scheduler, not application behaviour.
SENTIMENT_TASK_NAME = "ClaudeTrade Sentiment Collect"
DAILY_TASK_NAME = "ClaudeTrade Daily Refresh"

#: ``refresh_runs.entry_point`` recorded by ``schedule run-daily``. Deliberately
#: distinct from ``scheduler.SCHEDULER_ENTRY_POINT`` (``"scheduler"``, the in-app hourly
#: loop's own label): a status surface reading ``entry_point`` should be able to tell
#: "the in-app loop did this" from "an unattended OS-level task did this", which are
#: different things to see when nobody is at the keyboard.
TASK_SCHEDULER_ENTRY_POINT = "task_scheduler"

#: The ``schtasks.exe`` binary. Not resolved to a full path: Windows always has it on
#: PATH (``%SystemRoot%\System32\schtasks.exe``), and passing the bare name through
#: ``subprocess.run``'s list form (no ``shell=True``) is exactly as safe as a full path
#: here -- there is no shell to search PATH insecurely.
SCHTASKS_EXE = "schtasks.exe"

DOUBLE_COLLECTION_WARNING = (
    "WARNING: if you leave `claudetrade ui` (or the web API server) open all day, its "
    "in-app hourly social-collection loop ALSO runs, independently of this scheduled "
    "task. That is harmless -- both collectors take the same cross-process single-flight "
    "refresh lock, so whichever one loses a race skips for free rather than duplicating "
    "any work -- but it does mean social sources can be hit up to 2x/hour instead of "
    "1x/hour while both are active. To avoid that cadence, set "
    "`scheduler.social_collection_enabled = false` in config.toml so only this scheduled "
    "task collects."
)

X_SESSION_WARNING = (
    "NOTE: if X cookie-session mode is active (`x.session_enabled = true` with a stored "
    "session cookie, rather than the official paid API), running it unattended on an "
    "hourly schedule is a ToS / account-suspension risk for that X account -- X's terms "
    "do not sanction automated, unattended use of a logged-in browser session. This "
    "installer does not change `x.session_enabled` either way; review it yourself in "
    "config.toml before relying on unattended hourly collection."
)


class TaskSchedulerUnavailableError(RuntimeError):
    """Raised when this module's functions are used off Windows, or ``schtasks.exe``
    cannot be found/run. Always carries an actionable message (cron, for the former)."""


@dataclass(frozen=True, slots=True)
class _TaskSpec:
    name: str
    args: tuple[str, ...]
    schedule_args: tuple[str, ...]
    schedule_description: str


#: Every ``ClaudeTrade *`` scheduled task this module knows about, in install order.
_TASK_SPECS: tuple[_TaskSpec, ...] = (
    _TaskSpec(
        name=SENTIMENT_TASK_NAME,
        args=("sentiment", "collect"),
        # HOURLY + /MO 1 repeats every hour, every day, indefinitely from /ST -- social
        # sources never close, so there is no weekday/weekend distinction to make here.
        schedule_args=("/SC", "HOURLY", "/MO", "1", "/ST", "00:00"),
        schedule_description="every hour, every day (social sources never close)",
    ),
    _TaskSpec(
        name=DAILY_TASK_NAME,
        args=("schedule", "run-daily"),
        # 18:30 local, weekdays only: after the 4pm ET close plus time for the day's
        # bars to settle at the data provider, and prices do not move on weekends.
        schedule_args=("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "18:30"),
        schedule_description=(
            "weekdays (Mon-Fri) at 18:30 local -- after US market close and settle"
        ),
    ),
)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise TaskSchedulerUnavailableError(
            "Windows Task Scheduler integration (schtasks.exe) is only available on "
            "Windows. On Linux/macOS, use cron instead, e.g. crontab -e:\n"
            "  0 * * * *      <venv>/bin/claudetrade sentiment collect\n"
            "  30 18 * * 1-5  <venv>/bin/claudetrade schedule run-daily"
        )


# --------------------------------------------------------------------------
# Executable resolution
# --------------------------------------------------------------------------


def resolve_executable() -> list[str]:
    """The argv prefix used to invoke the ``claudetrade`` CLI from a scheduled task.

    Resolution order, each a fallback for the last:

    1. The console script installed next to the interpreter running THIS process --
       ``<sys.executable's dir>/Scripts/claudetrade.exe`` on Windows (``bin/claudetrade``
       on POSIX, for the cron fallback / non-Windows test runs). When this module is
       imported from inside a venv's site-packages, that is the venv's own Scripts dir,
       which is exactly the install ``schedule install`` should point at.
    2. ``shutil.which("claudetrade")`` -- a ``claudetrade`` on PATH outside this venv
       (e.g. a global pipx install).
    3. ``<python> -m claudetrade`` -- only offered if ``claudetrade.__main__`` actually
       exists; ``pyproject.toml``'s only entry point today is the console script
       (``project.scripts.claudetrade``), so this branch is not currently reachable, but
       is kept so this function does not need to change the day that entry point is
       added.

    Raises :class:`TaskSchedulerUnavailableError` with a specific, actionable message if none
    resolve. A task registered against a guessed/wrong path fails silently at 3 AM with
    nothing but an empty log to explain why -- refusing up front is strictly kinder.
    """
    python_dir = Path(sys.executable).resolve().parent
    candidate_names = ["claudetrade.exe"] if sys.platform == "win32" else ["claudetrade"]
    script_subdirs = ("Scripts", "bin")
    checked: list[Path] = []
    for name in candidate_names:
        for subdir in script_subdirs:
            candidate = python_dir / subdir / name
            checked.append(candidate)
            if candidate.is_file():
                return [str(candidate)]
        candidate = python_dir / name
        checked.append(candidate)
        if candidate.is_file():
            return [str(candidate)]

    which = shutil.which("claudetrade")
    if which:
        return [which]

    if importlib.util.find_spec("claudetrade.__main__") is not None:
        return [sys.executable, "-m", "claudetrade"]

    checked_str = ", ".join(str(p) for p in checked)
    raise TaskSchedulerUnavailableError(
        "Could not locate the 'claudetrade' console script (checked: "
        f"{checked_str}; also not on PATH, and this build has no `claudetrade.__main__` "
        "to fall back to `python -m claudetrade`). Install the package "
        "(`pip install -e .` from the repo root, inside the venv you want the scheduled "
        "tasks to run under) and retry."
    )


def _command_string(argv_prefix: list[str], args: tuple[str, ...]) -> str:
    """Build the single string ``schtasks /TR`` expects for a command line.

    ``/TR`` takes ONE argv element containing the whole command; ``schtasks`` parses
    quoting *within* that string itself (this process never invokes a shell, so no shell
    ever sees it). The executable path is always quoted -- venv paths routinely contain
    spaces (``C:\\Users\\Jane Doe\\...``) -- and any argument containing whitespace is
    quoted defensively, though none of this module's own task args do.
    """
    parts = [f'"{argv_prefix[0]}"']
    parts.extend(argv_prefix[1:])  # e.g. "-m", "claudetrade" -- never contain spaces
    for arg in args:
        parts.append(f'"{arg}"' if " " in arg else arg)
    return " ".join(parts)


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    """One ``schtasks.exe`` invocation. List-form args, never ``shell=True``."""
    cmd = [SCHTASKS_EXE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _looks_like_not_found(proc: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed ``schtasks`` call failed because the task does not exist.

    ``schtasks`` has no dedicated exit code for this; it prints an ``ERROR:`` line to
    stderr (occasionally stdout, observed across Windows builds) and returns non-zero.
    Matched case-insensitively on the stable substring rather than the full sentence,
    which has varied by locale/build.
    """
    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    return "cannot find" in combined or "does not exist" in combined


def _parse_query_list(output: str) -> dict[str, str]:
    """Parse ``schtasks /Query /TN <name> /FO LIST /V`` output into a flat dict.

    The ``LIST`` format is ``Field Name:    Value`` per line, one field per line, no
    nesting -- exactly what the ``/V`` (verbose) single-task query produces. Lines with
    no ``:`` (blank separators) are skipped; values are stripped but otherwise passed
    through as ``schtasks`` formatted them (dates, durations, etc. stay as its own
    locale-formatted strings -- this is a status surface, not a parser for scheduling
    math elsewhere in the app).
    """
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        fields[key] = value.strip()
    return fields


# --------------------------------------------------------------------------
# install / uninstall / status
# --------------------------------------------------------------------------


def _x_session_caveat() -> str:
    """:data:`X_SESSION_WARNING`, prefixed with a live read of ``x.session_enabled``
    when config is cheaply readable. Never raises -- an install report must not fail
    because a diagnostic aside could not load the config."""
    try:
        from claudetrade.config import get_config

        cfg = get_config()
        if cfg.x.session_enabled:
            return "X cookie-session mode is currently ENABLED in your config. " + (
                X_SESSION_WARNING
            )
    except Exception:
        log.debug("could not read config for X session-mode detection", exc_info=True)
    return X_SESSION_WARNING


def install(*, dry_run: bool = False) -> dict[str, Any]:
    """Register (or update, idempotently) both scheduled tasks.

    Each task is created with ``/F`` (overwrite if it already exists -- safe to call
    repeatedly, e.g. after upgrading the venv path) and ``/RL LIMITED`` (no admin
    elevation) under the current user (no ``/RU``/``/RP``, so no password prompt).

    ``dry_run=True`` resolves the executable and builds the exact ``schtasks`` commands
    without running them, for ``schedule install --dry-run`` and for tests.

    Returns a structured dict; never raises for an individual task's ``schtasks``
    failure (that is reported per-task via ``ok``/``returncode``/``stderr``) -- only
    :class:`TaskSchedulerUnavailableError` (wrong OS, or the console script cannot be found)
    is allowed to raise, since neither is a per-task outcome.
    """
    _require_windows()
    argv_prefix = resolve_executable()

    tasks: list[dict[str, Any]] = []
    for spec in _TASK_SPECS:
        tr = _command_string(argv_prefix, spec.args)
        args = ["/Create", "/TN", spec.name, "/TR", tr, *spec.schedule_args, "/RL", "LIMITED", "/F"]
        entry: dict[str, Any] = {
            "name": spec.name,
            "schedule": spec.schedule_description,
            "command": [SCHTASKS_EXE, *args],
        }
        if dry_run:
            entry["action"] = "dry-run"
        else:
            proc = _run_schtasks(args)
            entry["returncode"] = proc.returncode
            entry["stdout"] = proc.stdout.strip()
            entry["stderr"] = proc.stderr.strip()
            entry["ok"] = proc.returncode == 0
            entry["action"] = "created" if proc.returncode == 0 else "failed"
            if proc.returncode != 0:
                log.error(
                    "schtasks /Create failed for %s (rc=%d): %s",
                    spec.name, proc.returncode, proc.stderr.strip(),
                )
        tasks.append(entry)

    return {
        "platform": "windows",
        "executable": argv_prefix,
        "tasks": tasks,
        "double_collection_warning": DOUBLE_COLLECTION_WARNING,
        "x_session_note": _x_session_caveat(),
    }


def uninstall() -> dict[str, Any]:
    """Remove both scheduled tasks. A task that is already absent is reported, not an
    error -- ``schedule uninstall`` should be safe to run whether or not install ever
    ran."""
    _require_windows()

    tasks: list[dict[str, Any]] = []
    for spec in _TASK_SPECS:
        args = ["/Delete", "/TN", spec.name, "/F"]
        proc = _run_schtasks(args)
        if proc.returncode == 0:
            action = "removed"
        elif _looks_like_not_found(proc):
            action = "not_found"
        else:
            action = "failed"
            log.error(
                "schtasks /Delete failed for %s (rc=%d): %s",
                spec.name, proc.returncode, proc.stderr.strip(),
            )
        tasks.append(
            {
                "name": spec.name,
                "command": [SCHTASKS_EXE, *args],
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "action": action,
            }
        )
    return {"platform": "windows", "tasks": tasks}


def status() -> dict[str, Any]:
    """Current registration state of both tasks, read back from Task Scheduler itself
    (not from anything this process remembers) via ``schtasks /Query ... /FO LIST /V``.
    """
    _require_windows()

    tasks: dict[str, Any] = {}
    for spec in _TASK_SPECS:
        args = ["/Query", "/TN", spec.name, "/FO", "LIST", "/V"]
        proc = _run_schtasks(args)
        if proc.returncode != 0 or _looks_like_not_found(proc):
            tasks[spec.name] = {"found": False}
            continue
        tasks[spec.name] = {"found": True, "fields": _parse_query_list(proc.stdout)}
    return {"platform": "windows", "tasks": tasks}


# --------------------------------------------------------------------------
# CLI: `claudetrade schedule ...`
# --------------------------------------------------------------------------

schedule_app = typer.Typer(
    help=(
        "Windows Task Scheduler integration: keep sentiment collecting on a timer "
        "even when the app is closed."
    )
)


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


@schedule_app.command("install")
def schedule_install(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the schtasks commands without running them."),
    ] = False,
) -> None:
    """Register the two scheduled tasks (idempotent -- safe to re-run after upgrades).

    Creates ``ClaudeTrade Sentiment Collect`` (hourly, every day) and
    ``ClaudeTrade Daily Refresh`` (weekdays at 18:30 local, running
    ``claudetrade schedule run-daily``). Both run as the current user with no admin
    elevation and no stored password.
    """
    try:
        result = install(dry_run=dry_run)
    except TaskSchedulerUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    _echo_json(result)
    typer.echo("")
    for entry in result["tasks"]:
        colour = typer.colors.GREEN if entry.get("ok", True) else typer.colors.RED
        typer.secho(f"{entry['name']}: {entry['action']} -- {entry['schedule']}", fg=colour)

    typer.echo("")
    typer.secho(DOUBLE_COLLECTION_WARNING, fg=typer.colors.YELLOW)
    typer.echo("")
    typer.secho(result["x_session_note"], fg=typer.colors.YELLOW)

    if not dry_run and any(not entry.get("ok", True) for entry in result["tasks"]):
        raise typer.Exit(code=1)


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Remove both scheduled tasks. Safe to run even if they were never installed."""
    try:
        result = uninstall()
    except TaskSchedulerUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    _echo_json(result)
    for entry in result["tasks"]:
        typer.echo(f"{entry['name']}: {entry['action']}")
    if any(entry["action"] == "failed" for entry in result["tasks"]):
        raise typer.Exit(code=1)


@schedule_app.command("status")
def schedule_status() -> None:
    """Show what Task Scheduler currently has registered for both tasks."""
    try:
        result = status()
    except TaskSchedulerUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    _echo_json(result)
    for name, info in result["tasks"].items():
        if info["found"]:
            state = info["fields"].get("Scheduled Task State", "?")
            next_run = info["fields"].get("Next Run Time", "?")
            typer.secho(f"{name}: registered ({state}, next run {next_run})", fg=typer.colors.GREEN)
        else:
            typer.secho(f"{name}: not registered", fg=typer.colors.YELLOW)


@schedule_app.command("run-daily")
def schedule_run_daily(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml.")
    ] = None,
) -> None:
    """Refresh then scan, in-process -- the one task ``ClaudeTrade Daily Refresh`` runs.

    ``schtasks`` cannot chain two console-script invocations into a single scheduled
    task, so this subcommand exists to run ``refresh`` then ``scan`` back-to-back under
    ONE task: refresh completing (honestly, including its own failure) is sequenced
    before scan starts, rather than scanning a database a concurrent refresh might still
    be mid-write on.

    Takes the same cross-process single-flight lock every other refresh-triggering entry
    point does (``db.refresh_state_store``): if a refresh is already running anywhere
    (an operator's own ``claudetrade refresh``, the web API, MCP), this run SKIPS
    cleanly rather than racing it, and exits 0 -- a benign skip is not a task failure and
    must not be recorded as one by Task Scheduler's own history. It is recorded under
    ``entry_point="task_scheduler"``, distinguishing an unattended OS-level run from a
    human-triggered ``"cli"`` one in every status surface that reads
    ``refresh_runs.entry_point``.

    Task Scheduler discards this command's stdout, so nothing here is load-bearing for
    the operator unless they run it by hand -- what actually gets kept is the rotating
    log at ``<app_dir>/logs/claudetrade-task_scheduler.log``
    (``logging_setup.setup_logging``, ``component="task_scheduler"``).
    """
    from claudetrade.config import get_config
    from claudetrade.db import refresh_state_store
    from claudetrade.logging_setup import setup_logging
    from claudetrade.pipeline import Pipeline
    from claudetrade.utils.timeutils import current_trading_session, utc_now

    cfg = get_config(config, reload=True)
    setup_logging(cfg, component="task_scheduler")

    pipeline = Pipeline.bootstrap(cfg)
    outcome = refresh_state_store.try_acquire(pipeline.db, TASK_SCHEDULER_ENTRY_POINT)
    if not outcome.acquired:
        holder = outcome.holder
        reason = holder.describe() if holder else "another process holds the refresh lock"
        log.info("scheduled run-daily skipped -- %s", reason)
        _echo_json(
            {
                "status": "skipped",
                "reason": reason,
                "entry_point": TASK_SCHEDULER_ENTRY_POINT,
            }
        )
        return  # exit 0: a benign lock-skip must not read as a Task Scheduler failure

    handle = outcome.handle
    assert handle is not None  # acquired always carries a handle (refresh_state_store)

    end_date = utc_now().date()
    start_date = end_date - dt.timedelta(days=90)
    try:
        refresh_result = pipeline.refresh(
            start=start_date,
            end=end_date,
            symbols=None,
            progress_callback=handle.update_progress,
        )
    except Exception as exc:
        handle.finish("failed", error=str(exc))
        log.exception("scheduled refresh failed; scan skipped")
        _echo_json(
            {
                "status": "failed",
                "stage": "refresh",
                "error": str(exc),
                "entry_point": TASK_SCHEDULER_ENTRY_POINT,
            }
        )
        raise typer.Exit(code=1) from None
    handle.finish("done")

    session_date = current_trading_session()
    try:
        scan_outcome = pipeline.scan(session_date, record=True)
    except Exception as exc:
        log.exception("scheduled scan failed after a successful refresh")
        _echo_json(
            {
                "status": "partial",
                "stage": "scan",
                "error": str(exc),
                "refresh_summary": refresh_result.summary(),
                "entry_point": TASK_SCHEDULER_ENTRY_POINT,
            }
        )
        raise typer.Exit(code=1) from None

    _echo_json(
        {
            "status": "done",
            "entry_point": TASK_SCHEDULER_ENTRY_POINT,
            "refresh_summary": refresh_result.summary(),
            "scan_warnings": list(scan_outcome.warnings),
            "signals_found": len(scan_outcome.scan.signals) if scan_outcome.scan else 0,
        }
    )
    if scan_outcome.scan is None:
        raise typer.Exit(code=1)


__all__ = [
    "DAILY_TASK_NAME",
    "DOUBLE_COLLECTION_WARNING",
    "SENTIMENT_TASK_NAME",
    "TASK_SCHEDULER_ENTRY_POINT",
    "TaskSchedulerUnavailableError",
    "install",
    "resolve_executable",
    "schedule_app",
    "status",
    "uninstall",
]
