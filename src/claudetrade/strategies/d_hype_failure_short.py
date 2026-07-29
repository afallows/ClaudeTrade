"""Strategy D -- Hype Failure Short.

Thesis: a vertical, promotion-driven advance that fails at its breakout tends to
unwind quickly, because the marginal buyer was attracted by the move itself and
has no thesis to hold through a decline.

This is the one strategy where a *high* manipulation-risk score is part of the
setup rather than a disqualifier -- the pattern being traded is the promotion
failing. That inversion is deliberate and is why the usual manipulation filter
is not applied here; instead, a manipulation-risk reading that is too LOW is
the disqualifying fact (``organic_move`` stays a hard veto: without a
promotion signature there is no pattern to trade, just as strategy C cannot
function without sentiment at all).

Shorting constraints that are modelled rather than assumed away:

* **Borrow.** Heavily-promoted small caps are frequently hard or impossible to
  borrow, and the backtest cannot know historical borrow availability. The
  strategy therefore requires meaningful market cap and dollar volume as a crude
  borrow proxy -- this is the strategy's "liquidity" hard veto -- and the
  limitation is recorded in the signal's risks.
* **Unbounded loss.** A short squeeze has no ceiling. The stop is placed above
  the failed high and the time stop is short.
* **Squeeze risk.** High short-squeeze chatter *increases* the danger of exactly
  this trade, so it now reduces the score continuously rather than acting as a
  binary veto -- matching what this file always said it did before the veto
  contradicted the docstring.

Known weaknesses: timing. A promotion can extend far longer than seems possible,
and being early is indistinguishable from being wrong.

Scoring model (ADR-0007 Decision 2)
------------------------------------
Previously an AND-chain of roughly ten absolute-threshold checks. Now a
:class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`: the advance's
abnormal speed and the sentiment spike are scored against the symbol's own
trailing percentile distribution (``roc_20_pctl_120`` and an on-the-fly
percentile of sentiment acceleration) rather than fixed 25%/0.45 constants,
and the failed-breakout / bearish-confirmation / still-above-fast-MA
conditions become continuous instead of boolean. Only shorts-enabled,
insufficient history, the earnings window, sentiment availability, the
borrow/liquidity proxy, and the organic-move manipulation floor remain hard
vetoes.
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
class HypeFailureShortStrategy(Strategy):
    name = "hype_failure_short"
    version = "v2"
    description = "Short a failed breakout in a promotion-driven advance"
    direction_bias = Direction.SHORT
    min_history_bars = 80
    permits_earnings_risk = False
    requires_sentiment = True

    #: The promotion signature: concentrated sources or duplicated text. Below
    #: this the move looks organic and there is no pattern to short -- hard veto.
    MIN_MANIPULATION_RISK = 0.40
    #: Crude borrow-availability proxies -- the strategy's liquidity veto.
    MIN_MARKET_CAP_USD = 300_000_000
    MIN_DOLLAR_VOLUME = 5_000_000
    ATR_STOP_MULTIPLE = 1.6

    # --- score weights --------------------------------------------------------
    BASELINE = 10.0
    W_ADVANCE_PCTL = 16.0
    W_FAILURE_EVIDENCE = 16.0
    W_BEARISH_CONFIRMATION = 12.0
    W_BELOW_FAST_MA = 8.0
    W_SENTIMENT_SPIKE_PCTL = 14.0
    W_HYPE = 10.0
    W_MANIPULATION_MAGNITUDE = 8.0
    W_DUPLICATE_RATIO = 6.0
    PENALTY_SQUEEZE = -16.0
    PENALTY_REAL_CATALYST = -10.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        # --- hard vetoes ---------------------------------------------------
        if not self.config.signals.allow_shorts:
            self.decline(ctx, "shorts_disabled")
            return None
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        if self.earnings_blocked(ctx):
            self.decline(ctx, "earnings_window", f"{ctx.days_to_earnings()}d to earnings")
            return None
        if len(ctx.bars) < 2:
            self.decline(ctx, "insufficient_bars_for_confirmation")
            return None

        sentiment = ctx.sentiment
        if not self.sentiment_available(ctx) or sentiment is None:
            self.decline(ctx, "sentiment_required", "hype cannot be detected without social data")
            return None

        market_cap = ctx.security.market_cap_usd or 0.0
        if market_cap < self.MIN_MARKET_CAP_USD:
            self.decline(
                ctx, "borrow_unrealistic", f"market cap ${market_cap:,.0f} too small to borrow"
            )
            return None
        avg_dollar_volume = ctx.feature("avg_dollar_volume_20", 0.0)
        if avg_dollar_volume < self.MIN_DOLLAR_VOLUME:
            self.decline(ctx, "illiquid_for_short", f"${avg_dollar_volume:,.0f}")
            return None
        if sentiment.manipulation_risk < self.MIN_MANIPULATION_RISK:
            self.decline(
                ctx,
                "organic_move",
                f"manipulation risk {sentiment.manipulation_risk:.2f} -- move looks genuine",
            )
            return None

        recent_high = ctx.feature("donchian_high_20", 0.0)
        if recent_high <= 0:
            self.decline(ctx, "no_reference_high")
            return None

        price = ctx.price
        atr = ctx.atr

        # --- score accumulation ---------------------------------------------
        cal = self.config.calibration
        score = ScoreAccumulator(baseline=self.BASELINE)

        roc_20 = ctx.feature("roc_20", 0.0)
        roc_pctl = ctx.feature("roc_20_pctl_120", 0.5)
        score.add(
            "advance_speed_pctl",
            ramp_up(roc_pctl, cal.hype_advance_percentile - 0.25, cal.hype_advance_percentile),
            self.W_ADVANCE_PCTL,
        )

        failed = ctx.feature("failed_breakout", 0.0) > 0
        rollover_ratio = price / recent_high if recent_high > 0 else 1.0
        failure_fraction = max(1.0 if failed else 0.0, ramp_down(rollover_ratio, 1.005, 0.97))
        score.add("failed_breakout_evidence", failure_fraction, self.W_FAILURE_EVIDENCE)

        last, prior = ctx.bars[-1], ctx.bars[-2]
        bearish_fraction = 0.5 * ramp_down(
            last.close - last.open, 0.005 * price, -0.008 * price
        ) + 0.5 * ramp_down(last.close - prior.low, 0.01 * price, -0.005 * price)
        score.add("bearish_confirmation", bearish_fraction, self.W_BEARISH_CONFIRMATION)

        ema_9 = ctx.feature("ema_9", 0.0)
        if ema_9 > 0:
            score.add("below_fast_ma", ramp_down(price - ema_9, 0.01 * price, 0.0), self.W_BELOW_FAST_MA)
        else:
            score.add("below_fast_ma", 0.5, self.W_BELOW_FAST_MA)

        accel_history = [s.sentiment_acceleration for s in ctx.sentiment_history][
            -cal.sentiment_percentile_window :
        ]
        accel_pctl = percentile_rank(accel_history, sentiment.sentiment_acceleration)
        score.add(
            "sentiment_spike_pctl",
            ramp_up(accel_pctl, cal.hype_sentiment_spike_percentile - 0.25, cal.hype_sentiment_spike_percentile),
            self.W_SENTIMENT_SPIKE_PCTL,
        )
        score.add("hype", ramp_up(sentiment.hype, 0.35, 0.65), self.W_HYPE)
        score.add(
            "manipulation_magnitude",
            ramp_up(sentiment.manipulation_risk, self.MIN_MANIPULATION_RISK, self.MIN_MANIPULATION_RISK + 0.30),
            self.W_MANIPULATION_MAGNITUDE,
        )
        score.add("duplicate_content", ramp_up(sentiment.duplicate_ratio, 0.10, 0.40), self.W_DUPLICATE_RATIO)

        squeeze = sentiment.labels.get("short_squeeze", sentiment.labels.get("squeeze", 0.0))
        if squeeze > 0.35:
            score.penalty("squeeze_risk", self.PENALTY_SQUEEZE * ramp_up(squeeze, 0.35, 0.70))
        if sentiment.catalyst_quality > 0.30:
            score.penalty(
                "real_catalyst", self.PENALTY_REAL_CATALYST * ramp_up(sentiment.catalyst_quality, 0.30, 0.60)
            )

        evidence = [
            f"Advanced {roc_20:.0f}% over 20 sessions ({roc_pctl:.0%} percentile of its own history)",
            f"Failed at the {recent_high:.2f} high; now {100 * (rollover_ratio - 1):+.1f}%",
            f"Hype {sentiment.hype:.2f} with manipulation risk {sentiment.manipulation_risk:.2f} "
            f"(source concentration {sentiment.source_concentration:.2f}, "
            f"duplicate posts {sentiment.duplicate_ratio:.0%})",
            f"Sentiment acceleration {accel_pctl:.0%} percentile of its own trailing readings",
            f"Catalyst quality {sentiment.catalyst_quality:.2f}",
        ]
        risks = [
            "Short losses are unbounded; a squeeze can gap through the stop",
            "Historical borrow availability and cost are NOT modelled -- this trade may not "
            "have been executable in practice",
            "Promotions can extend well beyond what looks sustainable",
        ]
        if squeeze > 0.35:
            risks.append(f"Short-squeeze chatter elevated ({squeeze:.2f})")

        threshold = cal.proposal_score_threshold
        if score.score < threshold:
            self.decline(ctx, "score_below_threshold", score.summary(threshold))
            return None

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
            setup_score=score.score,
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
                f"{ctx.symbol} advanced {roc_20:.0f}% ({roc_pctl:.0%} percentile) on promotional "
                f"discussion, failed at {recent_high:.2f}, and has confirmed bearishly."
            ),
            extras={
                "failed_high": recent_high,
                "roc_20": roc_20,
                "squeeze_signal": squeeze,
                "score_breakdown": score.breakdown,
            },
        )
