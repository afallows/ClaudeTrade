"""Tests for risk-based position sizing."""

from __future__ import annotations

import pytest

from claudetrade.config import AppConfig
from claudetrade.domain import Direction
from claudetrade.risk.sizing import size_position


def _unconstrained(config: AppConfig) -> AppConfig:
    """Set risk limits so the base sizing rule alone decides the size.

    The shipped defaults (0.75% risk, 15% position cap) make the position-value
    cap bind at exactly the same share count as the risk budget, which would
    leave these tests unable to tell the two rules apart. Raising both limits
    isolates the base rule: $100k x 1% = $1,000 risk budget, and a 25% position
    cap ($25k) that is comfortably clear of the resulting $20k notional.
    """
    config.risk.account_size_usd = 100_000.0
    config.risk.max_risk_per_trade_pct = 1.0
    config.risk.max_position_size_pct = 25.0
    return config


class TestBaseSizing:
    """Base sizing rule: shares = risk_budget / risk_per_share."""

    def test_unconstrained_long(self, tmp_app_config: AppConfig):
        """Shares = floor(risk_budget / risk_per_share) for unconstrained long."""
        # Account: $100k, risk 1% per trade = $1000
        # Entry 100, stop 95 => risk_per_share = 5
        # Expected shares = floor(1000 / 5) = 200
        result = size_position(
            config=_unconstrained(tmp_app_config),
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
        )
        assert result.shares == 200
        assert result.notional_usd == 20_000.0
        assert result.dollar_risk == 1_000.0
        assert not result.rejected

    def test_unconstrained_short(self, tmp_app_config: AppConfig):
        """Shares = floor(risk_budget / risk_per_share) for unconstrained short."""
        result = size_position(
            config=_unconstrained(tmp_app_config),
            direction=Direction.SHORT,
            entry_price=100.0,
            stop_price=105.0,  # Stop above entry for short
        )
        assert result.shares == 200
        assert result.notional_usd == 20_000.0
        assert result.dollar_risk == 1_000.0
        assert not result.rejected


class TestRejections:
    """Invalid sizing requests are rejected."""

    def test_flat_direction_rejected(self, tmp_app_config: AppConfig):
        """FLAT direction is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.FLAT,
            entry_price=100.0,
            stop_price=95.0,
        )
        assert result.rejected
        assert result.shares == 0

    def test_entry_le_zero_rejected(self, tmp_app_config: AppConfig):
        """Entry price <= 0 is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=0.0,
            stop_price=95.0,
        )
        assert result.rejected

    def test_zero_risk_rejected(self, tmp_app_config: AppConfig):
        """Stop equals entry (zero risk) is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=100.0,
        )
        assert result.rejected

    def test_long_stop_not_below_entry_rejected(self, tmp_app_config: AppConfig):
        """Long stop not below entry is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=105.0,  # Stop above entry
        )
        assert result.rejected
        assert "stop is not below entry" in result.rejection_reason

    def test_short_stop_not_above_entry_rejected(self, tmp_app_config: AppConfig):
        """Short stop not above entry is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.SHORT,
            entry_price=100.0,
            stop_price=95.0,  # Stop below entry
        )
        assert result.rejected
        assert "stop is not above entry" in result.rejection_reason

    def test_equity_le_zero_rejected(self, tmp_app_config: AppConfig):
        """Equity <= 0 is rejected."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            account_equity=0.0,
        )
        assert result.rejected


class TestConstraintBindings:
    """Individual caps bind when they should and are noted."""

    def test_position_size_cap_binds(self, tmp_app_config: AppConfig):
        """max_position_size_pct reduces shares and sets binding constraint."""
        tmp_app_config.risk.max_position_size_pct = 5.0
        # Without cap: floor(1000 / 5) = 200 shares = $20k notional
        # With 5% cap: max = $5k notional = 50 shares
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
        )
        assert result.shares == 50
        assert result.binding_constraint == "max_position_size"
        assert "position value capped" in result.reason()

    def test_liquidity_cap_binds(self, tmp_app_config: AppConfig):
        """Liquidity cap reduces shares when ADV is low."""
        # 20-day ADV = $100k, 2% ADV = $2k notional = 20 shares at $100
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            avg_dollar_volume=100_000.0,
        )
        assert result.shares == 20
        assert result.binding_constraint == "liquidity"

    def test_buying_power_cap_binds(self, tmp_app_config: AppConfig):
        """Buying power cap on long when cash is insufficient."""
        # Only $5k cash available, can only buy 50 shares at $100
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            available_cash=5_000.0,
        )
        assert result.shares == 50
        assert result.binding_constraint == "buying_power"
        assert "cash" in result.reason()

    def test_portfolio_heat_cap_binds(self, tmp_app_config: AppConfig):
        """Portfolio heat cap reduces when risk budget is already committed."""
        # 9% of equity already at risk (of 6% max) means only 0% remaining
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            open_heat_pct=6.5,
        )
        assert result.shares == 0
        assert result.binding_constraint == "portfolio_heat"
        assert "exhausted" in result.reason()

    def test_liquidity_cap_skipped_when_unknown(self, tmp_app_config: AppConfig):
        """Liquidity cap is skipped and noted when ADV is unknown."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            avg_dollar_volume=None,
        )
        # Should not be liquidity-limited
        assert (
            "liquidity cap skipped" in result.reason() or result.binding_constraint != "liquidity"
        )


class TestRiskMultiplier:
    """Risk multiplier scales down but never scales up."""

    def test_multiplier_scales_down(self, tmp_app_config: AppConfig):
        """risk_multiplier < 1.0 reduces position size."""
        result = size_position(
            config=_unconstrained(tmp_app_config),
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            risk_multiplier=0.5,
        )
        # Base sizing: 200 shares
        # With 0.5 multiplier: 100 shares
        assert result.shares == 100
        assert result.dollar_risk == 500.0

    def test_multiplier_above_one_is_clamped(self, tmp_app_config: AppConfig):
        """risk_multiplier > 1.0 is clamped to 1.0."""
        result = size_position(
            config=_unconstrained(tmp_app_config),
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            risk_multiplier=2.0,
        )
        # Should be clamped to 1.0 multiplier, not doubled
        assert result.shares == 200
        assert "clamped" in result.reason()


class TestRiskCalculations:
    """Risk metrics are calculated correctly."""

    def test_risk_per_share(self, tmp_app_config: AppConfig):
        """risk_per_share = abs(entry - stop)."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=92.0,
        )
        assert result.risk_per_share == 8.0

    def test_dollar_risk_equals_shares_times_risk_per_share(self, tmp_app_config: AppConfig):
        """dollar_risk = shares * risk_per_share."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
        )
        expected_risk = result.shares * result.risk_per_share
        assert result.dollar_risk == expected_risk

    def test_risk_pct_of_account(self, tmp_app_config: AppConfig):
        """risk_pct_of_account = 100 * dollar_risk / equity."""
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            account_equity=100_000.0,
        )
        expected_pct = 100.0 * result.dollar_risk / 100_000.0
        assert result.risk_pct_of_account == pytest.approx(expected_pct)


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_very_tight_stop(self, tmp_app_config: AppConfig):
        """Very tight stop (small risk_per_share) produces large position."""
        # Stop $0.01 away => huge sizing
        result = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=99.99,
        )
        # risk_budget $1000 / 0.01 = 100,000 shares, then capped by other limits
        # Position size cap: 15% of $100k = $15k / $100 = 150 shares
        assert result.shares <= 150

    def test_custom_account_sizes(self, tmp_app_config: AppConfig):
        """Sizing scales with account equity."""
        result_small = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            account_equity=50_000.0,
        )
        result_large = size_position(
            config=tmp_app_config,
            direction=Direction.LONG,
            entry_price=100.0,
            stop_price=95.0,
            account_equity=200_000.0,
        )
        # Larger account should yield more shares (assuming no cap bite)
        assert result_large.shares > result_small.shares
