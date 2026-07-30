"""Launch the ClaudeTrade web UI: FastAPI + the built React SPA, in a desktop
shell when one is available.

    python -m claudetrade.webapi [--port PORT] [--config PATH] [--no-window]

Security model: the server binds to ``127.0.0.1`` only and has **no
authentication** -- this module implements ADR-0008 Decision 2's "a local web
app in a desktop shell" for one operator on their own machine. See
``claudetrade.webapi.app`` for the full statement of that assumption. Do not
expose this port beyond localhost.

Startup sequence:

1. Load configuration (``AppConfig.load``) and bootstrap the ``Pipeline``
   (applies migrations, matching every other entry point).
2. Start uvicorn in a background thread bound to ``127.0.0.1:<port>``.
3. Try to open a native window via ``pywebview``. When no GUI backend is
   available (headless box, missing system Qt/GTK libraries -- this raises
   ``webview.errors.WebViewException``) or ``--no-window`` was passed, fall
   back to the system default browser via ``webbrowser.open`` and keep the
   server thread alive until interrupted.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from claudetrade.config import AppConfig
from claudetrade.logging_setup import get_logger, setup_logging
from claudetrade.pipeline import Pipeline
from claudetrade.version import CODE_VERSION
from claudetrade.webapi.app import create_app

log = get_logger(__name__)

#: Non-negotiable: see the module docstring's security model.
HOST = "127.0.0.1"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m claudetrade.webapi",
        description="Launch the ClaudeTrade web UI (FastAPI + React SPA).",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="TCP port on 127.0.0.1 (default: config.ui.port)"
    )
    parser.add_argument("--config", type=str, default=None, help="Path to a config.toml")
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Always use the system browser; skip the pywebview native window",
    )
    return parser.parse_args(argv)


def _wait_until_listening(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll until ``host:port`` accepts a connection, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    config = AppConfig.load(args.config)
    setup_logging(config)
    port = args.port or config.ui.port

    pipeline = Pipeline.bootstrap(config)
    app = create_app(config, pipeline=pipeline)

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True, name="claudetrade-webapi")
    server_thread.start()

    if not _wait_until_listening(HOST, port):
        log.error("web server did not become ready on %s:%s", HOST, port)
        sys.exit(1)

    url = f"http://{HOST}:{port}/"
    print(f"ClaudeTrade v{CODE_VERSION} -- serving {url}")

    opened_native = False
    if not args.no_window:
        try:
            import webview

            webview.create_window("ClaudeTrade", url, width=1440, height=920, min_size=(1024, 700))
            webview.start()
            opened_native = True
        except Exception as exc:
            # No GUI backend installed (headless server, missing system Qt/GTK
            # libraries, ...) -- fall through to the plain-browser path rather
            # than crashing, matching ADR-0008 Decision 2's stated fallback.
            log.warning("native window unavailable (%s); opening the system browser instead", exc)

    if not opened_native:
        webbrowser.open(url)
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            server.should_exit = True
            server_thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
