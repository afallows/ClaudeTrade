"""Tests for look-ahead bias detection in strategy contexts."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from claudetrade.config import AppConfig
from claudetrade.domain import (
    Bar,
    EarningsEvent,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.features.feature_builder import FeatureBuilder
from claudetrade.strategies.base import LookaheadError, StrategyContext


class TestContextNoLookahead:
    """StrategyContext.assert_no_lookahead catches future data."""

    def test_bar_after_session_is_truncated(self, tmp_app_config: AppConfig):
        """A bar dated after the session is clipped, so the strategy cannot see it.

        Over-supplying history is a legitimate calling pattern, so this is
        truncated rather than rejected -- but the future bar must be gone.
        """
        session = dt.date(2023, 1, 3)
        bars = [
            Bar("TEST", dt.date(2023, 1, 3), 100.0, 102.0, 99.0, 101.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 4), 101.0, 103.0, 100.0, 102.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 5), 102.0, 104.0, 101.0, 103.0, 1_000_000),
        ]

        ctx = StrategyContext(
            session=session,
            symbol="TEST",
            bars=bars,
            features={},
            security=SecurityInfo("TEST"),
            regime=None,
            sentiment=None,
            sentiment_history=[],
            earnings=[],
            config=tmp_app_config,
        )
        assert [b.session for b in ctx.bars] == [session]
        assert ctx.last_bar.session == session
        ctx.assert_no_lookahead()

    def test_sentiment_after_session_raises(self, tmp_app_config: AppConfig):
        """Sentiment dated after session close raises LookaheadError."""
        session = dt.date(2023, 1, 3)
        bars = [
            Bar("TEST", dt.date(2023, 1, 3), 100.0, 102.0, 99.0, 101.0, 1_000_000),
        ]
        sentiment = SymbolSentiment(
            symbol="TEST",
            session=dt.date(2023, 1, 4),  # Created tomorrow
            post_count=5,
        )

        with pytest.raises(LookaheadError, match=r"sentiment dated .* exceeds session"):
            StrategyContext(
                session=session,
                symbol="TEST",
                bars=bars,
                features={},
                security=SecurityInfo("TEST"),
                regime=None,
                sentiment=sentiment,
                sentiment_history=[],
                earnings=[],
                config=tmp_app_config,
            )

    def test_earnings_as_of_after_session_raises(self, tmp_app_config: AppConfig):
        """Earnings row with as_of after session raises (leakage)."""
        session = dt.date(2023, 1, 3)
        bars = [
            Bar("TEST", dt.date(2023, 1, 3), 100.0, 102.0, 99.0, 101.0, 1_000_000),
        ]
        earnings = [
            EarningsEvent(
                symbol="TEST",
                report_date=dt.date(2023, 1, 5),
                as_of=dt.datetime(2023, 1, 4, 10, 0, 0, tzinfo=dt.UTC),  # Known after session
            ),
        ]

        with pytest.raises(LookaheadError, match="was only known from"):
            StrategyContext(
                session=session,
                symbol="TEST",
                bars=bars,
                features={},
                security=SecurityInfo("TEST"),
                regime=None,
                sentiment=None,
                sentiment_history=[],
                earnings=earnings,
                config=tmp_app_config,
            )


class TestContextTruncatesOverSupplied:
    """StrategyContext truncates over-supplied future bars."""

    def test_excess_bars_truncated(self, tmp_app_config: AppConfig):
        """Bars after session are truncated, no error."""
        session = dt.date(2023, 1, 3)
        bars_input = [
            Bar("TEST", dt.date(2023, 1, 1), 98.0, 100.0, 97.0, 99.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 2), 99.0, 101.0, 98.0, 100.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 3), 100.0, 102.0, 99.0, 101.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 4), 101.0, 103.0, 100.0, 102.0, 1_000_000),  # Future
            Bar("TEST", dt.date(2023, 1, 5), 102.0, 104.0, 101.0, 103.0, 1_000_000),  # Future
        ]

        # Should not raise; excess bars are truncated
        context = StrategyContext(
            session=session,
            symbol="TEST",
            bars=bars_input,
            features={},
            security=SecurityInfo("TEST"),
            regime=None,
            sentiment=None,
            sentiment_history=[],
            earnings=[],
            config=tmp_app_config,
        )

        # Only bars through session should be visible
        assert len(context.bars) == 3
        assert context.bars[-1].session == session


class TestContextNeverExposesNextSession:
    """Context built for session T never exposes T+1 data."""

    def test_sentiment_history_through_session(self, tmp_app_config: AppConfig):
        """Sentiment history includes only data through the session."""
        session = dt.date(2023, 1, 3)
        bars = [
            Bar("TEST", dt.date(2023, 1, 1), 98.0, 100.0, 97.0, 99.0, 1_000_000),
            Bar("TEST", dt.date(2023, 1, 3), 100.0, 102.0, 99.0, 101.0, 1_000_000),
        ]
        sentiment_history = [
            SymbolSentiment(symbol="TEST", session=dt.date(2023, 1, 1)),
            SymbolSentiment(symbol="TEST", session=dt.date(2023, 1, 3)),
            SymbolSentiment(symbol="TEST", session=dt.date(2023, 1, 4)),  # Future
        ]

        context = StrategyContext(
            session=session,
            symbol="TEST",
            bars=bars,
            features={},
            security=SecurityInfo("TEST"),
            regime=None,
            sentiment=None,
            sentiment_history=sentiment_history,
            earnings=[],
            config=tmp_app_config,
        )

        # Only data through session should be present
        filtered = [s for s in context.sentiment_history if s.session <= session]
        assert len(filtered) == 2
        assert all(s.session <= session for s in context.sentiment_history)

    def test_attention_history_through_session(self, tmp_app_config: AppConfig):
        """The per-source attention series is clipped like every other series.

        Attention history exists so an aggregator's reading can be ranked
        against its own past. A future-dated entry in that reference
        distribution would leak tomorrow's crowd into today's percentile --
        subtler than a future bar, and just as disqualifying.
        """
        session = dt.date(2023, 1, 3)
        series = [
            SymbolSentiment(symbol="TEST", session=dt.date(2023, 1, 1), source="apewisdom:4chan"),
            SymbolSentiment(symbol="TEST", session=session, source="apewisdom:4chan"),
            SymbolSentiment(symbol="TEST", session=dt.date(2023, 1, 4), source="apewisdom:4chan"),
        ]

        context = StrategyContext(
            session=session,
            symbol="TEST",
            bars=[Bar("TEST", session, 100.0, 102.0, 99.0, 101.0, 1_000_000)],
            features={},
            security=SecurityInfo("TEST"),
            regime=None,
            attention_history={"apewisdom:4chan": series},
            config=tmp_app_config,
        )

        kept = context.attention_history["apewisdom:4chan"]
        assert [s.session for s in kept] == [dt.date(2023, 1, 1), session]

    def test_future_dated_per_source_snapshot_raises(self, tmp_app_config: AppConfig):
        """A single per-source snapshot is a reading FOR this session, so --
        like ``sentiment`` and unlike the histories -- it cannot be safely
        clipped and a future-dated one is a bug upstream."""
        session = dt.date(2023, 1, 3)
        with pytest.raises(LookaheadError):
            StrategyContext(
                session=session,
                symbol="TEST",
                bars=[Bar("TEST", session, 100.0, 102.0, 99.0, 101.0, 1_000_000)],
                features={},
                security=SecurityInfo("TEST"),
                regime=None,
                sentiment_by_source={
                    "reddit": SymbolSentiment(
                        symbol="TEST", session=dt.date(2023, 1, 4), source="reddit"
                    )
                },
                config=tmp_app_config,
            )

    def test_future_dated_attention_snapshot_raises(self, tmp_app_config: AppConfig):
        session = dt.date(2023, 1, 3)
        with pytest.raises(LookaheadError):
            StrategyContext(
                session=session,
                symbol="TEST",
                bars=[Bar("TEST", session, 100.0, 102.0, 99.0, 101.0, 1_000_000)],
                features={},
                security=SecurityInfo("TEST"),
                regime=None,
                attention_by_source={
                    "apewisdom:4chan": SymbolSentiment(
                        symbol="TEST", session=dt.date(2023, 1, 4), source="apewisdom:4chan"
                    )
                },
                config=tmp_app_config,
            )


class TestEarningsWithConfirmation:
    """Unconfirmed (estimated) earnings dates are handled properly."""

    def test_unconfirmed_earnings_widened_by_uncertainty(self):
        """Estimated earnings are widened by estimated_date_uncertainty_days."""
        # This would be tested in an integration test with the full context builder
        # The test here documents the expected behavior
        confirmed = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=True,
        )
        estimated = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=False,
        )

        # Confirmed date is a point
        confirmed_range = confirmed.effective_risk_date_range(uncertainty_days=3)
        assert confirmed_range == (dt.date(2023, 1, 20), dt.date(2023, 1, 20))

        # Estimated date is widened
        estimated_range = estimated.effective_risk_date_range(uncertainty_days=3)
        assert estimated_range[0] == dt.date(2023, 1, 17)  # 3 days before
        assert estimated_range[1] == dt.date(2023, 1, 23)  # 3 days after


class TestNewMarketSignalFeaturesNoLookahead:
    """Market-signal adoption package items 1-4: the new engineered feature
    columns (gap_filled, gap_continuation_up/down, pivot points, Fibonacci
    levels, round_number_level, level_confluence_count, volume_divergence)
    must be reproducible from a point-in-time truncated bar history -- the
    same end-to-end guarantee ``TestContextNoLookahead`` proves for raw bars/
    sentiment/earnings, extended through ``FeatureBuilder`` for the newly
    wired columns.

    Mirrors ``features.indicators.assert_causal``'s technique (recompute on a
    truncated prefix, compare the truncated run's last row against the full
    run's row at the same session) but exercised through the actual
    ``FeatureBuilder`` pipeline these strategies read from, not a bare
    pattern function in isolation.
    """

    NEW_FEATURE_COLUMNS = (
        "gap_pct",
        "gap_filled",
        "gap_continuation_up",
        "gap_continuation_down",
        "pivot",
        "pivot_r1",
        "pivot_s1",
        "pivot_r2",
        "pivot_s2",
        "fib_23_6",
        "fib_38_2",
        "fib_50_0",
        "fib_61_8",
        "fib_78_6",
        "round_number_level",
        "level_confluence_count",
        "volume_divergence",
    )

    @staticmethod
    def _synthetic_bars(n: int, *, seed: int = 11) -> list[Bar]:
        rng = np.random.default_rng(seed)
        bars: list[Bar] = []
        day = dt.date(2023, 1, 3)
        price = 100.0
        for _ in range(n):
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            open_ = price * (1 + rng.normal(0, 0.006))
            close = open_ * (1 + rng.normal(0, 0.018))
            high = max(open_, close) * (1 + abs(rng.normal(0, 0.006)))
            low = min(open_, close) * (1 - abs(rng.normal(0, 0.006)))
            volume = 1_000_000 * (1 + abs(rng.normal(0, 0.5)))
            bars.append(Bar("TEST", day, open_, high, low, close, volume, close))
            price = close
            day += dt.timedelta(days=1)
        return bars

    def test_truncated_recomputation_matches_full_series_at_the_session(self):
        """A feature frame built from bars ending at session T must equal,
        column-for-column at T, the same feature built from a much longer
        bar history that happens to also include T -- i.e. nothing about a
        later bar (T+1, T+2, ...) can have influenced the value stamped at T.
        """
        full_bars = self._synthetic_bars(160)
        cutoff_index = 110
        session = full_bars[cutoff_index].session
        truncated_bars = full_bars[: cutoff_index + 1]
        assert truncated_bars[-1].session == session

        builder = FeatureBuilder(symbol="TEST")
        full_df = builder.build(bars=full_bars)
        truncated_df = builder.build(bars=truncated_bars)

        full_row = full_df.loc[session]
        truncated_row = truncated_df.iloc[-1]

        for column in self.NEW_FEATURE_COLUMNS:
            full_value = float(full_row[column])
            truncated_value = float(truncated_row[column])
            full_nan = full_value != full_value
            truncated_nan = truncated_value != truncated_value
            if full_nan and truncated_nan:
                continue
            assert full_nan == truncated_nan, (
                f"{column}: the two runs disagree about whether session {session} "
                f"is knowable yet (full={full_value!r}, truncated={truncated_value!r})"
            )
            assert np.isclose(full_value, truncated_value, atol=1e-6, rtol=1e-6), (
                f"{column}: look-ahead bias at session {session} -- full-series value "
                f"{full_value!r} != truncated-series value {truncated_value!r}"
            )

    def test_gap_continuation_columns_never_true_before_their_own_confirmation_bar(self):
        """A cross-check specific to items 1/2: the deferred-confirmation
        columns must never be True earlier than the session on which they are
        knowable. Built from a StrategyContext at an early session, both
        gap_continuation columns must be exactly 0.0 (unmarked) -- there has
        not been enough history yet for any breakout, let alone a confirmed
        continuation of one.
        """
        bars = self._synthetic_bars(40)
        # detect_breakout's own level (prior_high, lookback=20) is not even
        # defined until row 20 -- before that, no breakout is possible by
        # construction, so no continuation can be confirmed either, regardless
        # of what the random walk happens to do.
        early_session = bars[15].session
        builder = FeatureBuilder(symbol="TEST")
        features = builder.build_point_in_time(bars=[b for b in bars if b.session <= early_session])

        assert features.get("gap_continuation_up", 0.0) == 0.0
        assert features.get("gap_continuation_down", 0.0) == 0.0
