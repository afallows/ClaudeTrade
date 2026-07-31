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
        response = client.post("/api/system/credentials/stocktwits/test")
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


class TestAIConfigEndpoint:
    """``GET``/``PUT /api/system/ai-config`` -- the Configuration screen's
    provider/model switcher, immediately effective for this process."""

    def test_get_reflects_defaults(self, client) -> None:
        body = client.get("/api/system/ai-config").json()
        assert body["provider"] == "none"
        assert body["model"] == ""
        assert body["anthropic_default_model"] == "claude-opus-5"
        assert body["anthropic_api_key_credential"] == "anthropic_api_key"
        assert body["openai_api_key_credential"] == "openai_api_key"

    def test_put_updates_provider_and_model_immediately(self, client, tmp_app_config) -> None:
        response = client.put(
            "/api/system/ai-config", json={"provider": "anthropic", "model": "claude-haiku-4-5"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "anthropic"
        assert body["model"] == "claude-haiku-4-5"
        assert body["persisted"] is False
        assert tmp_app_config.ai.provider == "anthropic"
        assert tmp_app_config.ai.model == "claude-haiku-4-5"

        # The change is immediately visible on a subsequent GET, and the
        # provider selection is reflected in diagnostics too.
        assert client.get("/api/system/ai-config").json()["provider"] == "anthropic"
        pipelines = client.get("/api/system/diagnostics").json()["pipelines"]
        ai_pipeline = next(p for p in pipelines if p["name"] == "AI classifier")
        assert ai_pipeline["provider"] == "anthropic"

    def test_put_rejects_unknown_provider(self, client) -> None:
        response = client.put("/api/system/ai-config", json={"provider": "not-real"})
        assert response.status_code == 422


class TestXConnectivityTest:
    """``POST /api/system/credentials/x/test`` -- mirrors
    ``TestRedditConnectivityTest`` exactly; the transport is always mocked.
    """

    def test_not_configured_reports_ok_false(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_CT0", raising=False)
        response = client.post("/api/system/credentials/x/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["mode"] is None

    def test_successful_session_probe(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "owner-auth-token")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "owner-ct0")

        payload = {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {"timeline": {"instructions": [{"entries": []}]}}
                }
            }
        }

        def handler(request):
            import httpx as _httpx

            return _httpx.Response(200, json=payload)

        _mock_x_transport(monkeypatch, handler)
        response = client.post("/api/system/credentials/x/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["mode"] == "session"

    def test_blocked_probe_reports_failure_without_leaking_cookies(self, client, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "owner-auth-token")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "owner-ct0")

        def handler(request):
            import httpx as _httpx

            return _httpx.Response(403, json={"errors": [{"message": "forbidden"}]})

        _mock_x_transport(monkeypatch, handler)
        response = client.post("/api/system/credentials/x/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "owner-auth-token" not in response.text
        assert "owner-ct0" not in response.text


class TestAIConnectivityTest:
    """``POST /api/system/credentials/ai/test`` -- one minimal, fully-mocked
    classification call against the configured Claude/ChatGPT provider.
    """

    def test_provider_none_reports_ok_false(self, client, tmp_app_config) -> None:
        tmp_app_config.ai.provider = "none"
        response = client.post("/api/system/credentials/ai/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["mode"] is None

    def test_anthropic_not_configured_reports_ok_false(self, client, tmp_app_config, monkeypatch) -> None:
        tmp_app_config.ai.provider = "anthropic"
        monkeypatch.delenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", raising=False)
        response = client.post("/api/system/credentials/ai/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "anthropic_api_key" in body["status_detail"]

    def test_anthropic_successful_probe(self, client, tmp_app_config, monkeypatch) -> None:
        import json as _json
        from types import SimpleNamespace

        tmp_app_config.ai.provider = "anthropic"
        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")

        sentiment_payload = {
            "bullish": 0.6, "bearish": 0.1, "neutral": 0.3, "uncertainty": 0.1,
            "sarcasm": 0.0, "fear": 0.0, "hype": 0.1, "fomo": 0.0,
            "capitulation": 0.0, "earnings_speculation": 0.2, "product_catalyst": 0.0,
            "regulatory_catalyst": 0.0, "rumour": 0.0, "short_squeeze": 0.0,
            "pump_and_dump": 0.0, "position_disclosure": 0.0, "confidence": 0.7,
            "evidence": ["broke out on strong volume"],
        }
        block = SimpleNamespace(type="text", text=_json.dumps(sentiment_payload))
        fake_response = SimpleNamespace(
            content=[block],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )
        fake_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_response))

        import claudetrade.providers.ai.anthropic_provider as anthropic_provider

        monkeypatch.setattr(
            anthropic_provider.AnthropicProvider, "_get_client", lambda self, anthropic: fake_client
        )

        response = client.post("/api/system/credentials/ai/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["mode"] == "anthropic"
        assert "sk-ant-test" not in response.text


def _mock_x_transport(monkeypatch, handler) -> None:
    import httpx

    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.social.x_provider.httpx.Client", _factory)
    monkeypatch.setattr("claudetrade.providers.social.x_provider.time.sleep", lambda *_: None)


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
