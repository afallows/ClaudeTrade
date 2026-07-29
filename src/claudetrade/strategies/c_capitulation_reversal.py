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
  are not sentiment overreactions; they are repricings.

Known weaknesses: mean reversion works until it does not, and the cases where
it fails are exactly the cases that lose the most. The reduced size and the
tight time stop are what keep the left tail survivable, and they are not
optional.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy


@register_strategy
class CapitulationReversalStrategy(Strategy):
    name = "capitulation_reversal"
    version = "v1"
    description = "Reversal entry after a sentiment capitulation and price exhaustion"
    direction_bias = Direction.LONG
    min_history_bars = 100
    permits_earnings_risk = False
    requires_sentiment = True  # without sentiment there is no capitulation to detect

    #: How far below the 50-day average price must be stretched.
    MIN_DISTANCE_BELOW_SMA50 = -12.0
    MAX_RSI = 32.0
    #: Climax volume on the washout.
    MIN_CLIMAX_VOLUME = 1.8
    #: Sentiment must be decisively negative, not merely soft.
    MAX_SENTIMENT = -0.25
    #: Attention must be elevated -- a quiet decline is not a capitulation.
    MIN_MENTION_ACCELERATION = 0.35
    MIN_CAPITULATION_LABEL = 0.40
    #: Catalyst labels that mean "this is repricing, not panic".
    MAX_REGULATORY_CATALYST = 0.45
    ATR_STOP_MULTIPLE = 1.5
    #: Size reduction applied by the signal engine for this strategy.
    SIZE_MULTIPLIER = 0.5

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None

        sentiment = ctx.sentiment
        if not self.sentiment_available(ctx) or sentiment is None:
            self.decline(ctx, "sentiment_required", "no usable sentiment sample")
            return None

        price = ctx.price
        atr = ctx.atr

        # --- price must be genuinely stretched ----------------------------
        dist_50 = ctx.feature("dist_from_sma50_pct", 0.0)
        if dist_50 > self.MIN_DISTANCE_BELOW_SMA50:
            self.decline(ctx, "not_stretched", f"{dist_50:.1f}% from the 50-day average")
            return None
        rsi = ctx.feature("rsi_14", 50.0)
        if rsi > self.MAX_RSI:
            self.decline(ctx, "not_oversold", f"RSI {rsi:.1f}")
            return None

        # --- exhaustion and reversal evidence in the tape ------------------
        if len(ctx.bars) < 3:
            return None
        last, prior = ctx.bars[-1], ctx.bars[-2]
        rel_volume = ctx.feature("rel_volume_20", 1.0)
        if rel_volume < self.MIN_CLIMAX_VOLUME:
            self.decline(ctx, "no_climax_volume", f"relvol {rel_volume:.2f}")
            return None

        bar_range = max(last.high - last.low, 1e-9)
        close_position = (last.close - last.low) / bar_range
        # A reversal bar closes in its upper third, or engulfs the prior bar.
        reversal_bar = close_position >= 0.66
        engulfing = last.close > prior.open and last.low < prior.low
        higher_low = last.low > prior.low and last.close > prior.close
        if not (reversal_bar or engulfing or higher_low):
            self.decline(ctx, "no_reversal_evidence", f"close at {close_position:.0%} of range")
            return None

        # --- sentiment capitulation ----------------------------------------
        if sentiment.raw_sentiment > self.MAX_SENTIMENT:
            self.decline(ctx, "sentiment_not_negative", f"{sentiment.raw_sentiment:+.2f}")
            return None
        if sentiment.mention_acceleration < self.MIN_MENTION_ACCELERATION:
            # Quiet decline: nobody has capitulated because nobody is watching.
            self.decline(ctx, "attention_too_low", f"{sentiment.mention_acceleration:.2f}")
            return None
        capitulation = max(sentiment.capitulation, sentiment.labels.get("capitulation", 0.0))
        if capitulation < self.MIN_CAPITULATION_LABEL:
            self.decline(ctx, "no_capitulation_language", f"{capitulation:.2f}")
            return None

        # --- refuse an unresolved fundamental catastrophe -------------------
        regulatory = sentiment.labels.get("regulatory_catalyst", 0.0)
        if regulatory > self.MAX_REGULATORY_CATALYST:
            self.decline(
                ctx,
                "unresolved_catalyst",
                f"regulatory catalyst signal {regulatory:.2f}: repricing, not panic",
            )
            return None
        if sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
            self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
            return None

        evidence = [
            f"Price {dist_50:.1f}% below its 50-day average with RSI at {rsi:.0f}",
            f"Climax volume {rel_volume:.1f}x average on the washout",
            f"Reversal bar closing at {close_position:.0%} of its range",
            f"Sentiment decisively negative ({sentiment.raw_sentiment:+.2f}) with "
            f"capitulation language at {capitulation:.2f}",
            f"Attention elevated: mention rate {sentiment.mention_acceleration:+.0%}",
        ]
        risks = [
            "Mean-reversion entries fail hardest when the decline is fundamental",
            "Position size is halved for this strategy because of the wide outcome distribution",
            f"Fear reading {sentiment.fear:.2f}; further downside gaps are possible",
        ]

        setup_score = 52.0
        setup_score += min(10.0, (rel_volume - self.MIN_CLIMAX_VOLUME) * 5.0)
        setup_score += min(10.0, (self.MAX_RSI - rsi) * 0.6)
        setup_score += min(8.0, capitulation * 12.0)
        if sentiment.dispersion > 0.5:
            # Genuine disagreement is healthier than unanimous despair.
            setup_score += 4.0
            evidence.append(f"Opinion is split (dispersion {sentiment.dispersion:.2f})")

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
            setup_score=max(0.0, min(100.0, setup_score)),
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
                f"{ctx.symbol} washed out {dist_50:.0f}% below its 50-day average on "
                f"{rel_volume:.1f}x volume and reversed while discussion capitulated."
            ),
            extras={
                "washout_low": washout_low,
                "size_multiplier": self.SIZE_MULTIPLIER,
                "capitulation": capitulation,
            },
        )
