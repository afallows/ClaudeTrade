"""Thesis and explanation generation.

A deterministic template writer produces the thesis by default. An AI provider,
when configured, may *rewrite it more clearly* -- it may not change what the
signal says.

The separation is enforced rather than merely intended:

* The template writer runs first and its output is what gets stored if anything
  goes wrong downstream.
* The AI is given the already-computed numbers and asked only for prose. It is
  never asked what the entry, stop, target, size or direction should be.
* ``_is_safe_rewrite`` checks the returned text against the numbers that must
  appear and rejects a rewrite that introduces price levels the engine did not
  produce. A model that hallucinates a different stop gets discarded, not
  published.

This is the concrete meaning of "AI-generated text must not override hard risk
controls".
"""

from __future__ import annotations

import re

from claudetrade.config import AppConfig
from claudetrade.domain import Direction, RegimeState, SymbolSentiment
from claudetrade.logging_setup import get_logger
from claudetrade.providers.base import AIProvider, AIRequest
from claudetrade.strategies.base import StrategyContext, StrategyProposal
from claudetrade.utils.text import truncate

log = get_logger(__name__)

MAX_THESIS_CHARS = 900


def build_thesis(
    *,
    ctx: StrategyContext,
    proposal: StrategyProposal,
    regime: RegimeState,
    reward_risk: float,
    shares: int,
) -> str:
    """Deterministic thesis. Always available, never depends on a network call."""
    direction_word = "long" if proposal.direction is Direction.LONG else "short"
    sentiment: SymbolSentiment | None = ctx.sentiment

    parts: list[str] = []
    if proposal.thesis_hint:
        parts.append(proposal.thesis_hint.rstrip("."). rstrip() + ".")
    else:
        parts.append(
            f"{ctx.symbol} presents a {direction_word} swing setup on the "
            f"{proposal.strategy.replace('_', ' ')} pattern."
        )

    parts.append(
        f"Proposed entry {proposal.entry_low:.2f}-{proposal.entry_high:.2f} with a stop at "
        f"{proposal.stop_loss:.2f} and targets at "
        f"{', '.join(f'{t:.2f}' for t in proposal.targets)}, a reward-to-risk of "
        f"{reward_risk:.2f} to 1 on {shares} shares."
    )

    if sentiment is not None and sentiment.post_count > 0:
        parts.append(
            f"Social evidence: {sentiment.post_count} posts from "
            f"{sentiment.unique_authors} unique authors, net sentiment "
            f"{sentiment.raw_sentiment:+.2f}, attention "
            f"{sentiment.mention_acceleration:+.0%} versus baseline, "
            f"manipulation risk {sentiment.manipulation_risk:.2f}."
        )
    else:
        parts.append("No usable social sample; this thesis rests on price and volume alone.")

    days = ctx.days_to_earnings()
    if days is not None:
        event = ctx.next_earnings()
        qualifier = "confirmed" if (event and event.confirmed) else "estimated"
        parts.append(f"Next earnings in {days} days ({qualifier} date).")
    else:
        parts.append("No earnings date is known for this name.")

    parts.append(
        f"Market regime is {regime.regime.value.replace('_', ' ')} "
        f"(breadth {regime.breadth:.0%}, volatility percentile "
        f"{regime.volatility_percentile:.0%})."
    )
    return truncate(" ".join(parts), MAX_THESIS_CHARS)


_PRICE_RE = re.compile(r"\b\d+\.\d{1,2}\b")

#: Default plausible-length bounds for a thesis-length rewrite. Kept as the
#: defaults on :func:`validate_research_text` so :func:`_is_safe_rewrite`
#: (the AI-thesis-polish caller) needs no bounds of its own; a caller
#: validating shorter prose -- e.g. one invalidation-condition bullet from an
#: MCP research revision, see ``signals.research`` -- passes tighter ones.
DEFAULT_MIN_CHARS = 60
DEFAULT_MAX_CHARS = MAX_THESIS_CHARS * 2


def validate_research_text(
    original: str,
    rewrite: str,
    allowed_levels: list[float],
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[bool, str]:
    """Whether externally supplied prose may stand alongside the engine's own text.

    The shared guardrail behind two independent callers that both hand this
    application text an AI or a human did not compute: the AI thesis-polish
    path (:func:`polish_thesis`, wrapped as :func:`_is_safe_rewrite` below for
    backwards compatibility) and an MCP client's web-research revision
    (``signals.research.ResearchLedger.append_research_revision``, submitting
    an updated thesis and/or invalidation conditions). Both need exactly the
    same three checks and neither may duplicate them, or the two guardrails
    would drift apart the first time one is tightened and the other is not.

    Rejects the text when it:

    * is empty or implausibly short/long for what it claims to be (bounds are
      parameters because a thesis paragraph and a one-line invalidation
      condition have very different plausible lengths),
    * introduces a decimal price level that is not one the engine computed
      (already present in ``original`` or in ``allowed_levels``), or
    * contains an instruction-like directive -- submitted prose is a research
      finding, never a command to widen a stop or raise size.
    """
    text = rewrite.strip()
    if not text:
        return False, "empty"
    if len(text) < min_chars:
        return False, "too short to be plausible"
    if len(text) > max_chars:
        return False, "implausibly long"

    permitted = {f"{level:.2f}" for level in allowed_levels}
    permitted |= {f"{level:.1f}" for level in allowed_levels}
    # Numbers already present in the deterministic thesis are fine to echo.
    permitted |= set(_PRICE_RE.findall(original))
    for candidate in _PRICE_RE.findall(text):
        if candidate not in permitted:
            return False, f"introduced an unrecognised price level ({candidate})"

    lowered = text.lower()
    for directive in ("ignore the stop", "widen the stop", "increase the position", "override"):
        if directive in lowered:
            return False, f"contained a directive ({directive})"
    return True, ""


def _is_safe_rewrite(original: str, rewrite: str, allowed_levels: list[float]) -> tuple[bool, str]:
    """Whether an AI rewrite may replace the deterministic thesis.

    Thin, backwards-compatible wrapper around :func:`validate_research_text`
    at the original thesis-length bounds -- kept as a separate name because
    it is the established call site inside :func:`polish_thesis` and other
    code/tests may still import it directly.
    """
    return validate_research_text(original, rewrite, allowed_levels)


def polish_thesis(
    *,
    ai: AIProvider,
    config: AppConfig,
    original: str,
    evidence: list[str],
    risks: list[str],
    allowed_levels: list[float],
) -> tuple[str, dict[str, object]]:
    """Optionally rewrite the thesis for readability.

    Returns:
        ``(thesis, metadata)``. On any failure the *original* thesis is returned
        unchanged, with metadata recording why -- reduced-capability mode is the
        normal path, not an error.
    """
    metadata: dict[str, object] = {"ai_used": False}
    if config.ai.provider == "none":
        metadata["reason"] = "ai disabled"
        return original, metadata

    request = AIRequest(
        task="thesis",
        payload={
            "draft": original,
            "evidence": evidence[:8],
            "risks": risks[:6],
            # The model is told the levels but is not asked to choose them.
            "levels_do_not_change": [f"{level:.2f}" for level in allowed_levels],
        },
        schema_name="ThesisSummary",
        prompt_version=config.ai.prompt_version,
        max_output_tokens=config.ai.max_output_tokens,
        temperature=config.ai.temperature,
    )
    try:
        response = ai.complete(request)
    except Exception as exc:  # provider bugs must not break a scan
        log.warning("thesis polish failed: %s", exc)
        metadata["reason"] = f"provider error: {exc}"
        return original, metadata

    metadata.update(
        {
            "provider": response.provider,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "parsed_ok": response.parsed_ok,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
            "cache_hit": response.cache_hit,
            "fallback_used": response.fallback_used,
        }
    )
    if not response.parsed_ok or not response.data:
        metadata["reason"] = response.error or "schema validation failed"
        return original, metadata

    candidate = str(response.data.get("summary", "")).strip()
    safe, why = _is_safe_rewrite(original, candidate, allowed_levels)
    if not safe:
        log.info("rejected AI thesis rewrite: %s", why)
        metadata["reason"] = f"rejected: {why}"
        return original, metadata

    metadata["ai_used"] = True
    return truncate(candidate, MAX_THESIS_CHARS), metadata
