@echo off
REM Windows launcher for ClaudeTrade Streamlit UI
REM Activates the virtual environment and starts the app

setlocal enabledelayedexpansion

REM Find the script directory. %~dp0 ends in "...\scripts\"; strip that off to
REM get the project root. (Previous versions of this script built VENV_PATH as
REM "%PROJECT_ROOT%.venv" with no separator, which silently produced the wrong
REM path, e.g. "C:\...\ClaudeTrade.venv" instead of "C:\...\ClaudeTrade\.venv".)
set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR:~0,-9%
set VENV_PATH=%PROJECT_ROOT%\.venv

REM Activate virtual environment if it exists
if exist "%VENV_PATH%\Scripts\activate.bat" (
    echo Activating virtual environment...
    call "%VENV_PATH%\Scripts\activate.bat"
) else (
    echo Virtual environment not found at %VENV_PATH%
    echo Run scripts\build-windows.bat's prerequisites first, or see docs\windows-install.md.
    echo Continuing without activation ^(claudetrade/python must already be on PATH^).
)

REM Get the configured port from config or use default (matches UIConfig.port
REM in src\claudetrade\config.py; override with the [ui] port setting in
REM config.toml, or pass a port explicitly below).
set PORT=8501

REM Change to project directory
cd /d "%PROJECT_ROOT%"

REM Run the UI through the claudetrade CLI (src\claudetrade\cli.py: `ui`
REM command), which loads config, sets up logging and launches Streamlit --
REM equivalent to but more robust than calling `streamlit run` directly.
echo Starting ClaudeTrade UI on port %PORT%...
echo Open your browser to http://localhost:%PORT%

claudetrade ui --port %PORT%

pause
