"""Strategy A -- Sentiment-Confirmed Breakout.

Thesis: a breakout is more likely to follow through when *new* people started
paying attention shortly before it, and the breakout bar carries real volume.

The order of evidence matters and is deliberate. Sentiment is a *filter on*
price, not a substitute for it: this strategy will not act on enthusiasm alone,
and it will not act on a breakout that nobody funded with volume. The specific
failure mode it is built to avoid is buying a heavily-promoted name that is
being distributed into retail attention -- hence the manipulation-risk and
unique-author checks rather than a raw mention count.

Known weaknesses:

* Breakouts fail often; expect a modest win rate carried by reward:risk, not a
  high hit rate. This is the correct shape and must not be "fixed" by taking
  profits earlier.
* Requires social data. With sources disabled it degrades to a volume-confirmed
  breakout, and its score is capped to reflect the missing evidence.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy


@register_strategy
class SentimentBreakoutStrategy(Strategy):
    name = "sentiment_breakout"
    version = "v1"
    description = "Breakout above resistance with volume and rising unique-author attention"
    direction_bias = Direction.LONG
    min_history_bars = 80
    permits_earnings_risk = False
    requires_sentiment = False  # degrades rather than disabling

    #: Breakout bar must trade this multiple of its 20-day average volume.
    MIN_RELATIVE_VOLUME = 1.5
    #: Trend gate: price must hold above the intermediate average.
    MIN_ADX = 18.0
    #: Sentiment acceleration required when social data is available.
    MIN_SENTIMENT_ACCELERATION = 0.15
    MIN_MENTION_ACCELERATION = 0.20
    #: Stop distance in ATR when structural support is too far away.
    ATR_STOP_MULTIPLE = 2.0
    #: Reject if price has already run this far beyond the breakout level.
    MAX_EXTENSION_ATR = 1.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None

        price = ctx.price
        atr = ctx.atr
        resistance = ctx.feature("resistance_level", 0.0)
        donchian_high = ctx.feature("donchian_high_20", 0.0)
        # Prefer a clustered, touched resistance level; fall back to the
        # 20-day channel high when no level has enough touches to be meaningful.
        level = resistance if resistance > 0 else donchian_high
        if level <= 0:
            self.decline(ctx, "no_resistance_level")
            return None

        # --- price structure ---------------------------------------------
        broke_out = bool(ctx.feature("breakout_20d", 0.0) > 0) or price > level
        if not broke_out:
            self.decline(ctx, "no_breakout", f"close {price:.2f} vs level {level:.2f}")
            return None

        extension_atr = (price - level) / atr if atr > 0 else 0.0
        if extension_atr > self.MAX_EXTENSION_ATR:
            # The move already happened. Chasing it inverts the reward:risk that
            # justified the trade in the first place.
            self.decline(ctx, "extended", f"{extension_atr:.2f} ATR beyond the level")
            return None

        if ctx.feature("close", price) < ctx.feature("sma_50", 0.0):
            self.decline(ctx, "below_sma50")
            return None
        if ctx.feature("adx_14", 0.0) < self.MIN_ADX:
            self.decline(ctx, "weak_trend", f"ADX {ctx.feature('adx_14'):.1f}")
            return None

        # --- volume confirmation -----------------------------------------
        rel_volume = ctx.feature("rel_volume_20", 0.0)
        if rel_volume < self.MIN_RELATIVE_VOLUME:
            self.decline(ctx, "no_volume_confirmation", f"relvol {rel_volume:.2f}")
            return None

        evidence = [
            f"Closed at {price:.2f}, above the {level:.2f} resistance level",
            f"Breakout volume {rel_volume:.1f}x the 20-day average",
            f"ADX {ctx.feature('adx_14'):.0f} confirms a trending environment",
            f"Price {ctx.feature('dist_from_sma50_pct', 0.0):+.1f}% versus its 50-day average",
        ]
        risks: list[str] = []
        setup_score = 55.0
        setup_score += min(15.0, (rel_volume - self.MIN_RELATIVE_VOLUME) * 10.0)
        setup_score += min(10.0, max(0.0, ctx.feature("rs_percentile", 50.0) - 50.0) * 0.2)

        # --- sentiment confirmation ---------------------------------------
        sentiment = ctx.sentiment
        if self.sentiment_available(ctx) and sentiment is not None:
            if sentiment.sentiment_acceleration < self.MIN_SENTIMENT_ACCELERATION:
                self.decline(
                    ctx,
                    "sentiment_not_accelerating",
                    f"accel {sentiment.sentiment_acceleration:.2f}",
                )
                return None
            if sentiment.mention_acceleration < self.MIN_MENTION_ACCELERATION:
                self.decline(
                    ctx, "attention_flat", f"mention accel {sentiment.mention_acceleration:.2f}"
                )
                return None
            # Unique authors, not raw mentions: fifty posts from five accounts is
            # a promotion, not a change in opinion.
            if sentiment.unique_authors < self.config.filters.min_unique_authors:
                self.decline(
                    ctx, "too_few_authors", f"{sentiment.unique_authors} unique authors"
                )
                return None
            if sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
                self.decline(
                    ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}"
                )
                return None
            evidence.append(
                f"Sentiment accelerating ({sentiment.sentiment_acceleration:+.2f}) across "
                f"{sentiment.unique_authors} unique authors"
            )
            evidence.append(
                f"Mention rate {sentiment.mention_acceleration:+.0%} versus its baseline"
            )
            setup_score += min(15.0, sentiment.sentiment_acceleration * 30.0)
            if sentiment.hype > 0.6:
                risks.append(
                    f"Elevated hype ({sentiment.hype:.2f}); crowded entries reverse quickly"
                )
                setup_score -= 8.0
        else:
            # Reduced-capability mode: the setup is still valid on price and
            # volume, but it is a weaker claim and must score as one.
            setup_score = min(setup_score, 62.0)
            evidence.append("Social sentiment unavailable: scored on price and volume only")
            risks.append("No sentiment confirmation available for this candidate")

        # --- levels --------------------------------------------------------
        # Prefer the breakout base's support; use an ATR stop when that support
        # sits so far below that the resulting risk would be unreasonable.
        support = ctx.feature("support_level", 0.0)
        atr_stop = price - self.ATR_STOP_MULTIPLE * atr
        structural_stop = min(support, level * 0.995) if support > 0 else atr_stop
        stop = max(structural_stop, atr_stop) if structural_stop > 0 else atr_stop
        stop = min(stop, price - 0.35 * atr)  # never place the stop inside the noise band

        entry_low = max(level, price - 0.25 * atr)
        entry_high = price + 0.35 * atr
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
            setup_score=max(0.0, min(100.0, setup_score)),
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
                f"{ctx.symbol} broke {level:.2f} resistance on {rel_volume:.1f}x volume with "
                "attention broadening across new participants."
            ),
            extras={"breakout_level": level, "extension_atr": extension_atr},
        )
