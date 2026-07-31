"""Null AI provider: always offline, always returns fallback results.

This is the DEFAULT provider and allows the entire system to run with no
credentials or external dependencies. Useful for backtesting and demo mode.
"""

from __future__ import annotations

import datetime as dt
import logging

from claudetrade.config import AIConfig
from claudetrade.providers.base import AIRequest, AIResponse, ProviderStatus

log = logging.getLogger(__name__)


class NullAIProvider:
    """Offline fallback that returns parsed_ok=False, no cost.

    All responses have fallback_used='rules' to signal to callers that they
    should use deterministic rule-based sentiment instead.
    """

    name: str = "none"
    model: str = "none"

    def __init__(self, config: AIConfig | None = None):
        """Initialize the null provider.

        Args:
            config: Ignored; null provider needs no configuration.
        """
        self.config = config or AIConfig()

    def status(self) -> ProviderStatus:
        """Report that this is an offline-capable fallback."""
        return ProviderStatus(
            name=self.name,
            kind="ai",
            available=True,
            configured=True,
            message="Offline fallback; sentiment via rules only",
            supports_point_in_time=False,
            rate_limit_per_minute=None,
            licence_note="Built-in offline fallback",
            capabilities={"sentiment": True, "batch": True},
        )

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute one AI task, returning a fallback response.

        Args:
            request: AIRequest (ignored).

        Returns:
            AIResponse with parsed_ok=False and fallback_used='rules'.
        """
        return AIResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            prompt_version=request.prompt_version,
            created_at=dt.datetime.now(tz=dt.UTC),
            data=None,
            parsed_ok=False,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            fallback_used="rules",
            error=None,
        )

    def complete_batch(self, requests: list[AIRequest]) -> list[AIResponse]:
        """Execute several AI tasks, returning fallback responses.

        Args:
            requests: List of AIRequest.

        Returns:
            List of AIResponse, all with parsed_ok=False and fallback_used='rules'.
        """
        return [self.complete(req) for req in requests]
