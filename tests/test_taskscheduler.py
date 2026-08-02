"""``claudetrade.taskscheduler`` -- Windows Task Scheduler integration.

``schtasks.exe`` is never actually invoked here: every test that reaches
``install``/``uninstall``/``status`` monkeypatches ``subprocess.run`` with a stand-in
that records the argv it was called with and returns a canned
``CompletedProcess``. Nothing in this file registers, modifies or removes a real
scheduled task on the machine running the suite.

Also covers the ``claudetrade sentiment collect`` exit-code contract this feature
depends on (skip -> 0, failure -> 1) via CLI tests colocated in
``test_social_collection.py``, not here -- see that file's ``TestCollectCommand``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from claudetrade import taskscheduler
from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.session import reset_database_cache

runner = CliRunner()


def _fake_completed(
    args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class _RecordingSchtasks:
    """Stand-in for ``subprocess.run`` that records every call and replays
    canned responses (by position, or a single response for every call)."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if self._responses is None:
            return _fake_completed(cmd)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


@pytest.fixture
def fake_venv(tmp_path, monkeypatch):
    """A fake venv layout with ``Scripts/claudetrade.exe`` present, and
    ``sys.executable`` pointed at its ``python.exe`` -- ``resolve_executable``'s
    preferred (first) resolution path."""
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    exe = scripts_dir / "claudetrade.exe"
    exe.write_text("stub")
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("stub")
    monkeypatch.setattr(sys, "executable", str(python_exe))
    return exe


# --------------------------------------------------------------------------
# resolve_executable
# --------------------------------------------------------------------------


class TestResolveExecutable:
    def test_prefers_the_venv_scripts_dir_next_to_sys_executable(self, fake_venv):
        result = taskscheduler.resolve_executable()

        assert result == [str(fake_venv)]

    def test_falls_back_to_which_when_no_venv_script_is_found(self, tmp_path, monkeypatch):
        empty_python = tmp_path / "python.exe"
        empty_python.write_text("stub")
        monkeypatch.setattr(sys, "executable", str(empty_python))
        monkeypatch.setattr(taskscheduler.shutil, "which", lambda name: "C:\\PATH\\claudetrade.exe")

        result = taskscheduler.resolve_executable()

        assert result == ["C:\\PATH\\claudetrade.exe"]

    def test_falls_back_to_python_dash_m_when_main_module_exists(self, tmp_path, monkeypatch):
        empty_python = tmp_path / "python.exe"
        empty_python.write_text("stub")
        monkeypatch.setattr(sys, "executable", str(empty_python))
        monkeypatch.setattr(taskscheduler.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            taskscheduler.importlib.util, "find_spec", lambda name: object()
        )

        result = taskscheduler.resolve_executable()

        assert result == [str(empty_python), "-m", "claudetrade"]

    def test_raises_a_clear_error_when_nothing_resolves(self, tmp_path, monkeypatch):
        empty_python = tmp_path / "python.exe"
        empty_python.write_text("stub")
        monkeypatch.setattr(sys, "executable", str(empty_python))
        monkeypatch.setattr(taskscheduler.shutil, "which", lambda name: None)
        monkeypatch.setattr(taskscheduler.importlib.util, "find_spec", lambda name: None)

        with pytest.raises(taskscheduler.TaskSchedulerUnavailableError, match="Could not locate"):
            taskscheduler.resolve_executable()


# --------------------------------------------------------------------------
# Non-Windows handling
# --------------------------------------------------------------------------


class TestNonWindows:
    def test_install_names_cron_as_the_alternative(self, monkeypatch):
        monkeypatch.setattr(taskscheduler.sys, "platform", "linux")

        with pytest.raises(taskscheduler.TaskSchedulerUnavailableError, match="cron"):
            taskscheduler.install()

    def test_uninstall_raises_off_windows(self, monkeypatch):
        monkeypatch.setattr(taskscheduler.sys, "platform", "darwin")

        with pytest.raises(taskscheduler.TaskSchedulerUnavailableError):
            taskscheduler.uninstall()

    def test_status_raises_off_windows(self, monkeypatch):
        monkeypatch.setattr(taskscheduler.sys, "platform", "linux")

        with pytest.raises(taskscheduler.TaskSchedulerUnavailableError):
            taskscheduler.status()


# --------------------------------------------------------------------------
# install()
# --------------------------------------------------------------------------


class TestInstall:
    def test_builds_the_exact_hourly_sentiment_collect_command(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.install()

        sentiment_call = recorder.calls[0]
        assert sentiment_call[0] == taskscheduler.SCHTASKS_EXE
        assert sentiment_call[1] == "/Create"
        assert "/TN" in sentiment_call
        assert sentiment_call[sentiment_call.index("/TN") + 1] == taskscheduler.SENTIMENT_TASK_NAME
        tr_value = sentiment_call[sentiment_call.index("/TR") + 1]
        assert tr_value == f'"{fake_venv}" sentiment collect'
        assert "/SC" in sentiment_call
        assert sentiment_call[sentiment_call.index("/SC") + 1] == "HOURLY"
        assert sentiment_call[sentiment_call.index("/MO") + 1] == "1"
        assert sentiment_call[sentiment_call.index("/ST") + 1] == "00:00"

    def test_builds_the_exact_weekday_daily_refresh_command(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.install()

        daily_call = recorder.calls[1]
        assert daily_call[daily_call.index("/TN") + 1] == taskscheduler.DAILY_TASK_NAME
        tr_value = daily_call[daily_call.index("/TR") + 1]
        assert tr_value == f'"{fake_venv}" schedule run-daily'
        assert daily_call[daily_call.index("/SC") + 1] == "WEEKLY"
        assert daily_call[daily_call.index("/D") + 1] == "MON,TUE,WED,THU,FRI"
        assert daily_call[daily_call.index("/ST") + 1] == "18:30"

    def test_install_is_idempotent_via_the_force_flag(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.install()

        for call in recorder.calls:
            assert "/F" in call

    def test_uses_limited_run_level_no_admin_and_no_stored_password(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.install()

        for call in recorder.calls:
            assert call[call.index("/RL") + 1] == "LIMITED"
            assert "/RU" not in call
            assert "/RP" not in call

    def test_dry_run_never_calls_subprocess(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.install(dry_run=True)

        assert recorder.calls == []
        assert all(entry["action"] == "dry-run" for entry in result["tasks"])

    def test_result_carries_the_double_collection_and_x_session_warnings(
        self, fake_venv, monkeypatch
    ):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.install()

        assert "scheduler.social_collection_enabled" in result["double_collection_warning"]
        assert "ToS" in result["x_session_note"] or "suspension" in result["x_session_note"]

    def test_a_failed_schtasks_call_is_reported_per_task_not_raised(self, fake_venv, monkeypatch):
        recorder = _RecordingSchtasks(
            responses=[
                _fake_completed([], returncode=1, stderr="ERROR: access denied"),
                _fake_completed([], returncode=0),
            ]
        )
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.install()

        assert result["tasks"][0]["ok"] is False
        assert result["tasks"][0]["action"] == "failed"
        assert result["tasks"][1]["ok"] is True

    def test_install_never_uses_a_shell(self, fake_venv, monkeypatch):
        captured_kwargs: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return _fake_completed(cmd)

        monkeypatch.setattr(taskscheduler.subprocess, "run", fake_run)

        taskscheduler.install()

        assert captured_kwargs.get("shell", False) is False


# --------------------------------------------------------------------------
# uninstall()
# --------------------------------------------------------------------------


class TestUninstall:
    def test_builds_delete_commands_for_both_tasks(self, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.uninstall()

        names = {call[call.index("/TN") + 1] for call in recorder.calls}
        assert names == {taskscheduler.SENTIMENT_TASK_NAME, taskscheduler.DAILY_TASK_NAME}
        for call in recorder.calls:
            assert call[1] == "/Delete"
            assert "/F" in call

    def test_task_not_found_is_reported_not_raised(self, monkeypatch):
        recorder = _RecordingSchtasks(
            responses=[
                _fake_completed(
                    [], returncode=1, stderr="ERROR: The system cannot find the file specified."
                )
            ]
        )
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.uninstall()

        assert all(entry["action"] == "not_found" for entry in result["tasks"])


# --------------------------------------------------------------------------
# status() / /FO LIST /V parsing
# --------------------------------------------------------------------------

_SAMPLE_QUERY_LIST_OUTPUT = """
Folder: \\
HostName:                            DESKTOP-ABC123
TaskName:                            \\ClaudeTrade Sentiment Collect
Next Run Time:                       8/3/2026 1:00:00 AM
Status:                              Ready
Logon Mode:                          Interactive/Background
Last Run Time:                       8/2/2026 11:00:00 PM
Last Result:                         0
Author:                              DESKTOP-ABC123\\adria
Task To Run:                         "C:\\ClaudeTrade\\.venv\\Scripts\\claudetrade.exe" sentiment collect
Start In:                            N/A
Comment:                             N/A
Scheduled Task State:                Enabled
Idle Time:                           Disabled
Power Management:                    Stop On Battery Mode, No Start On Batteries
Run As User:                         DESKTOP-ABC123\\adria
Delete Task If Not Rescheduled:      Disabled
Stop Task If Runs X Hours and X Mins: 72:00:00
Schedule:                            Scheduling data is not available in this format.
Schedule Type:                       Hourly
Start Time:                          12:00:00 AM
Start Date:                          8/2/2026
End Date:                            N/A
Days:                                Every 1 day(s)
Months:                              N/A
Repeat: Every:                       1 Hour(s), 0 Minute(s)
Repeat: Until: Time:                 None
Repeat: Until: Duration:             Disabled
Repeat: Stop If Still Running:       Disabled
"""

_SAMPLE_NOT_FOUND_STDERR = (
    "ERROR: The system cannot find the file specified.\n"
)


class TestStatus:
    def test_parses_a_registered_task_from_a_captured_fo_list_v_sample(self, monkeypatch):
        recorder = _RecordingSchtasks(
            responses=[_fake_completed([], returncode=0, stdout=_SAMPLE_QUERY_LIST_OUTPUT)]
        )
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.status()

        info = result["tasks"][taskscheduler.SENTIMENT_TASK_NAME]
        assert info["found"] is True
        assert info["fields"]["Scheduled Task State"] == "Enabled"
        assert info["fields"]["Schedule Type"] == "Hourly"
        assert info["fields"]["Status"] == "Ready"

    def test_task_not_found_is_reported_cleanly(self, monkeypatch):
        recorder = _RecordingSchtasks(
            responses=[_fake_completed([], returncode=1, stderr=_SAMPLE_NOT_FOUND_STDERR)]
        )
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = taskscheduler.status()

        for info in result["tasks"].values():
            assert info == {"found": False}

    def test_queries_both_tasks_by_name(self, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        taskscheduler.status()

        queried_names = {call[call.index("/TN") + 1] for call in recorder.calls}
        assert queried_names == {taskscheduler.SENTIMENT_TASK_NAME, taskscheduler.DAILY_TASK_NAME}
        for call in recorder.calls:
            assert call[1] == "/Query"
            assert "/FO" in call and call[call.index("/FO") + 1] == "LIST"
            assert "/V" in call


# --------------------------------------------------------------------------
# `claudetrade schedule ...` CLI surface
# --------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()
    yield
    reset_config_cache()
    reset_database_cache()


class TestScheduleCliCommands:
    def test_schedule_install_reports_tasks_and_both_warnings(
        self, cli_env, fake_venv, monkeypatch
    ):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = runner.invoke(app, ["schedule", "install"])

        assert result.exit_code == 0, result.output
        assert taskscheduler.SENTIMENT_TASK_NAME in result.output
        assert taskscheduler.DAILY_TASK_NAME in result.output
        assert "social_collection_enabled" in result.output
        assert "ToS" in result.output or "suspension" in result.output

    def test_schedule_install_off_windows_names_cron(self, cli_env, monkeypatch):
        monkeypatch.setattr(taskscheduler.sys, "platform", "linux")

        result = runner.invoke(app, ["schedule", "install"])

        assert result.exit_code == 1
        assert "cron" in result.output

    def test_schedule_status_reports_not_registered(self, cli_env, monkeypatch):
        recorder = _RecordingSchtasks(
            responses=[_fake_completed([], returncode=1, stderr=_SAMPLE_NOT_FOUND_STDERR)]
        )
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, result.output
        assert "not registered" in result.output

    def test_schedule_uninstall_reports_removed(self, cli_env, monkeypatch):
        recorder = _RecordingSchtasks()
        monkeypatch.setattr(taskscheduler.subprocess, "run", recorder)

        result = runner.invoke(app, ["schedule", "uninstall"])

        assert result.exit_code == 0, result.output
        assert "removed" in result.output


# --------------------------------------------------------------------------
# `claudetrade schedule run-daily`
# --------------------------------------------------------------------------


def _fake_pipeline_class(tmp_path, captured: dict[str, object]):
    """A ``Pipeline`` stand-in carrying a real (migrated) throwaway ``Database`` --
    ``run-daily`` acquires the cross-process refresh lock through ``pipeline.db``
    exactly as ``claudetrade refresh`` does (F27)."""
    from claudetrade.db.migrations import init_database
    from claudetrade.db.session import Database
    from claudetrade.pipeline import PipelineResult

    class _FakeScanResult:
        signals: list[object] = []

    class _FakeScanOutcome:
        warnings: list[str] = []
        scan = _FakeScanResult()

    class _FakePipeline:
        @classmethod
        def bootstrap(cls, config):
            inst = cls()
            inst.db = Database(f"sqlite:///{tmp_path}/task-scheduler-run-daily.db")
            init_database(inst.db)
            return inst

        def refresh(self, *, start, end, symbols=None, progress_callback=None):
            captured["refresh_start"] = start
            captured["refresh_end"] = end
            captured["refresh_symbols"] = symbols
            captured["refresh_progress_callback"] = progress_callback
            return PipelineResult()

        def scan(self, session, *, record=True):
            captured["scan_session"] = session
            captured["scan_record"] = record
            captured["scan_called_after_refresh"] = "refresh_start" in captured
            return _FakeScanOutcome()

    return _FakePipeline


class TestRunDaily:
    def test_runs_refresh_then_scan_in_order(self, cli_env, tmp_path, monkeypatch):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "claudetrade.pipeline.Pipeline", _fake_pipeline_class(tmp_path, captured)
        )

        result = runner.invoke(app, ["schedule", "run-daily"])

        assert result.exit_code == 0, result.output
        assert captured["scan_called_after_refresh"] is True
        assert '"status": "done"' in result.output

    def test_skips_cleanly_and_exits_zero_when_the_lock_is_held(
        self, cli_env, tmp_path, monkeypatch
    ):
        from claudetrade.db import refresh_state_store

        captured: dict[str, object] = {}
        fake_cls = _fake_pipeline_class(tmp_path, captured)
        monkeypatch.setattr("claudetrade.pipeline.Pipeline", fake_cls)

        # Simulate a refresh already running (e.g. an operator's own `claudetrade
        # refresh`) in the same database file this run-daily would use.
        holder_db = fake_cls.bootstrap(None).db
        outcome = refresh_state_store.try_acquire(holder_db, "cli")
        assert outcome.acquired

        result = runner.invoke(app, ["schedule", "run-daily"])

        # A benign skip must exit 0 -- Task Scheduler must not log this as a
        # task failure just because an operator happened to be refreshing.
        assert result.exit_code == 0, result.output
        assert '"status": "skipped"' in result.output
        assert "refresh_start" not in captured  # Pipeline.refresh never ran

    def test_records_the_task_scheduler_entry_point_not_the_in_app_scheduler_one(
        self, cli_env, tmp_path, monkeypatch
    ):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "claudetrade.pipeline.Pipeline", _fake_pipeline_class(tmp_path, captured)
        )

        result = runner.invoke(app, ["schedule", "run-daily"])

        assert result.exit_code == 0, result.output
        assert f'"entry_point": "{taskscheduler.TASK_SCHEDULER_ENTRY_POINT}"' in result.output
        assert taskscheduler.TASK_SCHEDULER_ENTRY_POINT != "scheduler"

    def test_a_refresh_failure_exits_nonzero_and_never_scans(
        self, cli_env, tmp_path, monkeypatch
    ):
        captured: dict[str, object] = {}
        fake_cls = _fake_pipeline_class(tmp_path, captured)

        def broken_refresh(self, *, start, end, symbols=None, progress_callback=None):
            raise RuntimeError("provider unreachable")

        fake_cls.refresh = broken_refresh
        monkeypatch.setattr("claudetrade.pipeline.Pipeline", fake_cls)

        result = runner.invoke(app, ["schedule", "run-daily"])

        assert result.exit_code == 1
        assert '"status": "failed"' in result.output
        assert "scan_session" not in captured
