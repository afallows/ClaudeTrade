"""Anthropic/OpenAI AI provider adapters: SDK client paths, mocked at the
client-call boundary (never the real network -- ``AnthropicProvider._get_client``
/ ``OpenAIProvider._get_client`` are monkeypatched to return a stub whose
``messages.create``/``chat.completions.create`` is fully under test control).

Covers: successful classification -> parsed scores; typed SDK exceptions
(RateLimitError, APIStatusError, APIConnectionError) -> degrade to
``parsed_ok=False`` without raising; missing credentials -> clean fallback;
missing optional dependency -> clean fallback with an actionable error, only
surfaced when the provider is actually invoked.
"""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import httpx
import pytest

from claudetrade.config import AIConfig
from claudetrade.providers.ai.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from claudetrade.providers.ai.anthropic_provider import AnthropicProvider
from claudetrade.providers.ai.openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from claudetrade.providers.ai.openai_provider import OpenAIProvider
from claudetrade.providers.base import AIRequest

# The SDKs are optional extras (``claudetrade[anthropic]`` / ``[openai]``).
# Tests that drive ``complete()`` past the SDK import -- or raise the SDK's
# own typed exceptions -- need the real package; the no-credentials and
# missing-dependency degradation tests must keep running without it.
requires_anthropic_sdk = pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is None,
    reason="anthropic SDK not installed (pip install claudetrade[anthropic])",
)
requires_openai_sdk = pytest.mark.skipif(
    importlib.util.find_spec("openai") is None,
    reason="openai SDK not installed (pip install claudetrade[openai])",
)


def _isolate_credentials(monkeypatch) -> None:
    """Make 'no credentials' mean it: clear env AND the OS credential store.

    Secret resolution falls back to the Windows Credential Manager / Keychain
    (``claudetrade.secrets._keyring_backend``), so a developer machine with a
    real key stored would otherwise flip the no-credential assertions.
    """
    monkeypatch.setattr("claudetrade.secrets._keyring_backend", lambda: None)


def _valid_sentiment_payload() -> dict:
    return {
        "bullish": 0.7,
        "bearish": 0.1,
        "neutral": 0.2,
        "uncertainty": 0.1,
        "sarcasm": 0.0,
        "fear": 0.0,
        "hype": 0.3,
        "fomo": 0.1,
        "capitulation": 0.0,
        "earnings_speculation": 0.0,
        "product_catalyst": 0.0,
        "regulatory_catalyst": 0.0,
        "rumour": 0.0,
        "short_squeeze": 0.0,
        "pump_and_dump": 0.0,
        "position_disclosure": 0.0,
        "confidence": 0.8,
        "evidence": ["breaking out on volume"],
    }


def _request() -> AIRequest:
    return AIRequest(
        task="sentiment",
        payload={"symbol": "AAPL", "text": "<<<TEXT\nShares breaking out.\nTEXT>>>"},
        schema_name="sentiment_classification_v1",
        max_output_tokens=256,
    )


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class TestAnthropicProvider:
    def test_no_credentials_degrades_cleanly(self, monkeypatch):
        _isolate_credentials(monkeypatch)
        monkeypatch.delenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", raising=False)
        provider = AnthropicProvider(AIConfig(provider="anthropic"))
        assert provider.has_credentials is False
        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert response.fallback_used == "no_credentials"

    def test_default_model_used_when_config_model_empty(self, monkeypatch):
        monkeypatch.delenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", raising=False)
        provider = AnthropicProvider(AIConfig(provider="anthropic", model=""))
        assert provider.model == ANTHROPIC_DEFAULT_MODEL == "claude-opus-5"

    def test_missing_sdk_dependency_degrades_cleanly(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))
        assert provider.has_credentials is True

        def _boom():
            raise ImportError("no anthropic package here")

        monkeypatch.setattr(
            "claudetrade.providers.ai.anthropic_provider._require_anthropic_sdk", _boom
        )
        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert response.fallback_used == "missing_dependency"
        assert "anthropic" in response.error

    @requires_anthropic_sdk
    def test_successful_classification_parses_structured_output(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))

        block = SimpleNamespace(type="text", text=json.dumps(_valid_sentiment_payload()))
        fake_response = SimpleNamespace(
            content=[block],
            usage=SimpleNamespace(input_tokens=42, output_tokens=17),
            stop_reason="end_turn",
        )
        captured: dict = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_response

        fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
        monkeypatch.setattr(provider, "_get_client", lambda anthropic: fake_client)

        response = provider.complete(_request())

        assert response.parsed_ok is True
        assert response.data["bullish"] == pytest.approx(0.7)
        assert response.input_tokens == 42
        assert response.output_tokens == 17
        # Never sends removed sampling params; explicitly disables thinking
        # for this short, bounded classification call.
        assert "temperature" not in captured
        assert "top_p" not in captured
        assert "top_k" not in captured
        assert captured["thinking"] == {"type": "disabled"}
        assert captured["output_config"]["format"]["type"] == "json_schema"

    @requires_anthropic_sdk
    def test_refusal_stop_reason_degrades_without_raising(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))

        fake_response = SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=5, output_tokens=0), stop_reason="refusal")
        fake_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_response))
        monkeypatch.setattr(provider, "_get_client", lambda anthropic: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "refus" in response.error

    @requires_anthropic_sdk
    def test_rate_limit_error_degrades_without_raising(self, monkeypatch):
        import anthropic as real_anthropic

        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(429, request=req)

        def _raise(**kwargs):
            raise real_anthropic.RateLimitError("rate limited", response=resp, body=None)

        fake_client = SimpleNamespace(messages=SimpleNamespace(create=_raise))
        monkeypatch.setattr(provider, "_get_client", lambda anthropic: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "rate limited" in response.error.lower()

    @requires_anthropic_sdk
    def test_api_status_error_degrades_without_raising(self, monkeypatch):
        import anthropic as real_anthropic

        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(404, request=req)

        def _raise(**kwargs):
            raise real_anthropic.NotFoundError("model not found", response=resp, body=None)

        fake_client = SimpleNamespace(messages=SimpleNamespace(create=_raise))
        monkeypatch.setattr(provider, "_get_client", lambda anthropic: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "api error" in response.error.lower()

    @requires_anthropic_sdk
    def test_connection_error_degrades_without_raising(self, monkeypatch):
        import anthropic as real_anthropic

        monkeypatch.setenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
        provider = AnthropicProvider(AIConfig(provider="anthropic"))

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        def _raise(**kwargs):
            raise real_anthropic.APIConnectionError(request=req)

        fake_client = SimpleNamespace(messages=SimpleNamespace(create=_raise))
        monkeypatch.setattr(provider, "_get_client", lambda anthropic: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "connection error" in response.error.lower()

    def test_complete_batch_never_raises_on_mixed_failures(self, monkeypatch):
        _isolate_credentials(monkeypatch)
        monkeypatch.delenv("CLAUDETRADE_SECRET_ANTHROPIC_API_KEY", raising=False)
        provider = AnthropicProvider(AIConfig(provider="anthropic"))
        responses = provider.complete_batch([_request(), _request()])
        assert len(responses) == 2
        assert all(r.parsed_ok is False for r in responses)


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


class TestOpenAIProvider:
    def test_no_credentials_degrades_cleanly(self, monkeypatch):
        _isolate_credentials(monkeypatch)
        monkeypatch.delenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", raising=False)
        provider = OpenAIProvider(AIConfig(provider="openai"))
        assert provider.has_credentials is False
        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert response.fallback_used == "no_credentials"

    def test_default_model_used_when_config_model_empty(self, monkeypatch):
        monkeypatch.delenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", raising=False)
        provider = OpenAIProvider(AIConfig(provider="openai", model=""))
        assert provider.model == OPENAI_DEFAULT_MODEL

    def test_missing_sdk_dependency_degrades_cleanly(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", "sk-test")
        provider = OpenAIProvider(AIConfig(provider="openai"))
        assert provider.has_credentials is True

        def _boom():
            raise ImportError("no openai package here")

        monkeypatch.setattr(
            "claudetrade.providers.ai.openai_provider._require_openai_sdk", _boom
        )
        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert response.fallback_used == "missing_dependency"

    @requires_openai_sdk
    def test_successful_classification_parses_structured_output(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", "sk-test")
        provider = OpenAIProvider(AIConfig(provider="openai"))

        message = SimpleNamespace(content=json.dumps(_valid_sentiment_payload()))
        fake_response = SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=12),
        )
        captured: dict = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_response

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )
        monkeypatch.setattr(provider, "_get_client", lambda openai: fake_client)

        response = provider.complete(_request())

        assert response.parsed_ok is True
        assert response.data["bullish"] == pytest.approx(0.7)
        assert response.input_tokens == 30
        assert response.output_tokens == 12
        assert captured["response_format"]["type"] == "json_schema"

    @requires_openai_sdk
    def test_rate_limit_error_degrades_without_raising(self, monkeypatch):
        import openai as real_openai

        monkeypatch.setenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", "sk-test")
        provider = OpenAIProvider(AIConfig(provider="openai"))

        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(429, request=req)

        def _raise(**kwargs):
            raise real_openai.RateLimitError("rate limited", response=resp, body=None)

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_raise))
        )
        monkeypatch.setattr(provider, "_get_client", lambda openai: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "rate limited" in response.error.lower()

    @requires_openai_sdk
    def test_connection_error_degrades_without_raising(self, monkeypatch):
        import openai as real_openai

        monkeypatch.setenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", "sk-test")
        provider = OpenAIProvider(AIConfig(provider="openai"))

        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

        def _raise(**kwargs):
            raise real_openai.APIConnectionError(request=req)

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_raise))
        )
        monkeypatch.setattr(provider, "_get_client", lambda openai: fake_client)

        response = provider.complete(_request())
        assert response.parsed_ok is False
        assert "connection error" in response.error.lower()

    def test_complete_batch_never_raises_on_mixed_failures(self, monkeypatch):
        _isolate_credentials(monkeypatch)
        monkeypatch.delenv("CLAUDETRADE_SECRET_OPENAI_API_KEY", raising=False)
        provider = OpenAIProvider(AIConfig(provider="openai"))
        responses = provider.complete_batch([_request(), _request()])
        assert len(responses) == 2
        assert all(r.parsed_ok is False for r in responses)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestAIConfig:
    def test_default_provider_is_none_ai_stays_opt_in(self):
        assert AIConfig().provider == "none"

    def test_credential_names_are_provider_specific(self):
        cfg = AIConfig()
        assert cfg.anthropic_api_key_credential == "anthropic_api_key"
        assert cfg.openai_api_key_credential == "openai_api_key"

    def test_api_key_credential_property_follows_selected_provider(self):
        assert AIConfig(provider="anthropic").api_key_credential == "anthropic_api_key"
        assert AIConfig(provider="openai").api_key_credential == "openai_api_key"
        # "none" falls back to the historical Anthropic-first default.
        assert AIConfig(provider="none").api_key_credential == "anthropic_api_key"

    def test_invalid_provider_rejected(self):
        with pytest.raises(ValueError):
            AIConfig(provider="not-a-real-provider")
