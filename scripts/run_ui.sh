#!/bin/bash
# POSIX launcher for ClaudeTrade Streamlit UI
# Activates the virtual environment and starts the app

set -e

# Find script directory and project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( dirname "$SCRIPT_DIR" )"
VENV_PATH="$PROJECT_ROOT/.venv"

# Activate virtual environment if it exists
if [ -f "$VENV_PATH/bin/activate" ]; then
    echo "Activating virtual environment..."
    source "$VENV_PATH/bin/activate"
else
    echo "Warning: Virtual environment not found at $VENV_PATH"
    echo "Running without activation (Python must be in PATH)"
fi

# Get the configured port from config or use default (matches UIConfig.port
# in src/claudetrade/config.py)
PORT=8501

# Change to project directory
cd "$PROJECT_ROOT"

# Run the UI through the claudetrade CLI (src/claudetrade/cli.py: `ui`
# command), which loads config, sets up logging and launches Streamlit --
# equivalent to but more robust than calling `streamlit run` directly.
echo "Starting ClaudeTrade UI on port $PORT..."
echo "Open your browser to http://localhost:$PORT"

claudetrade ui --port "$PORT"
