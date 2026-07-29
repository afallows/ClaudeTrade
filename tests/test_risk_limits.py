"""Tests for portfolio-level risk limits."""

from __future__ import annotations

from claudetrade.config import AppConfig
from claudetrade.domain import Direction
from claudetrade.risk.limits import OpenPosition, PortfolioState, check_new_position


class TestHeatLimit:
    """Portfolio heat (open risk %) limits new positions."""

    def test_heat_breach_blocks_position(self, tmp_app_config: AppConfig):
        """Position is blocked when it would exceed portfolio heat."""
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            positions=[
                OpenPosition(
                    symbol="OLD",
                    direction=Direction.LONG,
                    shares=100,
                    entry_price=100.0,
                    stop_price=90.0,  # $1000 risk
                ),
            ],
        )
        # Current heat: 1%, max is 6%
        # Trying to add $1500 risk (1.5% of equity) => total 2.5%, OK
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=15_000.0,
            dollar_risk=1_500.0,
        )
        assert result.allowed

        # But try to add $5k risk (5% of equity) => total 6%, should hit limit
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=50_000.0,
            dollar_risk=5_000.0,
        )
        assert not result.allowed
        assert any("heat" in b.lower() for b in result.breaches)


class TestPositionSizeLimit:
    """max_position_size_pct limits position notional."""

    def test_position_size_breach(self, tmp_app_config: AppConfig):
        """Position exceeding max_position_size_pct is blocked."""
        tmp_app_config.risk.max_position_size_pct = 10.0
        state = PortfolioState(equity=100_000.0, cash=100_000.0)

        # Try to open a $20k position (20% of equity)
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="BIG",
            direction=Direction.LONG,
            notional=20_000.0,
            dollar_risk=1_000.0,
        )
        assert not result.allowed
        assert any("position" in b.lower() for b in result.breaches)


class TestSectorExposureLimit:
    """max_sector_exposure_pct limits sector concentration."""

    def test_sector_breach(self, tmp_app_config: AppConfig):
        """Adding to a sector that would exceed exposure limit is blocked."""
        tmp_app_config.risk.max_sector_exposure_pct = 20.0
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            positions=[
                OpenPosition(
                    symbol="TECH1",
                    direction=Direction.LONG,
                    shares=150,
                    entry_price=100.0,
                    stop_price=95.0,
                    sector="Technology",
                ),
            ],
        )
        # Current Technology exposure: $15k (15%)
        # Try to add another $10k Tech => 25%, exceeds 20% limit
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="TECH2",
            direction=Direction.LONG,
            notional=10_000.0,
            dollar_risk=500.0,
            sector="Technology",
        )
        assert not result.allowed
        assert any("Technology" in b for b in result.breaches)


class TestCorrelatedExposureLimit:
    """max_correlated_exposure_pct limits similar/correlated names."""

    def test_correlated_group_breach(self, tmp_app_config: AppConfig):
        """Correlated group limit is enforced."""
        tmp_app_config.risk.max_correlated_exposure_pct = 25.0
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            positions=[
                OpenPosition(
                    symbol="SEMI1",
                    direction=Direction.LONG,
                    shares=100,
                    entry_price=100.0,
                    stop_price=95.0,
                    correlation_group="semiconductors",
                ),
            ],
        )
        # Current semi exposure: $10k (10%)
        # Try to add $20k => 30%, exceeds 25% limit
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="SEMI2",
            direction=Direction.LONG,
            notional=20_000.0,
            dollar_risk=1_000.0,
            correlation_group="semiconductors",
        )
        assert not result.allowed


class TestConcurrentPositionsLimit:
    """max_concurrent_positions limits total open positions."""

    def test_concurrent_positions_limit(self, tmp_app_config: AppConfig):
        """Cannot open more than max_concurrent_positions."""
        tmp_app_config.risk.max_concurrent_positions = 3
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            positions=[
                OpenPosition("P1", Direction.LONG, 10, 100.0, 95.0),
                OpenPosition("P2", Direction.LONG, 10, 100.0, 95.0),
                OpenPosition("P3", Direction.LONG, 10, 100.0, 95.0),
            ],
        )
        # Already at 3 positions, cannot open 4th
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="P4",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("limit reached" in b.lower() for b in result.breaches)


class TestDailyLossLimit:
    """max_daily_loss_pct halts trading after daily drawdown."""

    def test_daily_loss_breach(self, tmp_app_config: AppConfig):
        """Position blocked when daily loss exceeds max_daily_loss_pct."""
        tmp_app_config.risk.max_daily_loss_pct = 2.0
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            realised_pnl_today=-2_500.0,  # -2.5% loss
        )
        # Already exceeded 2% daily loss limit
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("daily loss limit" in b.lower() for b in result.breaches)

    def test_daily_loss_warning_approaching(self, tmp_app_config: AppConfig):
        """Warning issued when approaching daily loss limit."""
        tmp_app_config.risk.max_daily_loss_pct = 3.0
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            realised_pnl_today=-2_000.0,  # -2%, approaching 3% limit
        )
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert result.allowed  # Not yet at limit
        assert any("approaching" in w.lower() for w in result.warnings)


class TestWeeklyLossLimit:
    """max_weekly_loss_pct halts trading after weekly drawdown."""

    def test_weekly_loss_breach(self, tmp_app_config: AppConfig):
        """Position blocked when weekly loss exceeds max_weekly_loss_pct."""
        tmp_app_config.risk.max_weekly_loss_pct = 5.0
        state = PortfolioState(
            equity=100_000.0,
            cash=50_000.0,
            realised_pnl_week=-6_000.0,  # -6% loss
        )
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed


class TestKillSwitch:
    """Kill switch blocks all new positions."""

    def test_kill_switch_blocks_all(self, tmp_app_config: AppConfig):
        """kill_switch_engaged blocks every new position."""
        state = PortfolioState(equity=100_000.0, cash=100_000.0, kill_switch_engaged=True)
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("kill switch" in b.lower() for b in result.breaches)

    def test_kill_switch_config_blocks(self, tmp_app_config: AppConfig):
        """Config kill_switch also blocks positions."""
        tmp_app_config.risk.kill_switch_engaged = True
        state = PortfolioState(equity=100_000.0, cash=100_000.0)
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed


class TestShortLimitAndPyramiding:
    """Short selling and pyramiding restrictions."""

    def test_shorts_disabled_blocks_short(self, tmp_app_config: AppConfig):
        """Short position blocked when allow_shorts=False."""
        tmp_app_config.signals.allow_shorts = False
        state = PortfolioState(equity=100_000.0, cash=100_000.0)
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.SHORT,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("short" in b.lower() for b in result.breaches)

    def test_pyramiding_blocked(self, tmp_app_config: AppConfig):
        """Cannot add to existing position (no pyramiding)."""
        state = PortfolioState(
            equity=100_000.0,
            cash=100_000.0,
            positions=[OpenPosition("ABC", Direction.LONG, 100, 100.0, 95.0)],
        )
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="ABC",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("already holding" in b.lower() for b in result.breaches)


class TestInsufficientCash:
    """Insufficient available cash blocks long positions."""

    def test_long_blocks_without_cash(self, tmp_app_config: AppConfig):
        """Long position blocks when cash is insufficient."""
        state = PortfolioState(equity=100_000.0, cash=2_000.0)
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=5_000.0,
            dollar_risk=500.0,
        )
        assert not result.allowed
        assert any("cash" in b.lower() for b in result.breaches)


class TestAllBreachesBroadcast:
    """LimitCheck.allowed is False and ALL breaches are listed."""

    def test_all_breaches_reported(self, tmp_app_config: AppConfig):
        """Multiple simultaneous breaches are all reported."""
        tmp_app_config.risk.max_position_size_pct = 5.0
        tmp_app_config.risk.max_portfolio_heat_pct = 2.0
        state = PortfolioState(
            equity=100_000.0,
            cash=10_000.0,
            positions=[
                OpenPosition("OLD", Direction.LONG, 100, 100.0, 90.0),
            ],
        )
        # Breach: heat (already at 1%), position size ($15k > 5%), and short disabled
        result = check_new_position(
            config=tmp_app_config,
            state=state,
            symbol="NEW",
            direction=Direction.LONG,
            notional=15_000.0,
            dollar_risk=2_000.0,
        )
        # Should report multiple breaches
        assert not result.allowed
        assert len(result.breaches) > 1
        assert "heat" in str(result.breaches).lower() or "position" in str(result.breaches).lower()
