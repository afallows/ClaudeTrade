@echo off
REM Build a standalone Windows executable for ClaudeTrade with PyInstaller.
REM
REM STATUS: this script and claudetrade.spec were authored on a Linux
REM development machine and have NEVER been run. PyInstaller does not
REM cross-compile, so producing and testing an actual Windows .exe requires
REM running this on a real Windows machine. See docs\windows-build.md for
REM the full explanation, prerequisites, and known caveats before running
REM this -- in particular, `claudetrade.exe ui` is not expected to work
REM without a follow-up code change; everything else should.
REM
REM Usage (from an activated venv, in the repo root):
REM   scripts\build-windows.bat

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR:~0,-9%
set VENV_PATH=%PROJECT_ROOT%\.venv

echo ClaudeTrade Windows build (PyInstaller, onedir)
echo ================================================
echo.

REM --- Prerequisite: virtual environment -----------------------------------
if not exist "%VENV_PATH%\Scripts\activate.bat" (
    echo ERROR: no virtual environment found at %VENV_PATH%
    echo Run the setup steps in docs\windows-install.md first:
    echo   py -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    echo   pip install -e .
    exit /b 1
)

echo Activating virtual environment...
call "%VENV_PATH%\Scripts\activate.bat"

cd /d "%PROJECT_ROOT%"

REM --- Prerequisite: build dependencies (PyInstaller + the app itself) ----
REM `pip install -e ".[build]"` pulls in pyinstaller>=6.6 (see the [build]
REM extra in pyproject.toml) on top of everything requirements.txt already
REM installed. This also re-installs claudetrade itself if it changed.
echo.
echo Installing build dependencies (pyinstaller)...
pip install -e ".[build]"
if errorlevel 1 (
    echo ERROR: pip install failed. See the output above.
    exit /b 1
)

REM --- Clean previous build output -----------------------------------------
if exist "%PROJECT_ROOT%\build" (
    echo Removing previous build\ directory...
    rmdir /s /q "%PROJECT_ROOT%\build"
)
if exist "%PROJECT_ROOT%\dist\claudetrade" (
    echo Removing previous dist\claudetrade\ directory...
    rmdir /s /q "%PROJECT_ROOT%\dist\claudetrade"
)

REM --- Build -----------------------------------------------------------------
echo.
echo Running PyInstaller (onedir mode; this can take several minutes,
echo especially the first time it has to collect Streamlit's frontend assets)...
pyinstaller claudetrade.spec
if errorlevel 1 (
    echo.
    echo BUILD FAILED. Common causes, in order of likelihood:
    echo   - A hidden import claudetrade.spec doesn't yet know about ^(see the
    echo     traceback for "ModuleNotFoundError" and add it to hiddenimports
    echo     in claudetrade.spec^)
    echo   - pywin32-ctypes missing ^(re-run: pip install -r requirements.txt^)
    echo   - PyInstaller itself not installed ^(re-run this script^)
    echo See docs\windows-build.md for the known caveats this build has not
    echo yet been validated against.
    exit /b 1
)

echo.
echo Build finished. Output is in dist\claudetrade\
echo.
echo Smoke-test the CLI commands first ^(these are expected to work^):
echo   dist\claudetrade\claudetrade.exe version
echo   dist\claudetrade\claudetrade.exe init
echo   dist\claudetrade\claudetrade.exe status
echo.
echo Then read docs\windows-build.md "Known caveats" before trying
echo   dist\claudetrade\claudetrade.exe ui
echo which is NOT expected to work without a follow-up code change to
echo src\claudetrade\cli.py's `ui` command.
echo.

endlocal
