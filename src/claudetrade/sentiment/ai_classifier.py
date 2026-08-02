"""AI-assisted sentiment classification, wrapping ``providers.base.AIProvider``.

This module is entirely optional in the running system: with
``AIConfig.provider == "none"`` (the default) nothing here is ever invoked, and
``sentiment.classifiers.RuleSentimentClassifier`` carries the full pipeline on
its own. Every failure mode here -- no credential, a malformed response, a
schema violation, hitting the cost cap, or the post being blocked outright on
injection-risk grounds -- is handled by returning ``None`` so the ensemble
falls back to the rule classifier. **This module must never raise into the
pipeline**; ordinary provider failures are caught and logged, not propagated.

Hard rule (enforced, not just documented): a post whose
``injection_risk_score`` exceeds ``AIConfig.injection_block_threshold`` is
never sent to the AI provider, full stop -- see ``_should_block``. Usernames,
author ids, karma, follower counts and post history are never included in a
request; only the sanitised, fenced post text and the target symbol are.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from claudetrade.config import AIConfig
from claudetrade.domain import SentimentScores, SocialPost
from claudetrade.providers.base import AIProvider, AIRequest, AIResponse
from claudetrade.utils.hashing import content_hash
from claudetrade.utils.text import injection_risk_score, prepare_for_ai

log = logging.getLogger(__name__)

SCHEMA_NAME = "sentiment_classification_v1"
TASK = "sentiment"

#: Fields the AI response must supply, each a number in [0, 1]. `coordinated`
#: is excluded on purpose: a single-post classification call has no visibility
#: into other posts, so it cannot legitimately judge cross-post coordination.
_REQUIRED_FIELDS = (
    "bullish",
    "bearish",
    "neutral",
    "uncertainty",
    "sarcasm",
    "fear",
    "hype",
    "fomo",
    "capitulation",
    "earnings_speculation",
    "product_catalyst",
    "regulatory_catalyst",
    "rumour",
    "short_squeeze",
    "pump_and_dump",
    "position_disclosure",
)


class AIResponseCache(Protocol):
    """Minimal cache contract; production wiring is a caller concern.

    The persistent implementation (backed by ``db.models.AICacheRow``) lives
    with the rest of the database access layer, not in this module -- this
    protocol lets ``AISentimentClassifier`` stay unit-testable and DB-agnostic.
    Pass ``None`` (the default) to run with no caching at all.
    """

    def get(self, cache_key: str) -> AIResponse | None: ...

    def put(self, cache_key: str, response: AIResponse) -> None: ...


@dataclass(slots=True)
class InMemoryAICache:
    """Trivial process-local cache, useful for tests and single-run scripts."""

    _store: dict[str, AIResponse] = field(default_factory=dict)

    def get(self, cache_key: str) -> AIResponse | None:
        return self._store.get(cache_key)

    def put(self, cache_key: str, response: AIResponse) -> None:
        self._store[cache_key] = response


def _cache_key(symbol: str, fenced_text: str, config: AIConfig) -> str:
    return content_hash(
        {
            "task": TASK,
            "symbol": symbol,
            "text": fenced_text,
            "prompt_version": config.prompt_version,
            "model": config.model,
        }
    )


def _validate_schema(data: object) -> bool:
    """Strict validation: every required field present, numeric, in [0, 1]."""
    if not isinstance(data, dict):
        return False
    for field_name in _REQUIRED_FIELDS:
        value = data.get(field_name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return False
        if not 0.0 <= float(value) <= 1.0:
            return False
    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        return False
    return 0.0 <= float(confidence) <= 1.0


def _to_scores(data: dict[str, object], model: str) -> SentimentScores:
    return SentimentScores(
        bullish=float(data["bullish"]),
        bearish=float(data["bearish"]),
        neutral=float(data["neutral"]),
        uncertainty=float(data["uncertainty"]),
        sarcasm=float(data["sarcasm"]),
        fear=float(data["fear"]),
        hype=float(data["hype"]),
        fomo=float(data["fomo"]),
        capitulation=float(data["capitulation"]),
        earnings_speculation=float(data["earnings_speculation"]),
        product_catalyst=float(data["product_catalyst"]),
        regulatory_catalyst=float(data["regulatory_catalyst"]),
        rumour=float(data["rumour"]),
        short_squeeze=float(data["short_squeeze"]),
        pump_and_dump=float(data["pump_and_dump"]),
        position_disclosure=float(data["position_disclosure"]),
        coordinated=0.0,
        confidence=float(data.get("confidence", 0.5)),
        classifier=f"ai:{model}",
    )


class AISentimentClassifier:
    """Batches sanitised posts to an ``AIProvider`` and validates the result.

    One instance is expected to live for the scope of one pipeline run (its
    ``max_calls_per_run`` and running cost counters are not persisted between
    instances). Cross-run daily cost accounting belongs to the caller, using
    the ``ai_calls`` ledger table -- this class only enforces a best-effort
    within-run cap.
    """

    def __init__(
        self,
        provider: AIProvider,
        config: AIConfig,
        *,
        cache: AIResponseCache | None = None,
    ):
        self.provider = provider
        self.config = config
        self.cache = cache
        self._calls_made = 0
        self._running_cost_usd = 0.0

    # -- public API ----------------------------------------------------------

    def classify(self, post: SocialPost, symbol: str) -> SentimentScores | None:
        """Classify one (post, symbol) pair, or ``None`` on any failure."""
        results = self.classify_batch([(post, symbol)])
        return results[0]

    def classify_batch(
        self, items: list[tuple[SocialPost, str]]
    ) -> list[SentimentScores | None]:
        """Classify many (post, symbol) pairs, batching provider calls.

        Order of the returned list matches ``items``. Never raises: any
        provider or parsing failure yields ``None`` at that position.
        """
        results: list[SentimentScores | None] = [None] * len(items)
        pending_idx: list[int] = []
        pending_requests: list[AIRequest] = []
        pending_keys: list[str] = []

        for i, (post, symbol) in enumerate(items):
            if self._should_block(post):
                log.warning(
                    "post %s blocked from AI classification: injection risk above threshold",
                    post.external_id,
                )
                continue

            fenced = prepare_for_ai(post.text)
            key = _cache_key(symbol, fenced, self.config)

            cached = self.cache.get(key) if self.cache is not None else None
            if cached is not None:
                results[i] = self._response_to_scores(cached)
                continue

            pending_idx.append(i)
            pending_requests.append(
                AIRequest(
                    task=TASK,
                    payload={"symbol": symbol, "text": fenced},
                    schema_name=SCHEMA_NAME,
                    prompt_version=self.config.prompt_version,
                    max_output_tokens=self.config.max_output_tokens,
                    temperature=self.config.temperature,
                )
            )
            pending_keys.append(key)

        for start in range(0, len(pending_requests), max(1, self.config.batch_size)):
            chunk_idx = pending_idx[start : start + self.config.batch_size]
            chunk_requests = pending_requests[start : start + self.config.batch_size]
            chunk_keys = pending_keys[start : start + self.config.batch_size]
            self._run_chunk(chunk_idx, chunk_requests, chunk_keys, results)

        return results

    # -- internals ------------------------------------------------------------

    def _should_block(self, post: SocialPost) -> bool:
        """Never send a likely prompt-injection attempt to the AI provider.

        Uses the higher of the post's stored score (computed at ingestion) and
        a fresh recomputation, so this holds even if ``post.injection_risk``
        was never populated by an upstream stage.
        """
        risk = max(post.injection_risk, injection_risk_score(post.text))
        return risk > self.config.injection_block_threshold

    def _run_chunk(
        self,
        idx: list[int],
        requests: list[AIRequest],
        keys: list[str],
        results: list[SentimentScores | None],
    ) -> None:
        if not requests:
            return
        remaining_calls = self.config.max_calls_per_run - self._calls_made
        if remaining_calls <= 0:
            log.info("AI call budget (%d) exhausted this run; skipping remainder", self.config.max_calls_per_run)
            return
        if self._running_cost_usd >= self.config.daily_cost_limit_usd:
            log.info("AI cost cap ($%.2f) reached this run; skipping remainder", self.config.daily_cost_limit_usd)
            return

        take = min(len(requests), remaining_calls)
        idx, requests, keys = idx[:take], requests[:take], keys[:take]

        try:
            responses = self.provider.complete_batch(requests)
        except Exception:
            log.exception("AI provider batch call failed; falling back to rules for this chunk")
            return

        self._calls_made += len(requests)
        for i, key, response in zip(idx, keys, responses, strict=True):
            self._running_cost_usd += response.estimated_cost_usd
            if not response.parsed_ok or response.data is None:
                log.warning(
                    "AI response for task=%s failed schema validation (error=%s)",
                    response.task,
                    response.error,
                )
                continue
            if not _validate_schema(response.data):
                log.warning("AI response for task=%s failed local schema check", response.task)
                continue
            if self.cache is not None:
                self.cache.put(key, response)
            results[i] = self._response_to_scores(response)

    def _response_to_scores(self, response: AIResponse) -> SentimentScores | None:
        if not response.parsed_ok or response.data is None or not _validate_schema(response.data):
            return None
        return _to_scores(response.data, response.model)
