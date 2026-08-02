"""Tests for the scan-path rejection funnel (signals.engine.ScanFunnel).

Mirrors the shape of ``test_engine.py``'s backtest-funnel coverage, but for
``SignalEngine.scan`` directly: a scripted scan built from local test-double
strategies (not the production strategies -- calibration is out of scope,
see ``test_strategies.py`` for that) engineered so every rejection stage
fires exactly once, plus a passing signal to prove signals are never counted
as rejections.

Each test-double strategy is a ``RouterStrategy``: it only acts on its own
dedicated symbol and returns ``None`` *without* calling ``decline()`` for any
other symbol, so many such doubles can share one context list/scan without
polluting each other's funnel entries (a plain ``return None`` produces no
``StrategyRejection`` to drain, so the engine records nothing for that pair).
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.config import AppConfig
from claudetrade.domain import Bar, Direction, MarketRegime, RegimeState, SecurityInfo
from claudetrade.risk.limits import OpenPosition, PortfolioState
from claudetrade.signals import funnel_store
from claudetrade.signals.engine import (
    NearMiss,
    ScanFunnel,
    SignalEngine,
    _parse_accumulator_summary,
    _weakest_strongest,
)
from claudetrade.signals.scoring import apply_hard_gates
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.scoring_utils import ScoreAccumulator

SESSION = dt.date(2024, 3, 15)


# --------------------------------------------------------------------------
# Pure-function unit tests
# --------------------------------------------------------------------------


class TestParseAccumulatorSummary:
    def test_parses_score_threshold_and_components(self):
        acc = ScoreAccumulator(baseline=10.0)
        acc.add("breakout", 0.3, 20.0)  # +6.0
        acc.penalty("risk_flag", -2.0)
        summary = acc.summary(threshold=48.0)

        score, threshold, components = _parse_accumulator_summary(summary)

        assert score == pytest.approx(acc.score)
        assert threshold == pytest.approx(48.0)
        assert components == [("breakout", pytest.approx(6.0)), ("risk_flag", pytest.approx(-2.0))]

    def test_no_components_still_parses_score_and_threshold(self):
        acc = ScoreAccumulator(baseline=30.0)
        score, threshold, components = _parse_accumulator_summary(acc.summary(threshold=48.0))
        assert score == pytest.approx(30.0)
        assert threshold == pytest.approx(48.0)
        assert components == []

    def test_non_matching_string_returns_none_and_empty(self):
        score, threshold, components = _parse_accumulator_summary("not a summary at all")
        assert score is None
        assert threshold is None
        assert components == []


class TestWeakestStrongest:
    def test_splits_ascending_and_descending(self):
        components = [("a", 10.0), ("b", -5.0), ("c", 20.0), ("d", 1.0)]
        weakest, strongest = _weakest_strongest(components, n=2)
        assert weakest == [("b", -5.0), ("d", 1.0)]
        assert strongest == [("c", 20.0), ("a", 10.0)]

    def test_empty_input_is_safe(self):
        assert _weakest_strongest([]) == ([], [])


class TestScanFunnelBounding:
    """``ScanFunnel.offer_near_miss`` keeps only the ``top_n`` closest-to-
    clearing candidates, in a bounded min-heap -- not by collecting every
    near-miss and sorting at the end."""

    def test_keeps_only_the_closest_top_n_by_margin(self):
        funnel = ScanFunnel(top_n=2)
        for i, margin in enumerate([-10.0, -1.0, -5.0, -0.5, -20.0]):
            funnel.offer_near_miss(
                NearMiss(
                    symbol=f"S{i}",
                    strategy="stub",
                    reason_code="score_below_threshold",
                    metric=50.0 + margin,
                    threshold=50.0,
                    margin=margin,
                )
            )
        funnel.finalize()
        assert [nm.margin for nm in funnel.near_misses] == [-0.5, -1.0]

    def test_zero_top_n_keeps_no_near_misses(self):
        funnel = ScanFunnel(top_n=0)
        funnel.offer_near_miss(
            NearMiss(symbol="S", strategy="stub", reason_code="score_below_threshold", metric=1.0, threshold=2.0, margin=-1.0)
        )
        funnel.finalize()
        assert funnel.near_misses == []

    def test_record_aggregates_by_strategy_and_reason(self):
        funnel = ScanFunnel()
        funnel.record(strategy="s1", reason_code="illiquid")
        funnel.record(strategy="s1", reason_code="illiquid")
        funnel.record(strategy="s2", reason_code="earnings_window")
        assert funnel.total_rejections == 3
        assert funnel.by_reason == {"illiquid": 2, "earnings_window": 1}
        assert funnel.by_strategy_reason == {"s1": {"illiquid": 2}, "s2": {"earnings_window": 1}}
        assert "s1: 2 (illiquid=2)" in funnel.table_lines()[1]

    def test_table_lines_handle_the_empty_funnel(self):
        assert ScanFunnel().table_lines() == ["No rejections."]


class TestGateFailureCodes:
    """``apply_hard_gates`` codes are stable, normalised keys -- distinct
    from the human-readable ``message`` that embeds the candidate's own
    numbers -- so the funnel can aggregate on them."""

    def test_reward_risk_floor_gets_a_stable_code(self, tmp_app_config: AppConfig):
        ctx, proposal = _ctx_and_good_proposal(tmp_app_config, "X", entry=100.0, stop=99.0, target=101.0)
        failures = apply_hard_gates(
            ctx=ctx, proposal=proposal, config=tmp_app_config, security=ctx.security, sentiment=None
        )
        codes = [f.code for f in failures]
        assert codes == ["reward_risk_floor"]
        assert "reward:risk" in failures[0].message

    def test_illiquid_code_matches_the_strategy_level_convention(self, tmp_app_config: AppConfig):
        """The engine gate and every strategy's own hard veto for the same
        underlying condition share the code "illiquid" -- see
        ``signals.scoring.apply_hard_gates``'s docstring -- so the funnel
        rolls both up under one bucket."""
        ctx, proposal = _ctx_and_good_proposal(tmp_app_config, "X", adv=100.0)
        failures = apply_hard_gates(
            ctx=ctx, proposal=proposal, config=tmp_app_config, security=ctx.security, sentiment=None
        )
        assert any(f.code == "illiquid" for f in failures)


# --------------------------------------------------------------------------
# Fixtures / helpers for the engine-level scripted scan
# --------------------------------------------------------------------------


def _bars(n: int, *, close: float = 100.0) -> list[Bar]:
    bars: list[Bar] = []
    day = SESSION
    for _ in range(n):
        bars.append(
            Bar(
                symbol="X",
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


#: Features engineered to clear every hard gate/liquidity/volatility filter
#: in ``signals.scoring`` with a comfortable margin (mirrors
#: ``test_engine.py``'s ``_qualifying_features``), so each scenario below is
#: isolated to the one axis it is testing.
_GOOD_FEATURES = {
    "avg_dollar_volume_20": 50_000_000.0,
    "atr_pct": 3.0,
    "atr_14": 2.0,
    "roc_10": 10.0,
    "roc_20": 10.0,
    "rs_percentile": 90.0,
    "rel_volume_20": 2.0,
    "obv_slope_10": 1.0,
}


def _cfg() -> AppConfig:
    cfg = AppConfig()
    # Isolates the engine's blended score to the strategy's own setup_score
    # component, so a scenario only needs to tune one number (setup_score)
    # to land a candidate above/below `min_overall_score` deterministically,
    # instead of needing to reverse-engineer all thirteen weighted inputs.
    # This is a *test-fixture* knob, not a change to any shipped threshold
    # or weight (see the "NO THRESHOLD CHANGES" constraint on this package).
    cfg.signals.component_weights = {"technical_setup": 1.0}
    return cfg


def _security(symbol: str) -> SecurityInfo:
    return SecurityInfo(symbol=symbol, sector="Technology", market_cap_usd=5e9, exchange="NASDAQ")


def _regime() -> RegimeState:
    # score_threshold_adjustment=0.0 pinned explicitly so `min_overall_score`
    # (55.0, unmodified) is the exact bar each scenario is tuned against.
    return RegimeState(
        session=SESSION, regime=MarketRegime.NEUTRAL, trend_score=0.0, score_threshold_adjustment=0.0
    )


def _proposal(
    setup_score: float,
    *,
    strategy_name: str,
    entry: float = 100.0,
    stop: float = 95.0,
    target: float = 115.0,
) -> StrategyProposal:
    return StrategyProposal(
        strategy=strategy_name,
        strategy_version="test",
        direction=Direction.LONG,
        entry_low=entry - 0.5,
        entry_high=entry + 0.5,
        stop_loss=stop,
        targets=[target],
        target_fractions=[1.0],
        expected_holding_days=5,
        time_stop_days=15,
        setup_score=setup_score,
    )


def _ctx(
    config: AppConfig,
    symbol: str,
    *,
    bars: int = 200,
    features: dict[str, float] | None = None,
    data_warnings: list[str] | None = None,
) -> StrategyContext:
    return StrategyContext(
        session=SESSION,
        symbol=symbol,
        bars=_bars(bars),
        features=dict(features if features is not None else _GOOD_FEATURES),
        security=_security(symbol),
        regime=_regime(),
        config=config,
        data_warnings=list(data_warnings or []),
    )


def _ctx_and_good_proposal(
    config: AppConfig, symbol: str, **proposal_kwargs
) -> tuple[StrategyContext, StrategyProposal]:
    """Convenience for the direct ``apply_hard_gates`` unit tests above."""
    features = dict(_GOOD_FEATURES)
    if "adv" in proposal_kwargs:
        features["avg_dollar_volume_20"] = proposal_kwargs.pop("adv")
    ctx = _ctx(config, symbol, features=features)
    proposal = _proposal(90.0, strategy_name="probe", **proposal_kwargs)
    return ctx, proposal


class RouterStrategy(Strategy):
    """Test double: acts only on ``target_symbol``; every other symbol gets
    a silent ``None`` (no ``decline()`` call), so several of these can share
    one scan without cross-contaminating each other's funnel entries."""

    version = "test"
    min_history_bars = 1
    requires_sentiment = False
    target_symbol = ""

    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        if ctx.symbol != self.target_symbol:
            return None
        return self._act(ctx)

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        raise NotImplementedError


class HardVetoStrategy(RouterStrategy):
    """Fires the strategy-stage hard-veto path (stage="strategy"), carrying
    the same structured ``metrics`` the production strategies now attach to
    their ``insufficient_history`` declines."""

    name = "hard_veto_stub"
    target_symbol = "VETO"
    min_history_bars = 60

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        self.decline(
            ctx,
            "insufficient_history",
            f"{len(ctx.bars)} bars",
            metrics={
                "bars_available": float(len(ctx.bars)),
                "bars_required": float(self.min_history_bars),
            },
        )
        return None


class StrategyNearMissStrategy(RouterStrategy):
    """Fires the strategy-stage "score_below_threshold" near-miss path,
    using a real ``ScoreAccumulator`` exactly as the production strategies
    do -- this is what exercises ``_parse_accumulator_summary`` end to end."""

    name = "near_miss_stub"
    target_symbol = "NEARSTRAT"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        acc = ScoreAccumulator(baseline=10.0)
        acc.add("breakout", 0.3, 20.0)  # +6.0 -- the "strongest" component
        acc.add("trend", 0.5, 10.0)  # +5.0
        acc.penalty("risk_flag", -2.0)  # -2.0 -- the "weakest" component
        self.decline(ctx, "score_below_threshold", acc.summary(threshold=48.0))
        return None


class GateFailStrategy(RouterStrategy):
    """A structurally valid proposal with reward:risk below the configured
    floor -- fires the engine's "gates" stage (stage="gates")."""

    name = "gate_fail_stub"
    target_symbol = "GATEFAIL"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        return _proposal(90.0, strategy_name=self.name, entry=100.0, stop=99.0, target=101.0)


class ScoreFailStrategy(RouterStrategy):
    """Clears every gate but the engine's blended score (== setup_score
    here, see ``_cfg``) sits below ``min_overall_score`` -- fires the
    engine's "score" stage."""

    name = "score_fail_stub"
    target_symbol = "SCOREFAIL"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        return _proposal(20.0, strategy_name=self.name)


class ConfidenceFailStrategy(RouterStrategy):
    """Clears score but the data-confidence gate (thin history + data
    warnings, via the "dirty" context the test wires to this symbol) sits
    below ``min_confidence`` -- fires the engine's "score"-stage confidence
    check."""

    name = "confidence_fail_stub"
    target_symbol = "CONFFAIL"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        return _proposal(90.0, strategy_name=self.name)


class SizingZeroStrategy(RouterStrategy):
    """Clears gates/score/confidence but the stop is so far from entry that
    every risk-budget cap floors the size to zero shares -- fires "sizing"."""

    name = "sizing_zero_stub"
    target_symbol = "SIZEZERO"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        return _proposal(
            90.0, strategy_name=self.name, entry=100.0, stop=-999_900.0, target=2_000_000.0
        )


class PassStrategy(RouterStrategy):
    """Clears every stage -- proves signals are not counted as rejections."""

    name = "pass_stub"
    target_symbol = "PASS"

    def _act(self, ctx: StrategyContext) -> StrategyProposal | None:
        return _proposal(90.0, strategy_name=self.name)


# --------------------------------------------------------------------------
# The scripted scan
# --------------------------------------------------------------------------


class TestScanFunnelEveryStage:
    def test_every_rejection_stage_fires_and_is_attributed(self):
        cfg = _cfg()
        strategies = [
            HardVetoStrategy(cfg),
            StrategyNearMissStrategy(cfg),
            GateFailStrategy(cfg),
            ScoreFailStrategy(cfg),
            ConfidenceFailStrategy(cfg),
            SizingZeroStrategy(cfg),
            PassStrategy(cfg),
        ]
        engine = SignalEngine(cfg, strategies=strategies, generate_thesis=False)

        contexts = [
            _ctx(cfg, "VETO", bars=3),  # 3 real bars: the metrics below cite them
            _ctx(cfg, "NEARSTRAT"),
            _ctx(cfg, "GATEFAIL"),
            _ctx(cfg, "SCOREFAIL"),
            _ctx(
                cfg,
                "CONFFAIL",
                bars=60,
                data_warnings=["stale price feed", "thin social sample", "provider fallback used"],
            ),
            _ctx(cfg, "SIZEZERO"),
            _ctx(cfg, "PASS"),
        ]

        result = engine.scan(contexts, session=SESSION, regime=_regime(), near_miss_top_n=5)

        # --- the passing candidate is a signal, never a rejection ----------
        assert [s.symbol for s in result.signals] == ["PASS"]

        funnel = result.funnel
        assert funnel.total_rejections == 6

        # --- reason -> count, rolled up across strategies -------------------
        assert funnel.by_reason["insufficient_history"] == 1
        # One from the strategy's own ScoreAccumulator threshold (NEARSTRAT),
        # one from the engine's blended-score threshold (SCOREFAIL) -- same
        # code, same bucket.
        assert funnel.by_reason["score_below_threshold"] == 2
        assert funnel.by_reason["reward_risk_floor"] == 1
        assert funnel.by_reason["confidence_below_threshold"] == 1
        assert funnel.by_reason["sizing_zero"] == 1

        # --- per-strategy breakdown ------------------------------------------
        assert funnel.by_strategy_reason["hard_veto_stub"] == {"insufficient_history": 1}
        assert funnel.by_strategy_reason["near_miss_stub"] == {"score_below_threshold": 1}
        assert funnel.by_strategy_reason["gate_fail_stub"] == {"reward_risk_floor": 1}
        assert funnel.by_strategy_reason["score_fail_stub"] == {"score_below_threshold": 1}
        assert funnel.by_strategy_reason["confidence_fail_stub"] == {"confidence_below_threshold": 1}
        assert funnel.by_strategy_reason["sizing_zero_stub"] == {"sizing_zero": 1}
        # The router pattern means PassStrategy/every other stub evaluated
        # every other symbol too, silently -- none of that leaked in.
        assert "pass_stub" not in funnel.by_strategy_reason

        # --- near-miss ranking: closest-to-clearing first, by margin *relative
        # to threshold* so a 0-1 confidence miss and a 0-100 score miss are
        # ranked on comparable footing (see `_relative_margin`) -------------
        # NEARSTRAT: score 19 vs threshold 48 -> margin -29 -> relative -0.60.
        # SCOREFAIL: score 20 vs threshold 55 -> margin -35 -> relative -0.64.
        # CONFFAIL: confidence ~0.11 vs threshold 0.45 -> relative ~ -0.75.
        assert [nm.symbol for nm in funnel.near_misses] == ["NEARSTRAT", "SCOREFAIL", "CONFFAIL"]

        strat_nm, engine_nm, confidence_nm = funnel.near_misses
        assert strat_nm.strategy == "near_miss_stub"
        assert strat_nm.reason_code == "score_below_threshold"
        assert strat_nm.metric == pytest.approx(19.0)
        assert strat_nm.threshold == pytest.approx(48.0)
        assert strat_nm.margin == pytest.approx(-29.0)
        # Reused straight from the accumulator's own breakdown -- not
        # re-derived: the penalty is the weakest, the biggest add() the
        # strongest.
        assert strat_nm.weakest_components[0] == ("risk_flag", pytest.approx(-2.0))
        assert strat_nm.strongest_components[0] == ("breakout", pytest.approx(6.0))

        assert engine_nm.strategy == "score_fail_stub"
        assert engine_nm.reason_code == "score_below_threshold"
        assert engine_nm.metric == pytest.approx(20.0)
        assert engine_nm.threshold == pytest.approx(55.0)
        assert engine_nm.overall_score == pytest.approx(20.0)
        assert engine_nm.confidence is not None
        # technical_setup (== setup_score, see _cfg's weight override) is the
        # clear minimum among the thirteen components computed for this
        # candidate; every other axis was engineered to score well.
        assert engine_nm.weakest_components[0] == ("technical_setup", pytest.approx(20.0))
        assert engine_nm.strongest_components[0][0] == "price_momentum"

        assert confidence_nm.strategy == "confidence_fail_stub"
        assert confidence_nm.reason_code == "confidence_below_threshold"
        assert confidence_nm.threshold == pytest.approx(cfg.signals.min_confidence)
        assert confidence_nm.metric == confidence_nm.confidence
        assert confidence_nm.overall_score == pytest.approx(90.0)

        # --- rejected list still carries reason_codes alongside the text ---
        veto_row = next(r for r in result.rejected if r.symbol == "VETO")
        assert veto_row.stage == "strategy"
        assert veto_row.reason_codes == ["insufficient_history"]
        gate_row = next(r for r in result.rejected if r.symbol == "GATEFAIL")
        assert gate_row.stage == "gates"
        assert gate_row.reason_codes == ["reward_risk_floor"]
        assert "reward:risk" in gate_row.reasons[0]

        # --- F23.3: the insufficient_history bucket carries quantities -----
        veto_metrics = funnel.metrics_by_strategy_reason["hard_veto_stub"]["insufficient_history"]
        assert veto_metrics == {
            "count": 1.0,
            "bars_available": 3.0,
            "bars_required": 60.0,
        }
        # ...and the log table renders them inline: "3 of WHAT" is answered.
        assert any(
            "insufficient_history=1 (median 3/60 bars)" in line
            for line in funnel.table_lines()
        )
        # Reasons whose declines carry no metrics simply have no entry.
        assert "gate_fail_stub" not in funnel.metrics_by_strategy_reason

        # --- per-strategy requirements block: declared once per scan -------
        assert funnel.strategy_requirements["hard_veto_stub"]["min_history_bars"] == 60
        assert funnel.strategy_requirements["pass_stub"]["min_history_bars"] == 1

        # --- F23.2: sentiment coverage for the evaluated session -----------
        assert result.sentiment_coverage["symbols_evaluated"] == 7
        assert result.sentiment_coverage["symbols_with_sentiment"] == 0
        assert "warming up" in result.sentiment_coverage["note"]

    def test_limits_stage_fires_on_a_saturated_portfolio(self):
        """A portfolio already at the concurrent-position cap: the candidate
        sizes fine (no risk-budget issue) but ``check_new_position`` refuses
        it -- stage="limits", distinct from "sizing"."""
        cfg = _cfg()
        strategy = PassStrategy(cfg)
        engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        ctx = _ctx(cfg, "PASS")

        positions = [
            OpenPosition(
                symbol=f"HOLD{i}",
                direction=Direction.LONG,
                shares=10,
                entry_price=50.0,
                stop_price=50.0,  # zero open risk: isolates the concurrent-
                # position breach from the portfolio-heat one.
                sector="Technology",
            )
            for i in range(cfg.risk.max_concurrent_positions)
        ]
        portfolio = PortfolioState(
            equity=cfg.risk.account_size_usd, cash=cfg.risk.account_size_usd, positions=positions
        )

        result = engine.scan([ctx], session=SESSION, regime=_regime(), portfolio=portfolio)

        assert result.signals == []
        assert result.funnel.total_rejections == 1
        [reason_code] = result.funnel.by_reason
        assert "concurrent position limit" in reason_code
        assert result.rejected[0].stage == "limits"

    def test_funnel_present_and_empty_on_a_clean_scan(self):
        """Every scan carries a funnel, even a 0-rejection one -- matching
        the backtest ``RejectionFunnel``'s "always present" guarantee."""
        cfg = _cfg()
        strategy = PassStrategy(cfg)
        engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)

        result = engine.scan([_ctx(cfg, "PASS")], session=SESSION, regime=_regime())

        assert len(result.signals) == 1
        assert result.funnel.total_rejections == 0
        assert result.funnel.near_misses == []
        assert result.funnel.by_reason == {}


# --------------------------------------------------------------------------
# Quantitative rejection context (F23.3) + sentiment coverage (F23.2)
# --------------------------------------------------------------------------


class TestFunnelMetricsAggregation:
    """``ScanFunnel.record(metrics=...)`` -> ``finalize()`` aggregation rules."""

    def test_varying_samples_expand_constant_samples_collapse(self):
        funnel = ScanFunnel()
        for available in (1.0, 3.0, 12.0):
            funnel.record(
                strategy="s1",
                reason_code="insufficient_history",
                metrics={"bars_available": available, "bars_required": 60.0},
            )
        funnel.finalize()

        summary = funnel.metrics_by_strategy_reason["s1"]["insufficient_history"]
        assert summary == {
            "count": 3.0,
            "bars_required": 60.0,  # identical in every sample -> bare key
            "bars_available_min": 1.0,
            "bars_available_median": 3.0,
            "bars_available_max": 12.0,
        }
        assert any(
            "insufficient_history=3 (median 3/60 bars)" in line
            for line in funnel.table_lines()
        )

    def test_metric_free_records_change_nothing(self):
        funnel = ScanFunnel()
        funnel.record(strategy="s1", reason_code="illiquid")
        funnel.finalize()
        assert funnel.metrics_by_strategy_reason == {}
        assert funnel.table_lines()[1] == "  s1: 1 (illiquid=1)"

    def test_to_dict_is_additive_over_the_previous_shape(self):
        """The React RejectionFunnelPanel and stored artifacts read the
        pre-existing keys; they must survive byte-for-byte, with the new
        context under NEW keys only."""
        funnel = ScanFunnel(top_n=5)
        funnel.record(
            strategy="s1",
            reason_code="insufficient_history",
            metrics={"bars_available": 3.0, "bars_required": 60.0},
        )
        funnel.strategy_requirements = {"s1": {"min_history_bars": 60}}
        funnel.finalize()
        payload = funnel.to_dict()

        # The original contract, unchanged:
        assert payload["top_n"] == 5
        assert payload["total_rejections"] == 1
        assert payload["by_reason"] == {"insufficient_history": 1}
        assert payload["by_strategy_reason"] == {"s1": {"insufficient_history": 1}}
        assert payload["near_misses"] == []
        # The additive keys:
        assert payload["metrics_by_strategy_reason"]["s1"]["insufficient_history"][
            "bars_required"
        ] == 60.0
        assert payload["strategy_requirements"] == {"s1": {"min_history_bars": 60}}

    def test_finalize_releases_the_raw_samples(self):
        funnel = ScanFunnel()
        funnel.record(
            strategy="s1",
            reason_code="insufficient_history",
            metrics={"bars_available": 3.0},
        )
        funnel.finalize()
        assert funnel._metric_samples == {}
        assert funnel._metric_counts == {}


class TestProductionStrategiesCarryMetrics:
    """End to end with the real strategy classes: a 3-bar context makes every
    production strategy decline ``insufficient_history`` with structured
    numbers, and the funnel surfaces each strategy's own declared minimum --
    so "insufficient_history (3/20)" is now "3 of sentiment_breakout's 80"."""

    def test_short_history_scan_reports_available_vs_required(self):
        from claudetrade.strategies.registry import build_strategies

        cfg = AppConfig()
        engine = SignalEngine(cfg, strategies=build_strategies(cfg), generate_thesis=False)
        result = engine.scan([_ctx(cfg, "TINY", bars=3)], session=SESSION, regime=_regime())

        metrics = result.funnel.metrics_by_strategy_reason
        breakout = metrics["sentiment_breakout"]["insufficient_history"]
        assert breakout["bars_available"] == 3.0
        assert breakout["bars_required"] == 80.0
        assert breakout["count"] == 1.0

        # Every production strategy's declared minimum is surfaced once, so
        # "is each minimum deliberate?" is answerable from scan output alone.
        requirements = result.funnel.strategy_requirements
        assert {
            name: req["min_history_bars"] for name, req in requirements.items()
        } == {
            "sentiment_breakout": 80,
            # The relabelled unconfirmed-breakout path (QA #7). It inherits
            # sentiment_breakout's mechanics, including its bar minimum, so
            # the two cannot drift apart on how a breakout is found.
            "volume_breakout": 80,
            "sentiment_pullback": 100,
            "capitulation_reversal": 100,
            "hype_failure_short": 80,
            "post_earnings_drift": 80,
        }
        # And each insufficient_history bucket cites ITS OWN strategy's bar.
        for name, reasons in metrics.items():
            summary = reasons.get("insufficient_history")
            if summary is not None:
                assert summary["bars_required"] == float(
                    requirements[name]["min_history_bars"]
                )


class TestSentimentCoverage:
    """F23.2: how much of the evaluated universe carried fresh sentiment."""

    def test_mixed_coverage_is_counted(self):
        import dataclasses

        from claudetrade.domain import SymbolSentiment

        cfg = _cfg()
        engine = SignalEngine(cfg, strategies=[PassStrategy(cfg)], generate_thesis=False)
        with_sentiment = _ctx(cfg, "PASS")
        with_sentiment = dataclasses.replace(
            with_sentiment,
            sentiment=SymbolSentiment(symbol="PASS", session=SESSION, post_count=12),
        )
        without_sentiment = _ctx(cfg, "OTHER")

        result = engine.scan(
            [with_sentiment, without_sentiment], session=SESSION, regime=_regime()
        )

        assert result.sentiment_coverage == {
            "symbols_with_sentiment": 1,
            "symbols_evaluated": 2,
            "note": (
                "sentiment components score neutral for symbols without stored "
                "sentiment (warming up)"
            ),
        }

    def test_coverage_present_even_on_an_empty_scan(self):
        cfg = _cfg()
        engine = SignalEngine(cfg, strategies=[PassStrategy(cfg)], generate_thesis=False)
        result = engine.scan([], session=SESSION, regime=_regime())
        assert result.sentiment_coverage["symbols_evaluated"] == 0
        assert result.sentiment_coverage["symbols_with_sentiment"] == 0


# --------------------------------------------------------------------------
# Cross-process persistence (signals.funnel_store)
# --------------------------------------------------------------------------


class TestFunnelStore:
    def test_round_trips_through_the_artifact_file(self, tmp_app_config: AppConfig):
        cfg = _cfg()
        strategy = ScoreFailStrategy(cfg)
        engine = SignalEngine(cfg, strategies=[strategy], generate_thesis=False)
        result = engine.scan([_ctx(cfg, "SCOREFAIL")], session=SESSION, regime=_regime())
        assert result.funnel.total_rejections == 1

        funnel_store.save(tmp_app_config, result)
        loaded = funnel_store.load_latest(tmp_app_config)

        assert loaded is not None
        assert loaded["session"] == SESSION.isoformat()
        assert loaded["rejected_count"] == 1
        assert loaded["funnel"]["by_reason"] == {"score_below_threshold": 1}
        assert loaded["funnel"]["near_misses"][0]["symbol"] == "SCOREFAIL"
        # Additive context rides along for cross-process "why no picks?"
        # readers: coverage of the sentiment warm-up plus each strategy's
        # declared requirements.
        assert loaded["sentiment_coverage"]["symbols_evaluated"] == 1
        assert loaded["sentiment_coverage"]["symbols_with_sentiment"] == 0
        assert loaded["funnel"]["strategy_requirements"]["score_fail_stub"][
            "min_history_bars"
        ] == 1
        assert loaded["funnel"]["metrics_by_strategy_reason"] == {}

    def test_a_later_scan_overwrites_the_artifact(self, tmp_app_config: AppConfig):
        cfg = _cfg()
        engine = SignalEngine(cfg, strategies=[ScoreFailStrategy(cfg)], generate_thesis=False)
        result1 = engine.scan([_ctx(cfg, "SCOREFAIL")], session=SESSION, regime=_regime())
        funnel_store.save(tmp_app_config, result1)

        engine2 = SignalEngine(cfg, strategies=[HardVetoStrategy(cfg)], generate_thesis=False)
        result2 = engine2.scan([_ctx(cfg, "VETO")], session=SESSION, regime=_regime())
        funnel_store.save(tmp_app_config, result2)

        loaded = funnel_store.load_latest(tmp_app_config)
        assert loaded["funnel"]["by_reason"] == {"insufficient_history": 1}

    def test_load_latest_is_none_when_nothing_has_been_saved(self, tmp_app_config: AppConfig):
        assert funnel_store.load_latest(tmp_app_config) is None

    def test_load_latest_degrades_on_a_corrupt_file(self, tmp_app_config: AppConfig):
        path = funnel_store.artifact_path(tmp_app_config)
        path.write_text("not json{{{")
        assert funnel_store.load_latest(tmp_app_config) is None

    def test_pipeline_scan_without_data_warns_and_preserves_artifact(
        self, tmp_app_config: AppConfig, tmp_db
    ):
        """A scan against a database with no price bars must fail loudly --
        an explicit warning naming the missing session -- and must NOT write
        (or clobber) the funnel artifact: a degenerate zero-context scan
        carries no diagnostic value, while the previous artifact may."""
        from claudetrade.pipeline import Pipeline

        pipeline = Pipeline(tmp_app_config, tmp_db)
        assert funnel_store.load_latest(tmp_app_config) is None

        result = pipeline.scan(SESSION)

        assert any("No price bars" in w for w in result.warnings)
        assert funnel_store.load_latest(tmp_app_config) is None
