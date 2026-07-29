"""Strategy E -- Post-Earnings Announcement Drift.

Thesis: prices tend to continue drifting in the direction of a large earnings
surprise for weeks after the report, because the market re-prices gradually.

The critical design point is *when* this strategy is allowed to act. It enters
**after** the report is public and after the immediate volatility has settled --
never before the announcement. Pre-announcement positioning is a coin flip
dressed as analysis, and it is also the easiest place for earnings-date leakage
to contaminate a backtest.

Two settling conditions must both hold:

1. At least ``MIN_DAYS_AFTER`` sessions have elapsed since the report.
2. The bar range has normalised relative to the earnings-day spike.

Direction comes from the surprise **and** must agree with what price and
sentiment have actually done since. A positive surprise that the market sold is
not drift; it is a rejection, and the strategy declines.

Known weaknesses: the drift effect has weakened over time as it became widely
known, and it is sensitive to how "surprise" is measured. Whether a vendor's
consensus was the true pre-announcement consensus is not verifiable here, so
surprise magnitude is treated as approximate.
"""

from __future__ import annotations

from claudetrade.domain import Direction
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import register_strategy


@register_strategy
class PostEarningsDriftStrategy(Strategy):
    name = "post_earnings_drift"
    version = "v1"
    description = "Drift continuation after a settled earnings surprise"
    direction_bias = Direction.LONG
    min_history_bars = 80
    #: The report is already public; the guard that matters is the *next* one.
    permits_earnings_risk = False
    requires_sentiment = False

    #: Settling window after the report.
    MIN_DAYS_AFTER = 2
    MAX_DAYS_AFTER = 12
    #: Surprise magnitude, in percent, needed to be worth trading.
    MIN_SURPRISE_PCT = 5.0
    #: Earnings-day move that qualifies as a genuine repricing.
    MIN_EVENT_MOVE_PCT = 3.0
    #: Volatility must have normalised to at most this multiple of average.
    MAX_SETTLED_RANGE_RATIO = 1.6
    #: Refuse to open a drift trade this close to the *next* report.
    MIN_DAYS_TO_NEXT_EARNINGS = 5
    ATR_STOP_MULTIPLE = 2.0

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if not self.has_sufficient_history(ctx):
            self.decline(ctx, "insufficient_history", f"{len(ctx.bars)} bars")
            return None

        event = ctx.last_earnings()
        if event is None:
            self.decline(ctx, "no_prior_earnings")
            return None

        days_after = ctx.days_since_earnings()
        if days_after is None:
            self.decline(ctx, "unknown_earnings_timing")
            return None
        if days_after < self.MIN_DAYS_AFTER:
            # Entering into the immediate post-report volatility is not drift
            # capture; it is paying the widest spreads of the quarter.
            self.decline(ctx, "not_settled", f"{days_after}d since the report")
            return None
        if days_after > self.MAX_DAYS_AFTER:
            self.decline(ctx, "drift_window_passed", f"{days_after}d since the report")
            return None

        # The *next* report must not land inside the expected holding period.
        days_to_next = ctx.days_to_earnings()
        if days_to_next is not None and days_to_next < self.MIN_DAYS_TO_NEXT_EARNINGS:
            self.decline(ctx, "next_earnings_too_close", f"{days_to_next}d")
            return None

        if event.surprise_pct is None:
            self.decline(ctx, "no_surprise_data")
            return None
        surprise = event.surprise_pct
        if abs(surprise) < self.MIN_SURPRISE_PCT:
            self.decline(ctx, "surprise_too_small", f"{surprise:+.1f}%")
            return None

        # --- the market's own reaction --------------------------------------
        event_move = self._event_day_move(ctx, event.report_date)
        if event_move is None:
            self.decline(ctx, "no_event_bar")
            return None
        if abs(event_move) < self.MIN_EVENT_MOVE_PCT:
            self.decline(ctx, "muted_reaction", f"{event_move:+.1f}% on the event bar")
            return None

        # Surprise and reaction must agree. A positive surprise that was sold
        # tells you the number was not the news.
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

        # --- volatility must have settled -------------------------------------
        settled_ratio = self._range_ratio(ctx)
        if settled_ratio > self.MAX_SETTLED_RANGE_RATIO:
            self.decline(ctx, "still_volatile", f"range {settled_ratio:.2f}x average")
            return None

        # --- price must be holding the gap, not filling it ---------------------
        price = ctx.price
        atr = ctx.atr
        ema_9 = ctx.feature("ema_9", 0.0)
        if direction is Direction.LONG and ema_9 > 0 and price < ema_9:
            self.decline(ctx, "gap_fading", "trading below the 9-day average")
            return None
        if direction is Direction.SHORT and ema_9 > 0 and price > ema_9:
            self.decline(ctx, "gap_fading", "trading above the 9-day average")
            return None

        evidence = [
            f"{'Positive' if surprise > 0 else 'Negative'} EPS surprise of {surprise:+.1f}% "
            f"reported {days_after} sessions ago"
            + (" (confirmed date)" if event.confirmed else " (estimated date)"),
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

        setup_score = 55.0 + min(15.0, (abs(surprise) - self.MIN_SURPRISE_PCT) * 0.5)
        setup_score += min(8.0, (abs(event_move) - self.MIN_EVENT_MOVE_PCT) * 0.8)

        sentiment = ctx.sentiment
        if self.sentiment_available(ctx) and sentiment is not None:
            aligned = (sentiment.raw_sentiment > 0) == (direction is Direction.LONG)
            if not aligned and abs(sentiment.raw_sentiment) > 0.3:
                self.decline(
                    ctx,
                    "sentiment_contradicts",
                    f"sentiment {sentiment.raw_sentiment:+.2f} against a "
                    f"{direction.value} drift",
                )
                return None
            if aligned:
                setup_score += 7.0
                evidence.append(
                    f"Discussion agrees with the drift direction "
                    f"({sentiment.raw_sentiment:+.2f})"
                )
        else:
            evidence.append("Social sentiment unavailable: scored on price and surprise only")

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
            setup_score=max(0.0, min(100.0, setup_score)),
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
                f"{ctx.symbol} reported a {surprise:+.1f}% surprise {days_after} sessions ago, "
                f"repriced {event_move:+.1f}%, and is holding the move as volatility settles."
            ),
            extras={
                "surprise_pct": surprise,
                "event_move_pct": event_move,
                "days_after_earnings": days_after,
                "earnings_confirmed": event.confirmed,
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
