"""Tests for performance metrics computation."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from claudetrade.backtest.metrics import compute_metrics
from claudetrade.config import BacktestConfig
from claudetrade.domain import Direction, ExitReason, Trade


class MockEquityPoint:
    """Mock EquityPointLike for testing."""

    def __init__(self, equity: float, exposure_pct: float = 50.0, drawdown_pct: float = 0.0):
        self.equity = equity
        self.exposure_pct = exposure_pct
        self.drawdown_pct = drawdown_pct


def make_closed_trade(
    trade_id: str,
    signal_id: str,
    net_pnl: float,
    gross_pnl: float | None = None,
    r_multiple: float = 1.0,
    holding_days: int = 5,
) -> Trade:
    """Factory for creating closed trades with specified outcomes."""
    if gross_pnl is None:
        gross_pnl = net_pnl + 50.0  # Small cost buffer

    return Trade(
        trade_id=trade_id,
        signal_id=signal_id,
        symbol="TEST",
        strategy="test",
        direction=Direction.LONG,
        entry_session=dt.date(2023, 1, 3),
        entry_price=100.0,
        shares=100,
        stop_loss=95.0,
        exit_session=dt.date(2023, 1, 3) + dt.timedelta(days=holding_days),
        exit_price=100.0 + net_pnl / 100.0,
        exit_reason=ExitReason.TARGET if net_pnl > 0 else ExitReason.STOP_LOSS,
        commission_total=50.0 if net_pnl != gross_pnl else 0.0,
        fees_total=0.0,
        borrow_cost_total=0.0,
        initial_risk_per_share=5.0,
    )


class TestWinLossRatio:
    """Win/loss ratio calculation."""

    def test_equal_wins_and_losses(self):
        """Win/loss ratio = 1.0 for equal wins and losses."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),  # win
            make_closed_trade("T2", "S2", -500.0),  # loss
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.win_loss_ratio == 1.0
        assert metrics.winning_trades == 1
        assert metrics.losing_trades == 1

    def test_more_wins_than_losses(self):
        """Win/loss ratio > 1 when more wins."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),
            make_closed_trade("T2", "S2", 500.0),
            make_closed_trade("T3", "S3", -200.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.winning_trades == 2
        assert metrics.losing_trades == 1
        assert metrics.win_loss_ratio == 2.0

    def test_no_losses_ratio_is_inf(self):
        """Win/loss ratio is inf (not capped) when zero losses."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),
            make_closed_trade("T2", "S2", 300.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.win_loss_ratio == math.inf
        assert metrics.win_loss_ratio_is_degenerate

    def test_no_wins_ratio_is_zero(self):
        """Win/loss ratio is 0 when zero wins."""
        trades = [
            make_closed_trade("T1", "S1", -500.0),
            make_closed_trade("T2", "S2", -300.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.win_loss_ratio == 0.0
        assert metrics.winning_trades == 0
        assert metrics.losing_trades == 2


class TestBreakevenExclusion:
    """Breakeven trades excluded from both win and loss counts."""

    def test_breakeven_trades_not_counted(self):
        """Breakeven trades excluded from win/loss counts."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),  # win
            make_closed_trade("T2", "S2", -500.0),  # loss
            make_closed_trade("T3", "S3", 3.0),  # breakeven
        ]
        metrics = compute_metrics(trades, [], BacktestConfig(), breakeven_threshold_pct=0.05)
        assert metrics.winning_trades == 1
        assert metrics.losing_trades == 1
        assert metrics.breakeven_trades == 1
        assert metrics.trade_count == 3
        assert metrics.win_loss_ratio == 1.0


class TestWinRate:
    """Win rate calculation."""

    def test_win_rate_calculation(self):
        """Win rate = wins / total_trades."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),  # win
            make_closed_trade("T2", "S2", 300.0),  # win
            make_closed_trade("T3", "S3", -200.0),  # loss
            make_closed_trade("T4", "S4", -150.0),  # loss
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.trade_count == 4
        assert metrics.win_rate == 0.5


class TestAverageProfits:
    """Average win and loss calculation."""

    def test_average_win_calculation(self):
        """Average win = mean of winning P&Ls."""
        trades = [
            make_closed_trade("T1", "S1", 1000.0),
            make_closed_trade("T2", "S2", 600.0),
            make_closed_trade("T3", "S3", -200.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        # Wins: [1000, 600], mean = 800
        assert metrics.average_win == pytest.approx(800.0, abs=50.0)

    def test_average_loss_calculation(self):
        """Average loss = mean magnitude of losing P&Ls."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),
            make_closed_trade("T2", "S2", -200.0),
            make_closed_trade("T3", "S3", -400.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        # Losses magnitudes: [200, 400], mean = 300
        assert metrics.average_loss == pytest.approx(300.0, abs=30.0)


class TestProfitFactor:
    """Profit factor calculation."""

    def test_profit_factor_calculation(self):
        """Profit factor = gross_profit / abs(gross_loss)."""
        trades = [
            make_closed_trade("T1", "S1", 1000.0),
            make_closed_trade("T2", "S2", 500.0),
            make_closed_trade("T3", "S3", -200.0),
            make_closed_trade("T4", "S4", -400.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        # Gross profit: 1500, Gross loss: 600, PF = 1500/600 = 2.5
        assert metrics.profit_factor == pytest.approx(2.5, abs=0.1)


class TestExpectancy:
    """Expectancy (average trade outcome) calculation."""

    def test_expectancy_calculation(self):
        """Expectancy = mean trade P&L."""
        trades = [
            make_closed_trade("T1", "S1", 1000.0),
            make_closed_trade("T2", "S2", -200.0),
            make_closed_trade("T3", "S3", 300.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        # Mean: (1000 - 200 + 300) / 3 = 366.67
        assert metrics.expectancy_dollars == pytest.approx(366.67, abs=10.0)


class TestValidationWarnings:
    """validation_warnings fires for pathological win/loss profiles."""

    def test_tiny_wins_huge_loss_warning(self):
        """Warn when win/loss ratio > 1 but expectancy negative."""
        # 10 small wins, 1 huge loss
        trades = [make_closed_trade(f"T{i}", f"S{i}", 50.0) for i in range(1, 11)]
        trades.append(make_closed_trade("T11", "S11", -1000.0))

        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.win_loss_ratio > 1.0  # 10 wins / 1 loss = 10
        assert metrics.expectancy_dollars < 0  # But net is -450
        # Should warn about this pathology
        assert any("tiny" in w.lower() or "expectancy" in w.lower() for w in metrics.warnings)

    def test_avg_loss_much_greater_than_avg_win(self):
        """Warn when average loss is 2x+ average win."""
        trades = [
            make_closed_trade("T1", "S1", 100.0),
            make_closed_trade("T2", "S2", -500.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.average_loss > 2 * metrics.average_win
        assert any("loss" in w.lower() for w in metrics.warnings)


class TestDegenerateWarning:
    """Warning issued for degenerate samples (no losses)."""

    def test_no_losses_warning(self):
        """Warn when zero losses (degenerate sample)."""
        trades = [
            make_closed_trade("T1", "S1", 500.0),
            make_closed_trade("T2", "S2", 300.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.win_loss_ratio == math.inf
        assert metrics.win_loss_ratio_is_degenerate


class TestMinTradesValidation:
    """Validation warnings for insufficient trade count."""

    def test_below_min_trades_warning(self):
        """Warn when trade count below min_trades_for_validation."""
        config = BacktestConfig(min_trades_for_validation=50)
        trades = [make_closed_trade(f"T{i}", f"S{i}", 100.0) for i in range(20)]
        metrics = compute_metrics(trades, [], config)
        assert metrics.trade_count == 20
        assert any("trades" in w.lower() or "validation" in w.lower() for w in metrics.warnings)


class TestOpenTradesAssertion:
    """compute_metrics asserts that no open trades are provided."""

    def test_open_trade_raises(self):
        """Open trade in the list raises AssertionError."""
        closed = make_closed_trade("T1", "S1", 100.0)
        open_trade = Trade(
            trade_id="T2",
            signal_id="S2",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            # exit_session=None => open
            exit_price=None,
            exit_reason=None,
            initial_risk_per_share=5.0,
        )
        with pytest.raises(AssertionError, match="open trade"):
            compute_metrics([closed, open_trade], [], BacktestConfig())


class TestMedianAndExtremes:
    """Median return, largest win/loss calculations."""

    def test_median_return(self):
        """Median return is the middle value of net returns."""
        trades = [
            make_closed_trade("T1", "S1", 1000.0),
            make_closed_trade("T2", "S2", 500.0),
            make_closed_trade("T3", "S3", -200.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        # Returns: [1000, 500, -200], sorted: [-200, 500, 1000], median = 500
        assert metrics.median_return_pct > 0

    def test_largest_win_and_loss(self):
        """Largest win and loss are correctly identified."""
        trades = [
            make_closed_trade("T1", "S1", 2000.0),
            make_closed_trade("T2", "S2", 500.0),
            make_closed_trade("T3", "S3", -1500.0),
            make_closed_trade("T4", "S4", -300.0),
        ]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.largest_win == pytest.approx(2000.0, abs=100.0)
        assert metrics.largest_loss < 0
