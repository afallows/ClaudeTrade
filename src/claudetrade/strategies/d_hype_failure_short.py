"""Strategy D -- Hype Failure Short.

Thesis: a vertical, promotion-driven advance that fails at its breakout tends to
unwind quickly, because the marginal buyer was attracted by the move itself and
has no thesis to hold through a decline.

This is the one strategy where a *high* manipulation-risk score is part of the
setup rather than a disqualifier -- the pattern being traded is the promotion
failing. That inversion is deliberate and is why the usual manipulation filter
is not applied here.

Shorting constraints that are modelled rather than assumed away:

* **Borrow.** Heavily-promoted small caps are frequently hard or impossible to
  borrow, and the backtest cannot know historical borrow availability. The
  strategy therefore requires meaningful market cap and dollar volume as a crude
  borrow proxy, and the limitation is recorded in the signal's risks.
* **Unbounded loss.** A short squeeze has no ceiling. The stop is placed above
  the failed high and the time stop is short.
* **Squeeze risk.** High short-squeeze chatter *increases* the danger of exactly
  this trade, so it reduces the score rather than confirming it.

Known weaknesses: timing. A promotion can extend far longer than seems possible,
and being early is indistinguishable from being wrong.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy


@register_strategy
class HypeFailureShortStrategy(Strategy):
    name = "hype_failure_short"
    version = "v1"
    description = "Short a failed breakout in a promotion-driven advance"
    direction_bias = Direction.SHORT
    min_history_bars = 80
    permits_earnings_risk = False
    requires_sentiment = True

    #: The advance must have been abnormally fast.
    MIN_ROC_20 = 25.0
    #: Sentiment must have spiked, not merely been positive.
    MIN_SENTIMENT_ACCELERATION = 0.45
    MIN_HYPE = 0.55
    #: The promotion signature: concentrated sources or duplicated text.
    MIN_MANIPULATION_RISK = 0.40
    #: Catalyst quality must be poor -- a real catalyst is a reason to be long.
    MAX_CATALYST_QUALITY = 0.40
    #: Squeeze chatter above this makes the short too dangerous to take.
    MAX_SHORT_SQUEEZE = 0.55
    #: Crude borrow-availability proxies.
    MIN_MARKET_CAP_USD = 300_000_000
    MIN_DOLLAR_VOLUME = 5_000_000
    ATR_STOP_MULTIPLE = 1.6

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if not self.config.signals.allow_shorts:
            self.decline(ctx, "shorts_disabled")
            return None
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None

        sentiment = ctx.sentiment
        if not self.sentiment_available(ctx) or sentiment is None:
            self.decline(ctx, "sentiment_required", "hype cannot be detected without social data")
            return None

        price = ctx.price
        atr = ctx.atr

        # --- borrow and liquidity realism ---------------------------------
        market_cap = ctx.security.market_cap_usd or 0.0
        if market_cap < self.MIN_MARKET_CAP_USD:
            self.decline(
                ctx, "borrow_unrealistic", f"market cap ${market_cap:,.0f} too small to borrow"
            )
            return None
        if ctx.feature("avg_dollar_volume_20", 0.0) < self.MIN_DOLLAR_VOLUME:
            self.decline(ctx, "illiquid_for_short")
            return None

        # --- the advance --------------------------------------------------
        roc_20 = ctx.feature("roc_20", 0.0)
        if roc_20 < self.MIN_ROC_20:
            self.decline(ctx, "no_vertical_advance", f"20-day ROC {roc_20:.1f}%")
            return None

        # --- the failure ----------------------------------------------------
        failed = ctx.feature("failed_breakout", 0.0) > 0
        recent_high = ctx.feature("donchian_high_20", 0.0)
        if recent_high <= 0:
            self.decline(ctx, "no_reference_high")
            return None
        # Either the pattern detector flagged a failed breakout, or price has
        # rolled back beneath the level it broke.
        rolled_over = price < recent_high * 0.97
        if not (failed or rolled_over):
            self.decline(ctx, "breakout_not_failed", f"price {price:.2f} vs high {recent_high:.2f}")
            return None

        # --- bearish price confirmation --------------------------------------
        if len(ctx.bars) < 2:
            return None
        last, prior = ctx.bars[-1], ctx.bars[-2]
        bearish = last.close < last.open and last.close < prior.low
        if not bearish:
            self.decline(ctx, "awaiting_bearish_confirmation")
            return None
        if price > ctx.feature("ema_9", 0.0) > 0:
            self.decline(ctx, "still_above_fast_ma")
            return None

        # --- the promotion signature -----------------------------------------
        if sentiment.sentiment_acceleration < self.MIN_SENTIMENT_ACCELERATION:
            self.decline(ctx, "no_sentiment_spike", f"{sentiment.sentiment_acceleration:.2f}")
            return None
        if sentiment.hype < self.MIN_HYPE:
            self.decline(ctx, "insufficient_hype", f"{sentiment.hype:.2f}")
            return None
        # Inverted on purpose: promotion is the setup here, not a filter.
        if sentiment.manipulation_risk < self.MIN_MANIPULATION_RISK:
            self.decline(
                ctx,
                "organic_move",
                f"manipulation risk {sentiment.manipulation_risk:.2f} -- move looks genuine",
            )
            return None
        if sentiment.catalyst_quality > self.MAX_CATALYST_QUALITY:
            self.decline(
                ctx, "real_catalyst", f"catalyst quality {sentiment.catalyst_quality:.2f}"
            )
            return None

        squeeze = sentiment.labels.get("short_squeeze", sentiment.labels.get("squeeze", 0.0))
        if squeeze > self.MAX_SHORT_SQUEEZE:
            self.decline(ctx, "squeeze_risk", f"short-squeeze chatter {squeeze:.2f}")
            return None

        evidence = [
            f"Advanced {roc_20:.0f}% over 20 sessions before failing",
            f"Failed at the {recent_high:.2f} high; now {100 * (price / recent_high - 1):+.1f}%",
            f"Bearish confirmation: close {last.close:.2f} beneath the prior low {prior.low:.2f}",
            f"Hype {sentiment.hype:.2f} with manipulation risk {sentiment.manipulation_risk:.2f} "
            f"(source concentration {sentiment.source_concentration:.2f}, "
            f"duplicate posts {sentiment.duplicate_ratio:.0%})",
            f"Catalyst quality only {sentiment.catalyst_quality:.2f}",
        ]
        risks = [
            "Short losses are unbounded; a squeeze can gap through the stop",
            "Historical borrow availability and cost are NOT modelled -- this trade may not "
            "have been executable in practice",
            "Promotions can extend well beyond what looks sustainable",
        ]

        setup_score = 50.0
        setup_score += min(12.0, (sentiment.manipulation_risk - self.MIN_MANIPULATION_RISK) * 30.0)
        setup_score += min(10.0, (roc_20 - self.MIN_ROC_20) * 0.2)
        setup_score += min(8.0, sentiment.duplicate_ratio * 16.0)
        setup_score -= squeeze * 15.0

        # --- levels -----------------------------------------------------------
        # Stop above the failed high: if it reclaims that level, the promotion
        # has not failed after all.
        stop = max(recent_high * 1.01, price + self.ATR_STOP_MULTIPLE * atr)
        entry_high = price + 0.2 * atr
        entry_low = price - 0.5 * atr
        reference = (entry_low + entry_high) / 2.0
        risk_per_share = stop - reference
        if risk_per_share <= 0:
            self.decline(ctx, "degenerate_risk")
            return None

        sma_20 = ctx.feature("sma_20", 0.0)
        first_target = reference - 1.5 * risk_per_share
        if 0 < sma_20 < reference:
            first_target = max(first_target, sma_20)
            if first_target >= reference - 0.9 * risk_per_share:
                first_target = reference - 1.4 * risk_per_share
        targets = [round(first_target, 2), round(reference - 2.8 * risk_per_share, 2)]
        targets = sorted(targets, reverse=True)

        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.SHORT,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            targets=targets,
            target_fractions=[0.5, 0.5],
            expected_holding_days=5,
            time_stop_days=8,
            trailing_stop_atr=1.5,
            setup_score=max(0.0, min(100.0, setup_score)),
            evidence=evidence,
            invalidation=[
                f"Close back above the {recent_high:.2f} failed high",
                "Renewed volume expansion to the upside",
                "A credible catalyst emerging that justifies the advance",
            ],
            exit_conditions=[
                f"Initial stop {stop:.2f} above the failed high",
                f"Cover half at {targets[0]:.2f}, remainder at {targets[1]:.2f}",
                "Trail at 1.5 ATR after the first target",
                "Time stop after 8 sessions -- unwinds are fast or they are not happening",
                "Cover immediately on short-squeeze conditions",
            ],
            risks=risks,
            thesis_hint=(
                f"{ctx.symbol} advanced {roc_20:.0f}% on promotional discussion, failed at "
                f"{recent_high:.2f}, and has confirmed bearishly."
            ),
            extras={"failed_high": recent_high, "roc_20": roc_20, "squeeze_signal": squeeze},
        )
