"""Tests for the backtest engine's rejection funnel and pipeline integrity.

ADR-0007 Decision 3(b): every run reports per-stage rejection counts so a
0-trade result is always attributable, and a regression test with a
deliberately-triggering fixture asserts the pipeline still produces trades at
all -- the "silent pipeline break" canary. The fixture strategies here are
local test doubles, not the production strategies in ``claudetrade.strategies``
(those are calibrated separately under ADR-0007 Decision 2); this keeps the
canary anchored to the engine/portfolio/execution wiring it exists to guard,
not to strategy threshold tuning.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.backtest.engine import BacktestEngine, DictContextProvider, RejectionFunnel
from claudetrade.config import AppConfig, BacktestConfig
from claudetrade.domain import Bar, Direction, MarketRegime, RegimeState, SecurityInfo
from claudetrade.signals.engine import SignalEngine
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal

SYMBOL = "ZZZZ"


class AlwaysLongStrategy(Strategy):
    """Test double: proposes the identical, comfortably-qualifying long trade
    every time it is asked, on every symbol/session. Deliberately trivial --
    the point of the canary test below is exercising the engine/portfolio/
    execution wiring, not strategy calibration logic (see module docstring).
    """

    name = "always_long_stub"
    version = "test"
    description = "Always proposes a long entry; used only in engine tests."
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
            entry_low=price - 1.0,
            entry_high=price + 1.0,
            stop_loss=price - 5.0,
            targets=[price + 13.0],
            target_fractions=[1.0],
            expected_holding_days=5,
            time_stop_days=30,
            setup_score=90.0,
        )


class AlwaysDeclineStrategy(Strategy):
    """Test double: always declines with a fixed reason, for funnel tests."""

    name = "always_decline_stub"
    version = "test"
    direction_bias = Direction.LONG
    min_history_bars = 1
    requires_sentiment = False

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        self.decline(ctx, "test_never_interested", "always declines")
        return None


def _history_bars(symbol: str, up_to_session: dt.date, trading_bar: Bar, n: int = 200) -> list[Bar]:
    """A run of ``n`` synthetic past bars ending at ``trading_bar``.

    Only the *count* and the last date matter here: ``score_candidate``'s
    ``_data_confidence_score`` penalises a context with fewer than 200 bars of
    history, and every indicator-derived score below is overridden directly
    via ``features`` rather than computed from these prices.
    """
    bars = []
    for i in range(n - 1):
        day = up_to_session - dt.timedelta(days=(n - 1 - i))
        price = 90.0 + i * 0.01
        bars.append(
            Bar(symbol=symbol, session=day, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=500_000.0)
        )
    bars.append(trading_bar)
    return bars


def _qualifying_features() -> dict[str, float]:
    """Feature values engineered to clear every hard gate and score threshold
    in ``claudetrade.signals.scoring`` with a comfortable margin, so the
    canary test isn't a coin flip on scoring internals it isn't testing."""
    return {
        "avg_dollar_volume_20": 50_000_000.0,
        "atr_pct": 3.0,
        "roc_10": 10.0,
        "roc_20": 10.0,
        "rs_percentile": 90.0,
        "rel_volume_20": 2.0,
        "obv_slope_10": 1.0,
    }


def _build_provider(
    sessions: list[dt.date],
    *,
    symbol: str = SYMBOL,
    with_context: bool = True,
) -> DictContextProvider:
    """A single-symbol fixture: a signal-day bar, then a flat run of bars that
    touch neither the stop nor the target, so a filled position survives to
    ``force_close_open_positions`` at the end of the run rather than exiting
    on a stop/target/time-stop whose exact timing this fixture doesn't need
    to control.
    """
    provider = DictContextProvider()
    security = SecurityInfo(symbol=symbol, name="Zzz Corp", exchange="NASDAQ", sector="Technology")
    provider.add_security(security)

    for i, session in enumerate(sessions):
        if i == 0:
            trading_bar = Bar(symbol=symbol, session=session, open=99.5, high=100.5, low=99.0, close=100.0, volume=1_000_000.0)
        elif i == 1:
            trading_bar = Bar(symbol=symbol, session=session, open=100.2, high=101.0, low=100.0, close=100.8, volume=1_000_000.0)
        else:
            trading_bar = Bar(symbol=symbol, session=session, open=101.0, high=101.0, low=101.0, close=101.0, volume=1_000_000.0)
        provider.add_bar(trading_bar)

        regime = RegimeState(
            session=session, regime=MarketRegime.BULL_QUIET, trend_score=0.5, long_short_bias=0.2
        )
        provider.add_regime(regime)

        if with_context:
            ctx = StrategyContext(
                session=session,
                symbol=symbol,
                bars=_history_bars(symbol, session, trading_bar),
                features=_qualifying_features(),
                security=security,
                regime=regime,
            )
            provider.add_context(ctx)

    return provider


def _sessions(n: int, start: dt.date = dt.date(2024, 1, 2)) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


def _config() -> AppConfig:
    return AppConfig(
        backtest=BacktestConfig(
            entry_reference="next_open",
            initial_capital_usd=100_000.0,
            force_close_open_positions=True,
            random_seed=42,
        )
    )


class TestCanaryProducesTrades:
    """ADR-0007 Decision 3(b): the 'silent pipeline break' canary.

    A fixture engineered to trigger at least one entry must actually produce
    a completed trade. If this ever regresses to 0 trades, something in the
    context/signal/execution wiring silently broke -- not the strategies,
    since ``AlwaysLongStrategy`` above has no discretion at all.
    """

    def test_engineered_fixture_produces_at_least_one_trade(self):
        cfg = _config()
        strategy = AlwaysLongStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(6))

        result = engine.run(provider)

        assert len(result.trades) > 0, (
            f"expected >0 trades from an engineered always-qualifying fixture; got 0. "
            f"Funnel: {result.funnel.summary_lines()}"
        )
        assert result.funnel.signals_generated > 0
        assert result.funnel.entries_filled > 0
        assert result.metrics["trade_count"] == len(result.trades)


class TestRejectionFunnel:
    """ADR-0007 Decision 3(b): funnel counts are populated and attributable."""

    def test_zero_trade_run_has_populated_no_context_bucket(self):
        """A provider offering bars but never a context: every candidate is
        attributable to `no_context`, not silence."""
        cfg = _config()
        strategy = AlwaysLongStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(4), with_context=False)

        result = engine.run(provider)

        assert len(result.trades) == 0
        assert result.funnel.universe_candidates == 4  # 1 symbol x 4 sessions
        assert result.funnel.no_context == 4
        assert result.funnel.signals_generated == 0
        assert result.funnel.universe_filtered_symbols == 1  # never produced a usable context
        # A 0-trade result is still reported, funnel included.
        assert isinstance(result.funnel, RejectionFunnel)

    def test_strategy_declines_tracked_by_reason(self):
        """Strategy.decline() calls are drained and aggregated by reason."""
        cfg = _config()
        strategy = AlwaysDeclineStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(4))

        result = engine.run(provider)

        assert len(result.trades) == 0
        assert result.funnel.signals_generated == 0
        declined = result.funnel.strategy_declined.get("always_decline_stub", {})
        assert declined.get("test_never_interested") == 4
        assert result.funnel.strategy_decline_total() == 4

    def test_funnel_counts_sum_sensibly(self):
        """Every universe candidate is accounted for by no_context or an evaluated context,
        and every generated signal either fills, is rejected downstream, or is
        still queued/expired at run end -- no candidate silently vanishes."""
        cfg = _config()
        strategy = AlwaysLongStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(6))

        result = engine.run(provider)
        funnel = result.funnel

        evaluated_with_context = funnel.universe_candidates - funnel.no_context
        assert evaluated_with_context >= 0
        assert funnel.no_context == 0  # every session in this fixture has a context

        order_outcomes = funnel.entries_filled + funnel.entries_expired_unfilled + funnel.entries_carried_to_end
        assert order_outcomes == funnel.orders_queued
        assert funnel.signals_generated >= funnel.orders_queued
        assert funnel.entries_filled == len(result.trades)
        # Every count is non-negative -- a funnel with a negative bucket would
        # itself be a bug in the accounting, not a real outcome.
        for value in (
            funnel.universe_candidates,
            funnel.universe_filtered_symbols,
            funnel.no_context,
            funnel.strategy_errors,
            funnel.gate_rejected,
            funnel.score_rejected,
            funnel.sizing_zero,
            funnel.limits_rejected,
            funnel.signals_generated,
            funnel.orders_queued,
            funnel.entries_filled,
            funnel.entries_expired_unfilled,
            funnel.entries_carried_to_end,
        ):
            assert value >= 0

    def test_funnel_present_even_with_zero_sessions_of_trading(self):
        """funnel.summary_lines() never raises, even on an empty funnel."""
        funnel = RejectionFunnel()
        lines = funnel.summary_lines()
        assert isinstance(lines, list)
        assert any("Universe candidates" in line for line in lines)


class TestCliFunnelRendering:
    """ADR-0007 Decision 3(b): the CLI backtest report renders the funnel,
    and does so without crashing on the exact 0-trade case it exists for
    (metrics.sharpe/sortino are None there -- see backtest/reporting.py)."""

    def test_zero_trade_result_renders_without_crashing(self):
        from claudetrade.backtest.reporting import render_markdown_report
        from claudetrade.cli import _render_funnel_report

        cfg = _config()
        strategy = AlwaysDeclineStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(4))

        result = engine.run(provider)
        assert not result.trades

        markdown = render_markdown_report(result)  # must not raise
        assert "unavailable" in markdown  # Sharpe/Sortino honestly unavailable, not a crash

        funnel_md = _render_funnel_report(result)
        assert "Rejection Funnel" in funnel_md
        assert "0 completed trades" in funnel_md
        # Per-strategy, per-reason detail is in the rendered table, not just a count.
        assert "test_never_interested" in funnel_md
        assert str(result.funnel.strategy_decline_total()) in "".join(result.funnel.summary_lines())

    def test_healthy_result_also_renders_the_funnel(self):
        from claudetrade.cli import _render_funnel_report

        cfg = _config()
        strategy = AlwaysLongStrategy(cfg)
        signal_engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        engine = BacktestEngine(cfg, signal_engine=signal_engine)
        provider = _build_provider(_sessions(6))

        result = engine.run(provider)
        assert result.trades

        funnel_md = _render_funnel_report(result)
        assert "Rejection Funnel" in funnel_md
        assert "0 completed trades" not in funnel_md
