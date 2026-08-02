"""Strategy A -- Sentiment-Confirmed Breakout.

Thesis: a breakout is more likely to follow through when *new* people started
paying attention shortly before it, and the breakout bar carries real volume.

The order of evidence matters and is deliberate. Sentiment is a *filter on*
price, not a substitute for it: this strategy will not act on enthusiasm alone,
and it will not act on a breakout that nobody funded with volume. The specific
failure mode it is built to avoid is buying a heavily-promoted name that is
being distributed into retail attention -- hence the manipulation-risk and
unique-author checks rather than a raw mention count.

Sentiment is a PRECONDITION here, not a bonus
---------------------------------------------
This strategy used to declare ``requires_sentiment = False`` and treat a
missing social sample as a few lost points: a candidate with no sentiment at
all -- or with actively negative sentiment -- could still be emitted, under
the name "sentiment breakout". A recommendation is a claim about why the
trade exists, and that one was false. There is no meaningful difference for
an operator between "this fired on rising positive sentiment" and "this fired
on volume and the name happens to contain the word sentiment".

Confirmation is therefore required and vetoed on, with reason codes that
reach the scan's rejection funnel so a zero-signal scan still explains
itself: ``sentiment_unavailable`` (no usable social sample),
``sentiment_not_positive`` (current polarity at or below
``calibration.breakout_min_positive_sentiment`` on either the raw decayed
mean or the one-vote-per-author mean) and ``sentiment_confidence_low`` (a
sample too unreliable to confirm anything).

The technical-only path is not deleted -- it is *relabelled*. A
volume-confirmed breakout with no social sample is still a legitimate
candidate in a multi-strategy scan; what was never legitimate was it arriving
under this strategy's name and version, indistinguishable in the ledger and
in every backtest attribution from a trade that genuinely had sentiment
behind it. It now lives in ``f_volume_breakout`` as ``volume_breakout``,
sharing the breakout mechanics below through :class:`BreakoutStrategyBase`
and differing only in what it asks of the social sample. The two are
mutually exclusive by construction: whichever of them takes a candidate, the
other declines it, so a signal's strategy name is an unambiguous answer to
"why is this on my list?".

Known weaknesses:

* Breakouts fail often; expect a modest win rate carried by reward:risk, not a
  high hit rate. This is the correct shape and must not be "fixed" by taking
  profits earlier.
* Requires social data, and now says so: with sources disabled or still
  warming up this strategy emits nothing at all, and its share of the
  breakout candidates surfaces under ``volume_breakout`` instead.

Scoring model (ADR-0007 Decision 2)
------------------------------------
This used to be an AND-chain: any one of six absolute-threshold checks
(resistance level found, not too extended, above the 50-day average, ADX
above a fixed 18, relative volume above a fixed 1.5x, then -- if sentiment was
available -- acceleration/mentions/authors/manipulation all clearing fixed
bars) returned ``None`` at the first miss. That shape is why the strategy
almost never fired: six independent gates each with, say, a 60% pass rate
compound to a ~5% joint pass rate.

It is now a :class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`:
each condition contributes points in proportion to how strongly it is met
(:func:`~claudetrade.strategies.scoring_utils.ramp_up`, or a percentile rank
against the symbol's own trailing distribution for relative volume, trend
strength and sentiment acceleration -- see ``config.calibration``), and a
proposal is only emitted once the total clears
``config.calibration.proposal_score_threshold``. That partial-credit shape is
unchanged; what changed is that "absent sentiment" is no longer one of the
things it applies to -- an unconfirmed breakout is now a ``volume_breakout``
candidate rather than a lower-scoring ``sentiment_breakout`` one.

Four conditions remain hard vetoes from the ADR: insufficient history, the
earnings window, illiquidity, and manipulation risk above the configured cap.
A missing reference level (no resistance and no Donchian channel) is also a
hard decline -- not because it is a curve-fit threshold, but because there is
no structural input left to build an entry/stop/target from; that is a
data-availability precondition, not a scored opinion. Positive sentiment
confirmation joins them for the reason above: it is what the strategy claims
to be, not a knob on how strongly it claims it. The STRENGTH of the sentiment
is still scored, not gated -- only its presence and its sign are absolute.
"""

from __future__ import annotations

from claudetrade.domain import Direction, SymbolSentiment
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy
from claudetrade.strategies.scoring_utils import ScoreAccumulator, percentile_rank, ramp_up


class BreakoutStrategyBase(Strategy):
    """Breakout mechanics shared by ``sentiment_breakout`` and ``volume_breakout``.

    Everything about *finding and shaping* a breakout -- the reference level,
    the extension boundary, the price/volume/trend scoring, the stop and
    target construction -- is identical between the two, and duplicating it
    would guarantee the copies drift. What differs is entirely about the
    social sample, and lives in two hooks:

    * :meth:`_sentiment_precondition` -- the hard question each strategy asks
      of the sample before it is willing to act at all. This is also what
      keeps the two mutually exclusive: each declines exactly the candidates
      the other takes.
    * :meth:`_score_sentiment` -- what the sample contributes to the score
      once the precondition passed.

    Never registered and never instantiated directly: it keeps the base
    ``name = "unnamed"``, which ``register_strategy`` rejects outright.
    """

    direction_bias = Direction.LONG
    min_history_bars = 80
    permits_earnings_risk = False

    #: Stop distance in ATR when structural support is too far away.
    ATR_STOP_MULTIPLE = 2.0
    #: Extension (in ATR beyond the level) at which the extension penalty
    #: starts, and at which it reaches its maximum -- a soft cap, not a veto:
    #: a very extended breakout with everything else exceptional can still
    #: score, just not as highly.
    EXTENSION_PENALTY_START_ATR = 1.0
    EXTENSION_PENALTY_FULL_ATR = 2.5

    # --- score weights (points out of the 0-100 setup_score) --------------
    # BASELINE is calibrated so a candidate with genuinely solid (not perfect)
    # evidence across the components below lands comfortably above both
    # ``config.calibration.proposal_score_threshold`` and the engine's
    # existing ``SignalConfig.min_overall_score`` blended gate, in which this
    # strategy's own score is only one of thirteen weighted components.
    BASELINE = 22.0
    W_BREAKOUT = 22.0
    W_TREND_CONTEXT = 8.0
    W_ADX_PERCENTILE = 15.0
    W_VOLUME_PERCENTILE = 18.0
    W_RS_PERCENTILE = 8.0
    W_SENTIMENT_ACCEL = 15.0
    W_MENTION_ACCEL = 8.0
    W_UNIQUE_AUTHORS = 6.0
    PENALTY_EXTENSION = -15.0
    PENALTY_HYPE = -10.0
    #: Market-signal adoption package items 1/2/3/4 -- modest, contributing
    #: (never dominant) gap/level/volume-quality components. Each weight sits
    #: below every "core" breakout component above (W_BREAKOUT/
    #: W_ADX_PERCENTILE/W_VOLUME_PERCENTILE), by design: gaps and level
    #: confluence are corroborating evidence for a breakout already
    #: established by price and volume, not a replacement for it.
    W_GAP_UP = 6.0
    W_GAP_CONTINUATION = 5.0
    W_LEVEL_CONFLUENCE = 6.0
    PENALTY_VOLUME_DIVERGENCE = -6.0
    #: A gap of this size or larger earns full credit for W_GAP_UP; smaller
    #: gaps ramp linearly from zero.
    GAP_UP_FULL_CREDIT_PCT = 2.5
    #: level_confluence_count ramps from 1 method (no real confluence -- the
    #: candidate's own breakout level trivially "agrees" with itself) to 3
    #: independent methods for full W_LEVEL_CONFLUENCE credit.
    LEVEL_CONFLUENCE_FULL_CREDIT_COUNT = 3.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        # --- hard vetoes ---------------------------------------------------
        if not self.has_sufficient_history(ctx):
            self.decline(
                ctx,
                "insufficient_history",
                f"{len(ctx.bars)} bars",
                metrics={
                    "bars_available": float(len(ctx.bars)),
                    "bars_required": float(self.min_history_bars),
                },
            )
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None
        adv = ctx.feature("avg_dollar_volume_20", 0.0)
        if adv < self.config.filters.min_avg_dollar_volume_usd:
            self.decline(ctx, "illiquid", f"avg dollar volume ${adv:,.0f}")
            return None

        price = ctx.price
        atr = ctx.atr
        resistance = ctx.feature("resistance_level", 0.0)
        donchian_high = ctx.feature("donchian_high_20", 0.0)
        # Prefer a clustered, touched resistance level; fall back to the
        # 20-day channel high when no level has enough touches to be meaningful.
        level = resistance if resistance > 0 else donchian_high
        if level <= 0:
            self.decline(ctx, "no_reference_level", "no resistance or Donchian high available")
            return None

        sentiment = ctx.sentiment
        if sentiment is not None and sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
            self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
            return None

        # Each subclass's hard question about the social sample. It records
        # its own decline (with the reason code the funnel aggregates), so a
        # False here needs no further explanation.
        if not self._sentiment_precondition(ctx):
            return None

        extension_atr = (price - level) / atr if atr > 0 else 0.0
        # Not a curve-fit threshold: this is the same structural boundary as
        # "no reference level" above -- far enough below the level that an
        # entry pegged to it would not be a breakout trade under any scoring,
        # only a bet on price travelling most of the way there first.
        if extension_atr < -1.5:
            self.decline(ctx, "not_near_breakout", f"{extension_atr:.2f} ATR below the level")
            return None

        # --- score accumulation ---------------------------------------------
        cal = self.config.calibration
        score = ScoreAccumulator(baseline=self.BASELINE)

        score.add(
            "breakout",
            ramp_up(price, level - 0.15 * atr, level + 0.10 * atr),
            self.W_BREAKOUT,
        )
        score.add(
            "above_sma50",
            ramp_up(ctx.feature("dist_from_sma50_pct", -5.0), -3.0, 0.5),
            self.W_TREND_CONTEXT,
        )

        adx_pctl = ctx.feature("adx_pctl_120", 0.5)
        score.add(
            "trend_strength_pctl",
            ramp_up(adx_pctl, cal.breakout_trend_percentile - 0.25, cal.breakout_trend_percentile),
            self.W_ADX_PERCENTILE,
        )

        rel_volume = ctx.feature("rel_volume_20", 1.0)
        vol_pctl = ctx.feature("rel_volume_pctl_120", 0.5)
        score.add(
            "volume_pctl",
            ramp_up(vol_pctl, cal.breakout_volume_percentile - 0.30, cal.breakout_volume_percentile),
            self.W_VOLUME_PERCENTILE,
        )
        score.add(
            "relative_strength",
            ramp_up(ctx.feature("rs_percentile", 50.0), 50.0, 90.0),
            self.W_RS_PERCENTILE,
        )

        # --- gap / level-confluence / volume-quality (market-signal adoption
        # package items 1, 2, 3, 4) -- corroborating evidence, not gates.
        # Absent features default to 0 (no gap, no confluence, not flagged),
        # so this degrades to zero contribution rather than crashing when a
        # candidate's feature frame predates these columns.
        gap_pct = ctx.feature("gap_pct", 0.0)
        score.add("gap_up", ramp_up(gap_pct, 0.0, self.GAP_UP_FULL_CREDIT_PCT), self.W_GAP_UP)

        gap_continuation_up = ctx.feature("gap_continuation_up", 0.0) > 0
        score.add("gap_continuation", 1.0 if gap_continuation_up else 0.0, self.W_GAP_CONTINUATION)

        level_confluence = ctx.feature("level_confluence_count", 0.0)
        score.add(
            "level_confluence",
            ramp_up(level_confluence, 1.0, self.LEVEL_CONFLUENCE_FULL_CREDIT_COUNT),
            self.W_LEVEL_CONFLUENCE,
        )

        volume_divergence = ctx.feature("volume_divergence", 0.0) > 0
        if volume_divergence:
            score.penalty("volume_divergence", self.PENALTY_VOLUME_DIVERGENCE)

        if extension_atr > self.EXTENSION_PENALTY_START_ATR:
            score.penalty(
                "extension",
                self.PENALTY_EXTENSION
                * ramp_up(extension_atr, self.EXTENSION_PENALTY_START_ATR, self.EXTENSION_PENALTY_FULL_ATR),
            )

        evidence = [
            f"Trading at {price:.2f} vs the {level:.2f} resistance level "
            f"({extension_atr:+.2f} ATR through it)",
            f"Relative volume {rel_volume:.2f}x its 20-day average, "
            f"{vol_pctl:.0%} percentile of its own trailing history",
            f"ADX {ctx.feature('adx_14'):.0f} ({adx_pctl:.0%} percentile of its own trailing history)",
        ]
        risks: list[str] = []
        if gap_pct > 0:
            evidence.append(f"Gapped up {gap_pct:+.1f}% into the move")
        if gap_continuation_up:
            evidence.append("A later session gapped further beyond the level, extending the breakout")
        if level_confluence >= 2:
            evidence.append(
                f"{level_confluence:.0f} independent methods (swings/pivots/Fibonacci/round-number/MA) "
                "agree on a nearby level"
            )
        if volume_divergence:
            risks.append(
                "Elevated volume with little same-day price follow-through "
                "(possible absorption, not yet resolved)"
            )

        # --- sentiment contribution (subclass hook) --------------------------
        self._score_sentiment(ctx, sentiment, score, evidence, risks)

        threshold = cal.proposal_score_threshold
        if score.score < threshold:
            self.decline(ctx, "score_below_threshold", score.summary(threshold))
            return None

        # --- levels --------------------------------------------------------
        # Prefer the breakout base's support; use an ATR stop when that support
        # sits so far below that the resulting risk would be unreasonable.
        support = ctx.feature("support_level", 0.0)
        atr_stop = price - self.ATR_STOP_MULTIPLE * atr
        structural_stop = min(support, level * 0.995) if support > 0 else atr_stop
        stop = max(structural_stop, atr_stop) if structural_stop > 0 else atr_stop
        stop = min(stop, price - 0.35 * atr)  # never place the stop inside the noise band

        # ``entry_low`` may sit at the level itself (a stop-entry, waiting for
        # the breakout) rather than at the current price when the candidate
        # scored on strength elsewhere while still approaching the level --
        # ``entry_high`` is anchored relative to ``entry_low``, never to
        # ``price`` alone, so the zone can never invert.
        entry_low = max(level, price - 0.25 * atr)
        entry_high = max(price + 0.35 * atr, entry_low + 0.30 * atr)
        risk_per_share = ((entry_low + entry_high) / 2.0) - stop
        if risk_per_share <= 0:
            self.decline(ctx, "degenerate_risk")
            return None

        targets = [
            round(entry_high + 1.5 * risk_per_share, 2),
            round(entry_high + 2.75 * risk_per_share, 2),
        ]

        risks.append("Breakouts fail frequently; the stop is the thesis, not a suggestion")
        if ctx.feature("dist_from_52w_high_pct", -100.0) > -3.0:
            evidence.append("Trading within 3% of its 52-week high")

        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.LONG,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            targets=targets,
            target_fractions=[0.5, 0.5],
            expected_holding_days=10,
            time_stop_days=15,
            trailing_stop_atr=2.5,
            setup_score=score.score,
            evidence=evidence,
            invalidation=[
                f"Close back below {level:.2f} reclaimed resistance",
                f"Close below the {stop:.2f} stop level",
                "Breakout volume not sustained within three sessions",
            ],
            exit_conditions=[
                f"Initial stop {stop:.2f} ({self.ATR_STOP_MULTIPLE:.1f} ATR basis)",
                f"Take half at {targets[0]:.2f}, remainder at {targets[1]:.2f}",
                "Trail at 2.5 ATR once the first target is reached",
                "Time stop after 15 sessions regardless of position",
                "Exit before a confirmed earnings report",
            ],
            risks=risks,
            thesis_hint=(
                f"{ctx.symbol} traded {extension_atr:+.2f} ATR through {level:.2f} resistance on "
                f"{rel_volume:.1f}x volume ({vol_pctl:.0%} percentile){self._thesis_suffix()}."
            ),
            extras={
                "breakout_level": level,
                "extension_atr": extension_atr,
                "score_breakdown": score.breakdown,
            },
        )

    # --- subclass hooks ----------------------------------------------------

    def _sentiment_precondition(self, ctx: StrategyContext) -> bool:
        """Whether this strategy is willing to act on ``ctx``'s social sample.

        Implementations must call :meth:`decline` with a stable reason code
        before returning ``False`` -- the scan funnel counts those codes, and
        a silent ``False`` would make a zero-signal scan unexplainable.
        """
        raise NotImplementedError

    def _score_sentiment(
        self,
        ctx: StrategyContext,
        sentiment: SymbolSentiment | None,
        score: ScoreAccumulator,
        evidence: list[str],
        risks: list[str],
    ) -> None:
        """Contribute the social sample's points, evidence and risks."""
        raise NotImplementedError

    def _thesis_suffix(self) -> str:
        """Clause appended to the thesis hint describing the social evidence."""
        return ""


def breakout_sentiment_is_confirming(
    strategy: Strategy, ctx: StrategyContext
) -> tuple[bool, str, str, dict[str, float]]:
    """Is this candidate's social sample a genuine positive confirmation?

    Single source of truth for the split between ``sentiment_breakout`` and
    ``volume_breakout``: the first requires ``True`` here and the second
    requires ``False``, so exactly one of them can ever take a candidate and
    a signal's strategy name is never ambiguous about what justified it.
    Written as one shared function rather than two independent conditions
    precisely so the pair cannot drift into overlapping (the same breakout
    emitted twice under two names) or into a gap between them (a breakout
    silently dropped by both).

    Returns:
        ``(confirming, reason_code, detail, metrics)``. The last three
        describe *why not* when ``confirming`` is False, ready to hand
        straight to :meth:`~claudetrade.strategies.base.Strategy.decline`.
    """
    sentiment = ctx.sentiment
    cfg = strategy.config.sentiment
    if sentiment is None or not strategy.sentiment_available(ctx):
        return (
            False,
            "sentiment_unavailable",
            (
                f"{sentiment.post_count} posts from {sentiment.unique_authors} authors"
                if sentiment is not None
                else "no stored sentiment for this session"
            ),
            {
                "post_count": float(sentiment.post_count) if sentiment else 0.0,
                "posts_required": float(cfg.min_posts_for_signal),
                "unique_authors": float(sentiment.unique_authors) if sentiment else 0.0,
                "authors_required": float(cfg.min_unique_authors_for_signal),
            },
        )
    # Both the raw decayed mean and the one-vote-per-author mean must be
    # positive. The author mean alone would ignore a crowd whose loudest
    # voices are negative; the raw mean alone can be carried by one prolific
    # poster, which is the exact promotion pattern the breakout strategies
    # exist to avoid buying into.
    floor = strategy.config.calibration.breakout_min_positive_sentiment
    polarity = min(sentiment.raw_sentiment, sentiment.unique_author_sentiment)
    if polarity <= floor:
        return (
            False,
            "sentiment_not_positive",
            f"raw {sentiment.raw_sentiment:+.2f} / per-author "
            f"{sentiment.unique_author_sentiment:+.2f}, need > {floor:+.2f}",
            {"polarity": polarity, "polarity_required": floor},
        )
    minimum = strategy.config.filters.min_sentiment_confidence
    if sentiment.confidence < minimum:
        return (
            False,
            "sentiment_confidence_low",
            f"confidence {sentiment.confidence:.2f} below the {minimum:.2f} minimum",
            {"confidence": sentiment.confidence, "confidence_required": minimum},
        )
    return True, "", "", {}


@register_strategy
class SentimentBreakoutStrategy(BreakoutStrategyBase):
    name = "sentiment_breakout"
    #: v3: sentiment confirmation became a precondition rather than a scored
    #: bonus, which changes which candidates this strategy can produce at all.
    #: Bumping the version keeps v2 signals comparable only with each other
    #: instead of silently re-labelling a different rule set.
    version = "v3"
    description = "Breakout above resistance with volume and confirming positive sentiment"
    #: The thesis rests on sentiment, so the sentiment-quality hard gates in
    #: ``signals.scoring.apply_hard_gates`` apply to this strategy's
    #: candidates too -- consistent with the strategy-level veto below rather
    #: than in tension with it.
    requires_sentiment = True

    def _sentiment_precondition(self, ctx: StrategyContext) -> bool:
        confirming, reason, detail, metrics = breakout_sentiment_is_confirming(self, ctx)
        if not confirming:
            self.decline(ctx, reason, detail, metrics=metrics)
        return confirming

    def _score_sentiment(
        self,
        ctx: StrategyContext,
        sentiment: SymbolSentiment | None,
        score: ScoreAccumulator,
        evidence: list[str],
        risks: list[str],
    ) -> None:
        """Score how STRONG the confirmation is; that it exists was vetoed on."""
        assert sentiment is not None  # guaranteed by the precondition above
        cal = self.config.calibration
        accel_history = [s.sentiment_acceleration for s in ctx.sentiment_history][
            -cal.sentiment_percentile_window :
        ]
        mention_history = [s.mention_acceleration for s in ctx.sentiment_history][
            -cal.sentiment_percentile_window :
        ]
        accel_pctl = percentile_rank(accel_history, sentiment.sentiment_acceleration)
        mention_pctl = percentile_rank(mention_history, sentiment.mention_acceleration)

        score.add(
            "sentiment_accel_pctl",
            ramp_up(
                accel_pctl,
                cal.breakout_sentiment_accel_percentile - 0.30,
                cal.breakout_sentiment_accel_percentile,
            ),
            self.W_SENTIMENT_ACCEL,
        )
        score.add(
            "mention_accel_pctl",
            ramp_up(
                mention_pctl,
                cal.breakout_mention_accel_percentile - 0.30,
                cal.breakout_mention_accel_percentile,
            ),
            self.W_MENTION_ACCEL,
        )
        # Unique authors, not raw mentions: fifty posts from five accounts is
        # a promotion, not a change in opinion. Soft here, not a hard gate --
        # manipulation_risk remains the hard backstop against that.
        score.add(
            "unique_authors",
            ramp_up(
                sentiment.unique_authors,
                self.config.filters.min_unique_authors * 0.4,
                self.config.filters.min_unique_authors,
            ),
            self.W_UNIQUE_AUTHORS,
        )
        if sentiment.hype > 0.5:
            score.penalty("hype", self.PENALTY_HYPE * ramp_up(sentiment.hype, 0.5, 0.85))
            risks.append(
                f"Elevated hype ({sentiment.hype:.2f}); crowded entries reverse quickly"
            )
        evidence.append(
            f"Sentiment positive on both measures (raw {sentiment.raw_sentiment:+.2f}, "
            f"per-author {sentiment.unique_author_sentiment:+.2f})"
        )
        evidence.append(
            f"Sentiment acceleration {sentiment.sentiment_acceleration:+.2f} "
            f"({accel_pctl:.0%} percentile) across {sentiment.unique_authors} unique authors"
        )
        evidence.append(
            f"Mention rate {sentiment.mention_acceleration:+.0%} ({mention_pctl:.0%} percentile)"
        )

    def _thesis_suffix(self) -> str:
        return " with attention broadening across new participants"
