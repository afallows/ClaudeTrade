"""Strict Pydantic schemas for AI classification outputs.

Each schema corresponds to a specific classification task. Schemas are validated
both by the vendor (through prompting) and by our validator before use.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    """Base for every AI response schema.

    ``extra="forbid"`` is the point of this class. Model output is untrusted:
    if a response carries fields the schema does not define, the model has not
    answered the question we asked and the result must be rejected rather than
    silently coerced into defaults. Without this, a malformed response
    validates as an empty object and a caller cannot tell the difference
    between "the model said nothing useful" and "the model was not asked".
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SentimentClassification(StrictSchema):
    """Sentiment scoring for a (post, symbol) pair.

    Each label is an independent probability in [0, 1]; a post can score high
    on multiple labels. ``evidence`` is a list of short quoted spans supporting
    the classification.
    """

    bullish: float = Field(
        0.0, ge=0.0, le=1.0, description="Upside bias or confidence in price increase"
    )
    bearish: float = Field(
        0.0, ge=0.0, le=1.0, description="Downside bias or price decline signal"
    )
    neutral: float = Field(
        0.0, ge=0.0, le=1.0, description="Factual or analytical without directional bias"
    )
    uncertainty: float = Field(
        0.0, ge=0.0, le=1.0, description="Expresses doubt or conditional phrasing"
    )
    sarcasm: float = Field(
        0.0, ge=0.0, le=1.0, description="Ironic or sarcastic tone, surface meaning inverted"
    )
    fear: float = Field(
        0.0, ge=0.0, le=1.0, description="Anxiety, risk, or catastrophe signal"
    )
    hype: float = Field(
        0.0, ge=0.0, le=1.0, description="Exaggerated enthusiasm or pump language"
    )
    fomo: float = Field(
        0.0, ge=0.0, le=1.0, description="Fear of missing out, urgency to act"
    )
    capitulation: float = Field(
        0.0, ge=0.0, le=1.0, description="Surrender or giving up after losses"
    )
    earnings_speculation: float = Field(
        0.0, ge=0.0, le=1.0, description="Guessing or predicting earnings outcomes"
    )
    product_catalyst: float = Field(
        0.0, ge=0.0, le=1.0, description="Product launch, release or feature news"
    )
    regulatory_catalyst: float = Field(
        0.0, ge=0.0, le=1.0, description="Regulatory, legal or compliance event"
    )
    rumour: float = Field(
        0.0, ge=0.0, le=1.0, description="Unconfirmed report or hearsay"
    )
    short_squeeze: float = Field(
        0.0, ge=0.0, le=1.0, description="Reference to short covering or squeeze dynamics"
    )
    pump_and_dump: float = Field(
        0.0, ge=0.0, le=1.0, description="Coordinated promotion or artificial hype"
    )
    position_disclosure: float = Field(
        0.0, ge=0.0, le=1.0, description="Disclosure or claim of personal position"
    )
    confidence: float = Field(
        0.5, ge=0.0, le=1.0, description="Confidence in this classification"
    )
    evidence: list[str] = Field(
        default_factory=list, max_length=5, description="Short quoted spans supporting labels"
    )


class TickerContextClassification(StrictSchema):
    """Entity resolution hints from post context.

    When a post is ambiguous (e.g. 'AI' could be a ticker or just mean
    artificial intelligence), this provides confidence the author intended a
    financial reference.
    """

    likely_financial: float = Field(
        0.5, ge=0.0, le=1.0, description="Confidence the context is financial"
    )
    confidence: float = Field(
        0.5, ge=0.0, le=1.0, description="Overall confidence in judgment"
    )
    evidence: list[str] = Field(default_factory=list, max_length=3)


class CatalystExtraction(StrictSchema):
    """Structured extraction of event-driving catalysts from a post."""

    catalyst_type: str = Field(
        "none",
        description="Type: earnings, product, regulatory, partnership, restructuring, none",
    )
    confidence: float = Field(
        0.0, ge=0.0, le=1.0, description="Confidence in catalyst extraction"
    )
    timeline: str = Field(
        "unspecified", description="When catalyst is expected: imminent, near_term, unspecified"
    )
    evidence: list[str] = Field(default_factory=list, max_length=3)


class SpamAssessment(StrictSchema):
    """Assessment of post quality and manipulation risk."""

    is_spam: float = Field(
        0.0, ge=0.0, le=1.0, description="Likelihood this is spam, pump, or low-quality content"
    )
    confidence: float = Field(
        0.5, ge=0.0, le=1.0, description="Confidence in spam assessment"
    )
    evidence: list[str] = Field(default_factory=list, max_length=3)


class ThesisSummary(StrictSchema):
    """High-level thesis extracted from grouped posts about a symbol."""

    summary: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Concise thesis statement: what is the bullish/bearish case?",
    )
    confidence: float = Field(
        0.5, ge=0.0, le=1.0, description="Confidence in the extracted thesis"
    )
    key_themes: list[str] = Field(
        default_factory=list, max_length=5, description="Tags: growth, value, risk, catalyst"
    )


# --- Registry and validation -----------------------------------------------


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "SentimentClassification": SentimentClassification,
    "TickerContextClassification": TickerContextClassification,
    "CatalystExtraction": CatalystExtraction,
    "SpamAssessment": SpamAssessment,
    "ThesisSummary": ThesisSummary,
}


def validate_ai_payload(
    schema_name: str, data: Any
) -> tuple[BaseModel | None, str | None]:
    """Validate a response payload against a registered schema.

    Args:
        schema_name: Key in SCHEMA_REGISTRY.
        data: Untrusted data (typically parsed from LLM JSON).

    Returns:
        (validated_model, error_message). On success, error_message is None.
        On failure, validated_model is None and error_message describes the issue.
    """
    schema_class = SCHEMA_REGISTRY.get(schema_name)
    if schema_class is None:
        return None, f"unknown schema: {schema_name}"
    try:
        model = schema_class(**data) if isinstance(data, dict) else schema_class.model_validate(data)
        return model, None
    except Exception as exc:
        return None, f"schema validation failed: {exc}"
