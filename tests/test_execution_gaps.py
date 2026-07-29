"""Tests for order execution simulation against price gaps."""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.backtest.costs import CostModel
from claudetrade.backtest.execution import (
    EntryOrder,
    ExecutionSimulator,
)
from claudetrade.config import CostModelConfig
from claudetrade.domain import Bar, Direction, ExitReason


class MockPosition:
    """Mock position for testing exits."""

    def __init__(
        self,
        direction: Direction = Direction.LONG,
        shares: int = 100,
        stop: float = 95.0,
        targets: list[float] | None = None,
    ):
        self.direction = direction
        self.shares = shares
        self.initial_shares = shares
        self.stop_loss = stop
        self.initial_stop_loss = stop
        self.targets = targets or [110.0]
        self.target_fractions = [1.0]
        self.targets_hit = [False] * len(self.targets)
        self.trailing_stop_atr = None
        self.time_stop_session = dt.date(2099, 1, 1)
        self.moving_average_exit_period = None
        self.pre_earnings_exit_days = None
        self.next_earnings_date = None
        self.highest_close_since_entry = 100.0
        self.lowest_close_since_entry = 100.0


class TestStopGapsThrough:
    """A stop that gaps through its trigger fills at open, not at stop price."""

    def test_long_stop_gaps_below(self):
        """Long stop gapped below fills at open (pessimistic)."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())
        position = MockPosition(direction=Direction.LONG, shares=100, stop=95.0)

        # Entry at 100, stop at 95. Bar opens at 92 (below stop).
        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=92.0,
            high=94.0,
            low=91.0,
            close=93.0,
            volume=1_000_000,
        )

        decision = sim.simulate_exit(position, bar)
        assert decision is not None
        # Should fill near the open, not at stop price
        assert decision.price < 95.0  # Not at stop
        assert decision.price <= 92.5  # Near but not above open (with slippage)
        assert decision.reason == ExitReason.STOP_LOSS

    def test_short_stop_gaps_above(self):
        """Short stop gapped above fills at open (pessimistic)."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())
        position = MockPosition(direction=Direction.SHORT, shares=100, stop=105.0)

        # Entry at 100, stop at 105. Bar opens at 108 (above stop).
        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=108.0,
            high=109.0,
            low=106.0,
            close=107.0,
            volume=1_000_000,
        )

        decision = sim.simulate_exit(position, bar)
        assert decision is not None
        assert decision.price > 105.0  # Not at stop
        assert decision.price >= 107.5  # Near but not below open


class TestIntrabarAmbiguity:
    """When both stop and target are reachable, pessimistic policy (stop) wins."""

    def test_pessimistic_stop_wins_over_target(self):
        """Pessimistic policy: stop is hit before target."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig(), intrabar_policy="pessimistic")
        position = MockPosition(direction=Direction.LONG, shares=100, stop=95.0, targets=[110.0])

        # Bar's range contains both stop (95) and target (110).
        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=111.0,
            low=94.0,
            close=105.0,
            volume=1_000_000,
        )

        decision = sim.simulate_exit(position, bar)
        assert decision is not None
        # Pessimistic: stop is hit
        assert decision.reason == ExitReason.STOP_LOSS

    def test_optimistic_target_wins_over_stop(self):
        """Optimistic policy: target is hit before stop."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig(), intrabar_policy="optimistic")
        position = MockPosition(direction=Direction.LONG, shares=100, stop=95.0, targets=[110.0])

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=111.0,
            low=94.0,
            close=105.0,
            volume=1_000_000,
        )

        decision = sim.simulate_exit(position, bar)
        assert decision is not None
        # Optimistic: target is hit
        assert decision.reason == ExitReason.TARGET


class TestZeroVolumeBar:
    """Zero-volume bars are treated as halted (nothing fills)."""

    def test_zero_volume_no_fill_normal_exit(self):
        """No normal exit fills against zero-volume bar."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())
        position = MockPosition(direction=Direction.LONG, shares=100, stop=95.0)

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0.0,  # Halted
        )

        decision = sim.simulate_exit(position, bar)
        # No fill on halted bar
        assert decision is None

    def test_zero_volume_forced_close_still_fills(self):
        """Forced close (time stop, end of backtest) fills despite zero volume."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())
        position = MockPosition(direction=Direction.LONG, shares=100, stop=95.0)

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0.0,
        )

        decision = sim.simulate_exit(position, bar, force_close=True)
        # Forced close fills at bar close regardless of volume
        assert decision is not None
        assert decision.price == 100.0


class TestParticipationCap:
    """Partial fills cap at max participation rate."""

    def test_participation_cap_limits_fill(self):
        """Partial fill when order exceeds participation rate."""
        cost_config = CostModelConfig(max_participation_rate=0.05)
        cost_model = CostModel(cost_config)
        sim = ExecutionSimulator(cost_model, cost_config)

        # Try to enter 1000 shares when bar volume is 500k
        # Max participation: 0.05 * 500k = 25k dollars worth
        # At $100/share, that's 250 shares
        order = EntryOrder(
            symbol="TEST",
            direction=Direction.LONG,
            shares=1000,
            order_type="market_on_open",
        )

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=102.0,
            low=98.0,
            close=101.0,
            volume=500_000.0,
        )

        result = sim.try_fill_entry(order, bar)
        assert result is not None
        # Should be partially filled
        assert result.shares < 1000
        assert result.is_partial

    def test_participation_cap_exempt_for_forced_close(self):
        """Forced close fills entire position regardless of participation cap."""
        cost_config = CostModelConfig(max_participation_rate=0.01)
        cost_model = CostModel(cost_config)
        sim = ExecutionSimulator(cost_model, cost_config)
        position = MockPosition(direction=Direction.LONG, shares=1000)

        # Bar volume is only 100k, very low participation
        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=102.0,
            low=98.0,
            close=101.0,
            volume=100_000.0,
        )

        decision = sim.simulate_exit(position, bar, force_close=True)
        assert decision is not None
        # Should close full position (1000 shares)
        assert decision.shares == 1000


class TestEntryOrders:
    """Entry order mechanics and fill conditions."""

    def test_market_on_open_fills_at_open(self):
        """Market-on-open entry fills at bar open."""
        cost_model = CostModel(CostModelConfig(base_slippage_bps=0.0, half_spread_bps=0.0))
        sim = ExecutionSimulator(cost_model, CostModelConfig())

        order = EntryOrder(
            symbol="TEST",
            direction=Direction.LONG,
            shares=100,
            order_type="market_on_open",
        )

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=102.0,
            low=98.0,
            close=101.0,
            volume=1_000_000.0,
        )

        result = sim.try_fill_entry(order, bar)
        assert result is not None
        assert result.filled
        assert result.shares == 100
        assert result.price == pytest.approx(100.0, abs=0.1)

    def test_limit_order_no_fill_above_limit(self):
        """Limit order doesn't fill if bar never reaches limit."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())

        order = EntryOrder(
            symbol="TEST",
            direction=Direction.LONG,
            shares=100,
            order_type="limit",
            limit_price=95.0,  # Limit to buy at 95
        )

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=100.0,
            high=102.0,
            low=96.0,
            close=101.0,
            volume=1_000_000.0,
        )

        result = sim.try_fill_entry(order, bar)
        # Low of 96 never reaches limit of 95
        assert result is None

    def test_stop_entry_fills_on_breakout(self):
        """Stop-entry order fills when price breaks through stop."""
        cost_model = CostModel(CostModelConfig())
        sim = ExecutionSimulator(cost_model, CostModelConfig())

        order = EntryOrder(
            symbol="TEST",
            direction=Direction.LONG,
            shares=100,
            order_type="stop_entry",
            stop_price=105.0,  # Entry stop above entry
        )

        bar = Bar(
            symbol="TEST",
            session=dt.date(2023, 1, 4),
            open=104.0,
            high=107.0,
            low=103.0,
            close=106.0,
            volume=1_000_000.0,
        )

        result = sim.try_fill_entry(order, bar)
        assert result is not None
        assert result.filled
