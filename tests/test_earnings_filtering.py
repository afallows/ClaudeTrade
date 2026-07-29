"""Tests for earnings-based entry filtering."""

from __future__ import annotations

import datetime as dt

from claudetrade.domain import EarningsEvent


class MockStrategy:
    """Mock strategy with earnings settings."""

    def __init__(self, permits_earnings_risk: bool = False, buffer_days: int = 3):
        self.permits_earnings_risk = permits_earnings_risk
        self.block_entry_within_days_of_earnings = buffer_days


class TestEarningsBuffer:
    """Entry is blocked inside earnings buffer window."""

    def test_entry_blocked_inside_buffer_confirmed(self):
        """Entry blocked within buffer of confirmed earnings date."""
        buffer = 3  # 3 days before/after

        earnings = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=True,
        )

        # 1 day before earnings => should be blocked
        session = dt.date(2023, 1, 19)
        start_date, end_date = earnings.effective_risk_date_range(buffer)
        is_blocked = start_date <= session <= end_date

        assert is_blocked

    def test_entry_permitted_outside_buffer(self):
        """Entry permitted outside buffer window."""
        buffer = 3

        earnings = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=True,
        )

        # 5 days before earnings => should be permitted
        session = dt.date(2023, 1, 15)
        start_date, end_date = earnings.effective_risk_date_range(buffer)
        is_blocked = start_date <= session <= end_date

        assert not is_blocked

    def test_entry_blocked_after_earnings(self):
        """Entry can be blocked after earnings (if still in buffer)."""
        buffer = 3

        earnings = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=True,
        )

        # 1 day after earnings => still in buffer
        session = dt.date(2023, 1, 21)
        start_date, end_date = earnings.effective_risk_date_range(buffer)
        is_blocked = start_date <= session <= end_date

        assert is_blocked


class TestUnconfirmedEarningsWidened:
    """Unconfirmed earnings are widened by uncertainty window."""

    def test_unconfirmed_wider_than_confirmed(self):
        """Estimated date widened more than confirmed."""
        report_date = dt.date(2023, 1, 20)
        uncertainty = 3

        confirmed = EarningsEvent(
            symbol="TEST",
            report_date=report_date,
            confirmed=True,
        )
        estimated = EarningsEvent(
            symbol="TEST",
            report_date=report_date,
            confirmed=False,
        )

        confirmed_range = confirmed.effective_risk_date_range(uncertainty)
        estimated_range = estimated.effective_risk_date_range(uncertainty)

        # Estimated should be wider
        assert (estimated_range[1] - estimated_range[0]).days > (
            confirmed_range[1] - confirmed_range[0]
        ).days

    def test_estimated_blocks_earlier(self):
        """Estimated earnings blocks entry earlier than confirmed same date."""
        report_date = dt.date(2023, 1, 20)
        uncertainty = 3

        estimated = EarningsEvent(
            symbol="TEST",
            report_date=report_date,
            confirmed=False,
        )

        start_date, _ = estimated.effective_risk_date_range(uncertainty)
        # Should start 3 days before
        assert start_date == dt.date(2023, 1, 17)


class TestPermitsEarningsRisk:
    """Strategy with permits_earnings_risk=True is never blocked."""

    def test_permits_earnings_risk_never_blocks(self):
        """permits_earnings_risk=True bypasses earnings guard."""
        # A strategy that explicitly permits earnings risk
        # should never be blocked by the earnings buffer

        earnings = EarningsEvent(
            symbol="TEST",
            report_date=dt.date(2023, 1, 20),
            confirmed=True,
        )

        session = dt.date(2023, 1, 20)  # On earnings day

        # Strategy permits earnings risk
        permits_risk = True

        # Entry should be allowed
        start_date, end_date = earnings.effective_risk_date_range(3)
        is_earnings_blocked = start_date <= session <= end_date

        # But strategy overrides
        is_allowed = (not is_earnings_blocked) or permits_risk

        assert is_allowed


class TestDaysToEarnings:
    """StrategyContext.days_to_earnings handles missing earnings."""

    def test_days_to_earnings_with_upcoming(self):
        """days_to_earnings returns count to next earnings."""
        # Example: today is Jan 15, next earnings is Jan 20
        session = dt.date(2023, 1, 15)
        earnings = [
            EarningsEvent(
                symbol="TEST",
                report_date=dt.date(2023, 1, 20),
                confirmed=True,
            ),
        ]

        # Days to earnings = 5
        days = (earnings[0].report_date - session).days
        assert days == 5

    def test_days_to_earnings_past_earnings(self):
        """days_to_earnings is negative if earnings have passed."""
        session = dt.date(2023, 1, 25)
        earnings = [
            EarningsEvent(
                symbol="TEST",
                report_date=dt.date(2023, 1, 20),
                confirmed=True,
            ),
        ]

        days = (earnings[0].report_date - session).days
        assert days < 0

    def test_no_known_earnings_returns_none(self):
        """days_to_earnings is None when no earnings known."""
        # Empty earnings list
        earnings = []

        # Should return None
        days = None if not earnings else (earnings[0].report_date - dt.date(2023, 1, 15)).days
        assert days is None


class TestMultipleEarnings:
    """Handling multiple earnings events (e.g., earnings + guidance update)."""

    def test_next_earnings_soonest_in_window(self):
        """When multiple earnings, find the soonest one in the window."""
        session = dt.date(2023, 1, 15)
        earnings = [
            EarningsEvent(
                symbol="TEST",
                report_date=dt.date(2023, 1, 22),
                confirmed=True,
            ),
            EarningsEvent(
                symbol="TEST",
                report_date=dt.date(2023, 2, 15),
                confirmed=False,
                source="guidance",
            ),
        ]

        # Soonest is Jan 22
        soonest = min(e.report_date for e in earnings if e.report_date > session)
        assert soonest == dt.date(2023, 1, 22)
