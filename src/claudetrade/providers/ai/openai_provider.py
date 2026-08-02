"""OpenAI API adapter for LLM-based sentiment classification.

Uses the **official OpenAI Python SDK** (``pip install openai``, or
``pip install claudetrade[openai]`` -- see ``pyproject.toml``'s optional
``openai`` extra), imported lazily via ``_require_openai_sdk`` so that
importing this module, and constructing ``OpenAIProvider`` itself, never
requires the package to be installed -- only an actual classification call
does (mirrors ``anthropic_provider.py``'s and ``mcp_server._require_fastmcp``'s
lazy-import pattern).

Sentiment classification uses OpenAI's JSON-schema structured output mode
(``response_format={"type": "json_schema", ...}``) against the same
``providers.ai.schemas.SENTIMENT_JSON_SCHEMA`` the Anthropic adapter uses --
see that constant's docstring for why it omits numeric range constraints.

**This module must never raise into the pipeline.** Every SDK-level failure
(missing package, no credentials, ``openai.RateLimitError``,
``openai.APIStatusError``, ``openai.APIConnectionError``, or any other
unexpected exception) is caught and turned into an ``AIResponse`` with
``parsed_ok=False`` -- ``AISentimentClassifier`` (``sentiment.ai_classifier``)
falls back to the deterministic rule ensemble on any such response, exactly
as it does for the null provider.

**Model naming is operator-configurable** (``AIConfig.model``; empty means
"use ``DEFAULT_MODEL`` below"). OpenAI's model lineup and pricing change
faster than this comment can track -- check current model names and pricing
at https://platform.openai.com before relying on the default here.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

from claudetrade.config import AIConfig
from claudetrade.providers.ai.base_llm import _build_system_prompt as build_system_prompt
from claudetrade.providers.ai.base_llm import build_ai_response, build_sentiment_prompt
from claudetrade.providers.ai.schemas import SENTIMENT_JSON_SCHEMA
from claudetrade.providers.base import AIRequest, AIResponse, ProviderStatus
from claudetrade.secrets import get_secret

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    import openai as openai_types

log = logging.getLogger(__name__)

#: Operator-configurable default -- see module docstring. Verify this is
#: still a current, available model name at https://platform.openai.com
#: before relying on it; OpenAI's lineup moves faster than this constant.
DEFAULT_MODEL = "gpt-5.1-mini"

SCHEMA_NAME = "sentiment_classification_v1"


def _require_openai_sdk() -> Any:
    """Import and return the ``openai`` module, or raise a clear error.

    Kept as its own function (rather than a bare module-level import) so
    that importing ``claudetrade.providers.ai.openai_provider`` -- and
    constructing ``OpenAIProvider`` -- never requires the package to be
    installed; only an actual API call does.
    """
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package (the official OpenAI Python SDK) is "
            "required to use ai.provider = 'openai'. Install it with "
            "`pip install claudetrade[openai]` (or `pip install openai`), "
            "then retry."
        ) from exc
    return openai


class OpenAIProvider:
    """OpenAI ChatGPT provider for structured sentiment analysis.

    Falls back to a "no credentials"/"no dependency" ``AIResponse``
    (``parsed_ok=False``) rather than raising, in every failure mode -- see
    the module docstring.
    """

    name: str = "openai"

    def __init__(self, config: AIConfig):
        """Initialize the OpenAI provider.

        Args:
            config: AIConfig with ``openai_api_key_credential`` and
                optionally ``model`` (empty uses ``DEFAULT_MODEL``).

        Never raises: a missing credential or a missing ``openai`` package
        is discovered lazily, on the first real ``complete()`` call.
        """
        self.config = config
        self.model = config.model or DEFAULT_MODEL

        self._api_key: str | None = None
        #: Public (not ``_``-prefixed): the credentials-test endpoint reads
        #: this directly rather than duplicating secret-resolution logic.
        self.has_credentials = False
        secret = get_secret(config.openai_api_key_credential)
        if secret is not None:
            self._api_key = secret.reveal()
            self.has_credentials = True
            log.info("OpenAI provider configured with model=%s", self.model)
        else:
            log.debug(
                "OpenAI provider credential '%s' not found; operating in null mode",
                config.openai_api_key_credential,
            )

        self._client: openai_types.OpenAI | None = None
        self._logged_no_credentials = False

    def status(self) -> ProviderStatus:
        """Report provider health and capabilities."""
        if not self.has_credentials:
            return ProviderStatus(
                name=self.name,
                kind="ai",
                available=False,
                configured=False,
                message=f"Credential '{self.config.openai_api_key_credential}' not configured",
                supports_point_in_time=False,
                licence_note="Requires an OpenAI API key (platform.openai.com); pay-per-use.",
            )
        return ProviderStatus(
            name=self.name,
            kind="ai",
            available=True,
            configured=True,
            message=f"OpenAI {self.model} ready",
            supports_point_in_time=False,
            licence_note="Requires an OpenAI API key; billed per the configured model's token pricing.",
            capabilities={"sentiment": True, "batch": False, "structured_outputs": True},
        )

    def _get_client(self, openai: Any) -> Any:
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self._api_key, timeout=self.config.request_timeout_s
            )
        return self._client

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute one AI task via OpenAI.

        Never raises: any missing dependency, missing credential, typed SDK
        exception, or unexpected error is caught and returned as a
        ``parsed_ok=False`` ``AIResponse``.
        """
        if not self.has_credentials:
            if not self._logged_no_credentials:
                log.info("OpenAI: no credentials; returning fallback response")
                self._logged_no_credentials = True
            return self._fallback_response(request, fallback_used="no_credentials")

        try:
            openai = _require_openai_sdk()
        except ImportError as exc:
            log.warning("OpenAI: %s", exc)
            return self._fallback_response(request, error=str(exc), fallback_used="missing_dependency")

        user_message = build_sentiment_prompt(request.payload)
        start = time.monotonic()

        try:
            client = self._get_client(openai)
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                messages=[
                    {"role": "system", "content": build_system_prompt()},
                    {"role": "user", "content": user_message},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": SCHEMA_NAME,
                        "schema": SENTIMENT_JSON_SCHEMA,
                        "strict": True,
                    },
                },
            )
        except openai.RateLimitError as exc:
            log.warning("OpenAI rate limit: %s", exc)
            return self._fallback_response(
                request, error=f"rate limited: {exc}", latency_ms=self._elapsed_ms(start)
            )
        except openai.APIConnectionError as exc:
            log.warning("OpenAI connection error: %s", exc)
            return self._fallback_response(
                request, error=f"connection error: {exc}", latency_ms=self._elapsed_ms(start)
            )
        except openai.APIStatusError as exc:
            log.warning("OpenAI API error (%s): %s", exc.status_code, exc)
            return self._fallback_response(
                request,
                error=f"api error ({exc.status_code}): {exc}",
                latency_ms=self._elapsed_ms(start),
            )
        except Exception as exc:  # never raise into the pipeline
            log.exception("OpenAI request failed unexpectedly: %s", exc)
            return self._fallback_response(
                request, error=f"unexpected error: {exc}", latency_ms=self._elapsed_ms(start)
            )

        latency_ms = self._elapsed_ms(start)

        choices = response.choices
        if not choices:
            return self._fallback_response(request, error="no choices in response", latency_ms=latency_ms)

        raw_text = choices[0].message.content or ""
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0

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

    def complete_batch(self, requests: list[AIRequest]) -> list[AIResponse]:
        """Execute several AI tasks by looping.

        For production use at high volume, consider OpenAI's async Batch
        API; this loop is what keeps the interface synchronous and matches
        ``AIProvider.complete_batch``'s contract.
        """
        return [self.complete(req) for req in requests]

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return (time.monotonic() - start) * 1000

    def _fallback_response(
        self,
        request: AIRequest,
        *,
        error: str | None = None,
        fallback_used: str | None = None,
        latency_ms: float = 0.0,
    ) -> AIResponse:
        return AIResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            prompt_version=request.prompt_version,
            created_at=dt.datetime.now(tz=dt.UTC),
            data=None,
            parsed_ok=False,
            error=error,
            fallback_used=fallback_used,
            latency_ms=latency_ms,
        )
