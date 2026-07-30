@echo off
REM ClaudeTrade one-script setup (ADR-0008 Decision 4).
REM
REM Thin double-click wrapper: relaunches into Windows PowerShell (the 5.1
REM build that ships on every Windows 10/11 machine, so nothing extra needs
REM installing first) with -ExecutionPolicy Bypass scoped to this one process
REM only -- it does not change any system-wide or user-wide execution policy.
REM All real logic lives in setup.ps1; keep this file a thin relauncher.
REM
REM Usage:
REM   Double-click scripts\setup.bat, OR from a terminal:
REM   scripts\setup.bat [-SkipData] [-Classic] [-NoLaunch]
REM
REM (Flags are forwarded as-is to setup.ps1; see that file's header for what
REM each one does.)

setlocal

set SCRIPT_DIR=%~dp0

where powershell >nul 2>nul
if errorlevel 1 (
    echo ERROR: powershell.exe was not found on this machine.
    echo ClaudeTrade's setup script requires Windows PowerShell, which ships
    echo with every supported version of Windows 10/11. If you are seeing
    echo this, something unusual is going on with this machine's install of
    echo Windows -- see docs\windows-install.md for the manual setup steps
    echo as a fallback.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1" %*
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% NEQ 0 (
    echo setup.bat: setup.ps1 exited with code %EXITCODE%. See the transcript
    echo log next to this script ^(scripts\setup-log-*.txt^) for details.
)

pause
exit /b %EXITCODE%
