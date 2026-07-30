"""Reconciliation tests for the backtest's portfolio-level equity/cash ledger.

These guard against the accounting defect where reported equity diverged
from the sum of closed-trade P&L by roughly the size of the whole account
(a 1-year backtest with 27 trades summing to about -$322 net P&L reported a
-112.86% total return / negative final equity instead of about -0.3%). Two
independent bugs combined to cause it:

1. ``BacktestEngine.run`` could queue a second entry order for a symbol that
   already had one working (unfilled) order, because the "already holding"
   guard only checked *open positions*, not *pending orders*. If both later
   filled, the second call to ``BacktestPortfolio.open_position`` silently
   overwrote the first ``Position`` in the ``positions`` dict -- the first
   position's entry cash debit was never reversed because it was never
   closed, permanently corrupting cash by that position's notional. See
   ``TestNoOrphanedPositions`` and ``TestOpenPositionGuardsAgainstOverwrite``.

2. ``BacktestPortfolio.mark_to_market`` marked an open short's contribution
   to equity as only its unrealised P&L, omitting that the short's entry had
   already *credited* cash by its full notional (short sales raise cash,
   unlike longs). That left equity overstated by a short's entry notional
   for as long as it stayed open. See ``TestEquityCurveConsistency``.

Both defects self-correct once every position is closed (closing cash flows
were always correct), which is exactly why they showed up as an equity-curve
defect rather than a defect in any individual trade's P&L.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.backtest.costs import CostModel
from claudetrade.backtest.engine import BacktestEngine, DictContextProvider
from claudetrade.backtest.execution import EntryOrder, ExecutionSimulator
from claudetrade.backtest.portfolio import BacktestPortfolio
from claudetrade.config import AppConfig, BacktestConfig
from claudetrade.domain import (
    Bar,
    Direction,
    ExitReason,
    MarketRegime,
    RegimeState,
    SecurityInfo,
)
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal


def _config(**overrides) -> AppConfig:
    return AppConfig(backtest=BacktestConfig(initial_capital_usd=100_000.0, **overrides))


def _bar(symbol: str, session: dt.date, price: float, *, volume: float = 1_000_000.0) -> Bar:
    return Bar(
        symbol=symbol,
        session=session,
        open=price,
        high=price + 0.5,
        low=price - 0.5,
        close=price,
        volume=volume,
    )


def _portfolio(cfg: AppConfig) -> BacktestPortfolio:
    cost_model = CostModel(cfg.costs)
    execution = ExecutionSimulator(
        cost_model=cost_model, cost_config=cfg.costs, intrabar_policy="pessimistic"
    )
    return BacktestPortfolio(
        config=cfg,
        cost_model=cost_model,
        execution=execution,
        cash=cfg.backtest.initial_capital_usd,
    )


def _open(
    portfolio: BacktestPortfolio,
    symbol: str,
    direction: Direction,
    shares: int,
    entry_bar: Bar,
    *,
    stop_loss: float,
    targets: list[float],
):
    order = EntryOrder(symbol=symbol, direction=direction, shares=shares, order_type="market_on_open")
    fill = portfolio.execution.try_fill_entry(order, entry_bar)
    assert fill is not None and fill.filled
    return portfolio.open_position(
        trade_id=f"{symbol}-entry",
        signal_id=f"{symbol}-signal",
        strategy="reconciliation_test",
        entry_order=order,
        fill=fill,
        bar=entry_bar,
        stop_loss=stop_loss,
        targets=targets,
        target_fractions=[1.0] * len(targets) if targets else [],
        trailing_stop_atr=None,
        time_stop_session=entry_bar.session + dt.timedelta(days=60),
    )


class TestEquityCurveConsistency:
    """Every session's mark must equal cash + the liquidation value of every
    open position -- independently derived, not copied from the formula
    under test (see module docstring point 2)."""

    def test_long_win_long_loss_and_short_reconcile_mid_run_and_at_close(self):
        cfg = _config()
        portfolio = _portfolio(cfg)
        day0 = dt.date(2024, 1, 2)
        day1 = dt.date(2024, 1, 3)

        win_entry = _bar("WINL", day0, 50.0)
        lose_entry = _bar("LOSL", day0, 50.0)
        short_entry = _bar("SHRT", day0, 50.0)

        _open(portfolio, "WINL", Direction.LONG, 100, win_entry, stop_loss=40.0, targets=[70.0])
        _open(portfolio, "LOSL", Direction.LONG, 100, lose_entry, stop_loss=40.0, targets=[70.0])
        _open(portfolio, "SHRT", Direction.SHORT, 100, short_entry, stop_loss=60.0, targets=[30.0])

        # --- Mid-run mark: three open positions at a new price each. ---
        marks = {"WINL": 55.0, "LOSL": 47.0, "SHRT": 48.0}
        point = portfolio.mark_to_market(day0, marks)

        expected_equity = portfolio.cash + sum(
            pos.direction.sign * marks[sym] * pos.shares for sym, pos in portfolio.positions.items()
        )
        assert point.equity == pytest.approx(expected_equity, abs=1e-6)
        assert point.cash == pytest.approx(portfolio.cash, abs=1e-6)
        # A short's contribution must NOT be just its unrealised P&L (the bug):
        # it must be the negative of its *full current notional*, since the
        # entry already credited cash with the full entry notional.
        short_pos = portfolio.positions["SHRT"]
        wrong_short_only_pnl = (marks["SHRT"] - short_pos.entry_price) * short_pos.shares * (-1)
        correct_short_contribution = -marks["SHRT"] * short_pos.shares
        assert correct_short_contribution != pytest.approx(wrong_short_only_pnl)
        # the fixed formula must match the correct (liquidation-value) one.
        assert point.equity == pytest.approx(
            portfolio.cash
            + marks["WINL"] * portfolio.positions["WINL"].shares
            + marks["LOSL"] * portfolio.positions["LOSL"].shares
            + correct_short_contribution,
            abs=1e-6,
        )

        # --- Force-close every position and reconcile against trades. ---
        day1_bars = {
            "WINL": _bar("WINL", day1, 60.0),  # win
            "LOSL": _bar("LOSL", day1, 45.0),  # loss
            "SHRT": _bar("SHRT", day1, 45.0),  # short wins (price dropped)
        }
        trades = []
        for symbol, bar in day1_bars.items():
            trade = portfolio.process_bar_for_position(
                symbol,
                bar,
                force_close=True,
                force_close_reason=ExitReason.END_OF_BACKTEST,
            )
            assert trade is not None
            trades.append(trade)

        assert not portfolio.positions
        final_point = portfolio.mark_to_market(day1, {})

        # Headline acceptance check: final equity must reconcile with the sum
        # of every trade's net P&L, not diverge by anything remotely close to
        # the account size.
        expected_final_equity = cfg.backtest.initial_capital_usd + sum(t.net_pnl for t in trades)
        assert final_point.equity == pytest.approx(expected_final_equity, abs=1e-6)
        assert final_point.equity == pytest.approx(portfolio.cash, abs=1e-6)
        assert final_point.equity > 0  # WINL's win comfortably covers LOSL's loss

    def test_open_short_does_not_overstate_equity_by_its_notional(self):
        """A single open short, marked at its own entry price (zero
        unrealised P&L), must leave equity unchanged from just before the
        short was opened -- not inflated by the short's notional."""
        cfg = _config()
        portfolio = _portfolio(cfg)
        day0 = dt.date(2024, 1, 2)

        equity_before = portfolio.cash
        entry_bar = _bar("SHRT", day0, 100.0)
        _open(portfolio, "SHRT", Direction.SHORT, 50, entry_bar, stop_loss=120.0, targets=[70.0])

        point = portfolio.mark_to_market(day0, {"SHRT": 100.0})  # unchanged price
        # Only commission/fees on the entry fill should separate the two --
        # not anywhere near the position's ~$5,000 notional.
        assert point.equity == pytest.approx(equity_before, abs=50.0)
        assert abs(point.equity - equity_before) < 5_000 * 0.5


class TestOpenPositionGuardsAgainstOverwrite:
    """``open_position`` must never silently overwrite a still-open position
    for the same symbol -- see module docstring point 1."""

    def test_second_open_for_already_open_symbol_raises_and_does_not_touch_cash(self):
        cfg = _config()
        portfolio = _portfolio(cfg)
        day0 = dt.date(2024, 1, 2)
        entry_bar = _bar("DUPE", day0, 50.0)

        _open(portfolio, "DUPE", Direction.LONG, 100, entry_bar, stop_loss=40.0, targets=[70.0])
        cash_after_first_open = portfolio.cash

        with pytest.raises(ValueError, match="already open"):
            _open(portfolio, "DUPE", Direction.LONG, 50, entry_bar, stop_loss=40.0, targets=[70.0])

        # The rejected second open must not have moved cash or replaced the
        # first position -- an exception with a side effect would still
        # corrupt the ledger.
        assert portfolio.cash == cash_after_first_open
        assert len(portfolio.positions) == 1
        assert portfolio.positions["DUPE"].shares == 100


# --------------------------------------------------------------------------
# Engine-level regression test for the duplicate-queued-order root cause.
# --------------------------------------------------------------------------

SYMBOL = "DUPE"


class _AlwaysBelowMarketLongStrategy(Strategy):
    """Proposes the same limit-priced long every session it is asked, with
    an entry band comfortably below the flat bars used in the fixture below
    -- so the order stays queued, unfilled, for several sessions, giving a
    naive engine every opportunity to queue it a second time."""

    name = "always_below_market_stub"
    version = "test"
    description = "Proposes a below-market long every session; engine dedup regression."
    direction_bias = Direction.LONG
    min_history_bars = 1
    permits_earnings_risk = True
    requires_sentiment = False

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        price = ctx.price
        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.LONG,
            entry_low=price - 12.0,
            entry_high=price - 10.0,
            stop_loss=price - 20.0,
            targets=[price + 20.0],
            target_fractions=[1.0],
            expected_holding_days=5,
            time_stop_days=60,
            setup_score=90.0,
        )


def _qualifying_features() -> dict[str, float]:
    return {
        "avg_dollar_volume_20": 50_000_000.0,
        "atr_pct": 3.0,
        "roc_10": 10.0,
        "roc_20": 10.0,
        "rs_percentile": 90.0,
        "rel_volume_20": 2.0,
        "obv_slope_10": 1.0,
    }


def _history_bars(symbol: str, up_to_session: dt.date, trading_bar: Bar, n: int = 200) -> list[Bar]:
    bars = []
    for i in range(n - 1):
        day = up_to_session - dt.timedelta(days=(n - 1 - i))
        price = 90.0 + i * 0.01
        bars.append(
            Bar(symbol=symbol, session=day, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=500_000.0)
        )
    bars.append(trading_bar)
    return bars


def _dupe_provider(sessions: list[dt.date]) -> DictContextProvider:
    """Flat-at-100 bars for several sessions (so the below-market limit order
    from ``_AlwaysBelowMarketLongStrategy`` never fills and the strategy
    re-proposes the same symbol every session), then a dip low enough to
    fill it, then a flat tail so the resulting position survives to
    force-close instead of exiting on a stop/target this fixture doesn't
    need to control.
    """
    provider = DictContextProvider()
    security = SecurityInfo(symbol=SYMBOL, name="Dupe Corp", exchange="NASDAQ", sector="Technology")
    provider.add_security(security)

    for i, session in enumerate(sessions):
        if i < len(sessions) - 2:
            trading_bar = _bar(SYMBOL, session, 100.0)
        elif i == len(sessions) - 2:
            # Dips to 88, which is inside every previously-queued order's
            # [price-12, price-10] = [88, 90] limit band -- whether there is
            # one working order or several duplicates, this bar is where the
            # bug (or its absence) becomes visible.
            trading_bar = Bar(symbol=SYMBOL, session=session, open=95.0, high=95.0, low=88.0, close=90.0, volume=1_000_000.0)
        else:
            trading_bar = _bar(SYMBOL, session, 90.0)
        provider.add_bar(trading_bar)

        regime = RegimeState(session=session, regime=MarketRegime.BULL_QUIET, trend_score=0.5, long_short_bias=0.2)
        provider.add_regime(regime)

        ctx = StrategyContext(
            session=session,
            symbol=SYMBOL,
            bars=_history_bars(SYMBOL, session, trading_bar),
            features=_qualifying_features(),
            security=security,
            regime=regime,
        )
        provider.add_context(ctx)

    return provider


def _sessions(n: int, start: dt.date = dt.date(2024, 1, 2)) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


class TestNoOrphanedPositions:
    """A symbol that keeps qualifying for a signal while its entry order is
    still working (unfilled) must only ever have one order queued for it --
    the exact scenario that produced the ~$112k equity/trades divergence."""

    def test_repeated_signals_before_fill_do_not_orphan_a_position(self):
        from claudetrade.config import CostModelConfig
        from claudetrade.signals.engine import SignalEngine

        cfg = AppConfig(
            backtest=BacktestConfig(
                entry_reference="next_open_limit",  # -> "limit" order type
                initial_capital_usd=100_000.0,
                force_close_open_positions=True,
                random_seed=42,
            ),
            costs=CostModelConfig(),
        )
        strategy = _AlwaysBelowMarketLongStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        # 6 sessions: flat at 100 for sessions 0-3 (the order queued on
        # session 0 stays unfilled and, by default, unexpired -- signal_
        # expiry_days=5 -- through session 4's dip), dip at session 4, flat
        # tail at session 5.
        provider = _dupe_provider(_sessions(6))

        result = engine.run(provider)

        # The strategy proposes DUPE on (almost) every session while its
        # order is unfilled -- proof the "re-propose while working" scenario
        # actually happened in this fixture.
        assert result.funnel.signals_generated >= 3

        # The core regression: exactly one order was ever queued for DUPE
        # (the dedup guard), it filled exactly once, and it produced exactly
        # one trade -- never more than one open position silently swallowed.
        assert result.funnel.orders_queued == 1
        assert result.funnel.entries_filled == 1
        assert len(result.trades) == 1
        assert result.funnel.entries_filled == len(result.trades)

        # And the ledger reconciles: no position was ever orphaned, so the
        # final equity-curve mark is within a dollar or two of cash +
        # trade net P&L (the last mark, taken mid-session before the
        # end-of-run force-close applies its own small execution costs, is
        # not byte-identical to the post-close figure -- that gap is normal
        # cost/rounding noise, not the ~$112k-scale divergence this test
        # guards against).
        assert result.equity_curve
        expected = cfg.backtest.initial_capital_usd + sum(t.net_pnl for t in result.trades)
        assert result.equity_curve[-1].equity == pytest.approx(expected, abs=2.0)
