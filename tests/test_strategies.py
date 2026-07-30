"""Tests for the five score-accumulation strategies (ADR-0007 Decision 2).

Each strategy class gets three kinds of coverage:

1. A fixture engineered to satisfy its setup produces a proposal with a
   coherent entry/stop/target (``StrategyProposal.validate()`` passes and the
   stop/targets sit on the correct side of the entry zone).
2. Every hard veto still declines outright, with a ``StrategyRejection``
   recorded via ``decline()``.
3. A near-miss (a candidate that clears every veto but falls short on score)
   still declines, and the decline's detail carries the full component
   breakdown -- the visibility requirement from ADR-0007 Decision 2 point 4.

Fixtures build ``StrategyContext`` directly with an explicit ``features``
dict rather than running the full ``FeatureBuilder`` pipeline over hundreds of
bars -- the strategies only ever read features through ``ctx.feature()``, so
supplying the values directly is a faithful, much cheaper way to hit a
specific scoring branch precisely.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.config import AppConfig
from claudetrade.domain import (
    Bar,
    Direction,
    EarningsEvent,
    EarningsSession,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.strategies.a_sentiment_breakout import SentimentBreakoutStrategy
from claudetrade.strategies.b_sentiment_pullback import SentimentPullbackStrategy
from claudetrade.strategies.base import StrategyContext
from claudetrade.strategies.c_capitulation_reversal import CapitulationReversalStrategy
from claudetrade.strategies.d_hype_failure_short import HypeFailureShortStrategy
from claudetrade.strategies.e_post_earnings_drift import PostEarningsDriftStrategy

SESSION = dt.date(2024, 3, 15)
SYMBOL = "TEST"


def make_bars(n: int, *, session: dt.date = SESSION, close: float = 100.0) -> list[Bar]:
    """``n`` ascending daily bars (business days) ending on ``close`` at ``session``.

    Flat/quiet by construction -- individual tests override whichever
    ``ctx.features`` values their scenario actually depends on, so the bar
    shapes themselves only need to supply a plausible last-bar OHLC and
    satisfy ``len(ctx.bars) >= min_history_bars``.
    """
    bars: list[Bar] = []
    day = session
    for _ in range(n):
        bars.append(
            Bar(
                symbol=SYMBOL,
                session=day,
                open=close * 0.995,
                high=close * 1.01,
                low=close * 0.985,
                close=close,
                volume=1_000_000,
                adj_close=close,
            )
        )
        day -= dt.timedelta(days=1)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
    bars.reverse()
    return bars


def make_sentiment(
    *,
    session: dt.date = SESSION,
    post_count: int = 50,
    unique_authors: int = 20,
    raw_sentiment: float = 0.3,
    sentiment_acceleration: float = 0.4,
    mention_acceleration: float = 0.5,
    manipulation_risk: float = 0.1,
    hype: float = 0.3,
    fear: float = 0.2,
    capitulation: float = 0.0,
    catalyst_quality: float = 0.2,
    dispersion: float = 0.1,
    confidence: float = 0.5,
    labels: dict[str, float] | None = None,
) -> SymbolSentiment:
    return SymbolSentiment(
        symbol=SYMBOL,
        session=session,
        post_count=post_count,
        unique_authors=unique_authors,
        raw_sentiment=raw_sentiment,
        sentiment_acceleration=sentiment_acceleration,
        mention_acceleration=mention_acceleration,
        manipulation_risk=manipulation_risk,
        hype=hype,
        fear=fear,
        capitulation=capitulation,
        catalyst_quality=catalyst_quality,
        dispersion=dispersion,
        confidence=confidence,
        labels=labels or {},
    )


def make_sentiment_history(
    *,
    n: int = 90,
    session: dt.date = SESSION,
    quiet_accel: float = 0.03,
    quiet_mention: float = 0.03,
    final: SymbolSentiment | None = None,
) -> list[SymbolSentiment]:
    """History of quiet readings, ending on ``final`` (today's snapshot).

    Gives the on-the-fly percentile helper (``scoring_utils.percentile_rank``)
    a real trailing distribution to rank ``final`` against.
    """
    history: list[SymbolSentiment] = []
    day = session - dt.timedelta(days=n)
    for _ in range(n - 1):
        day += dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        history.append(
            SymbolSentiment(
                symbol=SYMBOL,
                session=day,
                post_count=30,
                unique_authors=10,
                sentiment_acceleration=quiet_accel,
                mention_acceleration=quiet_mention,
            )
        )
    history.append(final if final is not None else make_sentiment(session=session))
    return history


def make_context(
    *,
    config: AppConfig,
    bars: list[Bar],
    features: dict[str, float],
    sentiment: SymbolSentiment | None = None,
    sentiment_history: list[SymbolSentiment] | None = None,
    earnings: list[EarningsEvent] | None = None,
    security: SecurityInfo | None = None,
    session: dt.date = SESSION,
) -> StrategyContext:
    return StrategyContext(
        session=session,
        symbol=SYMBOL,
        bars=bars,
        features=features,
        security=security or SecurityInfo(symbol=SYMBOL, sector="Technology", market_cap_usd=5e9),
        regime=None,
        sentiment=sentiment,
        sentiment_history=sentiment_history or [],
        earnings=earnings or [],
        config=config,
    )


@pytest.fixture
def cfg(tmp_app_config: AppConfig) -> AppConfig:
    return tmp_app_config


# --------------------------------------------------------------------------
# Strategy A -- sentiment_breakout
# --------------------------------------------------------------------------


class TestSentimentBreakout:
    def _good_features(self) -> dict[str, float]:
        return {
            "atr_14": 2.0,
            "resistance_level": 100.0,
            "donchian_high_20": 100.0,
            "sma_50": 95.0,
            "dist_from_sma50_pct": 6.0,
            "adx_14": 30.0,
            "adx_pctl_120": 0.92,
            "rel_volume_20": 2.6,
            "rel_volume_pctl_120": 0.95,
            "rs_percentile": 88.0,
            "avg_dollar_volume_20": 50_000_000,
            "support_level": 90.0,
            "dist_from_52w_high_pct": -1.0,
        }

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        bars = make_bars(90, close=101.5)
        sentiment = make_sentiment(sentiment_acceleration=0.4, mention_acceleration=0.5)
        history = make_sentiment_history(final=sentiment)
        ctx = make_context(
            config=cfg,
            bars=bars,
            features=self._good_features(),
            sentiment=sentiment,
            sentiment_history=history,
        )
        strategy = SentimentBreakoutStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is not None
        proposal.validate()  # raises on an incoherent trade
        assert proposal.direction is Direction.LONG
        assert proposal.entry_low <= proposal.entry_high
        assert proposal.stop_loss < proposal.entry_low
        assert proposal.targets[-1] > proposal.entry_high
        assert proposal.reward_risk_ratio > 0
        assert 0.0 <= proposal.setup_score <= 100.0
        assert proposal.setup_score >= cfg.calibration.proposal_score_threshold

    def test_insufficient_history_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(10, close=101.5)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features())
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        rejections = strategy.drain_rejections()
        assert any(r.reason == "insufficient_history" for r in rejections)

    def test_earnings_window_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(90, close=101.5)
        earnings = [
            EarningsEvent(
                symbol=SYMBOL,
                report_date=SESSION + dt.timedelta(days=1),
                session=EarningsSession.AFTER_CLOSE,
                confirmed=True,
            )
        ]
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), earnings=earnings)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        rejections = strategy.drain_rejections()
        assert any(r.reason == "earnings_window" for r in rejections)

    def test_illiquid_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(90, close=101.5)
        features = self._good_features()
        features["avg_dollar_volume_20"] = 1_000.0  # far below the filter floor
        ctx = make_context(config=cfg, bars=bars, features=features)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        rejections = strategy.drain_rejections()
        assert any(r.reason == "illiquid" for r in rejections)

    def test_manipulation_risk_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(90, close=101.5)
        sentiment = make_sentiment(manipulation_risk=0.95)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        rejections = strategy.drain_rejections()
        assert any(r.reason == "manipulation_risk" for r in rejections)

    def test_near_miss_decline_carries_score_breakdown(self, cfg: AppConfig):
        """A candidate that clears every veto but falls short on score still
        declines, and the decline's detail names the individual components
        that were weak -- not just a bare "no" (ADR-0007 Decision 2, point 4).
        """
        # Price sits below the level (no breakout yet, but within the
        # not-near-breakout veto's tolerance) and every other axis is weak.
        bars = make_bars(90, close=99.0)
        features = self._good_features()
        features.update(
            {
                "adx_14": 10.0,
                "adx_pctl_120": 0.1,
                "rel_volume_20": 0.8,
                "rel_volume_pctl_120": 0.1,
                "rs_percentile": 40.0,
                "dist_from_sma50_pct": -3.5,
            }
        )
        ctx = make_context(config=cfg, bars=bars, features=features)
        strategy = SentimentBreakoutStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is None
        rejections = strategy.drain_rejections()
        assert len(rejections) == 1
        rejection = rejections[0]
        assert rejection.reason == "score_below_threshold"
        assert "score" in rejection.detail
        assert "breakout=" in rejection.detail
        assert "trend_strength_pctl=" in rejection.detail
        assert "volume_pctl=" in rejection.detail


# --------------------------------------------------------------------------
# Strategy B -- sentiment_pullback
# --------------------------------------------------------------------------


class TestSentimentPullback:
    def _good_features(self) -> dict[str, float]:
        return {
            "atr_14": 2.0,
            "sma_20": 98.0,
            "sma_50": 95.0,
            "sma_200": 85.0,
            "adx_14": 28.0,
            "adx_pctl_120": 0.9,
            "hh_hl_score": 0.3,
            "donchian_high_20": 105.0,
            "rel_volume_20": 0.7,
            "rel_volume_pctl_120": 0.2,
            "rsi_14": 45.0,
            "rsi_pctl_120": 0.4,
            "dist_from_sma20_pct": 1.5,
            "dist_from_sma50_pct": 4.5,
            "avg_dollar_volume_20": 50_000_000,
            "support_level": 92.0,
            "swing_low_recent": 93.0,
        }

    def _bars_with_confirmation(self) -> list[Bar]:
        bars = make_bars(100, close=99.0)
        # Force a clean up-close confirmation bar as bars[-1] over bars[-2].
        bars[-2] = Bar(SYMBOL, bars[-2].session, 98.0, 98.5, 96.5, 97.0, 1_000_000, 97.0)
        bars[-1] = Bar(SYMBOL, bars[-1].session, 97.5, 100.0, 97.3, 99.5, 1_000_000, 99.5)
        return bars

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        bars = self._bars_with_confirmation()
        sentiment = make_sentiment(raw_sentiment=0.2, sentiment_acceleration=0.05)
        history = make_sentiment_history(final=sentiment)
        ctx = make_context(
            config=cfg,
            bars=bars,
            features=self._good_features(),
            sentiment=sentiment,
            sentiment_history=history,
        )
        strategy = SentimentPullbackStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is not None
        proposal.validate()
        assert proposal.direction is Direction.LONG
        assert proposal.stop_loss < proposal.entry_low <= proposal.entry_high
        assert proposal.targets[-1] > proposal.entry_high
        assert proposal.setup_score >= cfg.calibration.proposal_score_threshold

    def test_insufficient_history_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(10, close=99.0)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features())
        strategy = SentimentPullbackStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "insufficient_history" for r in strategy.drain_rejections())

    def test_illiquid_is_hard_veto(self, cfg: AppConfig):
        bars = self._bars_with_confirmation()
        features = self._good_features()
        features["avg_dollar_volume_20"] = 500.0
        ctx = make_context(config=cfg, bars=bars, features=features)
        strategy = SentimentPullbackStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "illiquid" for r in strategy.drain_rejections())

    def test_manipulation_risk_is_hard_veto(self, cfg: AppConfig):
        bars = self._bars_with_confirmation()
        sentiment = make_sentiment(manipulation_risk=0.9)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment)
        strategy = SentimentPullbackStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "manipulation_risk" for r in strategy.drain_rejections())

    def test_near_miss_decline_carries_score_breakdown(self, cfg: AppConfig):
        bars = make_bars(100, close=99.0)
        # A down-close final bar: no up-close confirmation.
        last = bars[-1]
        bars[-1] = Bar(SYMBOL, last.session, 99.5, 99.6, 97.5, 97.8, last.volume, 97.8)
        # Weak on every axis: no uptrend, soft trend strength, broken
        # structure, shallow pullback, heavy (not quiet) down-volume, RSI
        # outside the reset band, far from any moving average.
        features = self._good_features()
        features.update(
            {
                "sma_20": 94.0,
                "sma_50": 98.0,
                "sma_200": 110.0,
                "adx_14": 12.0,
                "adx_pctl_120": 0.1,
                "hh_hl_score": -0.5,
                "donchian_high_20": 98.5,  # price ~98 -> ~0.5% pullback, below the sweet spot
                "rel_volume_20": 1.8,
                "rel_volume_pctl_120": 0.95,
                "rsi_14": 75.0,
                "rsi_pctl_120": 0.97,
                "dist_from_sma20_pct": 9.0,
                "dist_from_sma50_pct": 12.0,
            }
        )
        ctx = make_context(config=cfg, bars=bars, features=features)
        strategy = SentimentPullbackStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is None
        rejections = strategy.drain_rejections()
        assert len(rejections) == 1
        assert rejections[0].reason == "score_below_threshold"
        assert "trend_strength_pctl=" in rejections[0].detail
        assert "price_confirmation=" in rejections[0].detail


# --------------------------------------------------------------------------
# Strategy C -- capitulation_reversal
# --------------------------------------------------------------------------


class TestCapitulationReversal:
    def _good_features(self) -> dict[str, float]:
        return {
            "atr_14": 2.0,
            "sma_20": 92.0,
            "dist_from_sma50_pct": -18.0,
            "dist_sma50_pctl_120": 0.05,
            "rsi_14": 22.0,
            "rsi_pctl_120": 0.05,
            "rel_volume_20": 2.5,
            "rel_volume_pctl_120": 0.95,
            "avg_dollar_volume_20": 50_000_000,
        }

    def _reversal_bars(self, low_close: float = 78.0) -> list[Bar]:
        bars = make_bars(100, close=low_close)
        # prior: washout low; last: reversal bar closing near the top of its range.
        bars[-2] = Bar(SYMBOL, bars[-2].session, 82.0, 82.5, 76.0, 77.0, 3_000_000, 77.0)
        bars[-1] = Bar(SYMBOL, bars[-1].session, 77.5, 81.5, 76.5, 80.8, 3_000_000, 80.8)
        return bars

    def _good_sentiment(self) -> SymbolSentiment:
        return make_sentiment(
            raw_sentiment=-0.4,
            sentiment_acceleration=0.1,
            mention_acceleration=0.5,
            manipulation_risk=0.1,
            capitulation=0.6,
            fear=0.7,
        )

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        bars = self._reversal_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        ctx = make_context(
            config=cfg,
            bars=bars,
            features=self._good_features(),
            sentiment=sentiment,
            sentiment_history=history,
        )
        strategy = CapitulationReversalStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is not None
        proposal.validate()
        assert proposal.direction is Direction.LONG
        assert proposal.stop_loss < proposal.entry_low <= proposal.entry_high
        assert proposal.extras["size_multiplier"] == CapitulationReversalStrategy.SIZE_MULTIPLIER
        assert proposal.setup_score >= cfg.calibration.proposal_score_threshold

    def test_insufficient_history_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(10, close=78.0)
        sentiment = self._good_sentiment()
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment)
        strategy = CapitulationReversalStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "insufficient_history" for r in strategy.drain_rejections())

    def test_sentiment_required_is_hard_veto(self, cfg: AppConfig):
        bars = self._reversal_bars()
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=None)
        strategy = CapitulationReversalStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "sentiment_required" for r in strategy.drain_rejections())

    def test_manipulation_risk_is_hard_veto(self, cfg: AppConfig):
        bars = self._reversal_bars()
        sentiment = self._good_sentiment()
        sentiment.manipulation_risk = 0.95
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment)
        strategy = CapitulationReversalStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "manipulation_risk" for r in strategy.drain_rejections())

    def test_unresolved_catalyst_is_hard_veto(self, cfg: AppConfig):
        bars = self._reversal_bars()
        sentiment = self._good_sentiment()
        sentiment.labels = {"regulatory_catalyst": 0.9}
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment)
        strategy = CapitulationReversalStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "unresolved_catalyst" for r in strategy.drain_rejections())

    def test_near_miss_decline_carries_score_breakdown(self, cfg: AppConfig):
        bars = make_bars(100, close=99.0)  # not stretched, no reversal bar engineered
        features = self._good_features()
        features.update(
            {
                "dist_from_sma50_pct": -2.0,
                "dist_sma50_pctl_120": 0.7,
                "rsi_14": 55.0,
                "rsi_pctl_120": 0.6,
                "rel_volume_20": 0.9,
                "rel_volume_pctl_120": 0.3,
            }
        )
        sentiment = make_sentiment(
            raw_sentiment=0.1, mention_acceleration=0.05, capitulation=0.05, manipulation_risk=0.1
        )
        ctx = make_context(config=cfg, bars=bars, features=features, sentiment=sentiment)
        strategy = CapitulationReversalStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is None
        rejections = strategy.drain_rejections()
        assert len(rejections) == 1
        assert rejections[0].reason == "score_below_threshold"
        assert "extension_below_sma50_pctl=" in rejections[0].detail
        assert "climax_volume_pctl=" in rejections[0].detail


# --------------------------------------------------------------------------
# Strategy D -- hype_failure_short
# --------------------------------------------------------------------------


class TestHypeFailureShort:
    def _good_features(self) -> dict[str, float]:
        return {
            "atr_14": 2.0,
            "sma_20": 118.0,
            "ema_9": 122.0,
            "roc_20": 40.0,
            "roc_20_pctl_120": 0.95,
            "donchian_high_20": 130.0,
            "failed_breakout": 1.0,
            "avg_dollar_volume_20": 50_000_000,
        }

    def _failure_bars(self) -> list[Bar]:
        bars = make_bars(90, close=126.0)
        bars[-2] = Bar(SYMBOL, bars[-2].session, 128.0, 130.0, 126.0, 129.0, 2_000_000, 129.0)
        bars[-1] = Bar(SYMBOL, bars[-1].session, 128.5, 128.8, 123.0, 124.0, 2_000_000, 124.0)
        return bars

    def _good_security(self) -> SecurityInfo:
        return SecurityInfo(symbol=SYMBOL, sector="Technology", market_cap_usd=1e9)

    def _good_sentiment(self) -> SymbolSentiment:
        return make_sentiment(
            sentiment_acceleration=0.55,
            mention_acceleration=0.4,
            manipulation_risk=0.6,
            hype=0.75,
            catalyst_quality=0.1,
        )

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        ctx = make_context(
            config=cfg,
            bars=bars,
            features=self._good_features(),
            sentiment=sentiment,
            sentiment_history=history,
            security=self._good_security(),
        )
        strategy = HypeFailureShortStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is not None
        proposal.validate()
        assert proposal.direction is Direction.SHORT
        assert proposal.stop_loss > proposal.entry_high >= proposal.entry_low
        assert proposal.targets[-1] < proposal.entry_low
        assert proposal.setup_score >= cfg.calibration.proposal_score_threshold

    def test_shorts_disabled_is_hard_veto(self, cfg: AppConfig):
        cfg.signals.allow_shorts = False
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        ctx = make_context(
            config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment,
            security=self._good_security(),
        )
        strategy = HypeFailureShortStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "shorts_disabled" for r in strategy.drain_rejections())

    def test_sentiment_required_is_hard_veto(self, cfg: AppConfig):
        bars = self._failure_bars()
        ctx = make_context(
            config=cfg, bars=bars, features=self._good_features(), sentiment=None,
            security=self._good_security(),
        )
        strategy = HypeFailureShortStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "sentiment_required" for r in strategy.drain_rejections())

    def test_borrow_unrealistic_is_hard_veto(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        small_cap = SecurityInfo(symbol=SYMBOL, sector="Technology", market_cap_usd=1e6)
        ctx = make_context(
            config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment, security=small_cap
        )
        strategy = HypeFailureShortStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "borrow_unrealistic" for r in strategy.drain_rejections())

    def test_organic_move_is_hard_veto(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        sentiment.manipulation_risk = 0.05  # looks genuine, not promoted
        ctx = make_context(
            config=cfg, bars=bars, features=self._good_features(), sentiment=sentiment,
            security=self._good_security(),
        )
        strategy = HypeFailureShortStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "organic_move" for r in strategy.drain_rejections())

    def test_near_miss_decline_carries_score_breakdown(self, cfg: AppConfig):
        bars = make_bars(90, close=126.0)  # no failure/bearish bars engineered
        features = self._good_features()
        features.update({"roc_20": 5.0, "roc_20_pctl_120": 0.3, "failed_breakout": 0.0})
        sentiment = self._good_sentiment()
        sentiment.sentiment_acceleration = 0.1
        sentiment.hype = 0.2
        ctx = make_context(
            config=cfg, bars=bars, features=features, sentiment=sentiment, security=self._good_security()
        )
        strategy = HypeFailureShortStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is None
        rejections = strategy.drain_rejections()
        assert len(rejections) == 1
        assert rejections[0].reason == "score_below_threshold"
        assert "advance_speed_pctl=" in rejections[0].detail
        assert "sentiment_spike_pctl=" in rejections[0].detail


# --------------------------------------------------------------------------
# Strategy E -- post_earnings_drift
# --------------------------------------------------------------------------


class TestPostEarningsDrift:
    def _bars_with_event(self, *, up: bool = True, move_pct: float = 8.0) -> tuple[list[Bar], dt.date]:
        """Bars with an event bar 5 sessions before ``SESSION``.

        ``report_date`` is read back off the actual bar at that list
        position rather than computed independently, so it is always a real
        trading session already present in ``bars`` -- an
        independently-computed calendar offset can land on a weekend and
        silently desynchronise the event bar from the earnings record.
        """
        bars = make_bars(90, close=100.0)
        event_index = len(bars) - 6
        report_date = bars[event_index].session
        prior_close = bars[event_index - 1].close
        move = 1.0 + (move_pct / 100.0 if up else -move_pct / 100.0)
        event_close = prior_close * move
        bars[event_index] = Bar(
            SYMBOL, report_date, prior_close, event_close * 1.01, prior_close * 0.99,
            event_close, 4_000_000, event_close,
        )
        # Carry the post-event price forward, holding the move.
        for i in range(event_index + 1, len(bars)):
            level = event_close * (1.01 if up else 0.99)
            bars[i] = Bar(
                SYMBOL, bars[i].session, level, level * 1.01, level * 0.99, level, 1_000_000, level
            )
        return bars, report_date

    def _good_features(self) -> dict[str, float]:
        return {"atr_14": 2.0, "ema_9": 106.0, "avg_dollar_volume_20": 50_000_000}

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        bars, report_date = self._bars_with_event(up=True)
        earnings = [
            EarningsEvent(
                symbol=SYMBOL, report_date=report_date, confirmed=True, surprise_pct=12.0
            )
        ]
        features = self._good_features()
        features["ema_9"] = bars[-1].close * 0.97
        ctx = make_context(config=cfg, bars=bars, features=features, earnings=earnings)
        strategy = PostEarningsDriftStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is not None
        proposal.validate()
        assert proposal.direction is Direction.LONG
        assert proposal.stop_loss < proposal.entry_low <= proposal.entry_high
        assert proposal.setup_score >= cfg.calibration.proposal_score_threshold

    def test_insufficient_history_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(10, close=100.0)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features())
        strategy = PostEarningsDriftStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "insufficient_history" for r in strategy.drain_rejections())

    def test_no_prior_earnings_is_hard_veto(self, cfg: AppConfig):
        bars = make_bars(90, close=100.0)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), earnings=[])
        strategy = PostEarningsDriftStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "no_prior_earnings" for r in strategy.drain_rejections())

    def test_reaction_contradicts_surprise_is_hard_veto(self, cfg: AppConfig):
        # Positive surprise, but the event bar reaction (built ``up=False``) is negative.
        bars, report_date = self._bars_with_event(up=False)
        earnings = [
            EarningsEvent(symbol=SYMBOL, report_date=report_date, confirmed=True, surprise_pct=15.0)
        ]
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), earnings=earnings)
        strategy = PostEarningsDriftStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "reaction_contradicts_surprise" for r in strategy.drain_rejections())

    def test_next_earnings_too_close_is_hard_veto(self, cfg: AppConfig):
        bars, report_date = self._bars_with_event(up=True)
        earnings = [
            EarningsEvent(symbol=SYMBOL, report_date=report_date, confirmed=True, surprise_pct=12.0),
            EarningsEvent(
                symbol=SYMBOL, report_date=SESSION + dt.timedelta(days=1), confirmed=True
            ),
        ]
        ctx = make_context(config=cfg, bars=bars, features=self._good_features(), earnings=earnings)
        strategy = PostEarningsDriftStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert any(r.reason == "earnings_window" for r in strategy.drain_rejections())

    def test_near_miss_decline_carries_score_breakdown(self, cfg: AppConfig):
        # A small, unremarkable surprise/reaction that ranks low against a
        # trailing history of much bigger surprises -- clears every veto but
        # scores weakly on every axis.
        bars, report_date = self._bars_with_event(up=True, move_pct=1.6)
        # Force the last bar's range far beyond its ATR, so volatility has
        # manifestly not settled.
        last = bars[-1]
        bars[-1] = Bar(
            SYMBOL, last.session, last.open, last.close * 1.5, last.close * 0.5,
            last.close, last.volume, last.close,
        )
        earnings = [
            EarningsEvent(
                symbol=SYMBOL,
                report_date=report_date - dt.timedelta(days=280),
                confirmed=True,
                surprise_pct=22.0,
            ),
            EarningsEvent(
                symbol=SYMBOL,
                report_date=report_date - dt.timedelta(days=90),
                confirmed=True,
                surprise_pct=28.0,
            ),
            EarningsEvent(symbol=SYMBOL, report_date=report_date, confirmed=True, surprise_pct=3.2),
        ]
        features = self._good_features()
        features["ema_9"] = bars[-1].close * 1.05  # price not holding above the average
        ctx = make_context(config=cfg, bars=bars, features=features, earnings=earnings)
        strategy = PostEarningsDriftStrategy(cfg)

        proposal = strategy.evaluate(ctx)

        assert proposal is None
        rejections = strategy.drain_rejections()
        assert len(rejections) == 1
        assert rejections[0].reason == "score_below_threshold"
        assert "surprise_magnitude_pctl=" in rejections[0].detail
        assert "holding_the_move=" in rejections[0].detail
