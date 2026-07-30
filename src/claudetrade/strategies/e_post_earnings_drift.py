"""Strategy E -- Post-Earnings Announcement Drift.

Thesis: prices tend to continue drifting in the direction of a large earnings
surprise for weeks after the report, because the market re-prices gradually.

The critical design point is *when* this strategy is allowed to act. It enters
**after** the report is public and after the immediate volatility has settled --
never before the announcement. Pre-announcement positioning is a coin flip
dressed as analysis, and it is also the easiest place for earnings-date leakage
to contaminate a backtest.

Direction comes from the surprise **and** must agree with what price has
actually done since. A positive surprise that the market sold is not drift; it
is a rejection, and the strategy still declines outright on that -- see below.

Known weaknesses: the drift effect has weakened over time as it became widely
known, and it is sensitive to how "surprise" is measured. Whether a vendor's
consensus was the true pre-announcement consensus is not verifiable here, so
surprise magnitude is treated as approximate -- which is exactly why it is now
scored against the symbol's OWN trailing surprise history rather than a bare
5% constant (a 5% surprise is routine for one name and enormous for another).

Scoring model (ADR-0007 Decision 2)
------------------------------------
Previously an AND-chain of roughly eight absolute-threshold checks: a fixed
2-12 day settling window, a fixed 5% surprise floor, a fixed 3% event-move
floor, a fixed volatility-settled ratio, and a fixed proximity-to-the-9-day-
average gap-fading test. Now a
:class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`: the settling
window becomes a scored sweet-spot band, surprise magnitude is ranked against
the symbol's own trailing earnings-surprise history (from ``ctx.earnings``,
which is already point-in-time filtered by knowledge date -- no look-ahead is
introduced by reading it further back), and the volatility/gap checks become
continuous ramps.

Two conditions stay hard vetoes because they are not threshold judgements at
all: **the market's reaction contradicting the surprise's sign** (a positive
surprise the market sold is not drift, it is a rejection -- there is no
partial credit for a thesis that has been falsified) and the ordinary
earnings-window / insufficient-history / illiquidity / manipulation-risk set
shared with the other strategies. A next earnings report landing inside the
expected holding window is folded into the earnings-window veto, since it is
the same underlying risk (holding through an unmodelled event) as the entry
buffer.
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
class PostEarningsDriftStrategy(Strategy):
    name = "post_earnings_drift"
    version = "v2"
    description = "Drift continuation after a settled earnings surprise"
    direction_bias = Direction.LONG
    min_history_bars = 80
    #: The report is already public; the guard that matters is the *next* one.
    permits_earnings_risk = False
    requires_sentiment = False

    #: Settling window after the report -- sweet-spot band, not a hard cutoff.
    MIN_DAYS_AFTER = 2
    MAX_DAYS_AFTER = 12
    DAYS_AFTER_TAPER = 2
    #: Refuse to open a drift trade this close to the *next* report (folded
    #: into the earnings-window hard veto -- same risk class as the entry buffer).
    MIN_DAYS_TO_NEXT_EARNINGS = 5
    #: Volatility must have normalised to at most roughly this multiple of
    #: average before the settled-volatility score component saturates.
    SETTLED_RANGE_RATIO_TARGET = 1.6
    ATR_STOP_MULTIPLE = 2.0

    # --- score weights --------------------------------------------------------
    # See Strategy A's BASELINE comment.
    BASELINE = 24.0
    W_DAYS_AFTER = 14.0
    W_SURPRISE_PCTL = 18.0
    W_EVENT_MOVE = 16.0
    W_VOLATILITY_SETTLED = 12.0
    W_HOLDING_THE_MOVE = 10.0
    W_SENTIMENT_ALIGNED = 10.0
    PENALTY_SENTIMENT_AGAINST = -12.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        # --- hard vetoes ---------------------------------------------------
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None
        adv = ctx.feature("avg_dollar_volume_20", 0.0)
        if adv < self.config.filters.min_avg_dollar_volume_usd:
            self.decline(ctx, "illiquid", f"avg dollar volume ${adv:,.0f}")
            return None

        event = ctx.last_earnings()
        if event is None:
            self.decline(ctx, "no_prior_earnings")
            return None
        days_after = ctx.days_since_earnings()
        if days_after is None:
            self.decline(ctx, "unknown_earnings_timing")
            return None
        if event.surprise_pct is None:
            self.decline(ctx, "no_surprise_data")
            return None
        surprise = event.surprise_pct

        event_move = self._event_day_move(ctx, event.report_date)
        if event_move is None:
            self.decline(ctx, "no_event_bar")
            return None

        # Surprise and reaction must agree. A positive surprise that was sold
        # tells you the number was not the news -- this falsifies the thesis
        # outright, it is not a matter of degree.
        if (surprise > 0) != (event_move > 0):
            self.decline(
                ctx,
                "reaction_contradicts_surprise",
                f"surprise {surprise:+.1f}% but the market moved {event_move:+.1f}%",
            )
            return None

        direction = Direction.LONG if event_move > 0 else Direction.SHORT
        if direction is Direction.SHORT and not self.config.signals.allow_shorts:
            self.decline(ctx, "shorts_disabled")
            return None

        # The *next* report must not land inside the expected holding period --
        # the same earnings-risk class as the entry buffer, so it is a veto too.
        days_to_next = ctx.days_to_earnings()
        if days_to_next is not None and days_to_next < self.MIN_DAYS_TO_NEXT_EARNINGS:
            self.decline(ctx, "earnings_window", f"next report in {days_to_next}d")
            return None

        sentiment = ctx.sentiment
        if sentiment is not None and sentiment.manipulation_risk > self.config.filters.max_manipulation_risk:
            self.decline(ctx, "manipulation_risk", f"{sentiment.manipulation_risk:.2f}")
            return None

        # --- score accumulation ---------------------------------------------
        cal = self.config.calibration
        score = ScoreAccumulator(baseline=self.BASELINE)

        score.add(
            "settling_window",
            band_credit(float(days_after), self.MIN_DAYS_AFTER, self.MAX_DAYS_AFTER, self.DAYS_AFTER_TAPER),
            self.W_DAYS_AFTER,
        )

        past_surprises = [
            abs(e.surprise_pct)
            for e in ctx.earnings
            if e.surprise_pct is not None and e.report_date < event.report_date
        ]
        surprise_pctl = percentile_rank(past_surprises, abs(surprise))
        score.add(
            "surprise_magnitude_pctl",
            ramp_up(surprise_pctl, cal.drift_reaction_percentile - 0.30, cal.drift_reaction_percentile),
            self.W_SURPRISE_PCTL,
        )

        score.add("event_move_magnitude", ramp_up(abs(event_move), 1.5, 5.0), self.W_EVENT_MOVE)

        settled_ratio = self._range_ratio(ctx)
        score.add(
            "volatility_settled",
            ramp_down(settled_ratio, self.SETTLED_RANGE_RATIO_TARGET * 1.3, self.SETTLED_RANGE_RATIO_TARGET),
            self.W_VOLATILITY_SETTLED,
        )

        price = ctx.price
        atr = ctx.atr
        ema_9 = ctx.feature("ema_9", 0.0)
        if ema_9 > 0:
            signed_gap = (price - ema_9) if direction is Direction.LONG else (ema_9 - price)
            score.add("holding_the_move", ramp_up(signed_gap, -0.01 * price, 0.005 * price), self.W_HOLDING_THE_MOVE)
        else:
            score.add("holding_the_move", 0.5, self.W_HOLDING_THE_MOVE)

        evidence = [
            f"{'Positive' if surprise > 0 else 'Negative'} EPS surprise of {surprise:+.1f}% "
            f"({surprise_pctl:.0%} percentile of its own trailing surprises) reported {days_after} "
            "sessions ago" + (" (confirmed date)" if event.confirmed else " (estimated date)"),
            f"Market repriced {event_move:+.1f}% on the event bar",
            f"Volatility settled: current range {settled_ratio:.2f}x its average",
            f"Price holding the move at {price:.2f}, "
            f"{'above' if direction is Direction.LONG else 'below'} the 9-day average",
        ]
        risks = [
            "Post-earnings drift has weakened as the effect became widely known",
            "Surprise is measured against a vendor consensus that cannot be verified here",
        ]
        if not event.confirmed:
            risks.append("The earnings date was estimated rather than confirmed")

        if self.sentiment_available(ctx) and sentiment is not None:
            aligned = (sentiment.raw_sentiment > 0) == (direction is Direction.LONG)
            if aligned:
                score.add(
                    "sentiment_aligned", ramp_up(abs(sentiment.raw_sentiment), 0.05, 0.35), self.W_SENTIMENT_ALIGNED
                )
                evidence.append(
                    f"Discussion agrees with the drift direction ({sentiment.raw_sentiment:+.2f})"
                )
            elif abs(sentiment.raw_sentiment) > 0.15:
                score.penalty(
                    "sentiment_against",
                    self.PENALTY_SENTIMENT_AGAINST * ramp_up(abs(sentiment.raw_sentiment), 0.15, 0.5),
                )
                risks.append(
                    f"Discussion ({sentiment.raw_sentiment:+.2f}) is not aligned with the drift direction"
                )
        else:
            evidence.append("Social sentiment unavailable: scored on price and surprise only")

        threshold = cal.proposal_score_threshold
        if score.score < threshold:
            self.decline(ctx, "score_below_threshold", score.summary(threshold))
            return None

        # --- levels ------------------------------------------------------------
        if direction is Direction.LONG:
            stop = min(price - self.ATR_STOP_MULTIPLE * atr, self._event_bar_low(ctx, event.report_date))
            entry_low = price - 0.3 * atr
            entry_high = price + 0.4 * atr
            risk_per_share = ((entry_low + entry_high) / 2.0) - stop
            if risk_per_share <= 0:
                self.decline(ctx, "degenerate_risk")
                return None
            targets = [
                round(entry_high + 1.6 * risk_per_share, 2),
                round(entry_high + 2.8 * risk_per_share, 2),
            ]
        else:
            stop = max(price + self.ATR_STOP_MULTIPLE * atr, self._event_bar_high(ctx, event.report_date))
            entry_high = price + 0.3 * atr
            entry_low = price - 0.4 * atr
            reference = (entry_low + entry_high) / 2.0
            risk_per_share = stop - reference
            if risk_per_share <= 0:
                self.decline(ctx, "degenerate_risk")
                return None
            targets = [
                round(reference - 1.6 * risk_per_share, 2),
                round(reference - 2.8 * risk_per_share, 2),
            ]

        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=direction,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            targets=targets,
            target_fractions=[0.5, 0.5],
            expected_holding_days=15,
            time_stop_days=20,
            trailing_stop_atr=2.5,
            setup_score=score.score,
            evidence=evidence,
            invalidation=[
                "Full retracement of the earnings-day move",
                f"Close through the {stop:.2f} stop",
                "A subsequent guidance revision reversing the surprise",
            ],
            exit_conditions=[
                f"Initial stop {stop:.2f} beyond the event bar",
                f"Scale out at {targets[0]:.2f} and {targets[1]:.2f}",
                "Trail at 2.5 ATR after the first target",
                "Time stop after 20 sessions",
                "Exit before the next confirmed earnings report",
            ],
            risks=risks,
            thesis_hint=(
                f"{ctx.symbol} reported a {surprise:+.1f}% surprise ({surprise_pctl:.0%} percentile) "
                f"{days_after} sessions ago, repriced {event_move:+.1f}%, and is holding the move as "
                "volatility settles."
            ),
            extras={
                "surprise_pct": surprise,
                "event_move_pct": event_move,
                "days_after_earnings": days_after,
                "earnings_confirmed": event.confirmed,
                "score_breakdown": score.breakdown,
            },
        )

    # --- helpers -----------------------------------------------------------

    def _event_day_move(self, ctx: StrategyContext, report_date) -> float | None:
        """Percentage move on the first session that priced the report.

        For an after-close report the reaction lands on the following session,
        so the first bar strictly after the report date is used.
        """
        for i, bar in enumerate(ctx.bars):
            if bar.session >= report_date and i > 0:
                prior_close = ctx.bars[i - 1].close
                if prior_close <= 0:
                    return None
                return 100.0 * (bar.close - prior_close) / prior_close
        return None

    def _event_bar_low(self, ctx: StrategyContext, report_date) -> float:
        for bar in ctx.bars:
            if bar.session >= report_date:
                return bar.low
        return ctx.price * 0.9

    def _event_bar_high(self, ctx: StrategyContext, report_date) -> float:
        for bar in ctx.bars:
            if bar.session >= report_date:
                return bar.high
        return ctx.price * 1.1

    def _range_ratio(self, ctx: StrategyContext) -> float:
        """Latest bar range relative to the 14-day ATR -- the settling test."""
        atr = ctx.feature("atr_14", 0.0)
        if atr <= 0:
            return 99.0
        return ctx.last_bar.range / atr
