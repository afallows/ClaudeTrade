"""Component scoring and confidence.

Every candidate is scored on sixteen components, each mapped to 0-100 where
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
* **One piece of evidence occupies one slot.** Each per-source polarity slot
  is scored from that source's OWN stored snapshot, and a slot whose source
  did not report contributes no weight at all rather than a neutral 50 or a
  copy of a neighbouring source. See ``_polarity_axis``.
* **Attention and polarity are separate axes.** Aggregator mention counts
  (ApeWisdom) feed the attention axis and nothing else -- never a polarity
  component, never manipulation risk, never data confidence. See
  ``_attention_score``.

**ADR-0009 (score promotion, shadow mode).** ``analyst_sentiment``/
``institutional_sentiment``/``cross_source_attention`` are three additional
components, computed unconditionally (``score_candidate`` always returns
them on ``ComponentScores``) but weighted only under
``SignalConfig.promoted_component_weights`` -- a SECOND, independent weight
table from ``component_weights``. ``score_candidate`` therefore returns TWO
overall scores on ``ScoreBreakdown``: ``overall`` (the pre-existing baseline,
computed EXACTLY as before this ADR -- ``component_weights`` has no entries
for the three new components, so their effective weight there is always
zero) and ``promoted_overall`` (the same weighted-mean machinery run again
against ``promoted_component_weights``). Computing ``promoted_overall`` is
unconditional too -- it is cheap and deterministic -- but what
``signals.engine.SignalEngine`` does with it depends on
``SignalConfig.promoted_scoring_mode`` (``"off"``/``"shadow"``/``"live"``): only
the RANKING policy differs by mode, not what gets computed. This split is
deliberate -- see ``docs/decisions/ADR-0009-score-promotion-weighting.md``'s
"Implementation notes" for the two-table rationale and
:func:`_promoted_overall`/:func:`_analyst_sentiment_score`/
:func:`_institutional_sentiment_score`/:func:`_cross_source_attention_score`
below for direction-awareness and evidence-absence handling per component.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from claudetrade.config import AppConfig
from claudetrade.data.analyst import analyst_delta
from claudetrade.domain import (
    AnalystRatingAction,
    ComponentScores,
    Direction,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.strategies.base import (
    StrategyContext,
    StrategyProposal,
    is_attention_only,
)
from claudetrade.strategies.scoring_utils import percentile_rank
from claudetrade.utils.timeutils import previous_trading_day, trading_days_between


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float, lo: float, hi: float) -> float:
    """Map ``value`` from the ``[lo, hi]`` range onto 0-100, clamped."""
    if hi <= lo:
        return 50.0
    return _clamp(100.0 * (value - lo) / (hi - lo))


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One hard-gate rejection: a stable ``code`` plus the human-readable message.

    ``code`` exists for the rejection funnel (``signals.engine.ScanFunnel``):
    the message alone (e.g. ``"average dollar volume $1,234 below the
    $10,000,000 minimum"``) embeds the candidate's own numbers, so it is
    unique per candidate and useless as an aggregation key. ``code`` is the
    same value regardless of the specific numbers involved, so "how many
    candidates failed on liquidity today" is a single dict lookup rather
    than a text-parsing exercise.
    """

    code: str
    message: str


@dataclass(slots=True)
class ScoreBreakdown:
    """Scoring result plus the reasons a candidate was gated out, if it was.

    ``promoted_overall`` (ADR-0009) is the SAME components, blended a second
    time against ``SignalConfig.promoted_component_weights`` instead of
    ``component_weights`` -- always computed (cheap, deterministic), never
    itself deciding rank. ``signals.engine.SignalEngine`` is what turns it
    into a stored divergence note (mode ``"shadow"``) or an actual ranking
    key (mode ``"live"``); see ``SignalConfig.promoted_scoring_mode``.
    """

    components: ComponentScores
    overall: float
    promoted_overall: float
    confidence: float
    gate_failures: list[GateFailure] = field(default_factory=list)
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

    The 50 returned for an absent sample is a DISPLAY value only. Scoring a
    missing source as "50 = no opinion" and then weighting it is not neutral
    at all -- it drags a strongly-evidenced candidate down toward 50 and lifts
    a weak one up. ``_polarity_axis`` therefore gives an unreported source
    zero weight, which is what actually leaves the decision to the axes that
    do have evidence.
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


@dataclass(frozen=True, slots=True)
class _PolarityAxis:
    """Per-source polarity readings plus the weight each one actually earned.

    ``reddit``/``x`` are the 0-100 display values that reach
    ``ComponentScores``; ``reddit_weight``/``x_weight`` are what the weighted
    average uses, and are zero for a source that did not report.
    """

    reddit: float
    x: float
    reddit_weight: float
    x_weight: float
    #: Source labels that actually contributed, for the score's notes.
    measured: tuple[str, ...]
    #: True when no per-source row existed and the combined row stood in.
    combined_fallback: bool


def _polarity_axis(
    ctx: StrategyContext, direction: Direction, weights: dict[str, float]
) -> _PolarityAxis:
    """Score the social-polarity axis from per-source snapshots.

    The defect this replaces: the combined ``"all"`` aggregate -- the only
    snapshot the context ever carried -- was handed to BOTH the
    ``reddit_sentiment`` and the ``x_sentiment`` slot whenever it existed. One
    sample then filled two independently-weighted slots, so a symbol
    discussed only on Reddit collected X's weight as well, and the score read
    as two sources agreeing when only one had ever been consulted.

    The rule now, in three parts:

    * **Own evidence only.** A slot is scored from the snapshot stored under
      its own ``source`` and from nothing else.
    * **Missing sources are renormalised away, not filled.** A source that did
      not report contributes no score and no weight; the remaining components
      divide by a smaller total, so the decision rests proportionally more on
      the axes that do have evidence. Filling the slot with a neutral 50 and
      weighting it would be an opinion ("the crowd is undecided") that nobody
      expressed.
    * **The combined row is a fallback, not a substitute.** When no per-source
      row cleared its own sample gate but the combined aggregate did, that
      aggregate is real evidence and must not be discarded -- but it is ONE
      unattributed sample, so it earns half the axis's two-source budget,
      split in the configured proportions. It never earns both slots in full.

    Sources with no dedicated component slot (news, stocktwits) reach the
    score only through the combined row's fallback; giving them their own
    weight needs a component of their own, which is a schema change rather
    than a scoring one.
    """
    w_reddit = weights.get("reddit_sentiment", 0.0)
    w_x = weights.get("x_sentiment", 0.0)

    reddit_row = ctx.polarity_for_source("reddit")
    x_row = ctx.polarity_for_source("x")
    measured: list[str] = []
    reddit_score, x_score = 50.0, 50.0
    reddit_weight, x_weight = 0.0, 0.0
    if reddit_row is not None:
        reddit_score = _sentiment_score(reddit_row, direction)
        reddit_weight = w_reddit
        measured.append("reddit")
    if x_row is not None:
        x_score = _sentiment_score(x_row, direction)
        x_weight = w_x
        measured.append("x")
    if measured:
        return _PolarityAxis(
            reddit_score, x_score, reddit_weight, x_weight, tuple(measured), False
        )

    combined = ctx.polarity_for_source("all") or ctx.sentiment
    if combined is None or is_attention_only(combined):
        return _PolarityAxis(50.0, 50.0, 0.0, 0.0, (), False)
    score = _sentiment_score(combined, direction)
    # Both display slots show the same reading because that reading genuinely
    # pools whatever platforms contributed -- but the WEIGHT behind them is
    # halved, so the axis counts one sample once.
    return _PolarityAxis(score, score, w_reddit / 2.0, w_x / 2.0, ("all",), True)


@dataclass(frozen=True, slots=True)
class _AttentionAxis:
    """Attention reading plus the weight it earned (zero when unmeasured)."""

    score: float
    weight: float
    measured: tuple[str, ...]


def _attention_score(ctx: StrategyContext, config: AppConfig) -> _AttentionAxis:
    """Attention is *unsigned*: a crowd gathering is neither bullish nor bearish.

    This is why mention count is scored on its own axis and never folded into
    polarity -- rising attention on a deteriorating name is a short setup, not
    a long one.

    Two inputs, blended by ``SignalConfig.attention_aggregator_weight``:

    * **Local** -- ``mention_acceleration`` from this application's own posts
      (the combined row). Narrow: whatever the rate-limited Reddit/X/news
      fetches happened to catch.
    * **Aggregator** -- ApeWisdom's per-community tallies, which count a far
      wider corpus continuously. These were previously excluded from every
      axis, on the correct reasoning that they carry no polarity. That
      reasoning does not extend to attention, which is the one thing they
      measure better than anything else here.

    The aggregator's counts run ~100x the local ones, so its reading is
    normalised against ITS OWN history -- a percentile rank of today's growth
    within that same source's trailing series for this symbol -- before it is
    blended. Ranks are unitless, so nothing about the two corpora's sizes
    survives into the blend; a raw sum or an unranked ratio comparison would
    let the wide corpus swamp the local signal, or (since a large corpus
    swings less) be permanently drowned by it.

    A source without ``attention_min_history_sessions`` observations has no
    distribution to be ranked against and is skipped entirely rather than
    mixed in raw. When neither input is available the axis reports zero
    weight, so it is renormalised away instead of scoring a neutral 50.

    Attention rows reach nothing else. Only ``mention_acceleration`` is ever
    read from them: they have no polarity, no authors, no text, so they
    cannot and must not touch ``raw_sentiment``, ``bull_bear_ratio``,
    ``unique_authors``, ``bot_risk``, ``duplicate_ratio`` or
    ``manipulation_risk``.
    """
    weight = config.signals.component_weights.get("attention_acceleration", 0.0)
    measured: list[str] = []

    local: float | None = None
    if ctx.sentiment is not None:
        local = _scale(ctx.sentiment.mention_acceleration, -0.3, 1.0)
        measured.append("local")

    ranked: list[float] = []
    minimum = max(1, config.signals.attention_min_history_sessions)
    for source, snapshot in sorted(ctx.attention_by_source.items()):
        history = [s.mention_acceleration for s in ctx.attention_history.get(source, [])]
        if len(history) < minimum:
            continue
        ranked.append(_scale(percentile_rank(history, snapshot.mention_acceleration), 0.0, 1.0))
        measured.append(source)
    aggregate = sum(ranked) / len(ranked) if ranked else None

    if local is None and aggregate is None:
        return _AttentionAxis(50.0, 0.0, ())
    if aggregate is None:
        return _AttentionAxis(_clamp(local or 50.0), weight, tuple(measured))
    if local is None:
        return _AttentionAxis(_clamp(aggregate), weight, tuple(measured))
    share = min(1.0, max(0.0, config.signals.attention_aggregator_weight))
    return _AttentionAxis(
        _clamp((1.0 - share) * local + share * aggregate), weight, tuple(measured)
    )


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


# --------------------------------------------------------------------------
# ADR-0009: promoted-scoring components (analyst / institutional / cross-
# source attention). Each returns ``(score_0_to_100, has_evidence)`` --
# ``has_evidence`` is what the caller uses to zero the component's weight
# when absent (mirroring ``_polarity_axis``/``_attention_score`` above,
# never a weighted neutral 50).
# --------------------------------------------------------------------------

#: A snapshot older than this many trading sessions is treated as stale (no
#: evidence) rather than acted on -- an analyst picture five sessions old is
#: not "current coverage" for a swing-trading composite.
ANALYST_STALE_SESSIONS = 5
#: Trailing trading-session window for the rating-action tilt sub-blend.
RATING_TILT_WINDOW_SESSIONS = 10
#: Divisor for the star-weighted rating-tilt sum before ``tanh`` squashing --
#: calibrated so a single 5-star reiterated buy already registers a strong
#: (but not saturated) tilt, and several corroborating actions saturate it.
RATING_TILT_SCALE = 3.0
#: Divisor for the coverage-change kicker before ``tanh`` squashing -- a
#: change of a few analysts is already a meaningful shift in a name's
#: covered-analyst count.
COVERAGE_CHANGE_SCALE = 3.0
#: Adanos feeds observed in this codebase (X, Reddit, News, Polymarket) --
#: used only as the corroboration sub-component's upper scale bound, an
#: honestly-approximate proxy (see ``_cross_source_attention_score``), not a
#: vendor-documented constant.
ADANOS_KNOWN_PLATFORMS = 4


def _sub_blend(parts: Sequence[tuple[float | None, float]]) -> float:
    """Weighted mean over ``(value, weight)`` pairs, skipping ``None`` values
    and renormalising the remaining weights.

    The same evidence-absent renormalisation ``_polarity_axis``/
    ``score_candidate`` apply at the top-level component, mirrored here one
    level down: an unavailable SUB-signal (no stored price target, no
    previous snapshot to diff a coverage change against, fewer than 7 points
    of Adanos trend history) contributes no weight rather than a neutral 50
    dragging the parent component toward the middle. Falls back to a neutral
    50.0 only when EVERY part in the blend is unavailable.
    """
    present = [(v, w) for v, w in parts if v is not None]
    total = sum(w for _, w in present)
    if total <= 0:
        return 50.0
    return sum(v * w for v, w in present) / total


def _rating_action_tilt(
    actions: Sequence[AnalystRatingAction], session: dt.date, sign: int
) -> float | None:
    """10-trading-session, star-weighted, sign-squashed rating tilt.

    **Rating-tilt substitute (coordinator-approved deviation from ADR-0009's
    literal wording).** The ADR asks for "star-weighted upgrades and
    initiations minus downgrades" -- unavailable as stated:
    ``domain.AnalystRatingAction.action_id``/``action_label`` are CONFIRMED
    only for ``action_id=3`` ("upgrade") and ``action_id=5`` ("reiterate");
    downgrades and initiations have no confirmed ``action_id`` mapping in
    this codebase (see that field's own docstring, and ADR-0008 Decision 1:
    never fabricate meaning for an unconfirmed field). Substituted instead:
    each action's CONFIRMED ``rating_label`` ("buy"/"sell"; "hold" and
    unrated actions carry no directional tilt and are excluded), star-
    weighted by ``analyst_stars`` (defaulting to 1.0 when the vendor did not
    report a star rating) and summed over the trailing
    ``RATING_TILT_WINDOW_SESSIONS`` trading sessions, then ``tanh``-squashed
    -- "recent rating tilt weighted by analyst quality" rather than literal
    upgrade/downgrade counting. SIGNED: this sub-blend is polarity-shaped
    (a buy-rating tilt is bullish, a sell-rating tilt is bearish), so it
    mirrors ``_sentiment_score``'s direction flip for shorts.

    Returns ``None`` (excluded from the parent blend, not scored 50) when no
    rating action with a directional label falls inside the window.
    """
    window_start = previous_trading_day(session, skip=RATING_TILT_WINDOW_SESSIONS)
    numerator = 0.0
    weight_total = 0.0
    for action in actions:
        if not (window_start <= action.date <= session):
            continue
        if action.rating_label == "buy":
            rating_sign = 1.0
        elif action.rating_label == "sell":
            rating_sign = -1.0
        else:
            continue  # "hold", unrated, or unmapped: no directional tilt
        stars = action.analyst_stars if action.analyst_stars and action.analyst_stars > 0 else 1.0
        numerator += rating_sign * stars
        weight_total += stars
    if weight_total <= 0:
        return None
    squashed = math.tanh(numerator / RATING_TILT_SCALE)
    return _scale(sign * squashed, -1.0, 1.0)


def _analyst_sentiment_score(
    ctx: StrategyContext, direction: Direction
) -> tuple[float, bool]:
    """ADR-0009: TipRanks analyst-consensus blend.

    Four sub-blends (``_sub_blend``, renormalising over whichever are
    available for this snapshot):

    * **Consensus tilt** (0.40, SIGNED): ``(buy - sell) / (buy + hold +
      sell)``, mapped to 0-100. Polarity-shaped -- mirrors
      ``_sentiment_score``'s sign flip for shorts.
    * **Price-target upside** (0.30, SIGNED): ``price_target_mean / price -
      1``, clamped to +/-30% before mapping. Polarity-shaped, same flip.
    * **Rating-action tilt** (0.20, SIGNED): see
      :func:`_rating_action_tilt` for the rating-tilt substitute this
      codebase uses in place of the ADR's unconfirmable upgrade/downgrade
      counting.
    * **Coverage-change kicker** (0.10, **UNSIGNED**): ``data.analyst
      .analyst_delta``'s ``coverage_change`` (analyst count added/dropped
      since the previous stored snapshot), ``tanh``-squashed. Deliberately
      NOT sign-flipped for shorts -- more analysts picking up coverage is a
      change in ATTENTION on the name, not a bullish or bearish opinion,
      the same "a crowd gathering is neither bullish nor bearish" reasoning
      ``_attention_score`` documents for mention counts. This is a
      coordinator-approved deviation from the ADR's flat "mirror
      reddit_sentiment" instruction, not an oversight -- see
      ``docs/decisions/ADR-0009-score-promotion-weighting.md``'s
      "Implementation notes".

    Evidence-absent (returns ``(50.0, False)``) when the symbol has no
    stored analyst snapshot, when the latest snapshot's ``analyst_count`` is
    0, or when the latest snapshot is older than ``ANALYST_STALE_SESSIONS``
    trading sessions (stale-snapshot guard).
    """
    history = ctx.analyst_history
    if not history:
        return 50.0, False
    latest = history[-1]
    if latest.analyst_count <= 0:
        return 50.0, False
    if trading_days_between(latest.as_of_session, ctx.session) > ANALYST_STALE_SESSIONS:
        return 50.0, False

    previous = history[-2] if len(history) >= 2 else None
    delta = analyst_delta(latest, previous)
    sign = direction.sign or 1

    total = latest.buy_count + latest.hold_count + latest.sell_count
    consensus_component = (
        _scale(sign * (latest.buy_count - latest.sell_count) / total, -1.0, 1.0)
        if total > 0
        else None
    )

    pt_component: float | None = None
    if latest.price_target_mean is not None and ctx.price > 0:
        upside = max(-0.30, min(0.30, latest.price_target_mean / ctx.price - 1.0))
        pt_component = _scale(sign * upside, -0.30, 0.30)

    rating_component = _rating_action_tilt(latest.recent_rating_actions, ctx.session, sign)

    coverage_component: float | None = None
    if delta.has_previous and delta.coverage_change is not None:
        coverage_component = _scale(
            math.tanh(delta.coverage_change / COVERAGE_CHANGE_SCALE), -1.0, 1.0
        )

    blended = _sub_blend(
        [
            (consensus_component, 0.40),
            (pt_component, 0.30),
            (rating_component, 0.20),
            (coverage_component, 0.10),
        ]
    )
    return blended, True


def _institutional_sentiment_score(
    ctx: StrategyContext, direction: Direction
) -> tuple[float, bool]:
    """ADR-0009: direct map of the stored institutional score.

    ``InstitutionalScorePoint.score`` is already the blended, staleness-
    discounted [-1, +1] figure ``tipranks_institutional.institutional_score``
    computed at INGEST time (see that function and ``data.institutional
    .load_history``) -- it is read straight off the row here, never
    recomputed, and staleness is already inside it, so no second discount is
    applied. SIGNED: this score is polarity-carrying (net insider/hedge-fund
    bullishness vs. bearishness), so it mirrors ``_sentiment_score``'s sign
    flip for shorts, per ADR-0009 Decision 3's instruction to mirror the
    existing direction-awareness precedent for polarity-shaped evidence.

    Evidence-absent (returns ``(50.0, False)``) when the symbol has no
    stored institutional snapshot, or the latest one's ``score`` is
    ``None`` (both axes absent/fully stale -- see
    ``InstitutionalScoreResult.score``'s own docstring).
    """
    history = ctx.institutional_history
    if not history:
        return 50.0, False
    latest = history[-1]
    if latest.score is None:
        return 50.0, False
    sign = direction.sign or 1
    return _clamp((sign * latest.score + 1.0) * 50.0), True


def _cross_source_attention_score(
    ctx: StrategyContext, direction: Direction
) -> tuple[float, bool]:
    """ADR-0009: Adanos cross-platform buzz/polarity/corroboration blend.

    Three sub-blends (``_sub_blend``) over the symbol's latest stored
    ``AttentionAggregate`` (``ctx.adanos_history[-1]``):

    * **Buzz percentile** (0.40, **UNSIGNED**): today's ``buzz_score``
      ranked (``strategies.scoring_utils.percentile_rank``) against the
      aggregate's OWN embedded ``trend_history`` (the vendor's 7-point
      trailing buzz series) -- "how loud is this, relative to its own
      recent normal", which carries no bullish/bearish direction.
    * **Bullish-minus-bearish spread** (0.40, SIGNED): ``bullish_pct -
      bearish_pct``, mapped from [-100, 100]. This is the one
      polarity-shaped sub-component, so it alone mirrors
      ``_sentiment_score``'s sign flip for shorts.
    * **Corroboration bonus** (0.20, **UNSIGNED**): the number of platforms
      reporting on the symbol this session, scaled against
      ``ADANOS_KNOWN_PLATFORMS``. An honest approximation -- Adanos does not
      expose a per-platform trend-AGREEMENT count, only the already-folded
      dominant ``trend`` -- documented as such rather than presented as a
      precise "platforms agreeing" tally.

    **Direction-awareness split (coordinator-approved deviation from the
    ADR's flat "mirror reddit_sentiment" instruction).** Buzz percentile and
    the corroboration bonus are ATTENTION-shaped ("how much" and "how many
    sources"), not polarity-shaped, so -- per the same "a crowd gathering is
    neither bullish nor bearish" reasoning ``_attention_score`` documents --
    they are never sign-flipped for shorts; only the bull/bear spread is.
    See ``docs/decisions/ADR-0009-score-promotion-weighting.md``'s
    "Implementation notes" for the full rationale.

    Evidence-absent (returns ``(50.0, False)``) when the symbol has no
    stored Adanos snapshot at all.
    """
    history = ctx.adanos_history
    if not history:
        return 50.0, False
    latest = history[-1]
    sign = direction.sign or 1

    buzz_component: float | None = None
    if latest.trend_history:
        pct = percentile_rank(latest.trend_history, latest.buzz_score)
        buzz_component = _scale(pct, 0.0, 1.0)  # unsigned: attention-shaped

    spread_component: float | None = None
    if latest.bullish_pct is not None and latest.bearish_pct is not None:
        spread = latest.bullish_pct - latest.bearish_pct
        spread_component = _scale(sign * spread, -100.0, 100.0)  # signed: polarity-shaped

    corroboration_component: float | None = None
    if latest.platforms:
        corroboration_component = _scale(
            float(len(latest.platforms)), 1.0, float(ADANOS_KNOWN_PLATFORMS)
        )  # unsigned: attention-shaped

    blended = _sub_blend(
        [
            (buzz_component, 0.40),
            (spread_component, 0.40),
            (corroboration_component, 0.20),
        ]
    )
    return blended, True


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
) -> list[GateFailure]:
    """Filters that no score can override.

    These exist because a weighted average is exactly the wrong tool for a
    disqualifying condition: a spectacular sentiment reading should not be able
    to average away an illiquid stock two days from earnings.

    Codes are chosen to match the equivalent strategy-level hard-veto codes
    (``Strategy.decline``'s first argument, e.g. ``"illiquid"``,
    ``"earnings_window"``, ``"manipulation_risk"``) wherever the same
    underlying condition exists at both layers, so the scan funnel
    (``signals.engine.ScanFunnel``) rolls the two up under one bucket instead
    of splitting an identical rejection reason across two labels.
    """
    failures: list[GateFailure] = []
    filters = config.filters
    price = ctx.price

    if price < filters.min_price:
        failures.append(
            GateFailure(
                "price_below_minimum",
                f"price {price:.2f} below the {filters.min_price:.2f} minimum",
            )
        )
    if price > filters.max_price:
        failures.append(
            GateFailure(
                "price_above_maximum",
                f"price {price:.2f} above the {filters.max_price:.2f} maximum",
            )
        )
    if filters.exclude_penny_stocks and price < 5.0:
        failures.append(GateFailure("penny_stock", "penny stocks are excluded"))

    if security.exchange and security.exchange not in config.universe.permitted_exchanges:
        failures.append(
            GateFailure(
                "exchange_not_permitted", f"exchange {security.exchange} is not permitted"
            )
        )

    market_cap = security.market_cap_usd or 0.0
    if market_cap and market_cap < filters.min_market_cap_usd:
        failures.append(
            GateFailure(
                "market_cap_too_small", f"market cap ${market_cap:,.0f} below the minimum"
            )
        )

    adv = ctx.feature("avg_dollar_volume_20", 0.0)
    if adv < filters.min_avg_dollar_volume_usd:
        failures.append(
            GateFailure(
                "illiquid",
                f"average dollar volume ${adv:,.0f} below the "
                f"${filters.min_avg_dollar_volume_usd:,.0f} minimum",
            )
        )

    if filters.exclude_leveraged_inverse_etfs and security.is_leveraged_or_inverse:
        failures.append(
            GateFailure("leveraged_etf", "leveraged and inverse ETFs are excluded")
        )
    if filters.exclude_binary_event_sectors and security.industry in filters.binary_event_sectors:
        failures.append(
            GateFailure(
                "binary_event_sector",
                f"{security.industry} is excluded as a binary-event sector",
            )
        )

    atr_pct = ctx.feature("atr_pct", 0.0)
    if atr_pct > filters.max_atr_pct:
        failures.append(
            GateFailure(
                "volatility_too_high",
                f"ATR {atr_pct:.1f}% of price exceeds the {filters.max_atr_pct:.1f}% cap",
            )
        )
    if 0 < atr_pct < filters.min_atr_pct:
        failures.append(
            GateFailure(
                "volatility_too_low",
                f"ATR {atr_pct:.1f}% too low for a swing move to clear costs",
            )
        )
    hv = ctx.feature("hv_20", 0.0)
    if hv > filters.max_annualised_volatility:
        failures.append(
            GateFailure(
                "realised_volatility_too_high", f"annualised volatility {hv:.2f} above the cap"
            )
        )

    # Earnings guard.
    days = ctx.days_to_earnings()
    if days is not None and 0 <= days < filters.min_days_to_earnings:
        failures.append(
            GateFailure(
                "earnings_window", f"earnings in {days} days, inside the exclusion window"
            )
        )
    if (
        filters.max_days_to_earnings is not None
        and days is not None
        and days > filters.max_days_to_earnings
    ):
        failures.append(
            GateFailure(
                "earnings_window_max", f"earnings {days} days away, beyond the configured window"
            )
        )

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
                GateFailure(
                    "sentiment_thin",
                    f"only {sentiment.unique_authors} unique authors, below the "
                    f"{filters.min_unique_authors} minimum",
                )
            )
        if sentiment.confidence < filters.min_sentiment_confidence:
            failures.append(
                GateFailure(
                    "sentiment_confidence_low",
                    f"sentiment confidence {sentiment.confidence:.2f} below the "
                    f"{filters.min_sentiment_confidence:.2f} minimum",
                )
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
            GateFailure(
                "manipulation_risk",
                f"manipulation risk {sentiment.manipulation_risk:.2f} above the "
                f"{filters.max_manipulation_risk:.2f} cap",
            )
        )

    # Reward:risk floor. This is the structural guard against a strategy that
    # wins often by taking tiny profits against large losses.
    if proposal.reward_risk_ratio < config.risk.min_reward_risk_ratio:
        failures.append(
            GateFailure(
                "reward_risk_floor",
                f"reward:risk {proposal.reward_risk_ratio:.2f} below the "
                f"{config.risk.min_reward_risk_ratio:.2f} minimum",
            )
        )

    return failures


# --------------------------------------------------------------------------
# Research-revision effective score
# --------------------------------------------------------------------------


def adjusted_overall(
    components: dict[str, float],
    overall_score: float,
    adjustments: dict[str, float],
    config: AppConfig,
) -> float:
    """The signal's overall score after append-only research adjustments.

    Read-time only: never stored, never mutates ``SignalRow.overall_score``.
    ``signals.research.ResearchLedger`` calls this to report the "effective
    score" for a signal that has one or more research revisions, and
    ``mcp_server.get_signals`` calls it to re-rank the screener.

    **Why a difference, not a rebuild.** The obvious alternative -- add the
    adjustments to ``components`` and re-run this module's own weighted-mean
    blend from scratch -- cannot be trusted to reproduce ``overall_score``.
    ``score_candidate`` weights each component by its EFFECTIVE weight, not
    its configured one: a sentiment axis with no evidence contributes zero
    weight and is renormalised away (see ``_polarity_axis``), and only the
    original scoring call -- which had ``ctx``, not just the persisted 0-100
    numbers -- knew which components were actually evidenced. Recomputing the
    base here would silently diverge from the audited ``overall_score``
    precisely for the candidates where that renormalisation mattered most,
    which defeats the whole point of ``overall_score`` being an audited,
    reproducible number.

    So this applies a *weighted delta* to the existing, already-correct
    score instead: only the CHANGE the research introduced needs a weighting
    scheme, and using the nominal configured weights -- renormalised over
    whichever components the stored dict actually has, weight 0 for any
    that are missing -- for that small delta is a reasonable, auditable
    approximation. It can never itself produce a base mismatch, because it
    never recomputes the base; at worst an unusual weighting of the delta.

    Args:
        components: the signal's stored ``ComponentScores.as_dict()``
            (0-100 per component, as recorded on ``SignalRow.components``).
        overall_score: the signal's stored, audited overall score.
        adjustments: component name -> already-clamped signed delta (see
            ``McpConfig.max_component_adjustment`` and
            ``signals.research.ResearchLedger.append_research_revision``,
            which is also where an unknown component name is rejected
            outright). A name here that is not a key of ``components`` is
            silently ignored rather than raising -- consistent with that
            rejection happening upstream, not a second, looser policy at
            this layer.
        config: supplies ``SignalConfig.component_weights``.

    Returns:
        ``overall_score`` plus the weighted mean of the deltas, clamped to
        [0, 100]. Unchanged (aside from the clamp) when ``adjustments`` is
        empty or none of its keys are present in ``components``.
    """
    if not adjustments:
        return _clamp(overall_score)

    weights = config.signals.component_weights
    total_weight = sum(weights.get(name, 0.0) for name in components)
    if total_weight <= 0:
        return _clamp(overall_score)

    weighted_delta = (
        sum(
            weights.get(name, 0.0) * float(delta)
            for name, delta in adjustments.items()
            if name in components
        )
        / total_weight
    )

    return _clamp(overall_score + weighted_delta)


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

    weights = config.signals.component_weights
    polarity = _polarity_axis(ctx, direction, weights)
    attention = _attention_score(ctx, config)
    # ADR-0009: computed unconditionally, exactly like every other component
    # -- ``has_evidence`` feeds the PROMOTED composite's renormalisation
    # below; it never touches the baseline ``overall`` computation, which
    # remains byte-identical to pre-ADR-0009 behaviour (``component_weights``
    # simply has no entries for these three names).
    analyst_score, analyst_evidence = _analyst_sentiment_score(ctx, direction)
    institutional_score, institutional_evidence = _institutional_sentiment_score(
        ctx, direction
    )
    cross_source_score, cross_source_evidence = _cross_source_attention_score(
        ctx, direction
    )

    components = ComponentScores(
        technical_setup=_technical_score(proposal),
        price_momentum=_momentum_score(ctx, direction),
        volume_confirmation=_volume_score(ctx),
        reddit_sentiment=polarity.reddit,
        x_sentiment=polarity.x,
        sentiment_acceleration=_acceleration_score(sentiment, direction),
        attention_acceleration=attention.score,
        catalyst_quality=_catalyst_score(sentiment),
        earnings_risk=_earnings_score(ctx, config, permits_earnings_risk),
        liquidity=_liquidity_score(ctx, config),
        market_regime=_regime_score(regime, direction),
        # Polarity-side risk only, and only ever from the combined row: an
        # aggregator attention tally reports no authors and no text, so it
        # has nothing to say about coordinated promotion or data quality.
        manipulation_risk=_manipulation_score(sentiment, invert_for_short=is_hype_short),
        analyst_sentiment=analyst_score,
        institutional_sentiment=institutional_score,
        cross_source_attention=cross_source_score,
        data_confidence=_data_confidence_score(ctx, sentiment, config),
    )

    scored = components.as_dict()
    # Effective (not configured) weights: a component whose evidence is
    # missing earns none. Dividing by the smaller total renormalises the rest,
    # which is what "we have no reading on this axis" should mean -- unlike
    # weighting a placeholder 50, which is an active vote for indecision and
    # pulls every candidate toward the middle in proportion to how quiet its
    # social coverage happens to be.
    #
    # NOTE (ADR-0009): this block is UNCHANGED from pre-ADR-0009 code, on
    # purpose -- ``weights`` is ``component_weights`` (the baseline table),
    # which has no entries for ``analyst_sentiment``/``institutional_
    # sentiment``/``cross_source_attention``, so ``weights.get(name, 0.0)``
    # already zeroes them out here with no extra code. This is exactly what
    # keeps shadow-mode's baseline ranking byte-identical to today's.
    effective = {k: weights.get(k, 0.0) for k in scored if k != "data_confidence"}
    effective["reddit_sentiment"] = polarity.reddit_weight
    effective["x_sentiment"] = polarity.x_weight
    effective["attention_acceleration"] = attention.weight
    total_weight = sum(effective.values())
    if total_weight <= 0:
        overall = 50.0
    else:
        overall = sum(scored[k] * effective[k] for k in effective) / total_weight

    # The regime raises or lowers the bar rather than the score, so a hostile
    # environment shrinks the candidate list instead of silently re-ranking it.
    overall = _clamp(overall)

    # ADR-0009: the PROMOTED composite -- same ``scored`` components, blended
    # a second time against ``promoted_component_weights`` (an entirely
    # independent table; see that field's docstring for the two-table
    # rationale), with the SAME evidence-conditional weighting the baseline
    # above uses for reddit/x/attention, extended to the three new
    # components via the ``has_evidence`` flags computed above.  A dedicated
    # dict, never touching ``effective`` above, so this cannot alter the
    # baseline by construction.
    promoted_weights = config.signals.promoted_component_weights
    promoted_effective = {k: promoted_weights.get(k, 0.0) for k in scored if k != "data_confidence"}
    promoted_effective["reddit_sentiment"] *= 1.0 if "reddit" in polarity.measured else (
        0.5 if polarity.combined_fallback else 0.0
    )
    promoted_effective["x_sentiment"] *= 1.0 if "x" in polarity.measured else (
        0.5 if polarity.combined_fallback else 0.0
    )
    promoted_effective["attention_acceleration"] *= 1.0 if attention.weight > 0 else 0.0
    promoted_effective["analyst_sentiment"] *= 1.0 if analyst_evidence else 0.0
    promoted_effective["institutional_sentiment"] *= 1.0 if institutional_evidence else 0.0
    promoted_effective["cross_source_attention"] *= 1.0 if cross_source_evidence else 0.0
    promoted_total_weight = sum(promoted_effective.values())
    if promoted_total_weight <= 0:
        promoted_overall = 50.0
    else:
        promoted_overall = (
            sum(scored[k] * promoted_effective[k] for k in promoted_effective)
            / promoted_total_weight
        )
    promoted_overall = _clamp(promoted_overall)

    # Confidence blends data quality with sample adequacy and agreement.
    confidence = components.data_confidence / 100.0
    if sentiment is not None:
        sentiment_factor = 0.5 + 0.5 * sentiment.confidence
        # Wide disagreement means the signal is contested, not confirmed.
        sentiment_factor *= 1.0 - 0.25 * min(1.0, sentiment.dispersion)
        # Floored at the no-sentiment multiplier below: a thin or noisy
        # social sample is missing evidence, not contrary evidence, and must
        # never leave a candidate WORSE off than having no sentiment row at
        # all -- otherwise storing a weak aggregate actively suppresses the
        # price-driven strategies for that symbol.
        confidence *= max(0.75, sentiment_factor)
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
    # Which evidence was MISSING, in the signal's own notes (these reach
    # ``Signal.data_warnings``, so only genuine caveats belong here). A
    # per-source slot scored from a stand-in used to be indistinguishable
    # from one scored from its own sample, and that is precisely what made
    # the double-count invisible for as long as it was. Complete two-source
    # evidence adds no note: there is nothing to caveat.
    if polarity.combined_fallback:
        notes.append(
            "No per-source sentiment breakdown stored; the combined aggregate was "
            "weighted as a single source rather than as agreement between several"
        )
    elif not polarity.measured:
        notes.append("No usable sentiment sample; the polarity components carry no weight")
    elif len(polarity.measured) == 1:
        notes.append(
            f"Sentiment scored from {polarity.measured[0]} alone; no other source "
            "reported a usable sample, so its weight was renormalised away"
        )
    if attention.weight <= 0:
        notes.append("No usable attention sample; the attention component carries no weight")
    if components.earnings_risk < 40:
        notes.append("Earnings risk is materially affecting this score")

    return ScoreBreakdown(
        components=components,
        overall=round(overall, 2),
        promoted_overall=round(promoted_overall, 2),
        confidence=round(confidence, 4),
        gate_failures=gate_failures,
        notes=notes,
    )
