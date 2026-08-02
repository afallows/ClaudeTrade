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


#: Raw JSON Schema for the Anthropic/OpenAI structured-output request
#: (``output_config.format`` / ``response_format``), mirroring
#: ``SentimentClassification`` above field-for-field. Deliberately omits
#: ``minimum``/``maximum`` on every numeric field -- current structured-output
#: implementations do NOT support numerical range constraints in the schema
#: (see ``claude-api`` skill's Structured Outputs -> JSON Schema Limitations:
#: "Not supported: Numerical constraints (minimum, maximum, multipleOf)").
#: Range enforcement instead happens locally, twice: ``validate_ai_payload``
#: above (via the ``ge=0.0, le=1.0`` Pydantic ``Field`` constraints on
#: ``SentimentClassification`` itself) and, redundantly,
#: ``sentiment.ai_classifier._validate_schema``. A response that is
#: syntactically valid JSON matching this schema but semantically
#: out-of-range (a model bug, not something ``additionalProperties: false``
#: can catch) is rejected by those checks, not this schema.
SENTIMENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bullish": {"type": "number", "description": "Upside bias or confidence in price increase"},
        "bearish": {"type": "number", "description": "Downside bias or price decline signal"},
        "neutral": {"type": "number", "description": "Factual or analytical without directional bias"},
        "uncertainty": {"type": "number", "description": "Expresses doubt or conditional phrasing"},
        "sarcasm": {"type": "number", "description": "Ironic or sarcastic tone, surface meaning inverted"},
        "fear": {"type": "number", "description": "Anxiety, risk, or catastrophe signal"},
        "hype": {"type": "number", "description": "Exaggerated enthusiasm or pump language"},
        "fomo": {"type": "number", "description": "Fear of missing out, urgency to act"},
        "capitulation": {"type": "number", "description": "Surrender or giving up after losses"},
        "earnings_speculation": {"type": "number", "description": "Guessing or predicting earnings outcomes"},
        "product_catalyst": {"type": "number", "description": "Product launch, release or feature news"},
        "regulatory_catalyst": {"type": "number", "description": "Regulatory, legal or compliance event"},
        "rumour": {"type": "number", "description": "Unconfirmed report or hearsay"},
        "short_squeeze": {"type": "number", "description": "Reference to short covering or squeeze dynamics"},
        "pump_and_dump": {"type": "number", "description": "Coordinated promotion or artificial hype"},
        "position_disclosure": {"type": "number", "description": "Disclosure or claim of personal position"},
        "confidence": {"type": "number", "description": "Confidence in this classification"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-5 short quoted spans supporting the labels",
        },
    },
    "required": [
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
        "confidence",
        "evidence",
    ],
    "additionalProperties": False,
}


# --- Registry and validation -----------------------------------------------


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "SentimentClassification": SentimentClassification,
    "TickerContextClassification": TickerContextClassification,
    "CatalystExtraction": CatalystExtraction,
    "SpamAssessment": SpamAssessment,
    "ThesisSummary": ThesisSummary,
    # Alias: sentiment.ai_classifier.SCHEMA_NAME ("sentiment_classification_v1")
    # is the schema_name every AIRequest for sentiment classification is
    # actually built with (see AISentimentClassifier.classify_batch) -- and
    # is also the value stamped into the cache key and the "ai:<model>"
    # classifier tag on SentimentScores, so it is not free to rename. Without
    # this alias, validate_ai_payload's lookup by that exact string always
    # missed (falling through to "unknown schema: ..."), so every real
    # Anthropic/OpenAI classification call silently failed schema validation
    # regardless of how well-formed the model's JSON was.
    "sentiment_classification_v1": SentimentClassification,
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
