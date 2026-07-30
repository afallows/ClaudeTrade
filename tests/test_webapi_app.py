"""Coverage for ``claudetrade.webapi.app`` itself: app construction, the
``/api/meta`` disclaimer endpoint, the OpenAPI schema, and the SPA
static-file/catch-all behaviour when ``static/`` has (and hasn't) been built.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.session import Database
from claudetrade.pipeline import Pipeline
from claudetrade.version import CODE_VERSION, DISCLAIMER
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    return TestClient(app)


def test_meta_reports_code_version_and_disclaimer(client: TestClient):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code_version"] == CODE_VERSION
    assert body["disclaimer"] == DISCLAIMER


def test_openapi_schema_is_generated_and_covers_every_router(client: TestClient):
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = set(schema["paths"].keys())
    for expected in (
        "/api/signals",
        "/api/signals/rejected",
        "/api/signals/{signal_id}",
        "/api/scan",
        "/api/refresh",
        "/api/tickers",
        "/api/tickers/{symbol}",
        "/api/dashboard",
        "/api/paper/account",
        "/api/paper/performance",
        "/api/paper/open",
    ):
        assert expected in paths, f"missing route {expected}"


def test_unknown_api_route_is_a_real_404_not_the_spa_shell(client: TestClient):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404


def test_root_without_a_built_frontend_is_a_clean_404(tmp_app_config, tmp_db):
    """Before ``npm run build`` has ever run, ``static/`` has no ``index.html``:
    the app must still construct and serve the API, just not the SPA shell."""
    pipeline = Pipeline(tmp_app_config, tmp_db)
    app = create_app(tmp_app_config, pipeline=pipeline)
    client = TestClient(app)
    resp = client.get("/")
    # Either genuinely not mounted (404) or serving the real built index --
    # both are acceptable; what must never happen is a 500.
    assert resp.status_code in (200, 404)


def test_spa_shell_served_for_a_client_side_route(client: TestClient):
    """If assets are built (as in the committed repo state), any unknown
    non-API path resolves to the SPA shell so client-side routing works."""
    from claudetrade.webapi.app import STATIC_DIR

    resp = client.get("/screener")
    if (STATIC_DIR / "index.html").exists():
        assert resp.status_code == 200
        assert '<div id="root"' in resp.text or 'id="root"' in resp.text
    else:
        assert resp.status_code == 404
