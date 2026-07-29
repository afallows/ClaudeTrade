"""Strategy B -- Sentiment Pullback.

Thesis: in an established uptrend, a controlled pullback on *falling* volume is
supply exhausting rather than demand breaking. Cooling-but-still-positive
sentiment is the tell that the crowd has stopped adding, not that it has turned.

Deliberate design choices:

* Entry requires **price confirmation** -- an up close off the pullback low.
  Buying a falling price because a moving average is nearby is how a pullback
  strategy becomes a falling-knife strategy.
* Sentiment must be positive but *cooling*. Sentiment that is still
  accelerating means the pullback has not finished shaking anyone out; sentiment
  that has turned negative means the trend itself is in question.

Known weaknesses: performs badly in choppy, trendless markets, where every
"pullback" is just the next leg of a range. The ADX and structure gates reduce
that but do not eliminate it.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy


@register_strategy
class SentimentPullbackStrategy(Strategy):
    name = "sentiment_pullback"
    version = "v1"
    description = "Pullback to support in an uptrend, entered on price confirmation"
    direction_bias = Direction.LONG
    min_history_bars = 100
    permits_earnings_risk = False
    requires_sentiment = False

    MIN_ADX = 20.0
    #: Pullback must be at least this deep to be worth trading...
    MIN_PULLBACK_PCT = 3.0
    #: ...and no deeper than this, beyond which it is a trend break.
    MAX_PULLBACK_PCT = 15.0
    #: Down-volume during the pullback relative to the 20-day average.
    MAX_PULLBACK_REL_VOLUME = 1.1
    #: RSI band: oversold within an uptrend, not outright broken.
    RSI_LOW = 35.0
    RSI_HIGH = 55.0
    ATR_STOP_MULTIPLE = 1.8

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None

        price = ctx.price
        atr = ctx.atr
        sma_20 = ctx.feature("sma_20", 0.0)
        sma_50 = ctx.feature("sma_50", 0.0)
        sma_200 = ctx.feature("sma_200", 0.0)

        # --- trend must already exist -------------------------------------
        if not (sma_20 > 0 and sma_50 > 0):
            self.decline(ctx, "missing_moving_averages")
            return None
        if sma_20 < sma_50:
            self.decline(ctx, "no_uptrend", "20-day below 50-day")
            return None
        if sma_200 > 0 and price < sma_200:
            self.decline(ctx, "below_200dma")
            return None
        if ctx.feature("adx_14", 0.0) < self.MIN_ADX:
            self.decline(ctx, "trendless", f"ADX {ctx.feature('adx_14'):.1f}")
            return None
        if ctx.feature("hh_hl_score", 0.0) < 0:
            self.decline(ctx, "structure_broken", "no higher-high/higher-low structure")
            return None

        # --- pullback depth and character ---------------------------------
        recent_high = ctx.feature("donchian_high_20", 0.0)
        if recent_high <= 0:
            self.decline(ctx, "no_reference_high")
            return None
        pullback_pct = 100.0 * (recent_high - price) / recent_high
        if pullback_pct < self.MIN_PULLBACK_PCT:
            self.decline(ctx, "shallow_pullback", f"{pullback_pct:.1f}%")
            return None
        if pullback_pct > self.MAX_PULLBACK_PCT:
            self.decline(ctx, "pullback_too_deep", f"{pullback_pct:.1f}% -- trend may be broken")
            return None

        rel_volume = ctx.feature("rel_volume_20", 1.0)
        if rel_volume > self.MAX_PULLBACK_REL_VOLUME:
            # Heavy volume into a decline is distribution, not a rest.
            self.decline(ctx, "heavy_down_volume", f"relvol {rel_volume:.2f}")
            return None

        rsi = ctx.feature("rsi_14", 50.0)
        if not self.RSI_LOW <= rsi <= self.RSI_HIGH:
            self.decline(ctx, "rsi_outside_band", f"RSI {rsi:.1f}")
            return None

        # --- proximity to a moving average --------------------------------
        dist_20 = abs(ctx.feature("dist_from_sma20_pct", 99.0))
        dist_50 = abs(ctx.feature("dist_from_sma50_pct", 99.0))
        near_ma = min(dist_20, dist_50)
        if near_ma > 4.0:
            self.decline(ctx, "not_near_support", f"{near_ma:.1f}% from the nearest average")
            return None

        # --- price confirmation: this is the entry trigger ------------------
        if len(ctx.bars) < 2:
            return None
        last, prior = ctx.bars[-1], ctx.bars[-2]
        confirmed = last.close > last.open and last.close > prior.close
        if not confirmed:
            self.decline(ctx, "awaiting_confirmation", "no up close off the pullback low")
            return None

        evidence = [
            f"Uptrend intact: 20-day above 50-day, price {price:.2f}",
            f"Pullback of {pullback_pct:.1f}% from the {recent_high:.2f} swing high",
            f"Down-volume subdued at {rel_volume:.2f}x average",
            f"RSI {rsi:.0f} -- reset within the trend rather than broken",
            f"Confirmation bar: up close at {last.close:.2f} above the prior {prior.close:.2f}",
        ]
        risks: list[str] = []
        setup_score = 58.0 + min(12.0, (self.MAX_PULLBACK_REL_VOLUME - rel_volume) * 20.0)
        setup_score += min(10.0, max(0.0, 4.0 - near_ma) * 2.0)

        sentiment = ctx.sentiment
        if self.sentiment_available(ctx) and sentiment is not None:
            if sentiment.raw_sentiment <= 0:
                self.decline(ctx, "sentiment_negative", f"{sentiment.raw_sentiment:.2f}")
                return None
            # Cooling is the point: still-accelerating sentiment means the
            # shakeout has not run its course.
            if sentiment.sentiment_acceleration > 0.30:
                self.decline(
                    ctx, "sentiment_still_hot", f"accel {sentiment.sentiment_acceleration:.2f}"
                )
                return None
            if sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
                self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
                return None
            evidence.append(
                f"Sentiment positive ({sentiment.raw_sentiment:+.2f}) but cooling "
                f"({sentiment.sentiment_acceleration:+.2f}) -- the crowd has stopped adding"
            )
            setup_score += 8.0
            if sentiment.fear > 0.55:
                risks.append(f"Fear reading elevated ({sentiment.fear:.2f})")
        else:
            setup_score = min(setup_score, 60.0)
            evidence.append("Social sentiment unavailable: scored on trend and volume only")

        # --- levels ---------------------------------------------------------
        support = ctx.feature("support_level", 0.0)
        swing_low = ctx.feature("swing_low_recent", 0.0)
        atr_stop = price - self.ATR_STOP_MULTIPLE * atr
        candidates = [v for v in (support, swing_low) if v > 0 and v < price]
        structural_stop = max(candidates) * 0.995 if candidates else atr_stop
        stop = min(structural_stop, price - 0.5 * atr)

        entry_low = price - 0.3 * atr
        entry_high = price + 0.5 * atr
        risk_per_share = ((entry_low + entry_high) / 2.0) - stop
        if risk_per_share <= 0:
            self.decline(ctx, "degenerate_risk")
            return None

        targets = [
            round(recent_high, 2),
            round(entry_high + 2.5 * risk_per_share, 2),
        ]
        # The prior high must actually be far enough away to be worth taking.
        if targets[0] <= entry_high + 0.8 * risk_per_share:
            targets[0] = round(entry_high + 1.4 * risk_per_share, 2)
        targets = sorted(targets)

        risks.append("A pullback that keeps going is indistinguishable from a trend break at entry")

        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.LONG,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            targets=targets,
            target_fractions=[0.5, 0.5],
            expected_holding_days=8,
            time_stop_days=12,
            trailing_stop_atr=2.0,
            setup_score=max(0.0, min(100.0, setup_score)),
            evidence=evidence,
            invalidation=[
                f"Close below {stop:.2f} structural support",
                "20-day average crossing back below the 50-day",
                "Expanding volume on further downside",
            ],
            exit_conditions=[
                f"Initial stop {stop:.2f} below structure",
                f"Scale out at {targets[0]:.2f} and {targets[1]:.2f}",
                "Trail at 2.0 ATR after the first target",
                "Time stop after 12 sessions",
                "Exit before a confirmed earnings report",
            ],
            risks=risks,
            thesis_hint=(
                f"{ctx.symbol} pulled back {pullback_pct:.1f}% to its rising averages on light "
                "volume and has printed an up close."
            ),
            extras={"pullback_pct": pullback_pct, "swing_high": recent_high},
        )
