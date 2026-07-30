"""Tests for `claudetrade ui`'s launch decision matrix.

Two axes: which interface (the ADR-0008 React/FastAPI desktop app by
default, the legacy Streamlit app under ``--classic``) and how it is started
(subprocess under a normal install; in-process under a PyInstaller-frozen
build, where ``sys.executable`` is the bootloader binary and ``-m anything``
would re-exec the frozen program). These tests cannot start real servers, so
every branch is exercised by mocking the exact call it makes.
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


# ---------------------------------------------------------------------------
# Default: the web/desktop app
# ---------------------------------------------------------------------------


def test_ui_default_spawns_webapi_subprocess(ui_env, monkeypatch):
    """Without --classic, a normal install spawns `python -m claudetrade.webapi`."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls: list[list[str]] = []

    import subprocess

    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)

    result = runner.invoke(app, ["ui", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    command = calls[0]
    assert command[0] == sys.executable
    assert "claudetrade.webapi" in command
    assert "streamlit" not in command
    assert "9999" in command


def test_ui_default_frozen_runs_webapi_in_process(ui_env, monkeypatch):
    """Under a frozen build, the web app's main() runs in this process.

    Re-executing the bootloader via subprocess would relaunch claudetrade
    itself, and actually calling the real main() here would start a live
    uvicorn server and hang the suite -- so the entry point is mocked and
    the test asserts it was chosen, with subprocess forbidden.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    import subprocess

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess must not run when sys.frozen is set")

    monkeypatch.setattr(subprocess, "call", _fail_if_called)

    import claudetrade.webapi.__main__ as webapi_main_module

    main_calls: list[list[str] | None] = []
    monkeypatch.setattr(
        webapi_main_module, "main", lambda argv=None: main_calls.append(argv)
    )

    result = runner.invoke(app, ["ui", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert main_calls == [["--port", "9999"]]


# ---------------------------------------------------------------------------
# --classic: the legacy Streamlit app
# ---------------------------------------------------------------------------


def test_ui_classic_spawns_streamlit_subprocess(ui_env, monkeypatch):
    """--classic on a normal install spawns `python -m streamlit run ...`."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls: list[list[str]] = []

    import subprocess

    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)

    in_process_calls = []
    monkeypatch.setattr(
        cli, "_run_streamlit_in_process", lambda *a, **k: in_process_calls.append((a, k))
    )

    result = runner.invoke(app, ["ui", "--classic", "--port", "9999"])

    assert result.exit_code == 0, result.output
    assert not in_process_calls, "frozen in-process path must not run when sys.frozen is unset"
    assert len(calls) == 1
    command = calls[0]
    assert command[0] == sys.executable
    assert "streamlit" in command
    assert "9999" in command


def test_ui_classic_frozen_runs_streamlit_in_process(ui_env, monkeypatch):
    """--classic under a frozen build uses Streamlit's bootstrap API in-process."""
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

    result = runner.invoke(app, ["ui", "--classic", "--port", "9999"])

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
