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
from claudetrade.strategies.f_volume_breakout import VolumeBreakoutStrategy

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
    # Defaults to a positive one-vote-per-author mean alongside the positive
    # raw mean. ``sentiment_breakout`` now requires BOTH to be positive (a
    # raw mean can be carried by one prolific poster), so leaving this at the
    # dataclass default of 0.0 would have made every "healthy sentiment"
    # fixture describe a crowd whose individual members were, on average,
    # neutral -- not the sample these tests mean to build.
    unique_author_sentiment: float = 0.25,
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
        unique_author_sentiment=unique_author_sentiment,
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

    def _confirmed_ctx(
        self,
        cfg: AppConfig,
        features: dict[str, float],
        *,
        close: float = 101.5,
        sentiment: SymbolSentiment | None = None,
    ) -> StrategyContext:
        """A context carrying the positive sentiment confirmation this
        strategy now REQUIRES.

        Before QA #7 these price/volume-focused cases could omit sentiment
        entirely and still expect a proposal, because ``sentiment_breakout``
        declared ``requires_sentiment = False`` and merely lost points when
        the social sample was missing. That was the mislabelling bug itself,
        not an incidental fixture convenience: a strategy named "sentiment
        breakout" was emitting trades with no sentiment behind them. The
        unconfirmed path did not disappear -- it moved to ``volume_breakout``,
        which ``TestVolumeBreakout`` covers -- so the cases below now supply
        the confirmation and assert what they always meant to assert about
        the price/volume components.
        """
        snapshot = sentiment if sentiment is not None else make_sentiment()
        return make_context(
            config=cfg,
            bars=make_bars(90, close=close),
            features=features,
            sentiment=snapshot,
            sentiment_history=make_sentiment_history(final=snapshot),
        )

    def test_satisfied_setup_produces_sensible_proposal(self, cfg: AppConfig):
        sentiment = make_sentiment(sentiment_acceleration=0.4, mention_acceleration=0.5)
        ctx = self._confirmed_ctx(cfg, self._good_features(), sentiment=sentiment)
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
        # Sentiment confirms (positive, adequately sampled) but is flat, so
        # the ONLY thing left to decline on is the score -- which is what
        # this test is about.
        ctx = self._confirmed_ctx(
            cfg,
            features,
            close=99.0,
            sentiment=make_sentiment(sentiment_acceleration=0.0, mention_acceleration=0.0),
        )
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

    # ---- market-signal adoption package: gap / confluence / volume-quality ----

    def test_gap_up_bonus_scales_with_gap_size(self, cfg: AppConfig):
        """A gap_pct at or beyond GAP_UP_FULL_CREDIT_PCT earns full W_GAP_UP
        credit; no gap (the feature absent, defaulting to 0.0) earns none --
        proving both direction and the exact magnitude bound."""
        strategy = SentimentBreakoutStrategy(cfg)

        features_no_gap = self._good_features()
        proposal_no_gap = strategy.evaluate(self._confirmed_ctx(cfg, features_no_gap))
        assert proposal_no_gap is not None
        assert "gap_up=+0.0/6" in proposal_no_gap.extras["score_breakdown"]

        features_gap = self._good_features()
        features_gap["gap_pct"] = 5.0  # well beyond GAP_UP_FULL_CREDIT_PCT (2.5)
        proposal_gap = strategy.evaluate(self._confirmed_ctx(cfg, features_gap))
        assert proposal_gap is not None
        assert f"gap_up=+{SentimentBreakoutStrategy.W_GAP_UP:.1f}/{SentimentBreakoutStrategy.W_GAP_UP:.0f}" in (
            proposal_gap.extras["score_breakdown"]
        )
        assert "Gapped up" in " ".join(proposal_gap.evidence)

    def test_gap_continuation_up_is_boolean_full_credit(self, cfg: AppConfig):
        strategy = SentimentBreakoutStrategy(cfg)

        features_off = self._good_features()
        proposal_off = strategy.evaluate(self._confirmed_ctx(cfg, features_off))
        assert proposal_off is not None
        assert "gap_continuation=+0.0/5" in proposal_off.extras["score_breakdown"]

        features_on = self._good_features()
        features_on["gap_continuation_up"] = 1.0
        proposal_on = strategy.evaluate(self._confirmed_ctx(cfg, features_on))
        assert proposal_on is not None
        assert (
            f"gap_continuation=+{SentimentBreakoutStrategy.W_GAP_CONTINUATION:.1f}"
            f"/{SentimentBreakoutStrategy.W_GAP_CONTINUATION:.0f}"
        ) in proposal_on.extras["score_breakdown"]

    def test_level_confluence_ramps_between_one_and_three_methods(self, cfg: AppConfig):
        strategy = SentimentBreakoutStrategy(cfg)

        # 1 agreeing method (or fewer) earns zero credit -- a single level is
        # not "confluence".
        features_one = self._good_features()
        features_one["level_confluence_count"] = 1.0
        proposal_one = strategy.evaluate(self._confirmed_ctx(cfg, features_one))
        assert proposal_one is not None
        assert "level_confluence=+0.0/6" in proposal_one.extras["score_breakdown"]

        # 3+ agreeing methods earns full credit.
        features_three = self._good_features()
        features_three["level_confluence_count"] = 3.0
        proposal_three = strategy.evaluate(self._confirmed_ctx(cfg, features_three))
        assert proposal_three is not None
        assert "level_confluence=+6.0/6" in proposal_three.extras["score_breakdown"]
        assert "independent methods" in " ".join(proposal_three.evidence)

    def test_volume_divergence_is_a_penalty_not_a_veto(self, cfg: AppConfig):
        """A moderate (non-saturating) scenario so the penalty is visible on
        the clamped setup_score too, not just in the raw breakdown."""
        strategy = SentimentBreakoutStrategy(cfg)
        moderate = self._good_features()
        moderate.update({"adx_pctl_120": 0.5, "rel_volume_pctl_120": 0.55, "rs_percentile": 60.0})

        features_clean = dict(moderate)
        proposal_clean = strategy.evaluate(self._confirmed_ctx(cfg, features_clean))
        assert proposal_clean is not None

        features_divergent = dict(moderate)
        features_divergent["volume_divergence"] = 1.0
        proposal_divergent = strategy.evaluate(self._confirmed_ctx(cfg, features_divergent))
        assert proposal_divergent is not None
        assert (
            f"volume_divergence={SentimentBreakoutStrategy.PENALTY_VOLUME_DIVERGENCE:.1f}"
            in proposal_divergent.extras["score_breakdown"]
        )
        # Direction: strictly lower score. Magnitude bound: never more than
        # the configured penalty.
        assert proposal_divergent.setup_score < proposal_clean.setup_score
        assert proposal_clean.setup_score - proposal_divergent.setup_score <= abs(
            SentimentBreakoutStrategy.PENALTY_VOLUME_DIVERGENCE
        )
        assert any("absorption" in r for r in proposal_divergent.risks)

    def test_new_gap_and_confluence_features_absent_degrades_gracefully(self, cfg: AppConfig):
        """None of the new features are required: a feature frame that
        predates this package (none of gap_pct/gap_continuation_up/
        level_confluence_count/volume_divergence present) must still produce
        a coherent proposal -- degrade to zero contribution, never crash."""
        features = self._good_features()
        for key in (
            "gap_pct",
            "gap_continuation_up",
            "level_confluence_count",
            "volume_divergence",
        ):
            features.pop(key, None)
        strategy = SentimentBreakoutStrategy(cfg)

        proposal = strategy.evaluate(self._confirmed_ctx(cfg, features))

        assert proposal is not None
        proposal.validate()

    # ---- QA #7: the name is a claim, and it is now enforced ------------------

    def test_missing_sentiment_declines_with_a_funnel_reason_code(self, cfg: AppConfig):
        """A strategy named "sentiment breakout" must not recommend a name it
        has no social sample for.

        This deliberately reverses the old expectation. ``requires_sentiment``
        was False and a missing sample only cost the candidate the sentiment
        components' points, so the strategy emitted price-and-volume trades
        under a name that claimed sentiment confirmation -- indistinguishable
        in the ledger and in every per-strategy backtest statistic from one
        that genuinely had it. The candidate is not lost: ``volume_breakout``
        takes it (see ``TestVolumeBreakout``).
        """
        bars = make_bars(90, close=101.5)
        ctx = make_context(config=cfg, bars=bars, features=self._good_features())
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        rejections = strategy.drain_rejections()
        assert [r.reason for r in rejections] == ["sentiment_unavailable"]
        # The funnel aggregates the numbers, not just the count.
        assert rejections[0].metrics["posts_required"] == float(
            cfg.sentiment.min_posts_for_signal
        )

    def test_thin_sample_declines_rather_than_confirming(self, cfg: AppConfig):
        """A sample below the post/author minimums is missing evidence, not a
        quiet confirmation."""
        thin = make_sentiment(post_count=2, unique_authors=1)
        ctx = self._confirmed_ctx(cfg, self._good_features(), sentiment=thin)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert [r.reason for r in strategy.drain_rejections()] == ["sentiment_unavailable"]

    @pytest.mark.parametrize(
        ("raw", "per_author"),
        [
            (-0.4, -0.3),  # both negative
            (-0.4, 0.3),  # raw negative, authors positive
            (0.4, -0.3),  # one prolific bull over a bearish crowd
            (0.01, 0.01),  # positive but below the confirmation floor
        ],
    )
    def test_non_positive_sentiment_declines(self, cfg: AppConfig, raw, per_author):
        """Confirmation requires BOTH polarity measures above the floor.

        The raw decayed mean alone can be carried by one prolific poster --
        exactly the promotion pattern this strategy exists to avoid buying
        into -- and the per-author mean alone ignores a crowd whose loudest
        voices are bearish.
        """
        sentiment = make_sentiment(raw_sentiment=raw, unique_author_sentiment=per_author)
        ctx = self._confirmed_ctx(cfg, self._good_features(), sentiment=sentiment)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert [r.reason for r in strategy.drain_rejections()] == ["sentiment_not_positive"]

    def test_low_confidence_sentiment_declines(self, cfg: AppConfig):
        """Positive but untrustworthy is not confirmation."""
        sentiment = make_sentiment(
            confidence=cfg.filters.min_sentiment_confidence - 0.01
        )
        ctx = self._confirmed_ctx(cfg, self._good_features(), sentiment=sentiment)
        strategy = SentimentBreakoutStrategy(cfg)

        assert strategy.evaluate(ctx) is None
        assert [r.reason for r in strategy.drain_rejections()] == ["sentiment_confidence_low"]

    def test_declares_that_it_requires_sentiment(self, cfg: AppConfig):
        """The engine reads this flag into the scan funnel's
        ``strategy_requirements`` and into ``apply_hard_gates``; it must agree
        with the strategy's actual behaviour."""
        assert SentimentBreakoutStrategy(cfg).requires_sentiment is True


# --------------------------------------------------------------------------
# Strategy F -- volume_breakout (the relabelled unconfirmed path)
# --------------------------------------------------------------------------


class TestVolumeBreakout:
    """The breakout with no confirming social sample, under an honest name.

    Coverage is unchanged by the split -- every candidate ``sentiment_breakout``
    now declines for a sentiment reason is taken here -- so these tests are as
    much about the *partition* being exhaustive and non-overlapping as about
    this strategy's own behaviour.
    """

    def _features(self) -> dict[str, float]:
        return TestSentimentBreakout()._good_features()

    def _ctx(self, cfg: AppConfig, sentiment: SymbolSentiment | None = None) -> StrategyContext:
        return make_context(
            config=cfg,
            bars=make_bars(90, close=101.5),
            features=self._features(),
            sentiment=sentiment,
            sentiment_history=(
                make_sentiment_history(final=sentiment) if sentiment is not None else []
            ),
        )

    def test_takes_the_candidate_sentiment_breakout_declines(self, cfg: AppConfig):
        """No social sample: declined as sentiment_breakout, proposed here."""
        ctx = self._ctx(cfg)

        assert SentimentBreakoutStrategy(cfg).evaluate(ctx) is None

        proposal = VolumeBreakoutStrategy(cfg).evaluate(ctx)
        assert proposal is not None
        proposal.validate()
        assert proposal.strategy == "volume_breakout"
        assert any("no usable social sample" in e.lower() for e in proposal.evidence)
        assert any("No sentiment confirmation" in r for r in proposal.risks)

    def test_declines_what_sentiment_breakout_takes(self, cfg: AppConfig):
        """The other half of the partition: exactly one of the two may fire,
        so a signal's strategy name is an unambiguous answer to "why is this
        on my list?"."""
        ctx = self._ctx(cfg, sentiment=make_sentiment())

        assert SentimentBreakoutStrategy(cfg).evaluate(ctx) is not None

        strategy = VolumeBreakoutStrategy(cfg)
        assert strategy.evaluate(ctx) is None
        assert [r.reason for r in strategy.drain_rejections()] == [
            "sentiment_confirmed_elsewhere"
        ]

    def test_negative_sentiment_is_penalised_but_not_vetoed(self, cfg: AppConfig):
        """"Nobody is talking about it" and "the people talking about it are
        bearish" must not score alike -- the second is contrary evidence, and
        costs the candidate rank without costing it its existence (price and
        volume are the thesis here)."""
        silent = VolumeBreakoutStrategy(cfg).evaluate(self._ctx(cfg))
        bearish = VolumeBreakoutStrategy(cfg).evaluate(
            self._ctx(
                cfg,
                sentiment=make_sentiment(raw_sentiment=-0.5, unique_author_sentiment=-0.5),
            )
        )

        assert silent is not None
        assert bearish is not None  # penalised, not vetoed
        assert bearish.setup_score < silent.setup_score
        assert "contrary_sentiment" in bearish.extras["score_breakdown"]
        assert any("bearish" in r for r in bearish.risks)

    def test_scores_no_sentiment_components_at_all(self, cfg: AppConfig):
        """Its ceiling is genuinely lower: none of the three sentiment
        components can contribute, so a confirmed breakout outranks an
        otherwise identical unconfirmed one."""
        proposal = VolumeBreakoutStrategy(cfg).evaluate(self._ctx(cfg))
        assert proposal is not None
        breakdown = proposal.extras["score_breakdown"]
        for label in ("sentiment_accel_pctl", "mention_accel_pctl", "unique_authors"):
            assert label not in breakdown

    def test_shares_every_hard_veto_with_sentiment_breakout(self, cfg: AppConfig):
        """Inherited, not re-implemented: the two cannot drift apart on how a
        breakout is found."""
        bars = make_bars(10, close=101.5)
        strategy = VolumeBreakoutStrategy(cfg)

        assert strategy.evaluate(make_context(config=cfg, bars=bars, features=self._features())) is None
        assert [r.reason for r in strategy.drain_rejections()] == ["insufficient_history"]

    def test_is_registered_and_enabled_by_default(self, cfg: AppConfig):
        """A strategy nobody runs is not a home for the relabelled path."""
        from claudetrade.strategies.registry import available_strategies, build_strategies

        assert "volume_breakout" in available_strategies()
        assert "volume_breakout" in [s.name for s in build_strategies(cfg)]


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

    # ---- market-signal adoption package item 3: level confluence ----------

    def test_level_confluence_ramps_between_one_and_three_methods(self, cfg: AppConfig):
        bars = self._bars_with_confirmation()
        sentiment = make_sentiment(raw_sentiment=0.2, sentiment_acceleration=0.05)
        history = make_sentiment_history(final=sentiment)
        strategy = SentimentPullbackStrategy(cfg)

        features_one = self._good_features()
        features_one["level_confluence_count"] = 1.0
        proposal_one = strategy.evaluate(
            make_context(
                config=cfg, bars=bars, features=features_one, sentiment=sentiment, sentiment_history=history
            )
        )
        assert proposal_one is not None
        assert "level_confluence=+0.0/6" in proposal_one.extras["score_breakdown"]

        features_three = self._good_features()
        features_three["level_confluence_count"] = 3.0
        proposal_three = strategy.evaluate(
            make_context(
                config=cfg, bars=bars, features=features_three, sentiment=sentiment, sentiment_history=history
            )
        )
        assert proposal_three is not None
        assert "level_confluence=+6.0/6" in proposal_three.extras["score_breakdown"]
        assert "independent methods" in " ".join(proposal_three.evidence)

    def test_level_confluence_absent_degrades_gracefully(self, cfg: AppConfig):
        bars = self._bars_with_confirmation()
        features = self._good_features()
        features.pop("level_confluence_count", None)
        strategy = SentimentPullbackStrategy(cfg)

        proposal = strategy.evaluate(make_context(config=cfg, bars=bars, features=features))

        assert proposal is not None
        proposal.validate()


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

    # ---- market-signal adoption package item 1(c): gap-down capitulation ----

    def test_gap_down_bonus_scales_with_gap_size(self, cfg: AppConfig):
        bars = self._reversal_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        strategy = CapitulationReversalStrategy(cfg)

        features_no_gap = self._good_features()
        proposal_no_gap = strategy.evaluate(
            make_context(
                config=cfg, bars=bars, features=features_no_gap, sentiment=sentiment, sentiment_history=history
            )
        )
        assert proposal_no_gap is not None
        assert "gap_down_capitulation=+0.0/8" in proposal_no_gap.extras["score_breakdown"]

        features_gap = self._good_features()
        features_gap["gap_pct"] = -6.0  # well beyond GAP_DOWN_FULL_CREDIT_PCT (-4.0)
        proposal_gap = strategy.evaluate(
            make_context(
                config=cfg, bars=bars, features=features_gap, sentiment=sentiment, sentiment_history=history
            )
        )
        assert proposal_gap is not None
        assert (
            f"gap_down_capitulation=+{CapitulationReversalStrategy.W_GAP_DOWN:.1f}"
            f"/{CapitulationReversalStrategy.W_GAP_DOWN:.0f}"
        ) in proposal_gap.extras["score_breakdown"]
        assert "Gapped down" in " ".join(proposal_gap.evidence)
        # A gap up (wrong direction) earns no credit, same as no gap at all.
        features_gap_up = self._good_features()
        features_gap_up["gap_pct"] = 3.0
        proposal_gap_up = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_gap_up,
                sentiment=sentiment,
                sentiment_history=history,
            )
        )
        assert proposal_gap_up is not None
        assert "gap_down_capitulation=+0.0/8" in proposal_gap_up.extras["score_breakdown"]

    def test_gap_pct_absent_degrades_gracefully(self, cfg: AppConfig):
        bars = self._reversal_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        features = self._good_features()
        features.pop("gap_pct", None)
        strategy = CapitulationReversalStrategy(cfg)

        proposal = strategy.evaluate(
            make_context(
                config=cfg, bars=bars, features=features, sentiment=sentiment, sentiment_history=history
            )
        )

        assert proposal is not None
        proposal.validate()


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

    # ---- market-signal adoption package items 1(d)/2/4 ----------------------

    def test_gap_down_failure_bonus_scales_with_gap_size(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        security = self._good_security()
        strategy = HypeFailureShortStrategy(cfg)

        features_no_gap = self._good_features()
        proposal_no_gap = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_no_gap,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_no_gap is not None
        assert "gap_down_failure=+0.0/8" in proposal_no_gap.extras["score_breakdown"]

        features_gap = self._good_features()
        features_gap["gap_pct"] = -5.0  # well beyond GAP_DOWN_FULL_CREDIT_PCT (-3.0)
        proposal_gap = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_gap,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_gap is not None
        assert (
            f"gap_down_failure=+{HypeFailureShortStrategy.W_GAP_DOWN_FAILURE:.1f}"
            f"/{HypeFailureShortStrategy.W_GAP_DOWN_FAILURE:.0f}"
        ) in proposal_gap.extras["score_breakdown"]
        assert "Gapped down" in " ".join(proposal_gap.evidence)

    def test_gap_continuation_down_is_boolean_full_credit(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        security = self._good_security()
        strategy = HypeFailureShortStrategy(cfg)

        features_off = self._good_features()
        proposal_off = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_off,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_off is not None
        assert "gap_continuation=+0.0/6" in proposal_off.extras["score_breakdown"]

        features_on = self._good_features()
        features_on["gap_continuation_down"] = 1.0
        proposal_on = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_on,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_on is not None
        assert (
            f"gap_continuation=+{HypeFailureShortStrategy.W_GAP_CONTINUATION_DOWN:.1f}"
            f"/{HypeFailureShortStrategy.W_GAP_CONTINUATION_DOWN:.0f}"
        ) in proposal_on.extras["score_breakdown"]
        assert "extending the breakdown" in " ".join(proposal_on.evidence)

    def test_volume_divergence_undercuts_the_failure_thesis(self, cfg: AppConfig):
        """A moderate (non-saturating) scenario so the penalty is visible on
        the clamped setup_score, not just in the raw breakdown."""
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        security = self._good_security()
        strategy = HypeFailureShortStrategy(cfg)
        moderate = self._good_features()
        moderate.update({"roc_20_pctl_120": 0.75})

        proposal_clean = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=dict(moderate),
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_clean is not None

        features_divergent = dict(moderate)
        features_divergent["volume_divergence"] = 1.0
        proposal_divergent = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features_divergent,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )
        assert proposal_divergent is not None
        assert (
            f"volume_divergence={HypeFailureShortStrategy.PENALTY_VOLUME_DIVERGENCE:.1f}"
            in proposal_divergent.extras["score_breakdown"]
        )
        assert proposal_divergent.setup_score < proposal_clean.setup_score
        assert proposal_clean.setup_score - proposal_divergent.setup_score <= abs(
            HypeFailureShortStrategy.PENALTY_VOLUME_DIVERGENCE
        )
        assert any("absorption" in r for r in proposal_divergent.risks)

    def test_new_gap_features_absent_degrades_gracefully(self, cfg: AppConfig):
        bars = self._failure_bars()
        sentiment = self._good_sentiment()
        history = make_sentiment_history(final=sentiment)
        security = self._good_security()
        features = self._good_features()
        for key in ("gap_pct", "gap_continuation_down", "volume_divergence"):
            features.pop(key, None)
        strategy = HypeFailureShortStrategy(cfg)

        proposal = strategy.evaluate(
            make_context(
                config=cfg,
                bars=bars,
                features=features,
                sentiment=sentiment,
                sentiment_history=history,
                security=security,
            )
        )

        assert proposal is not None
        proposal.validate()


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
