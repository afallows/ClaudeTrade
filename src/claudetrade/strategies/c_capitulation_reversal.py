"""Strategy C -- Capitulation Reversal.

Thesis: when a name is heavily discussed, uniformly hated, extended far below
its averages, and then prints a reversal bar on climax volume, the marginal
seller has often finished.

This is the most dangerous strategy in the set and is treated accordingly:

* **Position size is reduced** (``size_multiplier``) because the distribution of
  outcomes is wide and left-skewed -- some of these names are falling for a
  reason that has not finished playing out.
* It requires **price evidence of exhaustion**, never sentiment alone. Buying
  something because everyone hates it, with no reversal in the tape, is
  indistinguishable from catching a falling knife.
* It refuses to act when an **unresolved fundamental catastrophe** is visible in
  the catalyst signals (regulatory action, fraud/going-concern language). Those
  are not sentiment overreactions; they are repricings -- this stays a hard
  veto rather than a scored component, because a spectacular reversal-bar
  score should never be able to average away a live catastrophe.

Known weaknesses: mean reversion works until it does not, and the cases where
it fails are exactly the cases that lose the most. The reduced size and the
tight time stop are what keep the left tail survivable, and they are not
optional.

Scoring model (ADR-0007 Decision 2)
------------------------------------
Previously an AND-chain of eight absolute-threshold checks stacked on top of
"sentiment must exist at all" -- fixed distance below the 50-day average,
fixed RSI ceiling, fixed climax-volume multiple, a boolean reversal-bar test,
fixed sentiment-negativity and capitulation-language floors, a fixed
mention-acceleration floor. Now a
:class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`: extension,
oversold-ness and climax volume are scored against the symbol's OWN trailing
percentile distribution (``*_pctl_120`` features -- being 15% below its own
50-day average means something different for a low-beta utility than a
volatile small-cap), and the reversal-bar test becomes continuous rather than
a single boolean.

Sentiment remains a hard veto here (``sentiment_required``) because without it
there is no capitulation to detect at all -- this is not a threshold judgement,
it is the strategy's entire premise. Earnings window, insufficient history,
illiquidity, manipulation risk and the unresolved-catalyst check are the other
hard vetoes; everything else scores.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy
from claudetrade.strategies.scoring_utils import (
    ScoreAccumulator,
    percentile_rank,
    ramp_down,
    ramp_up,
)


@register_strategy
class CapitulationReversalStrategy(Strategy):
    name = "capitulation_reversal"
    version = "v2"
    description = "Reversal entry after a sentiment capitulation and price exhaustion"
    direction_bias = Direction.LONG
    min_history_bars = 100
    permits_earnings_risk = False
    requires_sentiment = True  # without sentiment there is no capitulation to detect

    #: Catalyst labels that mean "this is repricing, not panic" -- hard veto.
    MAX_REGULATORY_CATALYST = 0.45
    ATR_STOP_MULTIPLE = 1.5
    #: Size reduction applied by the signal engine for this strategy.
    SIZE_MULTIPLIER = 0.5

    # --- score weights --------------------------------------------------------
    # See Strategy A's BASELINE comment. Kept lower than the other four
    # strategies' baseline (relatively speaking) because this is the riskiest
    # setup in the set; SIZE_MULTIPLIER, not the score gate, is the primary
    # control on that risk.
    BASELINE = 16.0
    W_EXTENSION = 18.0
    W_OVERSOLD = 14.0
    W_CLIMAX_VOLUME = 16.0
    W_REVERSAL_BAR = 14.0
    W_SENTIMENT_NEGATIVE = 12.0
    W_ATTENTION = 10.0
    W_CAPITULATION_LABEL = 12.0
    BONUS_DISPERSION = 4.0
    #: Market-signal adoption package item 1(c): a gap-down on the washout
    #: bar is additional capitulation evidence -- a scored component, kept
    #: below W_REVERSAL_BAR/W_CLIMAX_VOLUME since the reversal bar and climax
    #: volume are the strategy's primary exhaustion evidence; a gap corroborates
    #: them, it does not replace them.
    W_GAP_DOWN = 8.0
    #: A gap of this size or larger earns full W_GAP_DOWN credit; smaller
    #: gaps ramp linearly from zero.
    GAP_DOWN_FULL_CREDIT_PCT = -4.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        # --- hard vetoes ---------------------------------------------------
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None
        adv = ctx.feature("avg_dollar_volume_20", 0.0)
        if adv < self.config.filters.min_avg_dollar_volume_usd:
            self.decline(ctx, "illiquid", f"avg dollar volume ${adv:,.0f}")
            return None
        if len(ctx.bars) < 3:
            self.decline(ctx, "insufficient_bars_for_reversal_read")
            return None

        sentiment = ctx.sentiment
        if not self.sentiment_available(ctx) or sentiment is None:
            self.decline(ctx, "sentiment_required", "no usable sentiment sample")
            return None
        if sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
            self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
            return None
        regulatory = sentiment.labels.get("regulatory_catalyst", 0.0)
        if regulatory > self.MAX_REGULATORY_CATALYST:
            self.decline(
                ctx,
                "unresolved_catalyst",
                f"regulatory catalyst signal {regulatory:.2f}: repricing, not panic",
            )
            return None

        price = ctx.price
        atr = ctx.atr

        # --- score accumulation ---------------------------------------------
        cal = self.config.calibration
        score = ScoreAccumulator(baseline=self.BASELINE)

        dist_50 = ctx.feature("dist_from_sma50_pct", 0.0)
        ext_pctl = ctx.feature("dist_sma50_pctl_120", 0.5)
        score.add(
            "extension_below_sma50_pctl",
            ramp_down(ext_pctl, cal.capitulation_extension_percentile + 0.30, cal.capitulation_extension_percentile),
            self.W_EXTENSION,
        )

        rsi = ctx.feature("rsi_14", 50.0)
        rsi_pctl = ctx.feature("rsi_pctl_120", 0.5)
        score.add(
            "oversold_pctl",
            ramp_down(rsi_pctl, cal.capitulation_oversold_percentile + 0.30, cal.capitulation_oversold_percentile),
            self.W_OVERSOLD,
        )

        last, prior = ctx.bars[-1], ctx.bars[-2]
        rel_volume = ctx.feature("rel_volume_20", 1.0)
        vol_pctl = ctx.feature("rel_volume_pctl_120", 0.5)
        score.add(
            "climax_volume_pctl",
            ramp_up(vol_pctl, cal.capitulation_climax_volume_percentile - 0.30, cal.capitulation_climax_volume_percentile),
            self.W_CLIMAX_VOLUME,
        )

        bar_range = max(last.high - last.low, 1e-9)
        close_position = (last.close - last.low) / bar_range
        engulfing = last.close > prior.open and last.low < prior.low
        higher_low = last.low > prior.low and last.close > prior.close
        reversal_fraction = max(
            ramp_up(close_position, 0.40, 0.70),
            1.0 if engulfing else 0.0,
            0.8 if higher_low else 0.0,
        )
        score.add("reversal_bar_evidence", reversal_fraction, self.W_REVERSAL_BAR)

        # Market-signal adoption package item 1(c): a gap-down on the washout
        # bar (today's own overnight gap -- see patterns.gap_analysis) is
        # additional capitulation evidence, scored alongside the reversal bar
        # rather than gating on it. Absent gap_pct defaults to 0.0 (no gap),
        # which ramp_down scores as zero credit, not a crash or a veto.
        gap_pct = ctx.feature("gap_pct", 0.0)
        score.add(
            "gap_down_capitulation",
            ramp_down(gap_pct, 0.0, self.GAP_DOWN_FULL_CREDIT_PCT),
            self.W_GAP_DOWN,
        )

        score.add(
            "sentiment_negative", ramp_down(sentiment.raw_sentiment, 0.05, -0.25), self.W_SENTIMENT_NEGATIVE
        )

        mention_history = [s.mention_acceleration for s in ctx.sentiment_history][
            -cal.sentiment_percentile_window :
        ]
        mention_pctl = percentile_rank(mention_history, sentiment.mention_acceleration)
        score.add(
            "attention_elevated_pctl",
            ramp_up(mention_pctl, cal.breakout_mention_accel_percentile - 0.30, cal.breakout_mention_accel_percentile),
            self.W_ATTENTION,
        )

        capitulation = max(sentiment.capitulation, sentiment.labels.get("capitulation", 0.0))
        score.add("capitulation_language", ramp_up(capitulation, 0.15, 0.45), self.W_CAPITULATION_LABEL)

        if sentiment.dispersion > 0.5:
            # Genuine disagreement is healthier than unanimous despair.
            score.add("opinion_split", ramp_up(sentiment.dispersion, 0.5, 0.8), self.BONUS_DISPERSION)

        evidence = [
            f"Price {dist_50:.1f}% below its 50-day average ({ext_pctl:.0%} percentile of its own history)",
            f"RSI {rsi:.0f} ({rsi_pctl:.0%} percentile of its own trailing history)",
            f"Volume {rel_volume:.1f}x average on the washout ({vol_pctl:.0%} percentile)",
            f"Reversal-bar evidence: close at {close_position:.0%} of range"
            + (", engulfing" if engulfing else "")
            + (", higher low" if higher_low else ""),
            f"Sentiment {sentiment.raw_sentiment:+.2f} with capitulation language at {capitulation:.2f}",
            f"Attention {sentiment.mention_acceleration:+.0%} ({mention_pctl:.0%} percentile)",
        ]
        if gap_pct < 0:
            evidence.append(f"Gapped down {gap_pct:+.1f}% into the washout")
        risks = [
            "Mean-reversion entries fail hardest when the decline is fundamental",
            "Position size is halved for this strategy because of the wide outcome distribution",
            f"Fear reading {sentiment.fear:.2f}; further downside gaps are possible",
        ]

        threshold = cal.proposal_score_threshold
        if score.score < threshold:
            self.decline(ctx, "score_below_threshold", score.summary(threshold))
            return None

        # --- levels -----------------------------------------------------------
        # The stop sits below the capitulation low. If that low breaks, the
        # premise -- that sellers are finished -- is simply wrong.
        washout_low = min(last.low, prior.low)
        stop = min(washout_low * 0.99, price - self.ATR_STOP_MULTIPLE * atr)
        entry_low = price - 0.2 * atr
        entry_high = price + 0.6 * atr
        risk_per_share = ((entry_low + entry_high) / 2.0) - stop
        if risk_per_share <= 0:
            self.decline(ctx, "degenerate_risk")
            return None

        sma_20 = ctx.feature("sma_20", 0.0)
        first_target = entry_high + 1.6 * risk_per_share
        if sma_20 > entry_high:
            # The declining 20-day average is the natural first resistance.
            first_target = min(first_target, sma_20)
            if first_target <= entry_high + 0.9 * risk_per_share:
                first_target = entry_high + 1.5 * risk_per_share
        targets = [round(first_target, 2), round(entry_high + 3.0 * risk_per_share, 2)]

        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.LONG,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            targets=targets,
            target_fractions=[0.6, 0.4],
            expected_holding_days=6,
            time_stop_days=10,
            trailing_stop_atr=1.8,
            setup_score=score.score,
            evidence=evidence,
            invalidation=[
                f"Close below the {washout_low:.2f} capitulation low",
                "Sentiment deteriorating further rather than stabilising",
                "A confirmed negative fundamental catalyst emerging",
            ],
            exit_conditions=[
                f"Initial stop {stop:.2f} beneath the washout low",
                f"Take 60% at {targets[0]:.2f}, remainder at {targets[1]:.2f}",
                "Trail at 1.8 ATR after the first target",
                "Time stop after 10 sessions -- reversals that stall are wrong",
                "Exit on renewed sentiment deterioration",
            ],
            risks=risks,
            thesis_hint=(
                f"{ctx.symbol} washed out {dist_50:.0f}% below its 50-day average "
                f"({ext_pctl:.0%} percentile of its own history) on {rel_volume:.1f}x volume and "
                "reversed while discussion capitulated."
            ),
            extras={
                "washout_low": washout_low,
                "size_multiplier": self.SIZE_MULTIPLIER,
                "capitulation": capitulation,
                "score_breakdown": score.breakdown,
            },
        )
