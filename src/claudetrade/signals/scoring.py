"""Component scoring and confidence.

Every candidate is scored on thirteen components, each mapped to 0-100 where
higher is always better. Risk-shaped components (earnings proximity,
manipulation risk) are inverted at the point of scoring so the weighted sum
needs no special cases and cannot be read backwards.

Two properties are enforced here rather than left to strategy authors:

* **Sentiment cannot carry a candidate on its own.** The sentiment components
  are capped in aggregate weight, and ``apply_hard_gates`` rejects a candidate
  whose price, volume, liquidity or earnings context contradicts the sentiment
  regardless of how strong that sentiment is.
* **Confidence is about the data, not the idea.** A candidate can have a high
  score and low confidence -- that combination means "this looks like a good
  setup, but we do not trust the inputs", and it is the correct output when a
  provider is stale or a sentiment sample is thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from claudetrade.config import AppConfig
from claudetrade.domain import (
    ComponentScores,
    Direction,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.strategies.base import StrategyContext, StrategyProposal


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float, lo: float, hi: float) -> float:
    """Map ``value`` from the ``[lo, hi]`` range onto 0-100, clamped."""
    if hi <= lo:
        return 50.0
    return _clamp(100.0 * (value - lo) / (hi - lo))


@dataclass(slots=True)
class ScoreBreakdown:
    """Scoring result plus the reasons a candidate was gated out, if it was."""

    components: ComponentScores
    overall: float
    confidence: float
    gate_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.gate_failures


# --------------------------------------------------------------------------
# Component scorers
# --------------------------------------------------------------------------


def _technical_score(proposal: StrategyProposal) -> float:
    """The strategy's own conviction in its setup."""
    return _clamp(proposal.setup_score)


def _momentum_score(ctx: StrategyContext, direction: Direction) -> float:
    """Trend agreement across several horizons, signed by trade direction."""
    roc_10 = ctx.feature("roc_10", 0.0)
    roc_20 = ctx.feature("roc_20", 0.0)
    rs_pct = ctx.feature("rs_percentile", 50.0)
    sign = direction.sign or 1
    blended = sign * (0.4 * roc_10 + 0.6 * roc_20)
    momentum = _scale(blended, -15.0, 15.0)
    # Relative strength is directional too: a short wants a weak name.
    rs_component = rs_pct if direction is Direction.LONG else (100.0 - rs_pct)
    return _clamp(0.65 * momentum + 0.35 * rs_component)


def _volume_score(ctx: StrategyContext) -> float:
    """Whether the move is funded. Flat volume on a 'breakout' is a warning."""
    rel_volume = ctx.feature("rel_volume_20", 1.0)
    obv_slope = ctx.feature("obv_slope_10", 0.0)
    base = _scale(rel_volume, 0.6, 2.5)
    confirmation = 60.0 if obv_slope > 0 else 40.0
    return _clamp(0.7 * base + 0.3 * confirmation)


def _sentiment_score(sentiment: SymbolSentiment | None, direction: Direction) -> float:
    """Directional sentiment, neutral (50) when there is no usable sample.

    Absent data scores neutral rather than zero: missing evidence is not
    negative evidence, and scoring it as negative would systematically suppress
    thinly-discussed names for no analytical reason.
    """
    if sentiment is None or sentiment.post_count == 0:
        return 50.0
    sign = direction.sign or 1
    # Unique-author sentiment is the manipulation-resistant measure and is
    # weighted most heavily; raw sentiment is the easiest number to inflate.
    blended = (
        0.20 * sentiment.raw_sentiment
        + 0.25 * sentiment.engagement_weighted
        + 0.25 * sentiment.credibility_weighted
        + 0.30 * sentiment.unique_author_sentiment
    )
    return _scale(sign * blended, -0.6, 0.6)


def _acceleration_score(sentiment: SymbolSentiment | None, direction: Direction) -> float:
    if sentiment is None:
        return 50.0
    sign = direction.sign or 1
    return _scale(sign * sentiment.sentiment_acceleration, -0.5, 0.5)


def _attention_score(sentiment: SymbolSentiment | None) -> float:
    """Attention is *unsigned*: a crowd gathering is neither bullish nor bearish.

    This is why mention count is scored on its own axis and never folded into
    polarity -- rising attention on a deteriorating name is a short setup, not
    a long one.
    """
    if sentiment is None:
        return 50.0
    return _scale(sentiment.mention_acceleration, -0.3, 1.0)


def _catalyst_score(sentiment: SymbolSentiment | None) -> float:
    if sentiment is None:
        return 50.0
    quality = sentiment.catalyst_quality
    rumour = sentiment.labels.get("rumour", 0.0)
    # An unverified rumour is not a catalyst; it discounts the score.
    return _clamp(100.0 * quality * (1.0 - 0.5 * rumour))


def _earnings_score(ctx: StrategyContext, config: AppConfig, permits_risk: bool) -> float:
    """Inverted risk: 100 means earnings pose no threat to the holding period.

    An *estimated* date is treated as the earlier bound of its uncertainty
    window, so an unconfirmed report never scores better than a confirmed one at
    the same nominal distance.
    """
    days = ctx.days_to_earnings()
    if days is None:
        # Unknown is not safe; it is unknown. Score it mid-range and let the
        # confidence penalty carry the uncertainty.
        return 60.0
    if permits_risk:
        return 85.0
    buffer = config.filters.block_entry_within_days_of_earnings
    if days <= buffer:
        return 0.0
    event = ctx.next_earnings()
    penalty = 0.0 if (event and event.confirmed) else 10.0
    return _clamp(_scale(float(days), float(buffer), 30.0) - penalty)


def _liquidity_score(ctx: StrategyContext, config: AppConfig) -> float:
    adv = ctx.feature("avg_dollar_volume_20", 0.0)
    minimum = config.filters.min_avg_dollar_volume_usd
    if adv <= 0:
        return 0.0
    # Log-ish scaling: the difference between $10M and $50M matters more than
    # between $500M and $540M.
    ratio = adv / max(minimum, 1.0)
    if ratio <= 1.0:
        return _clamp(50.0 * ratio)
    return _clamp(50.0 + 25.0 * min(2.0, ratio / 5.0) + 10.0 * min(1.0, ratio / 20.0))


def _regime_score(regime: RegimeState, direction: Direction) -> float:
    """How hospitable the environment is to this direction."""
    base = _scale(regime.trend_score, -1.0, 1.0)
    if direction is Direction.SHORT:
        base = 100.0 - base
    # A hostile bias should cost the candidate even in a nominally fine trend.
    bias_alignment = regime.long_short_bias * (direction.sign or 1)
    return _clamp(base + 15.0 * bias_alignment)


def _manipulation_score(sentiment: SymbolSentiment | None, invert_for_short: bool) -> float:
    """Inverted risk: 100 means no promotion signature detected.

    For the hype-failure short the promotion *is* the setup, so a high
    manipulation reading is not penalised there.
    """
    if sentiment is None:
        return 80.0
    risk = sentiment.manipulation_risk
    if invert_for_short:
        return _clamp(40.0 + 60.0 * risk)
    return _clamp(100.0 * (1.0 - risk))


def _data_confidence_score(
    ctx: StrategyContext,
    sentiment: SymbolSentiment | None,
    config: AppConfig,
) -> float:
    """How much the inputs behind this candidate can be trusted.

    This is a DATA-QUALITY metric -- sample adequacy, staleness, outstanding
    warnings -- not a second manipulation assessment. ``duplicate_ratio`` and
    ``source_concentration`` are already Herfindahl/duplicate-content inputs
    to ``sentiment.manipulation_risk`` (see ``sentiment.manipulation.detect``),
    which has its own dedicated scored component (``_manipulation_score``)
    AND its own hard veto (``apply_hard_gates``'s ``max_manipulation_risk``
    check). Subtracting them here too double-counts the identical
    measurement under a different label: a single coordinated-posting signal
    would otherwise both fail the manipulation gate/score *and* separately
    crater the unrelated confidence metric for every candidate touched by
    social data, drowning out what this score exists to measure. A single,
    smaller pass-through of the already-aggregated ``manipulation_risk``
    keeps confidence sensitive to promotion risk exactly once.
    """
    score = 100.0
    if ctx.data_warnings:
        score -= 15.0 * len(ctx.data_warnings)
    bars_available = len(ctx.bars)
    if bars_available < 200:
        score -= (200 - bars_available) * 0.1
    if sentiment is None:
        score -= 20.0
    else:
        if sentiment.post_count < config.sentiment.min_posts_for_signal:
            score -= 20.0
        if sentiment.unique_authors < config.sentiment.min_unique_authors_for_signal:
            score -= 15.0
        score -= 10.0 * sentiment.manipulation_risk
    return _clamp(score)


# --------------------------------------------------------------------------
# Hard gates
# --------------------------------------------------------------------------


def apply_hard_gates(
    *,
    ctx: StrategyContext,
    proposal: StrategyProposal,
    config: AppConfig,
    security: SecurityInfo,
    sentiment: SymbolSentiment | None,
    requires_sentiment: bool = True,
) -> list[str]:
    """Filters that no score can override.

    These exist because a weighted average is exactly the wrong tool for a
    disqualifying condition: a spectacular sentiment reading should not be able
    to average away an illiquid stock two days from earnings.
    """
    failures: list[str] = []
    filters = config.filters
    price = ctx.price

    if price < filters.min_price:
        failures.append(f"price {price:.2f} below the {filters.min_price:.2f} minimum")
    if price > filters.max_price:
        failures.append(f"price {price:.2f} above the {filters.max_price:.2f} maximum")
    if filters.exclude_penny_stocks and price < 5.0:
        failures.append("penny stocks are excluded")

    if security.exchange and security.exchange not in config.universe.permitted_exchanges:
        failures.append(f"exchange {security.exchange} is not permitted")

    market_cap = security.market_cap_usd or 0.0
    if market_cap and market_cap < filters.min_market_cap_usd:
        failures.append(f"market cap ${market_cap:,.0f} below the minimum")

    adv = ctx.feature("avg_dollar_volume_20", 0.0)
    if adv < filters.min_avg_dollar_volume_usd:
        failures.append(
            f"average dollar volume ${adv:,.0f} below the "
            f"${filters.min_avg_dollar_volume_usd:,.0f} minimum"
        )

    if filters.exclude_leveraged_inverse_etfs and security.is_leveraged_or_inverse:
        failures.append("leveraged and inverse ETFs are excluded")
    if filters.exclude_binary_event_sectors and security.industry in filters.binary_event_sectors:
        failures.append(f"{security.industry} is excluded as a binary-event sector")

    atr_pct = ctx.feature("atr_pct", 0.0)
    if atr_pct > filters.max_atr_pct:
        failures.append(f"ATR {atr_pct:.1f}% of price exceeds the {filters.max_atr_pct:.1f}% cap")
    if 0 < atr_pct < filters.min_atr_pct:
        failures.append(f"ATR {atr_pct:.1f}% too low for a swing move to clear costs")
    hv = ctx.feature("hv_20", 0.0)
    if hv > filters.max_annualised_volatility:
        failures.append(f"annualised volatility {hv:.2f} above the cap")

    # Earnings guard.
    days = ctx.days_to_earnings()
    if days is not None and 0 <= days < filters.min_days_to_earnings:
        failures.append(f"earnings in {days} days, inside the exclusion window")
    if (
        filters.max_days_to_earnings is not None
        and days is not None
        and days > filters.max_days_to_earnings
    ):
        failures.append(f"earnings {days} days away, beyond the configured window")

    # Sentiment quality gates.
    #
    # These only *veto* a candidate when the strategy's thesis actually rests on
    # sentiment. For a price-driven setup such as post-earnings drift, a thin or
    # noisy social sample is missing evidence, not contrary evidence: it belongs
    # in the confidence score, which already penalises it, rather than as a hard
    # veto. Vetoing there would silently disable every price-based strategy
    # whenever the social sources were quiet or unconfigured.
    if sentiment is not None and requires_sentiment:
        if sentiment.unique_authors < filters.min_unique_authors:
            failures.append(
                f"only {sentiment.unique_authors} unique authors, below the "
                f"{filters.min_unique_authors} minimum"
            )
        if sentiment.confidence < filters.min_sentiment_confidence:
            failures.append(
                f"sentiment confidence {sentiment.confidence:.2f} below the "
                f"{filters.min_sentiment_confidence:.2f} minimum"
            )

    # Manipulation risk applies whether or not the strategy leans on sentiment:
    # a coordinated promotion is a reason to avoid the name regardless of what
    # produced the setup. The promotion-failure short is the sole exception,
    # because that pattern *is* the trade.
    if (
        sentiment is not None
        and proposal.strategy != "hype_failure_short"
        and sentiment.manipulation_risk > filters.max_manipulation_risk
    ):
        failures.append(
            f"manipulation risk {sentiment.manipulation_risk:.2f} above the "
            f"{filters.max_manipulation_risk:.2f} cap"
        )

    # Reward:risk floor. This is the structural guard against a strategy that
    # wins often by taking tiny profits against large losses.
    if proposal.reward_risk_ratio < config.risk.min_reward_risk_ratio:
        failures.append(
            f"reward:risk {proposal.reward_risk_ratio:.2f} below the "
            f"{config.risk.min_reward_risk_ratio:.2f} minimum"
        )

    return failures


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def score_candidate(
    *,
    ctx: StrategyContext,
    proposal: StrategyProposal,
    config: AppConfig,
    security: SecurityInfo,
    regime: RegimeState,
    permits_earnings_risk: bool = False,
    requires_sentiment: bool = True,
) -> ScoreBreakdown:
    """Score one strategy proposal and apply the hard gates.

    Returns:
        A ``ScoreBreakdown``. ``passed`` is False when any gate failed, in which
        case the caller records the rejection rather than emitting a signal.
    """
    sentiment = ctx.sentiment
    direction = proposal.direction
    is_hype_short = proposal.strategy == "hype_failure_short"

    reddit_sentiment = sentiment if (sentiment and sentiment.source in {"all", "reddit"}) else None
    x_sentiment = sentiment if (sentiment and sentiment.source in {"all", "x"}) else None

    components = ComponentScores(
        technical_setup=_technical_score(proposal),
        price_momentum=_momentum_score(ctx, direction),
        volume_confirmation=_volume_score(ctx),
        reddit_sentiment=_sentiment_score(reddit_sentiment, direction),
        x_sentiment=_sentiment_score(x_sentiment, direction),
        sentiment_acceleration=_acceleration_score(sentiment, direction),
        attention_acceleration=_attention_score(sentiment),
        catalyst_quality=_catalyst_score(sentiment),
        earnings_risk=_earnings_score(ctx, config, permits_earnings_risk),
        liquidity=_liquidity_score(ctx, config),
        market_regime=_regime_score(regime, direction),
        manipulation_risk=_manipulation_score(sentiment, invert_for_short=is_hype_short),
        data_confidence=_data_confidence_score(ctx, sentiment, config),
    )

    weights = config.signals.component_weights
    scored = components.as_dict()
    total_weight = sum(weights.get(k, 0.0) for k in scored if k != "data_confidence")
    if total_weight <= 0:
        overall = 50.0
    else:
        overall = sum(
            scored[k] * weights.get(k, 0.0) for k in scored if k != "data_confidence"
        ) / total_weight

    # The regime raises or lowers the bar rather than the score, so a hostile
    # environment shrinks the candidate list instead of silently re-ranking it.
    overall = _clamp(overall)

    # Confidence blends data quality with sample adequacy and agreement.
    confidence = components.data_confidence / 100.0
    if sentiment is not None:
        confidence *= 0.5 + 0.5 * sentiment.confidence
        # Wide disagreement means the signal is contested, not confirmed.
        confidence *= 1.0 - 0.25 * min(1.0, sentiment.dispersion)
    else:
        confidence *= 0.75
    if ctx.data_warnings:
        confidence *= 0.7
    freshness_penalty = 1.0
    confidence = _clamp(confidence * freshness_penalty, 0.0, 1.0)

    gate_failures = apply_hard_gates(
        ctx=ctx,
        proposal=proposal,
        config=config,
        security=security,
        sentiment=sentiment,
        requires_sentiment=requires_sentiment,
    )

    notes: list[str] = []
    if sentiment is None:
        notes.append("Scored without social sentiment (source disabled or sample too small)")
    if components.earnings_risk < 40:
        notes.append("Earnings risk is materially affecting this score")

    return ScoreBreakdown(
        components=components,
        overall=round(overall, 2),
        confidence=round(confidence, 4),
        gate_failures=gate_failures,
        notes=notes,
    )
