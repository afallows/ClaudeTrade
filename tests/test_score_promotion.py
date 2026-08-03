"""Tests for ADR-0009 (score promotion, shadow mode).

Covers, in order:

* ``signals.scoring._sub_blend`` -- the evidence-absent renormalisation
  helper shared by all three new components' internal sub-blends.
* Each new component (``analyst_sentiment``/``institutional_sentiment``/
  ``cross_source_attention``) in isolation: evidence-absent -> neutral
  display value AND zero effective weight (never a weighted 50), plus the
  signed/unsigned direction-awareness split HANDOFF.md's plan documents as a
  coordinator-approved deviation from the ADR's literal "mirror
  reddit_sentiment" wording.
* The single most important guarantee in this change: ``mode="off"`` and
  ``mode="shadow"`` are BYTE-IDENTICAL to the pre-ADR-0009 baseline
  composite, computed here by hand from the OLD ``component_weights`` table
  alone.
* ``SignalConfig``'s two-table split: the promoted table sums to 1.00, the
  baseline table is untouched, and the mode literal is validated.
* The engine's ranking policy: ``shadow`` never reorders (only stamps
  ``Signal.extras["promoted_scoring"]``); ``live`` ranks by the promoted
  composite instead.
* Lookahead safety: a context built for session S must never see an
  analyst/institutional/adanos row stored for S+1 -- mirrors
  ``tests/test_lookahead.py``'s ``TestContextNeverExposesNextSession``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from claudetrade.config import AppConfig
from claudetrade.data.adanos_read import AttentionAggregate
from claudetrade.data.context import ContextBuilder, SymbolData
from claudetrade.domain import (
    AnalystRatingAction,
    AnalystSnapshot,
    Bar,
    Direction,
    InstitutionalScorePoint,
    InstitutionalSnapshot,
    MarketRegime,
    RegimeState,
    SecurityInfo,
)
from claudetrade.signals.engine import SignalEngine
from claudetrade.signals.scoring import _sub_blend, score_candidate
from claudetrade.strategies.base import LookaheadError, Strategy, StrategyContext, StrategyProposal

SESSION = dt.date(2024, 3, 15)
SYMBOL = "TEST"


# --------------------------------------------------------------------------
# Shared fixtures/helpers
# --------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_app_config: AppConfig) -> AppConfig:
    return tmp_app_config


def _bars(n: int = 220, close: float = 100.0, *, symbol: str = SYMBOL) -> list[Bar]:
    bars: list[Bar] = []
    day = SESSION
    for _ in range(n):
        bars.append(
            Bar(
                symbol=symbol,
                session=day,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
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


_GOOD_FEATURES = {
    "atr_14": 2.0,
    "avg_dollar_volume_20": 50_000_000.0,
    "rel_volume_20": 1.5,
    "roc_10": 2.0,
    "roc_20": 3.0,
    "rs_percentile": 60.0,
    "atr_pct": 3.0,
}


def _ctx(
    cfg: AppConfig,
    *,
    symbol: str = SYMBOL,
    direction_features: dict[str, float] | None = None,
    analyst_history: list[AnalystSnapshot] | None = None,
    institutional_history: list[InstitutionalScorePoint] | None = None,
    adanos_history: list[AttentionAggregate] | None = None,
) -> StrategyContext:
    return StrategyContext(
        session=SESSION,
        symbol=symbol,
        bars=_bars(symbol=symbol),
        features=dict(direction_features or _GOOD_FEATURES),
        security=SecurityInfo(symbol=symbol, exchange="NASDAQ", market_cap_usd=5e9),
        regime=RegimeState(session=SESSION, regime=MarketRegime.BULL_QUIET),
        analyst_history=list(analyst_history or []),
        institutional_history=list(institutional_history or []),
        adanos_history=list(adanos_history or []),
        config=cfg,
    )


def _proposal(direction: Direction = Direction.LONG, setup_score: float = 60.0) -> StrategyProposal:
    if direction is Direction.LONG:
        entry_low, entry_high, stop, targets = 99.0, 101.0, 95.0, [110.0]
    else:
        entry_low, entry_high, stop, targets = 99.0, 101.0, 105.0, [90.0]
    return StrategyProposal(
        strategy="sentiment_breakout",
        strategy_version="v3",
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        targets=targets,
        setup_score=setup_score,
    )


def _score(cfg: AppConfig, ctx: StrategyContext, direction: Direction = Direction.LONG):
    return score_candidate(
        ctx=ctx,
        proposal=_proposal(direction),
        config=cfg,
        security=ctx.security,
        regime=ctx.regime,
        requires_sentiment=False,
    )


def _analyst_snapshot(
    *,
    session: dt.date = SESSION,
    buy: int = 10,
    hold: int = 0,
    sell: int = 0,
    price_target_mean: float | None = None,
    rating_actions: list[AnalystRatingAction] | None = None,
) -> AnalystSnapshot:
    return AnalystSnapshot(
        symbol=SYMBOL,
        as_of_session=session,
        buy_count=buy,
        hold_count=hold,
        sell_count=sell,
        analyst_count=buy + hold + sell,
        price_target_mean=price_target_mean,
        recent_rating_actions=rating_actions or [],
    )


def _institutional_point(*, session: dt.date = SESSION, score: float | None) -> InstitutionalScorePoint:
    return InstitutionalScorePoint(
        session=session,
        snapshot=InstitutionalSnapshot(symbol=SYMBOL, as_of_session=session),
        score=score,
    )


def _attention_aggregate(
    *,
    session: dt.date = SESSION,
    buzz_score: float = 60.0,
    bullish_pct: float | None = 70.0,
    bearish_pct: float | None = 20.0,
    platforms: list[str] | None = None,
    trend_history: list[float] | None = None,
) -> AttentionAggregate:
    return AttentionAggregate(
        symbol=SYMBOL,
        session=session,
        platforms=platforms if platforms is not None else ["x", "reddit"],
        total_mentions=500,
        source_count=None,
        buzz_score=buzz_score,
        bullish_pct=bullish_pct,
        bearish_pct=bearish_pct,
        trend="rising",
        trend_history=trend_history if trend_history is not None else [10.0, 20.0, 30.0, 40.0, 50.0, 55.0, 58.0],
    )


# --------------------------------------------------------------------------
# _sub_blend: evidence-absent renormalisation, in isolation
# --------------------------------------------------------------------------


class TestSubBlend:
    def test_all_present_is_a_plain_weighted_mean(self):
        result = _sub_blend([(80.0, 0.5), (40.0, 0.3), (0.0, 0.2)])
        assert result == pytest.approx(80.0 * 0.5 + 40.0 * 0.3 + 0.0 * 0.2)

    def test_missing_parts_are_renormalised_away_not_scored_neutral(self):
        # Same two present values as above, but the third slot is now
        # unavailable (None) instead of a real, low reading of 0.0 -- the
        # result must differ from the "0.0 counted" case above, because an
        # absent sub-signal must not silently act like a bearish one.
        result = _sub_blend([(80.0, 0.5), (40.0, 0.3), (None, 0.2)])
        assert result == pytest.approx((80.0 * 0.5 + 40.0 * 0.3) / 0.8)
        assert result != pytest.approx(80.0 * 0.5 + 40.0 * 0.3 + 0.0 * 0.2)

    def test_everything_missing_falls_back_to_neutral(self):
        assert _sub_blend([(None, 0.4), (None, 0.6)]) == 50.0


# --------------------------------------------------------------------------
# analyst_sentiment
# --------------------------------------------------------------------------


class TestAnalystSentimentComponent:
    def test_no_snapshot_is_neutral_and_unevidenced(self, cfg: AppConfig):
        breakdown = _score(cfg, _ctx(cfg, analyst_history=[]))
        assert breakdown.components.analyst_sentiment == 50.0

    def test_zero_analyst_count_is_evidence_absent(self, cfg: AppConfig):
        snap = _analyst_snapshot(buy=0, hold=0, sell=0)
        breakdown = _score(cfg, _ctx(cfg, analyst_history=[snap]))
        assert breakdown.components.analyst_sentiment == 50.0

    def test_stale_snapshot_beyond_five_sessions_is_evidence_absent(self, cfg: AppConfig):
        stale_session = SESSION - dt.timedelta(days=14)  # well over 5 trading sessions back
        snap = _analyst_snapshot(session=stale_session, buy=10, sell=0)
        breakdown = _score(cfg, _ctx(cfg, analyst_history=[snap]))
        assert breakdown.components.analyst_sentiment == 50.0

    def test_bullish_consensus_scores_above_neutral_for_a_long(self, cfg: AppConfig):
        snap = _analyst_snapshot(buy=10, hold=0, sell=0)
        breakdown = _score(cfg, _ctx(cfg, analyst_history=[snap]), Direction.LONG)
        assert breakdown.components.analyst_sentiment > 50.0

    def test_consensus_tilt_flips_sign_for_a_short(self, cfg: AppConfig):
        """Consensus tilt is polarity-shaped: mirrors ``_sentiment_score``'s
        direction flip (D3), unlike the coverage-change kicker below."""
        snap = _analyst_snapshot(buy=10, hold=0, sell=0)
        long_breakdown = _score(cfg, _ctx(cfg, analyst_history=[snap]), Direction.LONG)
        short_breakdown = _score(cfg, _ctx(cfg, analyst_history=[snap]), Direction.SHORT)
        assert long_breakdown.components.analyst_sentiment > 50.0
        assert short_breakdown.components.analyst_sentiment < 50.0
        assert long_breakdown.components.analyst_sentiment == pytest.approx(
            100.0 - short_breakdown.components.analyst_sentiment
        )

    def test_coverage_change_kicker_is_unsigned_unlike_consensus_tilt(self, cfg: AppConfig):
        """Coordinator-approved deviation (HANDOFF.md): the coverage-change
        kicker is attention-shaped ("more analysts watching"), not
        polarity-shaped, so it must NOT flip for a short -- unlike the
        consensus tilt above. Isolate it by holding the consensus/PT/rating
        sub-blends at neutral-tilt (buy==sell) so only coverage_change moves
        the component.
        """
        previous = _analyst_snapshot(session=SESSION - dt.timedelta(days=7), buy=5, hold=0, sell=5)
        current = _analyst_snapshot(session=SESSION, buy=5, hold=0, sell=5)  # +... coverage grows below
        # Bump analyst_count via extra hold-rated analysts so coverage grew
        # without moving the (still-balanced) buy/sell tilt.
        current.hold_count = 4
        current.analyst_count = current.buy_count + current.hold_count + current.sell_count
        long_breakdown = _score(cfg, _ctx(cfg, analyst_history=[previous, current]), Direction.LONG)
        short_breakdown = _score(cfg, _ctx(cfg, analyst_history=[previous, current]), Direction.SHORT)
        assert long_breakdown.components.analyst_sentiment == pytest.approx(
            short_breakdown.components.analyst_sentiment
        )
        assert long_breakdown.components.analyst_sentiment > 50.0  # coverage grew

    def test_promoted_weight_is_zero_when_evidence_absent(self, cfg: AppConfig):
        """The renormalisation that matters: absent evidence must not just
        display 50 -- it must carry NO weight in the promoted composite."""
        cfg.signals.promoted_component_weights = {"analyst_sentiment": 1.0}
        no_evidence = _score(cfg, _ctx(cfg, analyst_history=[]))
        assert no_evidence.promoted_overall == 50.0  # falls back: zero total weight

        strong_evidence = _score(cfg, _ctx(cfg, analyst_history=[_analyst_snapshot(buy=10, sell=0)]))
        assert strong_evidence.promoted_overall > 90.0  # fully weighted, near-maximal tilt


# --------------------------------------------------------------------------
# institutional_sentiment
# --------------------------------------------------------------------------


class TestInstitutionalSentimentComponent:
    def test_no_snapshot_is_neutral(self, cfg: AppConfig):
        breakdown = _score(cfg, _ctx(cfg, institutional_history=[]))
        assert breakdown.components.institutional_sentiment == 50.0

    def test_none_score_is_evidence_absent(self, cfg: AppConfig):
        point = _institutional_point(score=None)
        breakdown = _score(cfg, _ctx(cfg, institutional_history=[point]))
        assert breakdown.components.institutional_sentiment == 50.0

    def test_score_is_read_off_the_row_never_recomputed(self, cfg: AppConfig):
        point = _institutional_point(score=0.5)
        breakdown = _score(cfg, _ctx(cfg, institutional_history=[point]), Direction.LONG)
        assert breakdown.components.institutional_sentiment == pytest.approx((0.5 + 1.0) * 50.0)

    def test_sign_flips_for_a_short(self, cfg: AppConfig):
        point = _institutional_point(score=0.5)
        long_breakdown = _score(cfg, _ctx(cfg, institutional_history=[point]), Direction.LONG)
        short_breakdown = _score(cfg, _ctx(cfg, institutional_history=[point]), Direction.SHORT)
        assert long_breakdown.components.institutional_sentiment == pytest.approx(75.0)
        assert short_breakdown.components.institutional_sentiment == pytest.approx(25.0)

    def test_promoted_weight_is_zero_when_evidence_absent(self, cfg: AppConfig):
        cfg.signals.promoted_component_weights = {"institutional_sentiment": 1.0}
        no_evidence = _score(cfg, _ctx(cfg, institutional_history=[_institutional_point(score=None)]))
        assert no_evidence.promoted_overall == 50.0
        strong_evidence = _score(cfg, _ctx(cfg, institutional_history=[_institutional_point(score=0.9)]))
        assert strong_evidence.promoted_overall == pytest.approx((0.9 + 1.0) * 50.0)


# --------------------------------------------------------------------------
# cross_source_attention
# --------------------------------------------------------------------------


class TestCrossSourceAttentionComponent:
    def test_no_adanos_row_is_neutral_and_unevidenced(self, cfg: AppConfig):
        breakdown = _score(cfg, _ctx(cfg, adanos_history=[]))
        assert breakdown.components.cross_source_attention == 50.0

    def test_bull_bear_spread_flips_sign_for_a_short(self, cfg: AppConfig):
        """Isolate the (SIGNED) spread sub-component by flattening the
        (UNSIGNED) buzz-percentile and corroboration sub-components: a
        single-point trend history makes the buzz percentile neutral (0.5),
        and a single platform keeps corroboration at its floor either way.
        """
        agg = _attention_aggregate(
            buzz_score=50.0, bullish_pct=90.0, bearish_pct=10.0, platforms=["x"], trend_history=[50.0]
        )
        long_breakdown = _score(cfg, _ctx(cfg, adanos_history=[agg]), Direction.LONG)
        short_breakdown = _score(cfg, _ctx(cfg, adanos_history=[agg]), Direction.SHORT)
        assert long_breakdown.components.cross_source_attention > 50.0
        assert short_breakdown.components.cross_source_attention < 50.0

    def test_buzz_percentile_is_unsigned(self, cfg: AppConfig):
        """Coordinator-approved deviation (HANDOFF.md): buzz percentile is
        attention-shaped, so it must NOT flip for a short. Isolate it by
        making bullish_pct/bearish_pct absent (so the SIGNED spread
        sub-component drops out of the blend entirely) and using a single
        platform (corroboration pinned at its floor either way).
        """
        agg = _attention_aggregate(
            buzz_score=100.0,
            bullish_pct=None,
            bearish_pct=None,
            platforms=["x"],
            trend_history=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
        )
        long_breakdown = _score(cfg, _ctx(cfg, adanos_history=[agg]), Direction.LONG)
        short_breakdown = _score(cfg, _ctx(cfg, adanos_history=[agg]), Direction.SHORT)
        assert long_breakdown.components.cross_source_attention == pytest.approx(
            short_breakdown.components.cross_source_attention
        )
        assert long_breakdown.components.cross_source_attention > 50.0  # today's buzz tops its own history

    def test_promoted_weight_is_zero_when_evidence_absent(self, cfg: AppConfig):
        cfg.signals.promoted_component_weights = {"cross_source_attention": 1.0}
        no_evidence = _score(cfg, _ctx(cfg, adanos_history=[]))
        assert no_evidence.promoted_overall == 50.0
        strong_evidence = _score(
            cfg,
            _ctx(cfg, adanos_history=[_attention_aggregate(bullish_pct=95.0, bearish_pct=5.0)]),
        )
        assert strong_evidence.promoted_overall > 50.0


# --------------------------------------------------------------------------
# Byte-identical baseline: the single most important guarantee here.
# --------------------------------------------------------------------------


class TestByteIdenticalBaseline:
    def _rich_ctx(self, cfg: AppConfig) -> StrategyContext:
        """A context with strong, non-neutral evidence on ALL THREE new
        components -- if the baseline computation leaked any weight to them,
        this fixture would show it immediately."""
        return _ctx(
            cfg,
            analyst_history=[_analyst_snapshot(buy=10, hold=0, sell=0)],
            institutional_history=[_institutional_point(score=0.8)],
            adanos_history=[_attention_aggregate(bullish_pct=90.0, bearish_pct=5.0)],
        )

    def _hand_computed_overall(self, cfg: AppConfig, breakdown) -> float:
        """Reimplements the pre-ADR-0009 baseline formula independently, from
        ``ComponentScores.as_dict()`` and ``component_weights`` ALONE -- the
        old 12-component table has no entries for the three new components,
        so ``.get(name, 0.0)`` already zeroes them without any extra code
        here either.

        ``_rich_ctx`` supplies no sentiment at all (no combined row, no
        per-source rows, no attention rows) -- by construction, that is
        already known to zero ``reddit_sentiment``/``x_sentiment``/
        ``attention_acceleration``'s EFFECTIVE weight in production (see
        ``_polarity_axis``/``_attention_score``), independently of this
        fixture's new-components evidence. Reproduced explicitly here rather
        than re-deriving polarity/attention evidence flags a second time.
        """
        scored = breakdown.components.as_dict()
        weights = cfg.signals.component_weights
        effective = {k: weights.get(k, 0.0) for k in scored if k != "data_confidence"}
        effective["reddit_sentiment"] = 0.0
        effective["x_sentiment"] = 0.0
        effective["attention_acceleration"] = 0.0
        total = sum(effective.values())
        return round(sum(scored[k] * effective[k] for k in effective) / total, 2)

    @pytest.mark.parametrize("mode", ["off", "shadow", "live"])
    def test_overall_is_unaffected_by_promoted_scoring_mode(self, cfg: AppConfig, mode: str):
        """``score_candidate`` itself never reads ``promoted_scoring_mode`` --
        only ``signals.engine`` does, for ranking policy. This proves
        ``overall`` truly cannot vary with the mode at the scoring layer."""
        cfg.signals.promoted_scoring_mode = mode
        breakdown = _score(cfg, self._rich_ctx(cfg))
        assert breakdown.overall == self._hand_computed_overall(cfg, breakdown)

    def test_off_and_shadow_produce_the_identical_float(self, cfg: AppConfig):
        ctx = self._rich_ctx(cfg)
        cfg.signals.promoted_scoring_mode = "off"
        off_breakdown = _score(cfg, ctx)
        cfg.signals.promoted_scoring_mode = "shadow"
        shadow_breakdown = _score(cfg, ctx)
        assert off_breakdown.overall == shadow_breakdown.overall

    def test_promoted_overall_differs_from_baseline_when_evidence_is_rich(self, cfg: AppConfig):
        """Sanity check that the promoted composite is actually a DIFFERENT
        number here -- otherwise the "byte-identical" assertions above would
        be trivially true for the wrong reason (e.g. a promoted table that
        collapsed to the baseline table)."""
        breakdown = _score(cfg, self._rich_ctx(cfg))
        assert breakdown.promoted_overall != breakdown.overall


# --------------------------------------------------------------------------
# SignalConfig: the two-table split and its validators
# --------------------------------------------------------------------------


class TestSignalConfigPromotionFields:
    def test_promoted_table_sums_to_one_and_old_table_is_unchanged(self):
        cfg = AppConfig()
        assert sum(cfg.signals.promoted_component_weights.values()) == pytest.approx(1.0, abs=1e-9)
        assert cfg.signals.component_weights == {
            "technical_setup": 0.20,
            "price_momentum": 0.12,
            "volume_confirmation": 0.10,
            "reddit_sentiment": 0.08,
            "x_sentiment": 0.05,
            "sentiment_acceleration": 0.08,
            "attention_acceleration": 0.05,
            "catalyst_quality": 0.07,
            "earnings_risk": 0.08,
            "liquidity": 0.07,
            "market_regime": 0.06,
            "manipulation_risk": 0.04,
        }

    def test_default_mode_is_shadow(self):
        assert AppConfig().signals.promoted_scoring_mode == "shadow"

    def test_invalid_mode_is_rejected(self):
        with pytest.raises(ValidationError):
            AppConfig(signals={"promoted_scoring_mode": "sideways"})

    def test_promoted_weights_must_sum_to_one(self):
        with pytest.raises(ValidationError):
            AppConfig(signals={"promoted_component_weights": {"technical_setup": 0.1}})


# --------------------------------------------------------------------------
# signals.research.VALID_COMPONENT_NAMES picks up the new fields
# --------------------------------------------------------------------------


class TestResearchGuardrailPicksUpNewComponents:
    def test_analyst_sentiment_is_a_valid_adjustment_target(self):
        from claudetrade.signals.research import VALID_COMPONENT_NAMES

        assert "analyst_sentiment" in VALID_COMPONENT_NAMES
        assert "institutional_sentiment" in VALID_COMPONENT_NAMES
        assert "cross_source_attention" in VALID_COMPONENT_NAMES

    def test_a_revision_can_adjust_analyst_sentiment(self, tmp_db, tmp_app_config, make_signal):
        from claudetrade.signals.ledger import SignalLedger
        from claudetrade.signals.research import ResearchLedger

        sig = make_signal(symbol="AAPL", overall_score=70.0)
        SignalLedger(tmp_db).record(sig)
        result = ResearchLedger(tmp_db).append_research_revision(
            sig.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"analyst_sentiment": 5.0},
            rationale="New analyst coverage since the scan.",
            sources=["https://example.com/note"],
            config=tmp_app_config,
        )
        assert result.applied_adjustments["analyst_sentiment"] == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Engine ranking policy: shadow never reorders; live ranks by promoted.
# --------------------------------------------------------------------------


class _FixedProposalStrategy(Strategy):
    """Test double: returns a fixed, comfortably-qualifying proposal whose
    ``setup_score`` is looked up per symbol -- isolates the ranking test to
    the engine's own post-scoring logic, not strategy calibration."""

    name = "fixed_stub"
    version = "test"
    min_history_bars = 1
    requires_sentiment = False

    def __init__(self, config: AppConfig, setup_scores: dict[str, float]):
        super().__init__(config)
        self._setup_scores = setup_scores

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        return StrategyProposal(
            strategy=self.name,
            strategy_version=self.version,
            direction=Direction.LONG,
            entry_low=99.0,
            entry_high=101.0,
            stop_loss=95.0,
            targets=[115.0],
            target_fractions=[1.0],
            setup_score=self._setup_scores[ctx.symbol],
        )


def _engine_ctx(cfg: AppConfig, symbol: str, *, analyst_history=None) -> StrategyContext:
    return StrategyContext(
        session=SESSION,
        symbol=symbol,
        bars=_bars(symbol=symbol),
        features=dict(_GOOD_FEATURES),
        security=SecurityInfo(symbol=symbol, exchange="NASDAQ", market_cap_usd=5e9),
        regime=RegimeState(session=SESSION, regime=MarketRegime.BULL_QUIET, score_threshold_adjustment=0.0),
        analyst_history=list(analyst_history or []),
        config=cfg,
    )


def _rank_engine(cfg: AppConfig) -> tuple[SignalEngine, list[StrategyContext]]:
    """Symbol A: strong technical setup, no analyst evidence. Symbol B:
    weaker technical setup, but strong analyst evidence. The baseline table
    below scores ONLY on ``technical_setup`` (A wins); the promoted table
    scores ONLY on ``analyst_sentiment`` (B wins) -- a clean, deliberately
    engineered rank flip.
    """
    cfg.signals.component_weights = {"technical_setup": 1.0}
    cfg.signals.promoted_component_weights = {"analyst_sentiment": 1.0}
    strategy = _FixedProposalStrategy(cfg, {"AAA": 90.0, "BBB": 70.0})
    engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
    contexts = [
        _engine_ctx(cfg, "AAA"),
        _engine_ctx(cfg, "BBB", analyst_history=[_analyst_snapshot(buy=10, sell=0)]),
    ]
    # ``_analyst_snapshot`` always stamps ``symbol=SYMBOL`` ("TEST"); fix it
    # up here rather than complicating the shared helper's signature for one
    # call site.
    for ctx in contexts:
        for snap in ctx.analyst_history:
            snap.symbol = ctx.symbol
    return engine, contexts


class TestEngineRankingPolicy:
    def test_off_mode_ranks_by_baseline_and_stamps_no_extras(self, cfg: AppConfig):
        cfg.signals.promoted_scoring_mode = "off"
        engine, contexts = _rank_engine(cfg)
        result = engine.scan(contexts, session=SESSION, regime=contexts[0].regime)
        assert [s.symbol for s in result.signals] == ["AAA", "BBB"]
        assert all("promoted_scoring" not in s.extras for s in result.signals)

    def test_shadow_mode_preserves_baseline_order_but_stamps_divergence(self, cfg: AppConfig):
        cfg.signals.promoted_scoring_mode = "shadow"
        engine, contexts = _rank_engine(cfg)
        result = engine.scan(contexts, session=SESSION, regime=contexts[0].regime)
        # Ranking is UNCHANGED from "off" -- shadow mode's entire point.
        assert [s.symbol for s in result.signals] == ["AAA", "BBB"]
        by_symbol = {s.symbol: s for s in result.signals}
        assert by_symbol["AAA"].extras["promoted_scoring"]["mode"] == "shadow"
        assert by_symbol["AAA"].extras["promoted_scoring"]["baseline_rank"] == 1
        assert by_symbol["AAA"].extras["promoted_scoring"]["promoted_rank"] == 2
        assert by_symbol["BBB"].extras["promoted_scoring"]["baseline_rank"] == 2
        assert by_symbol["BBB"].extras["promoted_scoring"]["promoted_rank"] == 1
        assert "+1" in by_symbol["BBB"].extras["promoted_scoring"]["rank_divergence_note"]

    def test_live_mode_ranks_by_promoted_composite(self, cfg: AppConfig):
        cfg.signals.promoted_scoring_mode = "live"
        engine, contexts = _rank_engine(cfg)
        result = engine.scan(contexts, session=SESSION, regime=contexts[0].regime)
        # Order is REVERSED relative to "off"/"shadow": BBB's analyst
        # evidence now wins under the promoted table.
        assert [s.symbol for s in result.signals] == ["BBB", "AAA"]
        # Signal.overall_score itself is never mutated by ranking policy --
        # it stays the baseline, audited figure regardless of mode.
        by_symbol = {s.symbol: s for s in result.signals}
        assert by_symbol["AAA"].overall_score == pytest.approx(90.0)
        assert by_symbol["BBB"].overall_score == pytest.approx(70.0)


# --------------------------------------------------------------------------
# Lookahead safety
# --------------------------------------------------------------------------


class TestLookaheadSafety:
    def test_context_post_init_truncates_future_dated_histories(self, cfg: AppConfig):
        """Mirrors ``test_lookahead.py``'s ``TestContextNeverExposesNextSession``
        exactly, one level down: over-supplying a full history (including a
        future session) is a safe calling pattern, truncated silently."""
        future = SESSION + dt.timedelta(days=1)
        ctx = StrategyContext(
            session=SESSION,
            symbol=SYMBOL,
            bars=_bars(),
            features={},
            security=SecurityInfo(SYMBOL),
            regime=RegimeState(session=SESSION, regime=MarketRegime.UNKNOWN),
            analyst_history=[_analyst_snapshot(session=SESSION), _analyst_snapshot(session=future)],
            institutional_history=[
                _institutional_point(session=SESSION, score=0.1),
                _institutional_point(session=future, score=0.9),
            ],
            adanos_history=[
                _attention_aggregate(session=SESSION),
                _attention_aggregate(session=future),
            ],
            config=cfg,
        )
        assert [s.as_of_session for s in ctx.analyst_history] == [SESSION]
        assert [p.session for p in ctx.institutional_history] == [SESSION]
        assert [a.session for a in ctx.adanos_history] == [SESSION]
        ctx.assert_no_lookahead()

    def test_future_dated_single_history_entry_direct_construction_is_still_safe(self, cfg: AppConfig):
        """Direct construction with a future entry never raises -- these are
        HISTORY lists (like ``sentiment_history``/``attention_history``),
        truncated rather than rejected, unlike the single-reading fields
        (``sentiment``, ``sentiment_by_source``)."""
        future = SESSION + dt.timedelta(days=3)
        ctx = StrategyContext(
            session=SESSION,
            symbol=SYMBOL,
            bars=_bars(),
            features={},
            security=SecurityInfo(SYMBOL),
            regime=RegimeState(session=SESSION, regime=MarketRegime.UNKNOWN),
            institutional_history=[_institutional_point(session=future, score=0.5)],
            config=cfg,
        )
        assert ctx.institutional_history == []

    def test_context_builder_never_exposes_a_row_from_the_next_session(self, cfg: AppConfig):
        """The production path: ``ContextBuilder.build`` slices
        ``SymbolData``'s full stored history per-session, exactly like
        ``sentiment_history`` already does -- a row stored for S+1 must not
        reach a context built for S even when ``SymbolData`` (as
        ``DatabaseContextProvider._load`` would hand it) already carries it.
        """
        next_session = SESSION + dt.timedelta(days=1)
        data = SymbolData(
            symbol=SYMBOL,
            security=SecurityInfo(SYMBOL, exchange="NASDAQ"),
            bars=_bars(),
            analyst_history=[
                _analyst_snapshot(session=SESSION, buy=5, sell=5),
                _analyst_snapshot(session=next_session, buy=10, sell=0),
            ],
            institutional_history=[
                _institutional_point(session=SESSION, score=0.1),
                _institutional_point(session=next_session, score=0.9),
            ],
            adanos_history=[
                _attention_aggregate(session=SESSION, buzz_score=10.0),
                _attention_aggregate(session=next_session, buzz_score=99.0),
            ],
        )
        builder = ContextBuilder(cfg)
        # ``DatabaseContextProvider._load`` builds the feature frame once per
        # symbol before ``build()`` slices it per-session; reproduce that
        # step here since this test bypasses the provider entirely.
        data.features = builder.features.build(symbol=SYMBOL, bars=data.bars)
        ctx = builder.build(data, SESSION, regime=RegimeState(session=SESSION, regime=MarketRegime.UNKNOWN))
        assert ctx is not None
        assert [s.as_of_session for s in ctx.analyst_history] == [SESSION]
        assert [p.session for p in ctx.institutional_history] == [SESSION]
        assert [a.session for a in ctx.adanos_history] == [SESSION]
        # The S+1 evidence must be entirely invisible, not merely last in a
        # longer list: assert on the actual VALUES, not just the dates.
        assert ctx.analyst_history[0].buy_count == 5
        assert ctx.institutional_history[0].score == pytest.approx(0.1)
        assert ctx.adanos_history[0].buzz_score == pytest.approx(10.0)

    def test_future_dated_analyst_snapshot_stamped_directly_would_be_caught(self, cfg: AppConfig):
        """Belt-and-braces: if a caller ever bypassed truncation (e.g. by
        mutating ``analyst_history`` after construction), ``assert_no_
        lookahead`` independently re-detects a future-dated entry, exactly
        like it already does for ``sentiment_history``/``attention_history``.
        """
        ctx = StrategyContext(
            session=SESSION,
            symbol=SYMBOL,
            bars=_bars(),
            features={},
            security=SecurityInfo(SYMBOL),
            regime=RegimeState(session=SESSION, regime=MarketRegime.UNKNOWN),
            config=cfg,
        )
        ctx.analyst_history.append(_analyst_snapshot(session=SESSION + dt.timedelta(days=1)))
        with pytest.raises(LookaheadError):
            ctx.assert_no_lookahead()
