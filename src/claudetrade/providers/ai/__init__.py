"""AI provider adapters for LLM-based sentiment classification.

Exports:
    NullAIProvider: Offline fallback (default).
    OpenAIProvider: OpenAI ChatGPT adapter.
    AnthropicProvider: Anthropic Claude adapter.
    AIResponseCache: Response caching layer.

All providers implement the AIProvider protocol from providers.base.
All can run offline; external dependencies (API keys) are optional.
"""

from __future__ import annotations

from claudetrade.providers.ai.anthropic_provider import AnthropicProvider
from claudetrade.providers.ai.cache import AIResponseCache
from claudetrade.providers.ai.null_provider import NullAIProvider
from claudetrade.providers.ai.openai_provider import OpenAIProvider
from claudetrade.providers.ai.schemas import (
    SCHEMA_REGISTRY,
    CatalystExtraction,
    SentimentClassification,
    SpamAssessment,
    ThesisSummary,
    TickerContextClassification,
    validate_ai_payload,
)

__all__ = [
    "SCHEMA_REGISTRY",
    "AIResponseCache",
    "AnthropicProvider",
    "CatalystExtraction",
    "NullAIProvider",
    "OpenAIProvider",
    "SentimentClassification",
    "SpamAssessment",
    "ThesisSummary",
    "TickerContextClassification",
    "validate_ai_payload",
]
