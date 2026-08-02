"""Anthropic Claude API adapter for LLM-based sentiment classification.

Uses the **official Anthropic Python SDK** (``pip install anthropic``, or
``pip install claudetrade[anthropic]`` -- see ``pyproject.toml``'s optional
``anthropic`` extra), imported lazily via ``_require_anthropic_sdk`` so that
importing this module, and constructing ``AnthropicProvider`` itself, never
requires the package to be installed -- only an actual classification call
does (mirrors ``mcp_server._require_fastmcp``'s lazy-import pattern).

Sentiment classification uses Anthropic's **structured outputs**
(``output_config={"format": {"type": "json_schema", "schema": ...}}``) so the
per-post JSON reliably parses -- see ``providers.ai.schemas.SENTIMENT_JSON_SCHEMA``
for why that schema omits numeric range constraints and where those ranges
are actually enforced.

**This module must never raise into the pipeline.** Every SDK-level failure
(missing package, no credentials, ``anthropic.RateLimitError``,
``anthropic.APIStatusError``, ``anthropic.APIConnectionError``, a safety
classifier refusal, or any other unexpected exception) is caught and turned
into an ``AIResponse`` with ``parsed_ok=False`` -- ``AISentimentClassifier``
(``sentiment.ai_classifier``) falls back to the deterministic rule ensemble
on any such response, exactly as it does for the null provider.

**Model choice is operator-configurable** (``AIConfig.model``; empty means
"use ``DEFAULT_MODEL`` below"). The default is Claude Opus 5, the higher-
capability/higher-cost model ($5/$25 per MTok input/output at time of
writing); ``claude-haiku-4-5`` ($1/$5 per MTok) is the deliberately cheaper,
faster choice for high-volume per-post classification and is a fully
supported, one-line config change (``ai.model = "claude-haiku-4-5"``) -- this
module does not pick that tradeoff for the operator.

Thinking is explicitly **disabled** for this call (``thinking={"type":
"disabled"}``): a single-post sentiment classification is a short, bounded,
non-open-ended task that does not benefit from extended reasoning, and
leaving thinking on its (Opus-5) default would consume ``max_tokens`` budget
on reasoning rather than the structured JSON answer this call actually needs.
Sampling parameters (``temperature``/``top_p``/``top_k``) are never sent --
they are removed on current Claude models (Opus 5, Sonnet 5) and return a 400
if included.
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
    import anthropic as anthropic_types

log = logging.getLogger(__name__)

#: Operator-configurable default (see module docstring for the haiku-4-5
#: cost/quality tradeoff). Kept as a module constant, not a hardcoded literal
#: inline, so it is the one place to bump when a newer default is chosen.
DEFAULT_MODEL = "claude-opus-5"

#: Schema name recorded on the request/response for cache-key and logging
#: purposes; must match ``sentiment.ai_classifier.SCHEMA_NAME``.
SCHEMA_NAME = "sentiment_classification_v1"


def _require_anthropic_sdk() -> Any:
    """Import and return the ``anthropic`` module, or raise a clear error.

    Kept as its own function (rather than a bare module-level import) so
    that importing ``claudetrade.providers.ai.anthropic_provider`` -- and
    constructing ``AnthropicProvider`` -- never requires the package to be
    installed; only an actual API call does.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package (the official Anthropic Python SDK) is "
            "required to use ai.provider = 'anthropic'. Install it with "
            "`pip install claudetrade[anthropic]` (or `pip install anthropic`), "
            "then retry."
        ) from exc
    return anthropic


class AnthropicProvider:
    """Anthropic Claude provider for structured sentiment analysis.

    Falls back to a "no credentials"/"no dependency" ``AIResponse``
    (``parsed_ok=False``) rather than raising, in every failure mode -- see
    the module docstring.
    """

    name: str = "anthropic"

    def __init__(self, config: AIConfig):
        """Initialize the Anthropic provider.

        Args:
            config: AIConfig with ``anthropic_api_key_credential`` and
                optionally ``model`` (empty uses ``DEFAULT_MODEL``).

        Never raises: a missing credential or a missing ``anthropic``
        package is discovered lazily, on the first real ``complete()`` call,
        not at construction time -- this keeps ``get_ai_provider`` (and any
        code that merely constructs this class to inspect ``status()``)
        working with no package installed at all.
        """
        self.config = config
        self.model = config.model or DEFAULT_MODEL
        self.base_url = config.base_url

        self._api_key: str | None = None
        #: Public (not ``_``-prefixed): the credentials-test endpoint
        #: (``webapi.routers.system``) reads this directly rather than
        #: duplicating the secret-resolution logic.
        self.has_credentials = False
        secret = get_secret(config.anthropic_api_key_credential)
        if secret is not None:
            self._api_key = secret.reveal()
            self.has_credentials = True
            log.info("Anthropic provider configured with model=%s", self.model)
        else:
            log.debug(
                "Anthropic provider credential '%s' not found; operating in null mode",
                config.anthropic_api_key_credential,
            )

        self._client: anthropic_types.Anthropic | None = None
        self._logged_no_credentials = False

    def status(self) -> ProviderStatus:
        """Report provider health and capabilities."""
        if not self.has_credentials:
            return ProviderStatus(
                name=self.name,
                kind="ai",
                available=False,
                configured=False,
                message=f"Credential '{self.config.anthropic_api_key_credential}' not configured",
                supports_point_in_time=False,
                licence_note="Requires an Anthropic API key (platform.claude.com); pay-per-use.",
            )
        return ProviderStatus(
            name=self.name,
            kind="ai",
            available=True,
            configured=True,
            message=f"Anthropic {self.model} ready",
            supports_point_in_time=False,
            licence_note="Requires an Anthropic API key; billed per the configured model's token pricing.",
            capabilities={"sentiment": True, "batch": False, "structured_outputs": True},
        )

    def _get_client(self, anthropic: Any) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                "timeout": self.config.request_timeout_s,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute one AI task via Anthropic Claude.

        Never raises: any missing dependency, missing credential, typed SDK
        exception, safety-classifier refusal, or unexpected error is caught
        and returned as a ``parsed_ok=False`` ``AIResponse``.
        """
        if not self.has_credentials:
            if not self._logged_no_credentials:
                log.info("Anthropic: no credentials; returning fallback response")
                self._logged_no_credentials = True
            return self._fallback_response(request, fallback_used="no_credentials")

        try:
            anthropic = _require_anthropic_sdk()
        except ImportError as exc:
            log.warning("Anthropic: %s", exc)
            return self._fallback_response(request, error=str(exc), fallback_used="missing_dependency")

        user_message = build_sentiment_prompt(request.payload)
        start = time.monotonic()

        try:
            client = self._get_client(anthropic)
            response = client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": user_message}],
                # No temperature/top_p/top_k: removed on current Claude
                # models (Opus 5, Sonnet 5) -- sending them returns a 400.
                thinking={"type": "disabled"},
                output_config={
                    "format": {"type": "json_schema", "schema": SENTIMENT_JSON_SCHEMA}
                },
            )
        except anthropic.RateLimitError as exc:
            log.warning("Anthropic rate limit: %s", exc)
            return self._fallback_response(
                request, error=f"rate limited: {exc}", latency_ms=self._elapsed_ms(start)
            )
        except anthropic.APIConnectionError as exc:
            log.warning("Anthropic connection error: %s", exc)
            return self._fallback_response(
                request, error=f"connection error: {exc}", latency_ms=self._elapsed_ms(start)
            )
        except anthropic.APIStatusError as exc:
            log.warning("Anthropic API error (%s): %s", exc.status_code, exc)
            return self._fallback_response(
                request,
                error=f"api error ({exc.status_code}): {exc}",
                latency_ms=self._elapsed_ms(start),
            )
        except Exception as exc:  # never raise into the pipeline
            log.exception("Anthropic request failed unexpectedly: %s", exc)
            return self._fallback_response(
                request, error=f"unexpected error: {exc}", latency_ms=self._elapsed_ms(start)
            )

        latency_ms = self._elapsed_ms(start)

        # Opus 5's elevated safety classifiers can decline a request with a
        # normal HTTP 200 and stop_reason == "refusal" -- content is empty
        # (pre-output) or partial (mid-stream); never index content[0]
        # unconditionally.
        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("Anthropic classification refused by safety classifiers")
            return self._fallback_response(
                request, error="classification refused by safety classifiers", latency_ms=latency_ms
            )

        text_blocks = [
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ]
        raw_text = text_blocks[0] if text_blocks else ""
        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0

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

        The Anthropic Messages API has no synchronous native batch endpoint
        for this call shape, so this loops with each call's own error
        handling; ``AISentimentClassifier`` is what enforces
        ``max_calls_per_run``/cost caps across the loop, not this method.
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
