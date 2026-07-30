"""Configuration and diagnostics API security contract."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.session import Database
from claudetrade.pipeline import Pipeline
from claudetrade.secrets import SecretValue
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    return TestClient(create_app(tmp_app_config, pipeline=Pipeline(tmp_app_config, tmp_db)))


def test_credentials_never_return_secret(client, monkeypatch) -> None:
    import claudetrade.webapi.routers.system as system
    monkeypatch.setattr(system, "get_secret", lambda name: SecretValue(name, "top-secret-1234", "keyring"))
    response = client.get("/api/system/credentials")
    assert response.status_code == 200
    assert "top-secret" not in response.text
    assert response.json()["credentials"][0]["masked"] == "****1234"


def test_write_credential_is_allowlisted_and_response_is_redacted(client, monkeypatch) -> None:
    import claudetrade.webapi.routers.system as system
    written = {}
    monkeypatch.setattr(system, "set_secret", lambda name, value: written.update(name=name, value=value) or "keyring")
    name = client.get("/api/system/credentials").json()["credentials"][0]["name"]
    response = client.put(f"/api/system/credentials/{name}", json={"value": "sensitive-value"})
    assert response.status_code == 200
    assert written == {"name": name, "value": "sensitive-value"}
    assert "sensitive-value" not in response.text
    assert client.put("/api/system/credentials/unapproved", json={"value": "x"}).status_code == 404


def test_diagnostics_has_price_and_sentiment_pipelines(client) -> None:
    response = client.get("/api/system/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert {item["kind"] for item in body["pipelines"]} == {"stock_price", "sentiment"}
    assert all(item["status"] in {"reachable", "configured", "not_configured"} for item in body["pipelines"])
    assert all("secret" not in item for item in body["pipelines"])
