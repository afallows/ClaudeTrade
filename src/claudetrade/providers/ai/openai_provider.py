"""OpenAI API adapter for LLM-based sentiment classification.

Uses the official OpenAI REST API (Chat Completions endpoint) with httpx.
Requires a valid API key via the secret resolver.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

from claudetrade.config import AIConfig
from claudetrade.providers.ai.base_llm import (
    _build_system_prompt,
    build_ai_response,
    build_sentiment_prompt,
)
from claudetrade.providers.base import (
    AIRequest,
    AIResponse,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
)
from claudetrade.secrets import get_secret

log = logging.getLogger(__name__)


class OpenAIProvider:
    """OpenAI ChatGPT provider for structured sentiment analysis.

    Default model is gpt-4o-mini. Falls back to null-provider behavior
    (no-op responses) if credentials are absent.
    """

    name: str = "openai"

    def __init__(self, config: AIConfig):
        """Initialize the OpenAI provider.

        Args:
            config: AIConfig with api_key_credential and model.

        No exception on missing credentials; the provider silently degrades
        to null-provider behaviour (see status() and complete()).
        """
        self.config = config
        self.model = config.model or "gpt-4o-mini"

        # Try to resolve credentials
        self._api_key: str | None = None
        self._has_credentials = False
        secret = get_secret(config.api_key_credential)
        if secret is not None:
            self._api_key = secret.reveal()
            self._has_credentials = True
            log.info("OpenAI provider configured with model=%s", self.model)
        else:
            log.debug(
                "OpenAI provider credential '%s' not found; operating in null mode",
                config.api_key_credential,
            )

        self._rate_limiter = RateLimiter(
            config.ai.rate_limit_per_minute if hasattr(config, "ai") else 60,
            name="openai",
            max_wait_s=config.request_timeout_s if hasattr(config, "request_timeout_s") else 45.0,
        )
        self._logged_no_credentials = False

    def status(self) -> ProviderStatus:
        """Report provider health and capabilities."""
        if not self._has_credentials:
            return ProviderStatus(
                name=self.name,
                kind="ai",
                available=False,
                configured=False,
                message=f"Credential '{self.config.api_key_credential}' not configured",
                supports_point_in_time=False,
                licence_note="Requires paid OpenAI API tier",
            )
        return ProviderStatus(
            name=self.name,
            kind="ai",
            available=True,
            configured=True,
            message=f"OpenAI {self.model} ready",
            supports_point_in_time=False,
            rate_limit_per_minute=None,  # Vendor-enforced
            licence_note="Requires paid OpenAI API tier for production use",
            capabilities={"sentiment": True, "batch": False},
        )

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute one AI task via OpenAI.

        Args:
            request: AIRequest.

        Returns:
            Fully-populated AIResponse. On no credentials, behaves like null provider.
        """
        if not self._has_credentials:
            if not self._logged_no_credentials:
                log.info("OpenAI: no credentials; returning fallback response")
                self._logged_no_credentials = True
            return AIResponse(
                task=request.task,
                provider=self.name,
                model=self.model,
                prompt_version=request.prompt_version,
                created_at=dt.datetime.now(tz=dt.UTC),
                data=None,
                parsed_ok=False,
                fallback_used="no_credentials",
            )

        user_message = build_sentiment_prompt(request.payload)

        try:
            self._rate_limiter.acquire()
        except RateLimitError as exc:
            log.warning("OpenAI rate limit: %s", exc)
            return AIResponse(
                task=request.task,
                provider=self.name,
                model=self.model,
                prompt_version=request.prompt_version,
                created_at=dt.datetime.now(tz=dt.UTC),
                data=None,
                parsed_ok=False,
                error=str(exc),
            )

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.config.request_timeout_s) as client:
                response = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": _build_system_prompt()},
                            {"role": "user", "content": user_message},
                        ],
                        "temperature": request.temperature,
                        "max_tokens": request.max_output_tokens,
                    },
                )
                latency_ms = (time.monotonic() - start) * 1000

                if response.status_code == 401:
                    log.error("OpenAI authentication failed")
                    return AIResponse(
                        task=request.task,
                        provider=self.name,
                        model=self.model,
                        prompt_version=request.prompt_version,
                        created_at=dt.datetime.now(tz=dt.UTC),
                        data=None,
                        parsed_ok=False,
                        error="authentication failed",
                        latency_ms=latency_ms,
                    )

                if response.status_code == 429:
                    log.warning("OpenAI rate limit hit")
                    raise RateLimitError(
                        "OpenAI rate limit reached",
                        provider=self.name,
                        retry_after_s=60.0,
                    )

                response.raise_for_status()

                payload = response.json()
                choices = payload.get("choices", [])
                if not choices:
                    return AIResponse(
                        task=request.task,
                        provider=self.name,
                        model=self.model,
                        prompt_version=request.prompt_version,
                        created_at=dt.datetime.now(tz=dt.UTC),
                        data=None,
                        parsed_ok=False,
                        error="no choices in response",
                        latency_ms=latency_ms,
                    )

                raw_text = choices[0].get("message", {}).get("content", "")
                input_tokens = payload.get("usage", {}).get("prompt_tokens", 0)
                output_tokens = payload.get("usage", {}).get("completion_tokens", 0)

                return build_ai_response(
                    task=request.task,
                    provider_name=self.name,
                    model=self.model,
                    prompt_version=request.prompt_version,
                    raw_text=raw_text,
                    schema_name=request.schema_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    config=self.config,
                )

        except httpx.TimeoutException:
            log.warning("OpenAI request timeout")
            return AIResponse(
                task=request.task,
                provider=self.name,
                model=self.model,
                prompt_version=request.prompt_version,
                created_at=dt.datetime.now(tz=dt.UTC),
                data=None,
                parsed_ok=False,
                error="request timeout",
                latency_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as exc:
            log.exception("OpenAI request failed: %s", exc)
            return AIResponse(
                task=request.task,
                provider=self.name,
                model=self.model,
                prompt_version=request.prompt_version,
                created_at=dt.datetime.now(tz=dt.UTC),
                data=None,
                parsed_ok=False,
                error=f"request error: {exc}",
                latency_ms=(time.monotonic() - start) * 1000,
            )

    def complete_batch(self, requests: list[AIRequest]) -> list[AIResponse]:
        """Execute several AI tasks by looping with rate limiter.

        OpenAI has no native batch API in the synchronous path, so we loop
        with rate limiting. For production use, consider the async batch API.

        Args:
            requests: List of AIRequest.

        Returns:
            List of AIResponse in the same order.
        """
        return [self.complete(req) for req in requests]
