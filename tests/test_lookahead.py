"""Tests for look-ahead bias detection in strategy contexts."""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.config import AppConfig
from claudetrade.domain import (
    Bar,
    EarningsEvent,
    SecurityInfo,
    SymbolSentiment,
)
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

        with pytest.raises(LookaheadError, match="sentiment dated .* exceeds session"):
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
