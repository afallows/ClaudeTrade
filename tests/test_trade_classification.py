"""Tests for trade outcome classification (win/loss/breakeven)."""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.domain import Direction, ExitReason, Trade, TradeOutcome


class TestOutcomeClassification:
    """Trade.outcome() classifies closed trades as win, loss, or breakeven."""

    def test_winning_long_trade(self):
        """Long trade with positive net return is a WIN."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=110.0,
            exit_reason=ExitReason.TARGET,
            initial_risk_per_share=5.0,
        )
        assert trade.outcome() == TradeOutcome.WIN

    def test_losing_long_trade(self):
        """Long trade with negative net return is a LOSS."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=90.0,
            exit_reason=ExitReason.STOP_LOSS,
            initial_risk_per_share=5.0,
        )
        assert trade.outcome() == TradeOutcome.LOSS

    def test_winning_short_trade(self):
        """Short trade closing at lower price is a WIN."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.SHORT,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=105.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=90.0,
            exit_reason=ExitReason.TARGET,
            initial_risk_per_share=5.0,
        )
        assert trade.outcome() == TradeOutcome.WIN

    def test_losing_short_trade(self):
        """Short trade closing at higher price is a LOSS."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.SHORT,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=105.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=110.0,
            exit_reason=ExitReason.STOP_LOSS,
            initial_risk_per_share=5.0,
        )
        assert trade.outcome() == TradeOutcome.LOSS


class TestBreakevenBand:
    """Trades within breakeven band are excluded from both win and loss counts."""

    def test_small_win_in_band_is_breakeven(self):
        """Small win within breakeven_threshold_pct is BREAKEVEN."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=100.03,  # +0.03% return
            exit_reason=ExitReason.TIME_STOP,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
            initial_risk_per_share=5.0,
        )
        # With default 0.05% threshold, +0.03% is breakeven
        assert trade.outcome(breakeven_threshold_pct=0.05) == TradeOutcome.BREAKEVEN

    def test_small_loss_in_band_is_breakeven(self):
        """Small loss within breakeven_threshold_pct is BREAKEVEN."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=99.97,  # -0.03% return
            exit_reason=ExitReason.TIME_STOP,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
            initial_risk_per_share=5.0,
        )
        # With default 0.05% threshold, -0.03% is breakeven
        assert trade.outcome(breakeven_threshold_pct=0.05) == TradeOutcome.BREAKEVEN

    def test_breakeven_band_is_configurable(self):
        """breakeven_threshold_pct can be customized."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=100.5,  # +0.5% return
            exit_reason=ExitReason.TIME_STOP,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
            initial_risk_per_share=5.0,
        )
        # With 0.05% threshold, +0.5% is a WIN
        assert trade.outcome(breakeven_threshold_pct=0.05) == TradeOutcome.WIN
        # With 1.0% threshold, +0.5% is BREAKEVEN
        assert trade.outcome(breakeven_threshold_pct=1.0) == TradeOutcome.BREAKEVEN


class TestNetReturnUsedNotGross:
    """Classification uses net return (after costs), not gross."""

    def test_gross_winner_net_loser_is_loss(self):
        """Gross winner that costs turn negative classifies as LOSS."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=102.0,  # +$200 gross P&L
            exit_reason=ExitReason.TARGET,
            commission_total=150.0,
            fees_total=75.0,
            borrow_cost_total=0.0,
            initial_risk_per_share=5.0,
        )
        # Gross: +$200, Costs: $225, Net: -$25
        assert trade.net_pnl < 0
        assert trade.outcome() == TradeOutcome.LOSS


class TestOpenTradeRaises:
    """Open trades cannot be classified; attempting to do so raises ValueError."""

    def test_open_trade_raises_error(self):
        """Calling outcome() on an open trade raises ValueError."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            # exit_session=None => open trade
            exit_price=None,
            exit_reason=None,
            initial_risk_per_share=5.0,
        )
        with pytest.raises(ValueError, match="still open"):
            trade.outcome()


class TestRMultipleCalculation:
    """r_multiple arithmetic is correct for long and short."""

    def test_long_r_multiple_calculation(self):
        """R-multiple for long: net_pnl / initial_risk."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,  # 100 shares
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=110.0,
            exit_reason=ExitReason.TARGET,
            initial_risk_per_share=5.0,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
        )
        # Gross: (110 - 100) * 100 = +$1000
        # Risk: 5 * 100 = $500
        # R-multiple: 1000 / 500 = +2.0R
        assert trade.gross_pnl == 1_000.0
        assert trade.r_multiple == pytest.approx(2.0)

    def test_short_r_multiple_calculation(self):
        """R-multiple for short: net_pnl / initial_risk."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.SHORT,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=105.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=90.0,
            exit_reason=ExitReason.TARGET,
            initial_risk_per_share=5.0,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
        )
        # Gross: (100 - 90) * 100 * sign(-1) = +$1000
        # Risk: 5 * 100 = $500
        # R-multiple: 1000 / 500 = +2.0R
        assert trade.gross_pnl == 1_000.0
        assert trade.r_multiple == pytest.approx(2.0)

    def test_losing_trade_negative_r(self):
        """Losing trade has negative R-multiple."""
        trade = Trade(
            trade_id="T1",
            signal_id="S1",
            symbol="TEST",
            strategy="test",
            direction=Direction.LONG,
            entry_session=dt.date(2023, 1, 3),
            entry_price=100.0,
            shares=100,
            stop_loss=95.0,
            exit_session=dt.date(2023, 1, 5),
            exit_price=93.0,  # Stop-hit at 95, actually 93
            exit_reason=ExitReason.STOP_LOSS,
            initial_risk_per_share=5.0,
            commission_total=0.0,
            fees_total=0.0,
            borrow_cost_total=0.0,
        )
        # Gross: (93 - 100) * 100 = -$700
        # Risk: 5 * 100 = $500
        # R-multiple: -700 / 500 = -1.4R
        assert trade.r_multiple == pytest.approx(-1.4)
