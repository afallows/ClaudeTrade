"""Configuration and diagnostics API security contract."""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
from fastapi.testclient import TestClient

from claudetrade.config import AppConfig
from claudetrade.db.session import Database
from claudetrade.pipeline import Pipeline, PipelineResult
from claudetrade.secrets import SecretValue
from claudetrade.webapi.app import create_app


@pytest.fixture
def client(tmp_app_config: AppConfig, tmp_db: Database) -> TestClient:
    return TestClient(
        create_app(tmp_app_config, pipeline=Pipeline(tmp_app_config, tmp_db)),
        base_url="http://127.0.0.1",
    )


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


class TestRedditConnectivityTest:
    """``POST /api/system/credentials/reddit/test`` -- an on-demand live
    connectivity probe. Every test here mocks the transport (same pattern as
    ``tests/test_reddit_provider.py``); this endpoint must NEVER be exercised
    against the real network from a test.
    """

    def test_unsupported_source_returns_404(self, client) -> None:
        response = client.post("/api/system/credentials/x/test")
        assert response.status_code == 404

    def test_not_configured_reports_ok_false(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_SESSION_COOKIE", raising=False)
        response = client.post("/api/system/credentials/reddit/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["mode"] is None

    def test_successful_cookie_session_probe(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_REDDIT_SESSION_COOKIE", "owner-cookie")

        def handler(request):
            import httpx as _httpx

            return _httpx.Response(
                200,
                json={
                    "kind": "Listing",
                    "data": {
                        "children": [
                            {
                                "kind": "t3",
                                "data": {
                                    "id": "a",
                                    "name": "t3_a",
                                    "created_utc": dt.datetime.now(tz=dt.UTC).timestamp(),
                                    "title": "hi",
                                    "selftext": "",
                                    "score": 1,
                                    "num_comments": 0,
                                    "author": "u",
                                },
                            }
                        ],
                        "after": None,
                    },
                },
            )

        _mock_reddit_transport(monkeypatch, handler)
        response = client.post("/api/system/credentials/reddit/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["mode"].startswith("cookie_session")
        assert "reddit_session only" in body["mode"]

    def test_blocked_probe_mentions_token_v2_in_detail(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_REDDIT_SESSION_COOKIE", "owner-cookie")

        def handler(request):
            import httpx as _httpx

            return _httpx.Response(403, json={"error": "blocked"})

        _mock_reddit_transport(monkeypatch, handler)
        response = client.post("/api/system/credentials/reddit/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "token_v2" in body["status_detail"]
        assert "not sensitive" not in body["status_detail"]  # sanity: no leaked secret marker


def _mock_reddit_transport(monkeypatch, handler) -> None:
    import httpx

    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.social.reddit.httpx.Client", _factory)


class TestBackgroundRefresh:
    """``POST /api/system/refresh`` / ``GET /api/system/refresh/status`` --
    item 5's UI-first-startup mechanism: the setup script starts the UI
    first and triggers a refresh against this endpoint instead of blocking
    UI startup behind an inline ``claudetrade refresh``.
    """

    @pytest.fixture
    def client_and_pipeline(self, tmp_app_config, tmp_db):
        pipeline = Pipeline(tmp_app_config, tmp_db)
        client = TestClient(create_app(tmp_app_config, pipeline=pipeline), base_url="http://127.0.0.1")
        return client, pipeline

    def test_status_idle_before_any_refresh(self, client_and_pipeline):
        client, _ = client_and_pipeline
        body = client.get("/api/system/refresh/status").json()
        assert body["running"] is False
        assert body["phase"] == "idle"

    def test_start_returns_immediately_and_reports_running(self, client_and_pipeline):
        client, pipeline = client_and_pipeline
        started = threading.Event()
        release = threading.Event()

        def fake_refresh(*, start, end, symbols=None, progress_callback=None):
            started.set()
            if progress_callback:
                progress_callback("prices", 3, 10)
            release.wait(timeout=5)
            return PipelineResult()

        pipeline.refresh = fake_refresh

        response = client.post("/api/system/refresh")
        assert response.status_code == 200
        assert response.json() == {"started": True}

        assert started.wait(timeout=2)
        status_body = client.get("/api/system/refresh/status").json()
        assert status_body["running"] is True
        assert status_body["phase"] == "prices"
        assert status_body["symbols_done"] == 3
        assert status_body["symbols_total"] == 10

        release.set()

    def test_conflicting_refresh_returns_409(self, client_and_pipeline):
        client, pipeline = client_and_pipeline
        started = threading.Event()
        release = threading.Event()

        def fake_refresh(*, start, end, symbols=None, progress_callback=None):
            started.set()
            release.wait(timeout=5)
            return PipelineResult()

        pipeline.refresh = fake_refresh

        first = client.post("/api/system/refresh")
        assert first.status_code == 200
        assert started.wait(timeout=2)

        second = client.post("/api/system/refresh")
        assert second.status_code == 409

        release.set()

    def test_completed_refresh_reports_not_running_and_finished_at(self, client_and_pipeline):
        client, pipeline = client_and_pipeline

        def fake_refresh(*, start, end, symbols=None, progress_callback=None):
            return PipelineResult()

        pipeline.refresh = fake_refresh

        client.post("/api/system/refresh")
        deadline = time.monotonic() + 5
        body = client.get("/api/system/refresh/status").json()
        while body["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
            body = client.get("/api/system/refresh/status").json()

        assert body["running"] is False
        assert body["finished_at"] is not None

    def test_failed_refresh_is_reported_and_unblocks_the_next_run(self, client_and_pipeline):
        client, pipeline = client_and_pipeline

        def failing_refresh(*, start, end, symbols=None, progress_callback=None):
            raise RuntimeError("boom")

        pipeline.refresh = failing_refresh

        client.post("/api/system/refresh")
        deadline = time.monotonic() + 5
        body = client.get("/api/system/refresh/status").json()
        while body["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
            body = client.get("/api/system/refresh/status").json()

        assert body["running"] is False
        assert "boom" in body["last_error"]

        # A failed run must not leave the lock stuck -- the next request
        # starts cleanly rather than 409ing forever.
        second = client.post("/api/system/refresh")
        assert second.status_code == 200


class TestHostValidation:
    """DNS-rebinding guard: non-local Host headers are rejected.

    The API binds 127.0.0.1, but a hostile page can point its own hostname at
    127.0.0.1 and the browser will send the request with that Host -- CORS
    never applies because it is same-origin from the browser's view. With a
    credential-writing endpoint on this server, Host must be validated.
    """

    def test_rebound_host_is_rejected(self, client):
        response = client.get("/api/meta", headers={"host": "evil.example.com"})
        assert response.status_code == 400

    def test_localhost_host_is_accepted(self, client):
        response = client.get("/api/meta", headers={"host": "127.0.0.1:8765"})
        assert response.status_code == 200
