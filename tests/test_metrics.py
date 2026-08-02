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
    """Factory for creating closed trades with specified outcomes.

    ``net_pnl`` is the P&L *after* costs, matching ``Trade.net_pnl``. The exit
    price is therefore solved backwards from the requested net figure plus the
    commission, so that a trade asked for as +3.0 nets +3.0 rather than -47.0.
    Getting this wrong silently reclassifies small winners as losers, which is
    exactly the kind of error these metrics exist to catch.
    """
    commission = 50.0
    if gross_pnl is None:
        gross_pnl = net_pnl + commission

    shares = 100
    return Trade(
        trade_id=trade_id,
        signal_id=signal_id,
        symbol="TEST",
        strategy="test",
        direction=Direction.LONG,
        entry_session=dt.date(2023, 1, 3),
        entry_price=100.0,
        shares=shares,
        stop_loss=95.0,
        exit_session=dt.date(2023, 1, 3) + dt.timedelta(days=holding_days),
        exit_price=100.0 + gross_pnl / shares,
        exit_reason=ExitReason.TARGET if net_pnl > 0 else ExitReason.STOP_LOSS,
        commission_total=commission,
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


class TestDegenerateRatioMetricsAreUnavailableNotGarbage:
    """ADR-0007 Decision 3(a): insufficient-sample ratios come out None with a
    machine-readable reason, never a number like the historical Sharpe =
    -9.2e16 -- and never float('nan') either, which would pass an `is None`
    check but corrupt any downstream arithmetic that forgot to guard for it.
    """

    def test_zero_trades_zero_equity_points(self):
        """No trades and no equity curve: every ratio is None with a reason."""
        metrics = compute_metrics([], [], BacktestConfig())
        assert metrics.trade_count == 0
        assert metrics.sharpe is None
        assert metrics.sortino is None
        assert metrics.calmar is None
        assert "sharpe" in metrics.unavailable_reasons
        assert "sortino" in metrics.unavailable_reasons
        assert "calmar" in metrics.unavailable_reasons
        for reason in metrics.unavailable_reasons.values():
            assert reason  # non-empty, machine-readable

    def test_one_trade_no_equity_curve(self):
        """A single trade cannot support a ratio estimate either."""
        trades = [make_closed_trade("T1", "S1", 250.0)]
        metrics = compute_metrics(trades, [], BacktestConfig())
        assert metrics.trade_count == 1
        assert metrics.sharpe is None
        assert metrics.sortino is None
        assert metrics.calmar is None
        assert metrics.unavailable_reasons["sharpe"].startswith("no_equity_curve")

    def test_short_equity_curve_below_observation_floor(self):
        """Fewer return observations than the floor: insufficient_sample, not a number."""
        equity = [MockEquityPoint(100_000.0), MockEquityPoint(100_500.0), MockEquityPoint(101_000.0)]
        metrics = compute_metrics([], equity, BacktestConfig())
        assert metrics.sharpe is None
        assert metrics.sortino is None
        assert metrics.calmar is None
        assert metrics.unavailable_reasons["sharpe"].startswith("insufficient_sample")
        assert metrics.unavailable_reasons["sortino"].startswith("insufficient_sample")

    def test_zero_variance_equity_curve(self):
        """A flat equity curve (enough points, zero return variance) is unavailable, not 0.0-as-a-measurement."""
        equity = [MockEquityPoint(100_000.0, drawdown_pct=0.0) for _ in range(8)]
        metrics = compute_metrics([], equity, BacktestConfig())
        assert metrics.sharpe is None
        assert metrics.sortino is None
        assert metrics.calmar is None
        assert metrics.unavailable_reasons["sharpe"].startswith("zero_variance")
        # Calmar's own guard fires on zero measured drawdown, which is the
        # accompanying symptom of a perfectly flat curve.
        assert metrics.unavailable_reasons["calmar"].startswith("no_drawdown")

    def test_healthy_sample_still_returns_real_numbers(self):
        """Sanity check: a sample that clears every floor still produces plain floats."""
        # drawdown_pct is supplied directly (mirroring what the real portfolio
        # marks each session) rather than derived, so it must reflect the
        # peak-to-date for max_dd_pct/calmar to see a real drawdown.
        equity = [
            MockEquityPoint(100_000.0, drawdown_pct=0.0),
            MockEquityPoint(101_000.0, drawdown_pct=0.0),
            MockEquityPoint(100_500.0, drawdown_pct=0.495),
            MockEquityPoint(102_000.0, drawdown_pct=0.0),
            MockEquityPoint(103_500.0, drawdown_pct=0.0),
            MockEquityPoint(102_800.0, drawdown_pct=0.676),
            MockEquityPoint(104_200.0, drawdown_pct=0.0),
        ]
        metrics = compute_metrics([], equity, BacktestConfig())
        assert isinstance(metrics.sharpe, float)
        assert isinstance(metrics.sortino, float)
        assert isinstance(metrics.calmar, float)
        assert not metrics.unavailable_reasons


class TestSignificanceGate:
    """ADR-0007 Decision 3(c): significance is computed inside compute_metrics,
    not left for a caller to opt into, and carries both a structured field and
    an explicit warning.
    """

    def test_below_trade_floor_is_not_significant(self):
        config = BacktestConfig(min_trades_for_validation=30)
        trades = [make_closed_trade(f"T{i}", f"S{i}", 100.0) for i in range(5)]
        metrics = compute_metrics(trades, [], config)
        assert metrics.is_statistically_significant is False
        assert metrics.significance_reason is not None
        assert "trade_count_below_floor" in metrics.significance_reason
        assert any("NOT STATISTICALLY SIGNIFICANT" in w for w in metrics.warnings)

    def test_zero_trades_is_not_significant(self):
        metrics = compute_metrics([], [], BacktestConfig())
        assert metrics.is_statistically_significant is False
        assert metrics.significance_reason is not None
        assert any("NOT STATISTICALLY SIGNIFICANT" in w for w in metrics.warnings)

    def test_above_floor_with_clear_edge_is_significant(self):
        """Enough trades, all winners of the same size: the CI cannot include zero."""
        config = BacktestConfig(min_trades_for_validation=10)
        trades = [make_closed_trade(f"T{i}", f"S{i}", 500.0) for i in range(20)]
        metrics = compute_metrics(trades, [], config)
        assert metrics.trade_count == 20
        assert metrics.is_statistically_significant is True
        assert metrics.significance_reason is None
        assert not any("NOT STATISTICALLY SIGNIFICANT" in w for w in metrics.warnings)
