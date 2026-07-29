"""Tests for `claudetrade ui`'s subprocess vs in-process launch decision.

Under a normal install, ``sys.executable`` is a real Python interpreter and
Streamlit is spawned as a subprocess. Under a PyInstaller-frozen build,
``sys.executable`` is the bootloader binary itself, so `-m streamlit` would
try to re-exec the frozen program as if it were `python -m streamlit`.
``claudetrade.cli.ui`` must detect ``sys.frozen`` and launch Streamlit
in-process instead -- these tests cannot start a real Streamlit server, so
both branches are exercised by mocking the call each one makes.
"""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from claudetrade import cli
from claudetrade.cli import app
from claudetrade.config import reset_config_cache
from claudetrade.db.session import reset_database_cache

runner = CliRunner()


@pytest.fixture
def ui_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))
    reset_config_cache()
    reset_database_cache()
    yield
    reset_config_cache()
    reset_database_cache()


def test_ui_launches_subprocess_when_not_frozen(ui_env, monkeypatch):
    """The ordinary (non-frozen) install path spawns `python -m streamlit`."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls: list[list[str]] = []

    import subprocess

    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)

    in_process_calls = []
    monkeypatch.setattr(
        cli, "_run_streamlit_in_process", lambda *a, **k: in_process_calls.append((a, k))
    )

    result = runner.invoke(app, ["ui", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert not in_process_calls, "frozen in-process path must not run when sys.frozen is unset"
    assert len(calls) == 1
    command = calls[0]
    assert command[0] == sys.executable
    assert "streamlit" in command
    assert "9999" in command


def test_ui_launches_in_process_when_frozen(ui_env, monkeypatch):
    """Under a PyInstaller build (sys.frozen=True), Streamlit runs in-process."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    import subprocess

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.call must not run when sys.frozen is set")

    monkeypatch.setattr(subprocess, "call", _fail_if_called)

    in_process_calls = []
    monkeypatch.setattr(
        cli,
        "_run_streamlit_in_process",
        lambda app_path, port: in_process_calls.append((app_path, port)),
    )

    result = runner.invoke(app, ["ui", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert len(in_process_calls) == 1
    app_path, port = in_process_calls[0]
    assert app_path.endswith("app.py")
    assert port == 9999


def test_run_streamlit_in_process_calls_bootstrap(monkeypatch):
    """``_run_streamlit_in_process`` itself must call Streamlit's bootstrap API,
    not subprocess, and must not actually start a server in this test."""
    calls = []

    class _FakeBootstrap:
        @staticmethod
        def run(main_script_path, is_hello, args, flag_options):
            calls.append((main_script_path, is_hello, args, flag_options))

    monkeypatch.setitem(sys.modules, "streamlit.web.bootstrap", _FakeBootstrap)

    cli._run_streamlit_in_process("/tmp/app.py", 8600)

    assert len(calls) == 1
    main_script_path, is_hello, _args, flag_options = calls[0]
    assert main_script_path == "/tmp/app.py"
    assert is_hello is False
    assert flag_options.get("server.port") == 8600
