"""Strategy B -- Sentiment Pullback.

Thesis: in an established uptrend, a controlled pullback on *falling* volume is
supply exhausting rather than demand breaking. Cooling-but-still-positive
sentiment is the tell that the crowd has stopped adding, not that it has turned.

Deliberate design choices:

* Entry wants **price confirmation** -- an up close off the pullback low.
  Buying a falling price because a moving average is nearby is how a pullback
  strategy becomes a falling-knife strategy, so confirmation carries real
  weight in the score even though it is no longer an outright veto.
* Sentiment should be positive but *cooling*. Sentiment that is still
  accelerating means the pullback has not finished shaking anyone out; sentiment
  that has turned decisively negative means the trend itself is in question.

Known weaknesses: performs badly in choppy, trendless markets, where every
"pullback" is just the next leg of a range. The ADX and structure components
reduce that but do not eliminate it.

Scoring model (ADR-0007 Decision 2)
------------------------------------
Previously an AND-chain of nine absolute-threshold checks (uptrend by MA
order, price above the 200-day, ADX above a fixed 20, structure intact,
pullback depth inside a fixed 3-15% band, down-volume below a fixed 1.1x,
RSI inside a fixed 35-55 band, proximity to a moving average, an up-close
confirmation bar, then -- if sentiment was available -- four more absolute
checks). Now a :class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`:
each of those becomes a weighted, partial-credit component, several scored
against the symbol's own trailing distribution (ADX and RSI percentile, and
down-volume's relative-volume percentile, rather than fixed multiples) via
``config.calibration``.

Only earnings window, insufficient history, illiquidity and manipulation risk
remain hard vetoes. A pullback with no discoverable reference high, or too
little bar history to read a confirmation bar, is also declined outright --
not a threshold judgement, a missing structural input.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy
from claudetrade.strategies.scoring_utils import (
    ScoreAccumulator,
    band_credit,
    percentile_rank,
    ramp_down,
    ramp_up,
)


@register_strategy
class SentimentPullbackStrategy(Strategy):
    name = "sentiment_pullback"
    version = "v2"
    description = "Pullback to support in an uptrend, entered on price confirmation"
    direction_bias = Direction.LONG
    min_history_bars = 100
    permits_earnings_risk = False
    requires_sentiment = False

    ATR_STOP_MULTIPLE = 1.8
    #: Sweet-spot pullback depth band, in percent off the recent swing high,
    #: and how far outside it partial credit still extends.
    PULLBACK_LOW_PCT = 3.0
    PULLBACK_HIGH_PCT = 15.0
    PULLBACK_TAPER_PCT = 4.0

    # --- score weights ------------------------------------------------------
    # See Strategy A's BASELINE comment: calibrated against the engine's
    # existing blended min_overall_score gate, not tuned to any one outcome.
    BASELINE = 20.0
    W_UPTREND = 14.0
    W_ABOVE_200 = 6.0
    W_ADX_PERCENTILE = 14.0
    W_STRUCTURE = 8.0
    W_PULLBACK_DEPTH = 14.0
    W_QUIET_VOLUME = 12.0
    W_RSI_BAND = 10.0
    W_NEAR_MA = 8.0
    W_CONFIRMATION = 10.0
    W_SENTIMENT_POSITIVE = 8.0
    W_SENTIMENT_COOLING = 8.0

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
        if len(ctx.bars) < 2:
            self.decline(ctx, "insufficient_bars_for_confirmation")
            return None

        sentiment = ctx.sentiment
        if sentiment is not None and sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
            self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
            return None

        price = ctx.price
        atr = ctx.atr
        sma_20 = ctx.feature("sma_20", 0.0)
        sma_50 = ctx.feature("sma_50", 0.0)
        sma_200 = ctx.feature("sma_200", 0.0)
        if not (sma_20 > 0 and sma_50 > 0):
            self.decline(ctx, "missing_moving_averages")
            return None

        recent_high = ctx.feature("donchian_high_20", 0.0)
        if recent_high <= 0:
            self.decline(ctx, "no_reference_high")
            return None

        # --- score accumulation ---------------------------------------------
        cal = self.config.calibration
        score = ScoreAccumulator(baseline=self.BASELINE)

        score.add("uptrend_ma_order", ramp_up(sma_20 - sma_50, -0.01 * sma_50, 0.0), self.W_UPTREND)
        if sma_200 > 0:
            score.add(
                "above_200dma", ramp_up(price - sma_200, -0.03 * sma_200, 0.0), self.W_ABOVE_200
            )
        else:
            score.add("above_200dma", 0.5, self.W_ABOVE_200)  # unknown: neutral half credit

        adx_pctl = ctx.feature("adx_pctl_120", 0.5)
        score.add(
            "trend_strength_pctl",
            ramp_up(adx_pctl, cal.pullback_trend_percentile - 0.25, cal.pullback_trend_percentile),
            self.W_ADX_PERCENTILE,
        )
        score.add(
            "structure_intact", ramp_up(ctx.feature("hh_hl_score", 0.0), -0.3, 0.1), self.W_STRUCTURE
        )

        pullback_pct = 100.0 * (recent_high - price) / recent_high
        score.add(
            "pullback_depth",
            band_credit(pullback_pct, self.PULLBACK_LOW_PCT, self.PULLBACK_HIGH_PCT, self.PULLBACK_TAPER_PCT),
            self.W_PULLBACK_DEPTH,
        )

        rel_volume = ctx.feature("rel_volume_20", 1.0)
        vol_pctl = ctx.feature("rel_volume_pctl_120", 0.5)
        score.add(
            "quiet_down_volume_pctl",
            ramp_down(vol_pctl, cal.pullback_quiet_volume_percentile + 0.30, cal.pullback_quiet_volume_percentile),
            self.W_QUIET_VOLUME,
        )

        rsi = ctx.feature("rsi_14", 50.0)
        rsi_pctl = ctx.feature("rsi_pctl_120", 0.5)
        score.add(
            "rsi_band_pctl",
            band_credit(rsi_pctl, cal.pullback_rsi_low_percentile, cal.pullback_rsi_high_percentile, 0.15),
            self.W_RSI_BAND,
        )

        dist_20 = abs(ctx.feature("dist_from_sma20_pct", 8.0))
        dist_50 = abs(ctx.feature("dist_from_sma50_pct", 8.0))
        near_ma = min(dist_20, dist_50)
        score.add("near_support_ma", ramp_down(near_ma, 7.0, 3.0), self.W_NEAR_MA)

        last, prior = ctx.bars[-1], ctx.bars[-2]
        confirmed = last.close > last.open and last.close > prior.close
        confirmation_fraction = 0.5 * ramp_up(
            last.close - last.open, -0.005 * price, 0.008 * price
        ) + 0.5 * ramp_up(last.close - prior.close, -0.006 * price, 0.006 * price)
        score.add("price_confirmation", confirmation_fraction, self.W_CONFIRMATION)

        evidence = [
            f"Uptrend {'intact' if sma_20 >= sma_50 else 'weakening'}: 20-day {'above' if sma_20 >= sma_50 else 'below'} 50-day, price {price:.2f}",
            f"Pullback of {pullback_pct:.1f}% from the {recent_high:.2f} swing high",
            f"Down-volume at {rel_volume:.2f}x average ({vol_pctl:.0%} percentile of its own history)",
            f"RSI {rsi:.0f} ({rsi_pctl:.0%} percentile of its own trailing history)",
        ]
        risks: list[str] = []
        if confirmed:
            evidence.append(f"Confirmation bar: up close at {last.close:.2f} above the prior {prior.close:.2f}")
        else:
            risks.append("No clean up-close confirmation bar yet; entry timing is weaker evidence")

        # --- sentiment (soft: absent costs points, not the trade) -----------
        if self.sentiment_available(ctx) and sentiment is not None:
            accel_history = [s.sentiment_acceleration for s in ctx.sentiment_history][
                -cal.sentiment_percentile_window :
            ]
            accel_pctl = percentile_rank(accel_history, sentiment.sentiment_acceleration)
            score.add(
                "sentiment_positive", ramp_up(sentiment.raw_sentiment, -0.05, 0.15), self.W_SENTIMENT_POSITIVE
            )
            # Cooling is the point: a LOW acceleration percentile means the
            # crowd has stopped adding; a high one means the shakeout has not
            # run its course yet.
            score.add("sentiment_cooling_pctl", ramp_down(accel_pctl, 0.85, 0.55), self.W_SENTIMENT_COOLING)
            evidence.append(
                f"Sentiment {sentiment.raw_sentiment:+.2f}, acceleration {accel_pctl:.0%} percentile "
                "of its own trailing readings"
            )
            if sentiment.fear > 0.55:
                risks.append(f"Fear reading elevated ({sentiment.fear:.2f})")
        else:
            evidence.append("Social sentiment unavailable: scored on trend and volume only")

        threshold = cal.proposal_score_threshold
        if score.score < threshold:
            self.decline(ctx, "score_below_threshold", score.summary(threshold))
            return None

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
            setup_score=score.score,
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
                "volume" + (" and has printed an up close." if confirmed else ", awaiting a clean confirmation bar.")
            ),
            extras={
                "pullback_pct": pullback_pct,
                "swing_high": recent_high,
                "score_breakdown": score.breakdown,
            },
        )
