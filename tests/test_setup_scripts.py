"""Cheap static checks for the Windows one-script setup (ADR-0008 Decision 4).

These do NOT execute PowerShell or batch files -- there is no Windows/pwsh
runtime in CI for that, and these files are reviewed statically (see the
header comment of scripts/setup.ps1 for the full validation method and test
matrix). This just guards against the two failure modes that matter most for
an unattended reviewer: the files existing at all, and the required steps /
flags not silently disappearing from setup.ps1 in a future edit.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_PS1 = REPO_ROOT / "scripts" / "setup.ps1"
SETUP_BAT = REPO_ROOT / "scripts" / "setup.bat"


def test_setup_files_exist() -> None:
    assert SETUP_PS1.is_file(), "scripts/setup.ps1 is missing"
    assert SETUP_BAT.is_file(), "scripts/setup.bat is missing"


def test_setup_bat_relaunches_powershell_bypass() -> None:
    text = SETUP_BAT.read_text(encoding="utf-8")
    assert "powershell" in text.lower()
    assert "-ExecutionPolicy" in text
    assert "Bypass" in text
    assert "setup.ps1" in text


def test_setup_ps1_declares_required_flags() -> None:
    text = SETUP_PS1.read_text(encoding="utf-8")
    for flag in ("$SkipData", "$Classic", "$NoLaunch"):
        assert flag in text, f"setup.ps1 is missing the {flag} parameter"


def test_setup_ps1_covers_every_required_step() -> None:
    text = SETUP_PS1.read_text(encoding="utf-8")

    # (a) Python 3.11+ detection, with an official-source-only install path.
    assert "-3.11" in text
    assert "-3.12" in text
    assert "winget" in text
    assert "python.org" in text

    # (b) venv + dependency install.
    assert "-m venv" in text or "'venv'" in text
    assert "requirements.txt" in text
    assert "pip install -e" in text or "-e $ProjectRoot" in text

    # (c) database init.
    assert "init" in text

    # (d) first data load, with a non-fatal degraded path.
    assert "refresh" in text
    assert "probe" in text  # the hint printed on a degraded/failed refresh

    # (e) launch the UI, honouring --classic passthrough.
    assert "claudetrade" in text.lower()
    assert " ui" in text or "'ui'" in text
    assert "--classic" in text

    # Idempotency / re-runnability: venv creation is conditional on existence.
    assert "Test-Path" in text

    # A transcript log is written next to the script.
    assert "Start-Transcript" in text
    assert "$LogPath" in text or "setup-log" in text

    # Non-zero exit on genuine failure.
    assert "exit $Code" in text or "Exit-Setup" in text


def test_setup_ps1_documents_it_was_not_executed_on_windows() -> None:
    """The script must not claim to have been run/verified on Windows."""
    text = SETUP_PS1.read_text(encoding="utf-8")
    assert "has NOT been executed" in text
    assert "TEST MATRIX" in text
