"""Contract tests for the broker execution boundary (ADR-0007 Decision 4).

Covers: the guard functions in isolation, ``PaperBroker`` as the first
``BrokerProvider`` implementation (submit -> fill -> position -> close,
cancel, kill-switch refusal), and ``NullLiveBroker`` as the second
implementation shape (live-mode refusal without explicit config,
``NotConfiguredError`` once the guard is satisfied).
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.brokers.base import (
    BrokerOrderError,
    NotConfiguredError,
    OrderRequest,
    TradingHaltedError,
    guard_live_side_effect,
    guard_new_order,
)
from claudetrade.brokers.null_live import NullLiveBroker
from claudetrade.config import AppConfig, TradingModeConfig
from claudetrade.db.session import Database
from claudetrade.domain import (
    ACTIVE_STATUSES,
    Bar,
    ComponentScores,
    Direction,
    ExitReason,
    OrderStatus,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.paper.broker import PaperBroker


def make_signal(
    *,
    signal_id: str = "SIG-1",
    symbol: str = "TEST",
    session: dt.date = dt.date(2023, 1, 3),
    entry_low: float = 99.0,
    entry_high: float = 101.0,
    stop_loss: float = 95.0,
    targets: list[float] | None = None,
    shares: int = 10,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        created_at=dt.datetime(2023, 1, 3, 15, 0, tzinfo=dt.UTC),
        session=session,
        symbol=symbol,
        company_name="Test Co",
        strategy="test",
        direction=Direction.LONG,
        status=SignalStatus.ACTIONABLE,
        reference_price=100.0,
        price_as_of=dt.datetime(2023, 1, 3, 15, 0, tzinfo=dt.UTC),
        overall_score=75.0,
        confidence=0.8,
        components=ComponentScores(),
        plan=TradePlan(
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            targets=targets or [110.0],
            shares=shares,
        ),
    )


def make_fill_bar(symbol: str = "TEST", session: dt.date = dt.date(2023, 1, 4)) -> Bar:
    """A bar whose range reaches the entry zone used by ``make_signal``."""
    return Bar(
        symbol=symbol,
        session=session,
        open=100.0,
        high=101.5,
        low=99.0,
        close=100.5,
        volume=1_000_000,
    )


@pytest.fixture
def broker(tmp_app_config: AppConfig, tmp_db: Database) -> PaperBroker:
    return PaperBroker(tmp_app_config, tmp_db)


# --------------------------------------------------------------------------
# Guard functions, tested directly
# --------------------------------------------------------------------------


class TestGuardNewOrder:
    def test_paper_passes_by_default(self, tmp_app_config: AppConfig):
        guard_new_order(tmp_app_config, is_paper=True)  # must not raise

    def test_non_paper_refused_without_live_mode(self, tmp_app_config: AppConfig):
        assert tmp_app_config.trading.mode == "paper"
        with pytest.raises(TradingHaltedError, match="not 'live'"):
            guard_new_order(tmp_app_config, is_paper=False)

    def test_non_paper_passes_when_live_and_authorised(self, tmp_app_config: AppConfig):
        tmp_app_config.trading = TradingModeConfig(
            mode="live", live_trading_authorised=True, broker="alpaca"
        )
        guard_new_order(tmp_app_config, is_paper=False)  # must not raise

    def test_kill_switch_blocks_paper(self, tmp_app_config: AppConfig):
        tmp_app_config.risk.kill_switch_engaged = True
        with pytest.raises(TradingHaltedError, match="kill switch"):
            guard_new_order(tmp_app_config, is_paper=True)

    def test_trading_kill_switch_blocks_paper(self, tmp_app_config: AppConfig):
        tmp_app_config.trading.kill_switch_engaged = True
        with pytest.raises(TradingHaltedError, match="kill switch"):
            guard_new_order(tmp_app_config, is_paper=True)

    def test_kill_switch_blocks_live(self, tmp_app_config: AppConfig):
        tmp_app_config.trading = TradingModeConfig(
            mode="live", live_trading_authorised=True, broker="alpaca"
        )
        tmp_app_config.risk.kill_switch_engaged = True
        with pytest.raises(TradingHaltedError, match="kill switch"):
            guard_new_order(tmp_app_config, is_paper=False)


class TestGuardLiveSideEffect:
    """Cancel/modify are gated on live authorisation, but NOT on the kill switch."""

    def test_paper_passes_even_with_kill_switch(self, tmp_app_config: AppConfig):
        tmp_app_config.risk.kill_switch_engaged = True
        guard_live_side_effect(tmp_app_config, is_paper=True)  # must not raise

    def test_non_paper_refused_without_live_mode(self, tmp_app_config: AppConfig):
        with pytest.raises(TradingHaltedError, match="not 'live'"):
            guard_live_side_effect(tmp_app_config, is_paper=False)

    def test_live_authorised_passes_even_with_kill_switch(self, tmp_app_config: AppConfig):
        tmp_app_config.trading = TradingModeConfig(
            mode="live", live_trading_authorised=True, broker="alpaca"
        )
        tmp_app_config.risk.kill_switch_engaged = True
        guard_live_side_effect(tmp_app_config, is_paper=False)  # must not raise


# --------------------------------------------------------------------------
# PaperBroker as a BrokerProvider
# --------------------------------------------------------------------------


class TestPaperBrokerLifecycle:
    def test_submit_fill_position_close(self, broker: PaperBroker):
        signal = make_signal()
        broker.ledger.record(signal)
        bar = make_fill_bar()

        order = broker.submit_order(OrderRequest(signal=signal, next_bar=bar))

        assert order.status is OrderStatus.FILLED
        assert order.filled_shares == 10
        assert order.average_fill_price is not None
        assert order.direction is Direction.LONG

        positions = broker.get_positions()
        assert len(positions) == 1
        trade = positions[0]
        assert trade.symbol == "TEST"
        assert trade.is_open
        assert trade.shares == 10

        same = broker.get_position("TEST")
        assert same is not None
        assert same.trade_id == trade.trade_id
        assert broker.get_position("NOPE") is None

        fetched_order = broker.get_order(order.order_id)
        assert fetched_order is not None
        assert fetched_order.status is OrderStatus.FILLED

        # No order is left "working": paper fills synchronously.
        assert broker.get_open_orders() == []

        # Close the position (mirrors what process_open_positions does on a
        # stop/target hit) and confirm it drops out of get_positions/get_position.
        broker.portfolio.close_trade(
            trade.trade_id,
            exit_session=dt.date(2023, 1, 5),
            exit_price=105.0,
            reason=ExitReason.TARGET,
        )
        assert broker.get_positions() == []
        assert broker.get_position("TEST") is None

        balances = broker.get_balances()
        assert balances.equity > 0
        assert balances.kill_switch_engaged is False

    def test_submit_rejected_when_price_never_reaches_zone(self, broker: PaperBroker):
        # A long limit fills when the bar's low reaches down to entry_high; a
        # zone entirely below the bar's low (99.0) is never reached.
        signal = make_signal(entry_low=50.0, entry_high=60.0)
        broker.ledger.record(signal)
        bar = make_fill_bar()  # low is 99.0, never dips into the 50-60 zone

        order = broker.submit_order(OrderRequest(signal=signal, next_bar=bar))

        assert order.status is OrderStatus.REJECTED
        assert order.reasons
        assert broker.get_positions() == []

    def test_submit_order_requires_next_bar(self, broker: PaperBroker):
        signal = make_signal()
        with pytest.raises(ValueError, match="next_bar"):
            broker.submit_order(OrderRequest(signal=signal))

    def test_cancel_filled_order_is_refused(self, broker: PaperBroker):
        signal = make_signal()
        broker.ledger.record(signal)
        order = broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

        with pytest.raises(BrokerOrderError, match="already"):
            broker.cancel_order(order.order_id)

    def test_cancel_unknown_order(self, broker: PaperBroker):
        with pytest.raises(BrokerOrderError, match="no such"):
            broker.cancel_order("does-not-exist")

    def test_modify_order_adjusts_open_position_stop(self, broker: PaperBroker):
        signal = make_signal()
        broker.ledger.record(signal)
        order = broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

        updated = broker.modify_order(order.order_id, stop_loss=97.0)
        assert updated.status is OrderStatus.FILLED  # order itself stays filled

        position = broker.get_position("TEST")
        assert position is not None
        assert position.stop_loss == 97.0

    def test_modify_order_after_close_is_refused(self, broker: PaperBroker):
        signal = make_signal()
        broker.ledger.record(signal)
        order = broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))
        trade = broker.get_position("TEST")
        assert trade is not None
        broker.portfolio.close_trade(
            trade.trade_id,
            exit_session=dt.date(2023, 1, 5),
            exit_price=105.0,
            reason=ExitReason.TARGET,
        )

        with pytest.raises(BrokerOrderError, match="no open position"):
            broker.modify_order(order.order_id, stop_loss=97.0)


class TestPaperBrokerKillSwitchRefusal:
    def test_submit_refused_when_config_kill_switch_engaged(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ):
        tmp_app_config.risk.kill_switch_engaged = True
        broker = PaperBroker(tmp_app_config, tmp_db)
        signal = make_signal()
        broker.ledger.record(signal)

        with pytest.raises(TradingHaltedError, match="kill switch"):
            broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

        # The guard must fire before any DB write -- no order or trade recorded.
        assert broker.get_positions() == []
        assert broker.get_open_orders() == []

    def test_submit_refused_when_trading_kill_switch_engaged(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ):
        tmp_app_config.trading.kill_switch_engaged = True
        broker = PaperBroker(tmp_app_config, tmp_db)
        signal = make_signal()
        broker.ledger.record(signal)

        with pytest.raises(TradingHaltedError, match="kill switch"):
            broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

    def test_cancel_still_permitted_under_kill_switch(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ):
        """Kill switch blocks new entries only -- an existing order can still be managed."""
        broker = PaperBroker(tmp_app_config, tmp_db)
        signal = make_signal()
        broker.ledger.record(signal)
        order = broker.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

        tmp_app_config.risk.kill_switch_engaged = True
        # Refused because the order is already filled (see
        # test_cancel_filled_order_is_refused), not because of the kill switch.
        with pytest.raises(BrokerOrderError, match="already"):
            broker.cancel_order(order.order_id)


# --------------------------------------------------------------------------
# NullLiveBroker: the second implementation shape
# --------------------------------------------------------------------------


class TestNullLiveBroker:
    def test_is_not_paper_and_not_backtesting(self, tmp_app_config: AppConfig):
        live = NullLiveBroker(tmp_app_config)
        assert live.is_paper is False
        assert live.is_backtesting is False

    def test_submit_refused_by_guard_without_explicit_live_config(
        self, tmp_app_config: AppConfig
    ):
        """Default config (mode='paper') refuses before NullLiveBroker is even asked."""
        live = NullLiveBroker(tmp_app_config)
        signal = make_signal()

        with pytest.raises(TradingHaltedError, match="not 'live'"):
            live.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

    def test_cancel_and_modify_also_refused_without_explicit_live_config(
        self, tmp_app_config: AppConfig
    ):
        live = NullLiveBroker(tmp_app_config)
        with pytest.raises(TradingHaltedError):
            live.cancel_order("some-id")
        with pytest.raises(TradingHaltedError):
            live.modify_order("some-id", stop_loss=1.0)

    def test_not_configured_once_guard_is_satisfied(self, tmp_app_config: AppConfig):
        """With live mode explicitly authorised, the guard passes and the stub answers honestly."""
        tmp_app_config.trading = TradingModeConfig(
            mode="live", live_trading_authorised=True, broker="alpaca"
        )
        live = NullLiveBroker(tmp_app_config)
        signal = make_signal()

        with pytest.raises(NotConfiguredError):
            live.submit_order(OrderRequest(signal=signal, next_bar=make_fill_bar()))

    def test_read_only_surface_raises_not_configured_unguarded(self, tmp_app_config: AppConfig):
        """Reads are not gated by the trading-mode guard at all -- they go straight to the stub."""
        live = NullLiveBroker(tmp_app_config)
        with pytest.raises(NotConfiguredError):
            live.get_balances()
        with pytest.raises(NotConfiguredError):
            live.get_positions()
        with pytest.raises(NotConfiguredError):
            live.get_open_orders()


def test_active_statuses_reused_from_domain():
    """The broker boundary's OrderStatus vocabulary is domain's, not a local copy."""
    assert OrderStatus.WORKING in ACTIVE_STATUSES
    assert OrderStatus.FILLED not in ACTIVE_STATUSES
