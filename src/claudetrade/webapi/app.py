"""FastAPI application factory and static SPA mount.

Security model (documented once, here, and repeated in
``claudetrade.webapi.__main__``): this app is built to be served on
``127.0.0.1`` only, for one operator on their own machine, with **no
authentication**. That is a deliberate scope decision (ADR-0008 Decision 2:
"localhost, personal use") -- do not put this behind a public bind address,
a reverse proxy, or a port-forward without adding auth first, because every
endpoint that can act (scan, refresh, open a paper trade) has no access
control beyond "can reach this loopback port".
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline
from claudetrade.version import CODE_VERSION, DISCLAIMER
from claudetrade.webapi.routers import dashboard, paper, signals, system, tickers

#: Built frontend assets (``npm run build`` output), committed to the repo so
#: end users never need Node -- see ``frontend/DESIGN.md``.
STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: AppConfig, pipeline: Pipeline | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        config: Effective application configuration.
        pipeline: An already-constructed ``Pipeline`` (tests inject one built
            against a migrated tmp database, e.g. via ``tests/conftest.py``'s
            ``tmp_db`` fixture). When omitted, ``Pipeline.bootstrap`` opens the
            database and applies migrations, matching every other entry point.
    """
    app = FastAPI(
        title="ClaudeTrade",
        version=CODE_VERSION,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    # The server binds 127.0.0.1 only, but binding alone does not stop DNS
    # rebinding: a hostile page can point its own hostname at 127.0.0.1 and
    # the browser will happily send requests here with that hostname in the
    # Host header -- same-origin as far as the browser is concerned, so CORS
    # never enters into it. Since this API can WRITE credentials
    # (PUT /api/credentials/*), reject any request whose Host is not a local
    # name. Port numbers are ignored by the middleware's comparison.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
    )
    app.state.config = config
    app.state.pipeline = pipeline or Pipeline.bootstrap(config)
    app.state.last_scan_result = None

    app.include_router(signals.router)
    app.include_router(tickers.router)
    app.include_router(dashboard.router)
    app.include_router(paper.router)
    app.include_router(system.router)

    @app.get("/api/meta", tags=["meta"])
    def meta() -> dict[str, object]:
        """Build identity and the standing research-only disclaimer."""
        return {"code_version": CODE_VERSION, "disclaimer": DISCLAIMER}

    _mount_static(app)
    return app


def _mount_static(app: FastAPI) -> None:
    """Serve the built SPA, with a client-side-router-friendly catch-all.

    A no-op (API-only, 404 on ``/``) when ``static/`` hasn't been built yet --
    that is a normal state during frontend development, not an error.
    """
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_catch_all(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # Any other path (client-side route, or "/") falls through to the SPA
        # shell, which resolves it with its own router.
        return FileResponse(index_path)


__all__ = ["STATIC_DIR", "create_app"]
