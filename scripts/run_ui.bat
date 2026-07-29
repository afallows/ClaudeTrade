@echo off
REM Windows launcher for ClaudeTrade Streamlit UI
REM Activates the virtual environment and starts the app

setlocal enabledelayedexpansion

REM Find the script directory
set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR:~0,-9%
set VENV_PATH=%PROJECT_ROOT%.venv

REM Activate virtual environment if it exists
if exist "%VENV_PATH%\Scripts\activate.bat" (
    echo Activating virtual environment...
    call "%VENV_PATH%\Scripts\activate.bat"
) else (
    echo Virtual environment not found at %VENV_PATH%
    echo Running without activation (Python must be in PATH)
)

REM Get the configured port from config or use default
set PORT=8501

REM Change to project directory
cd /d "%PROJECT_ROOT%"

REM Run Streamlit
echo Starting ClaudeTrade UI on port %PORT%...
echo Open your browser to http://localhost:%PORT%

streamlit run src/claudetrade/ui/app.py --server.port=%PORT% --logger.level=info

pause
