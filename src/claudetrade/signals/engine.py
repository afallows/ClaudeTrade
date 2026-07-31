"""The signal engine.

Turns strategy proposals into complete, sized, ranked signals. The pipeline for
each candidate is:

1. Run every enabled strategy against the point-in-time context.
2. Score the proposal's components and apply the hard gates.
3. Size the position against the risk budget and portfolio limits.
4. Classify the signal's status relative to the current price.
5. Build a thesis, invalidation conditions and an expiry date.

Deliberate properties:

* **Sentiment alone never produces a signal.** A proposal must first come from a
  strategy whose entry conditions are grounded in price and volume, and it must
  then clear the hard gates in ``scoring.apply_hard_gates``.
* **Every signal expires.** ``expires_after`` is set from
  ``SignalConfig.signal_expiry_days`` so a missed entry cannot be revived later
  at a flattering price.
* **Signals are emitted, not mutated.** The engine hands finished signals to the
  ledger, which is append-only.
* The engine takes contexts as an argument rather than fetching data itself, so
  the live scanner and the backtester drive the identical code path. A strategy
  that behaves differently in backtest than in production is not a strategy, it
  is a bug.
"""

from __future__ import annotations

import datetime as dt
import heapq
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from claudetrade.config import AppConfig
from claudetrade.domain import (
    Direction,
    MarketRegime,
    RegimeState,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.logging_setup import get_logger
from claudetrade.providers.base import AIProvider
from claudetrade.risk.limits import LimitCheck, PortfolioState, check_new_position
from claudetrade.risk.sizing import size_position
from claudetrade.signals.ledger import make_signal_id
from claudetrade.signals.scoring import score_candidate
from claudetrade.signals.thesis import build_thesis, polish_thesis
from claudetrade.strategies.base import Strategy, StrategyContext, StrategyProposal
from claudetrade.strategies.registry import build_strategies
from claudetrade.utils.timeutils import next_trading_day, session_close_utc, utc_now
from claudetrade.version import CODE_VERSION

log = get_logger(__name__)

#: Default size of ``ScanFunnel``'s near-miss list -- see ``SignalEngine.scan``.
DEFAULT_NEAR_MISS_TOP_N = 20


@dataclass(slots=True)
class RejectedCandidate:
    """A candidate that did not become a signal, and why.

    Surfaced in the UI: "why is X not on the list?" is a question the operator
    will ask, and silence is a bad answer.
    """

    symbol: str
    strategy: str
    stage: str  # "strategy" | "gates" | "score" | "sizing" | "limits"
    reasons: list[str] = field(default_factory=list)
    #: Normalised reason code per entry in ``reasons``, same order/length --
    #: e.g. "illiquid" or "manipulation_risk" rather than the free-text
    #: message that embeds this candidate's own numbers. This is what
    #: ``ScanFunnel`` aggregates on; ``reasons`` stays human-readable text for
    #: display (the Screener's rejected-candidates table, the ``/api/signals
    #: /rejected`` payload) exactly as before this field was added.
    reason_codes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Rejection funnel (ADR-0007 Decision 3(b) extended to the live scan path):
# the backtester has had a per-run RejectionFunnel since Decision 3(b)
# (``backtest.engine.RejectionFunnel``); ``SignalEngine.scan`` previously
# only counted ``len(result.rejected)``, so a zero-signal scan's log line
# said *that* nothing cleared the bar but never *why*. ``ScanFunnel`` below
# is the scan-path counterpart: not a copy of the backtest one (a live scan
# has no fills/orders/entries to track), just the same idea -- reason ->
# count, attributable, always present even when empty -- sized for a single
# scan instead of a multi-session run.
# --------------------------------------------------------------------------


def _weakest_strongest(
    components: list[tuple[str, float]], n: int = 3
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Split a component breakdown into its ``n`` lowest- and highest-valued entries.

    Ascending for "weakest" (what dragged the candidate down, including any
    penalty entries), descending for "strongest" (what carried it). Used for
    both a strategy's own ``ScoreAccumulator`` breakdown (points contributed,
    parsed via :func:`_parse_accumulator_summary`) and the engine's blended
    ``ComponentScores`` (raw 0-100 component values) -- the two are on
    different scales, but "which ``n`` were lowest/highest" is meaningful for
    either.
    """
    ordered = sorted(components, key=lambda item: item[1])
    weakest = ordered[:n]
    strongest = list(reversed(ordered[-n:])) if ordered else []
    return weakest, strongest


#: Matches ``ScoreAccumulator.summary()``'s exact format (see
#: ``strategies.scoring_utils.ScoreAccumulator``): e.g.
#: ``"score 42.3/100 (need 48.0): breakout=+10.5/22; above_sma50=+3.2/8"``.
_ACCUMULATOR_SUMMARY_RE = re.compile(
    r"^score\s+(?P<score>[+-]?\d+(?:\.\d+)?)/100\s*"
    r"\(need\s+(?P<threshold>[+-]?\d+(?:\.\d+)?)\)\s*:\s*(?P<body>.*)$"
)
#: Matches one ``label=+points`` or ``label=+points/max_points`` component
#: within that summary's body (``ScoreAccumulator.add``/``.penalty``).
_ACCUMULATOR_COMPONENT_RE = re.compile(
    r"(?P<label>[A-Za-z_][A-Za-z0-9_]*)=(?P<points>[+-]?\d+(?:\.\d+)?)(?:/\d+(?:\.\d+)?)?"
)


def _parse_accumulator_summary(
    detail: str,
) -> tuple[float | None, float | None, list[tuple[str, float]]]:
    """Recover the score, threshold and per-condition contributions a
    strategy's own :class:`~claudetrade.strategies.scoring_utils.ScoreAccumulator`
    already computed, from the formatted detail string a ``"score_below_
    threshold"`` :meth:`~claudetrade.strategies.base.Strategy.decline` call
    records (``ScoreAccumulator.summary()``'s output).

    The accumulator instance itself is local to the strategy's ``evaluate()``
    and never reaches the engine -- only this formatted string does, via
    ``StrategyRejection.detail``. Parsing the string the accumulator already
    produced -- rather than plumbing a second, structured-data return path
    through ``Strategy.decline`` and every strategy call site -- keeps this
    reusing the accumulator's own numbers instead of re-deriving them, at the
    cost of coupling this parser to ``ScoreAccumulator.summary``'s exact
    format. Returns ``(None, None, [])`` on anything that does not match
    (a strategy declining for a different, non-score reason never reaches
    this function; a future format change would degrade to "no near-miss
    detail available" rather than raising mid-scan).
    """
    match = _ACCUMULATOR_SUMMARY_RE.match(detail)
    if not match:
        return None, None, []
    score = float(match.group("score"))
    threshold = float(match.group("threshold"))
    components = [
        (m.group("label"), float(m.group("points")))
        for m in _ACCUMULATOR_COMPONENT_RE.finditer(match.group("body"))
    ]
    return score, threshold, components


@dataclass(slots=True)
class NearMiss:
    """A below-threshold candidate close enough to the bar to be worth a second look.

    ``margin`` is ``metric - threshold``: always negative here (a candidate
    that cleared the bar is a signal, not a near-miss), closer to zero means
    closer to becoming one. ``ScanFunnel`` keeps the ``top_n`` candidates with
    the largest (closest-to-zero) margins, so the near-miss list answers
    "what almost made it" rather than just "what got rejected first".
    """

    symbol: str
    strategy: str
    reason_code: str  # "score_below_threshold" | "confidence_below_threshold"
    metric: float
    threshold: float
    margin: float
    overall_score: float | None = None
    confidence: float | None = None
    weakest_components: list[tuple[str, float]] = field(default_factory=list)
    strongest_components: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "reason_code": self.reason_code,
            "metric": self.metric,
            "threshold": self.threshold,
            "margin": self.margin,
            "overall_score": self.overall_score,
            "confidence": self.confidence,
            "weakest_components": [list(c) for c in self.weakest_components],
            "strongest_components": [list(c) for c in self.strongest_components],
        }


def _relative_margin(candidate: NearMiss) -> float:
    """``margin`` as a fraction of ``threshold`` -- the near-miss ranking key.

    Puts a score-threshold miss (0-100 scale) and a confidence-threshold
    miss (0-1 scale) on comparable footing: "60% of the way to the bar" means
    the same thing regardless of what the bar's units are. Falls back to the
    raw margin when ``threshold`` is zero (nothing to divide by).
    """
    if candidate.threshold == 0:
        return candidate.margin
    return candidate.margin / abs(candidate.threshold)


@dataclass(slots=True)
class ScanFunnel:
    """Aggregated, memory-bounded rejection reasons for one scan.

    Two things survive a scan of any size: a ``strategy -> reason_code ->
    count`` table (bounded by the small, fixed set of reason codes the code
    produces, not by the number of symbols scanned) and a ``top_n``-bounded
    near-miss list maintained with a min-heap so it never holds more than
    ``top_n`` candidates in memory regardless of how many thousand
    below-threshold candidates the scan evaluates. Full per-candidate detail
    is NOT retained here -- that already lives on ``ScanResult.rejected``
    (unbounded, pre-existing, unaffected by this class) for the caller that
    wants every row; this funnel exists so "why zero signals" is answerable
    from one log block or one API response instead of scrolling thousands of
    rows of it.
    """

    top_n: int = DEFAULT_NEAR_MISS_TOP_N
    total_rejections: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    by_strategy_reason: dict[str, dict[str, int]] = field(default_factory=dict)
    near_misses: list[NearMiss] = field(default_factory=list)
    #: Bounded min-heap of ``(margin, tiebreak, NearMiss)`` while the scan is
    #: in progress; drained into ``near_misses`` (best-first) by
    #: :meth:`finalize`. Not part of the public shape of this class -- excluded
    #: from ``to_dict()`` -- and reset once finalized so nothing beyond
    #: ``top_n`` candidates is ever retained.
    _heap: list[tuple[float, int, NearMiss]] = field(
        default_factory=list, init=False, repr=False
    )
    _counter: int = field(default=0, init=False, repr=False)

    def record(self, *, strategy: str, reason_code: str) -> None:
        """Count one rejection under ``strategy``/``reason_code``."""
        self.total_rejections += 1
        self.by_reason[reason_code] = self.by_reason.get(reason_code, 0) + 1
        per_strategy = self.by_strategy_reason.setdefault(strategy, {})
        per_strategy[reason_code] = per_strategy.get(reason_code, 0) + 1

    def offer_near_miss(self, candidate: NearMiss) -> None:
        """Consider ``candidate`` for the bounded top-``top_n`` near-miss list.

        Ranked by ``margin`` *relative to* ``threshold``, not the raw
        difference: a strategy's own score threshold is on a 0-100 scale
        while the confidence threshold is 0-1, so an absolute margin of
        ``-0.3`` (75% short of a 0.45 confidence bar) would otherwise always
        look "closer" than an absolute margin of ``-5`` on a 0-100 score
        scale (roughly 9% short) purely because of the metric's scale, not
        because it is actually a closer miss.
        """
        if self.top_n <= 0:
            return
        self._counter += 1
        rank_key = _relative_margin(candidate)
        entry = (rank_key, self._counter, candidate)
        if len(self._heap) < self.top_n:
            heapq.heappush(self._heap, entry)
        elif rank_key > self._heap[0][0]:
            heapq.heapreplace(self._heap, entry)

    def finalize(self) -> None:
        """Drain the bounded heap into ``near_misses``, best (closest) first."""
        self.near_misses = [
            item[2] for item in sorted(self._heap, key=lambda item: item[0], reverse=True)
        ]
        self._heap = []

    def table_lines(self) -> list[str]:
        """Human-readable ``reason -> count`` table, one line per strategy."""
        if not self.total_rejections:
            return ["No rejections."]
        lines = [f"Total rejections: {self.total_rejections}"]
        for strategy in sorted(self.by_strategy_reason):
            reasons = self.by_strategy_reason[strategy]
            top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)
            detail = ", ".join(f"{reason}={count}" for reason, count in top)
            lines.append(f"  {strategy}: {sum(reasons.values())} ({detail})")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "total_rejections": self.total_rejections,
            "by_reason": dict(self.by_reason),
            "by_strategy_reason": {k: dict(v) for k, v in self.by_strategy_reason.items()},
            "near_misses": [nm.to_dict() for nm in self.near_misses],
        }


@dataclass(slots=True)
class ScanResult:
    """Output of one scan."""

    session: dt.date
    generated_at: dt.datetime
    regime: RegimeState
    signals: list[Signal] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    evaluated_symbols: int = 0
    data_snapshot_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    #: signal_id -> error message, for signals the caller tried to persist
    #: (via ``SignalLedger.record_or_report``) but that collided under an id
    #: already claimed by different content. Left empty by ``scan()`` itself,
    #: which only builds signals -- populated by the recording caller (see
    #: ``pipeline.scan``) so a genuine collision shows up as one signal
    #: missing from the ledger rather than aborting the whole scan.
    record_errors: dict[str, str] = field(default_factory=dict)
    #: Aggregated, memory-bounded rejection funnel -- see ``ScanFunnel``.
    #: Always present, including on a 0-rejection scan, so "why zero signals"
    #: is answerable from this field alone rather than only from
    #: ``rejected``'s full (and potentially very large) per-candidate list.
    funnel: ScanFunnel = field(default_factory=ScanFunnel)

    @property
    def longs(self) -> list[Signal]:
        return [s for s in self.signals if s.direction is Direction.LONG]

    @property
    def shorts(self) -> list[Signal]:
        return [s for s in self.signals if s.direction is Direction.SHORT]

    def top(self, n: int = 10) -> list[Signal]:
        return self.signals[:n]


class SignalEngine:
    """Generates ranked signals from point-in-time contexts."""

    def __init__(
        self,
        config: AppConfig,
        *,
        strategies: list[Strategy] | None = None,
        ai_provider: AIProvider | None = None,
        generate_thesis: bool = True,
    ):
        """
        Args:
            generate_thesis: Default for :meth:`scan`. Bulk backtests set this
                False -- building prose for hundreds of thousands of candidate
                evaluations is pure overhead and changes no decision.
        """
        self.config = config
        self.strategies = strategies if strategies is not None else build_strategies(config)
        self.ai_provider = ai_provider
        self.generate_thesis = generate_thesis

    # --- public API -------------------------------------------------------

    def scan(
        self,
        contexts: Iterable[StrategyContext],
        *,
        session: dt.date,
        regime: RegimeState,
        portfolio: PortfolioState | None = None,
        data_snapshot_hash: str = "",
        generate_thesis: bool | None = None,
        near_miss_top_n: int = DEFAULT_NEAR_MISS_TOP_N,
    ) -> ScanResult:
        """Evaluate every context and return ranked signals.

        Args:
            contexts: Point-in-time contexts, one per symbol.
            session: The decision date. Must match every context.
            regime: Classified market environment for ``session``.
            portfolio: Current portfolio, for heat and concentration checks.
                When omitted, an empty portfolio at the configured account size
                is assumed -- appropriate for a research scan.
            data_snapshot_hash: Manifest digest, stamped for reproducibility.
            generate_thesis: Overrides the engine-level default; ``None`` uses
                it. Set False in bulk backtests to skip prose building.
            near_miss_top_n: How many below-threshold candidates
                ``result.funnel.near_misses`` retains, closest-to-clearing
                first. See ``ScanFunnel``.

        Returns:
            A ``ScanResult`` whose ``signals`` are sorted best-first.
        """
        build_thesis_text = (
            self.generate_thesis if generate_thesis is None else generate_thesis
        )
        state = portfolio or PortfolioState(
            equity=self.config.risk.account_size_usd,
            cash=self.config.risk.account_size_usd,
        )
        result = ScanResult(
            session=session,
            generated_at=utc_now(),
            regime=regime,
            data_snapshot_hash=data_snapshot_hash,
            funnel=ScanFunnel(top_n=near_miss_top_n),
        )

        if state.kill_switch_engaged or self.config.trading.kill_switch_engaged:
            result.warnings.append(
                "Kill switch is engaged: candidates are shown for research but no entries "
                "will be permitted."
            )

        candidates: list[tuple[Signal, float]] = []
        for ctx in contexts:
            result.evaluated_symbols += 1
            if ctx.session != session:
                log.warning(
                    "context for %s is dated %s but the scan session is %s; skipping",
                    ctx.symbol,
                    ctx.session,
                    session,
                )
                continue
            # Structural integrity check on every context, every time.
            ctx.assert_no_lookahead()
            ctx.config = ctx.config or self.config

            for strategy in self.strategies:
                signal = self._evaluate_one(
                    ctx=ctx,
                    strategy=strategy,
                    regime=regime,
                    state=state,
                    session=session,
                    data_snapshot_hash=data_snapshot_hash,
                    generate_thesis=build_thesis_text,
                    rejected=result.rejected,
                    funnel=result.funnel,
                )
                if signal is not None:
                    candidates.append((signal, signal.overall_score))

        result.funnel.finalize()

        # Rank best-first; ties broken by confidence then reward:risk, so a
        # better-evidenced idea outranks an equally-scored guess.
        candidates.sort(
            key=lambda pair: (
                pair[1],
                pair[0].confidence,
                pair[0].plan.reward_risk_ratio,
            ),
            reverse=True,
        )
        result.signals = [s for s, _ in candidates[: self.config.signals.max_candidates]]

        if not result.signals and result.evaluated_symbols:
            result.warnings.append(
                f"No candidate cleared the thresholds across {result.evaluated_symbols} symbols. "
                "An empty list is a valid result, not a failure."
            )
        log.info(
            "scan complete: %d signals from %d symbols (%d rejections)",
            len(result.signals),
            result.evaluated_symbols,
            len(result.rejected),
        )
        if result.rejected:
            # One INFO block, not one line per candidate: the funnel table
            # (reason -> count per strategy) plus the closest 5 near-misses,
            # so a single log paste diagnoses the next zero-signal scan
            # without the operator needing to correlate thousands of
            # individual rejection lines by hand.
            block = ["Rejection funnel:"]
            block.extend(f"  {line}" for line in result.funnel.table_lines())
            if result.funnel.near_misses:
                block.append("Top near-misses (closest to clearing first):")
                for nm in result.funnel.near_misses[:5]:
                    confidence_str = (
                        f"{nm.confidence:.2f}" if nm.confidence is not None else "n/a"
                    )
                    block.append(
                        f"  {nm.symbol}/{nm.strategy} [{nm.reason_code}]: "
                        f"{nm.metric:.1f} vs {nm.threshold:.1f} threshold "
                        f"(margin {nm.margin:+.1f}), confidence={confidence_str}; "
                        f"weakest={nm.weakest_components}; strongest={nm.strongest_components}"
                    )
            log.info("\n".join(block))
        return result

    # --- internals --------------------------------------------------------

    def _evaluate_one(
        self,
        *,
        ctx: StrategyContext,
        strategy: Strategy,
        regime: RegimeState,
        state: PortfolioState,
        session: dt.date,
        data_snapshot_hash: str,
        generate_thesis: bool,
        rejected: list[RejectedCandidate],
        funnel: ScanFunnel,
    ) -> Signal | None:
        try:
            proposal = strategy.evaluate(ctx)
        except Exception as exc:  # one bad symbol must not abort the scan
            log.exception("strategy %s failed on %s: %s", strategy.name, ctx.symbol, exc)
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "strategy",
                    [f"error: {exc}"],
                    reason_codes=["strategy_error"],
                )
            )
            funnel.record(strategy=strategy.name, reason_code="strategy_error")
            strategy.drain_rejections()  # discard: already reported as an error above
            return None

        # Surface every ``decline()`` the strategy recorded -- hard vetoes and,
        # since ADR-0007 Decision 2, near-miss score shortfalls with their full
        # component breakdown. This is what makes "why didn't X show up?"
        # answerable from the scan's rejected list instead of only from logs.
        strategy_declines = strategy.drain_rejections()
        if proposal is None:
            if strategy_declines:
                rejected.append(
                    RejectedCandidate(
                        ctx.symbol,
                        strategy.name,
                        "strategy",
                        [
                            f"{r.reason}: {r.detail}" if r.detail else r.reason
                            for r in strategy_declines
                        ],
                        reason_codes=[r.reason for r in strategy_declines],
                    )
                )
                for r in strategy_declines:
                    funnel.record(strategy=strategy.name, reason_code=r.reason)
                    if r.reason == "score_below_threshold":
                        score, threshold, components = _parse_accumulator_summary(r.detail)
                        if score is not None and threshold is not None:
                            weakest, strongest = _weakest_strongest(components)
                            funnel.offer_near_miss(
                                NearMiss(
                                    symbol=ctx.symbol,
                                    strategy=strategy.name,
                                    reason_code="score_below_threshold",
                                    metric=score,
                                    threshold=threshold,
                                    margin=score - threshold,
                                    weakest_components=weakest,
                                    strongest_components=strongest,
                                )
                            )
            return None

        try:
            proposal.validate()
        except ValueError as exc:
            log.error("invalid proposal from %s for %s: %s", strategy.name, ctx.symbol, exc)
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "strategy",
                    [str(exc)],
                    reason_codes=["invalid_proposal"],
                )
            )
            funnel.record(strategy=strategy.name, reason_code="invalid_proposal")
            return None

        breakdown = score_candidate(
            ctx=ctx,
            proposal=proposal,
            config=self.config,
            security=ctx.security,
            regime=regime,
            permits_earnings_risk=strategy.permits_earnings_risk,
            requires_sentiment=strategy.requires_sentiment,
        )
        if not breakdown.passed:
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "gates",
                    [gf.message for gf in breakdown.gate_failures],
                    reason_codes=[gf.code for gf in breakdown.gate_failures],
                )
            )
            for gf in breakdown.gate_failures:
                funnel.record(strategy=strategy.name, reason_code=gf.code)
            return None

        # The regime moves the bar, not the score.
        threshold = self.config.signals.min_overall_score + regime.score_threshold_adjustment
        if breakdown.overall < threshold:
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "score",
                    [f"score {breakdown.overall:.1f} below the {threshold:.1f} threshold"],
                    reason_codes=["score_below_threshold"],
                )
            )
            funnel.record(strategy=strategy.name, reason_code="score_below_threshold")
            weakest, strongest = _weakest_strongest(list(breakdown.components.as_dict().items()))
            funnel.offer_near_miss(
                NearMiss(
                    symbol=ctx.symbol,
                    strategy=strategy.name,
                    reason_code="score_below_threshold",
                    metric=breakdown.overall,
                    threshold=threshold,
                    margin=breakdown.overall - threshold,
                    overall_score=breakdown.overall,
                    confidence=breakdown.confidence,
                    weakest_components=weakest,
                    strongest_components=strongest,
                )
            )
            return None
        if breakdown.confidence < self.config.signals.min_confidence:
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "score",
                    [
                        f"confidence {breakdown.confidence:.2f} below the "
                        f"{self.config.signals.min_confidence:.2f} minimum"
                    ],
                    reason_codes=["confidence_below_threshold"],
                )
            )
            funnel.record(strategy=strategy.name, reason_code="confidence_below_threshold")
            weakest, strongest = _weakest_strongest(list(breakdown.components.as_dict().items()))
            funnel.offer_near_miss(
                NearMiss(
                    symbol=ctx.symbol,
                    strategy=strategy.name,
                    reason_code="confidence_below_threshold",
                    metric=breakdown.confidence,
                    threshold=self.config.signals.min_confidence,
                    margin=breakdown.confidence - self.config.signals.min_confidence,
                    overall_score=breakdown.overall,
                    confidence=breakdown.confidence,
                    weakest_components=weakest,
                    strongest_components=strongest,
                )
            )
            return None

        plan, limit_check = self._build_plan(
            ctx=ctx, proposal=proposal, regime=regime, state=state
        )
        if plan is None:
            stage = "sizing" if limit_check is None else "limits"
            reasons = limit_check.breaches if limit_check else ["position sized to zero shares"]
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    stage,
                    reasons,
                    reason_codes=list(reasons) if limit_check else ["sizing_zero"],
                )
            )
            if limit_check:
                for breach in limit_check.breaches:
                    funnel.record(strategy=strategy.name, reason_code=breach)
            else:
                funnel.record(strategy=strategy.name, reason_code="sizing_zero")
            return None

        status = self._classify_status(ctx, proposal)
        thesis = ""
        ai_metadata: dict[str, object] = {}
        if generate_thesis:
            thesis = build_thesis(
                ctx=ctx,
                proposal=proposal,
                regime=regime,
                reward_risk=plan.reward_risk_ratio,
                shares=plan.shares,
            )
            if self.ai_provider is not None and self.config.ai.provider != "null":
                thesis, ai_metadata = polish_thesis(
                    ai=self.ai_provider,
                    config=self.config,
                    original=thesis,
                    evidence=proposal.evidence,
                    risks=proposal.risks,
                    allowed_levels=[
                        proposal.entry_low,
                        proposal.entry_high,
                        proposal.stop_loss,
                        *proposal.targets,
                        ctx.price,
                    ],
                )

        event = ctx.next_earnings()
        signal_id = make_signal_id(
            ctx.symbol, strategy.name, session, self.config.config_hash, CODE_VERSION
        )
        freshness = self._freshness_hours(ctx, session)

        risks = list(proposal.risks)
        if limit_check is not None and limit_check.warnings:
            risks.extend(limit_check.warnings)
        if freshness > self.config.market_data.stale_after_hours:
            risks.append(f"Price data is {freshness:.0f} hours old")

        return Signal(
            signal_id=signal_id,
            created_at=utc_now(),
            session=session,
            symbol=ctx.symbol,
            company_name=ctx.security.name or ctx.symbol,
            strategy=strategy.name,
            direction=proposal.direction,
            status=status,
            reference_price=ctx.price,
            price_as_of=session_close_utc(session),
            overall_score=breakdown.overall,
            confidence=breakdown.confidence,
            components=breakdown.components,
            plan=plan,
            regime=regime.regime if regime else MarketRegime.UNKNOWN,
            next_earnings_date=event.report_date if event else None,
            days_to_earnings=ctx.days_to_earnings(),
            earnings_confirmed=bool(event and event.confirmed),
            thesis=thesis,
            invalidation=list(proposal.invalidation),
            exit_conditions=list(proposal.exit_conditions),
            evidence=list(proposal.evidence),
            risks=risks,
            data_freshness_hours=freshness,
            data_warnings=list(ctx.data_warnings) + breakdown.notes,
            expires_after=next_trading_day(
                session, skip=self.config.signals.signal_expiry_days
            ),
            code_version=CODE_VERSION,
            config_hash=self.config.config_hash,
            strategy_version=strategy.version,
            data_snapshot_hash=data_snapshot_hash,
            ai_metadata=ai_metadata,
            extras=dict(proposal.extras),
        )

    def _build_plan(
        self,
        *,
        ctx: StrategyContext,
        proposal: StrategyProposal,
        regime: RegimeState,
        state: PortfolioState,
    ) -> tuple[TradePlan | None, LimitCheck | None]:
        """Size the position and vet it against portfolio limits."""
        entry_reference = (proposal.entry_low + proposal.entry_high) / 2.0
        # Strategy-specific reduction (capitulation halves its size) composed
        # with the regime multiplier. Both may only shrink the position.
        strategy_multiplier = float(proposal.extras.get("size_multiplier", 1.0))
        multiplier = min(1.0, strategy_multiplier) * min(1.0, regime.size_multiplier)

        sizing = size_position(
            config=self.config,
            direction=proposal.direction,
            entry_price=entry_reference,
            stop_price=proposal.stop_loss,
            account_equity=state.equity,
            available_cash=state.cash,
            avg_dollar_volume=ctx.feature("avg_dollar_volume_20", 0.0) or None,
            open_heat_pct=state.open_heat_pct,
            risk_multiplier=multiplier,
        )
        if not sizing.is_tradable:
            return None, None

        limit_check = check_new_position(
            config=self.config,
            state=state,
            symbol=ctx.symbol,
            direction=proposal.direction,
            notional=sizing.notional_usd,
            dollar_risk=sizing.dollar_risk,
            sector=ctx.security.sector,
            correlation_group=ctx.security.sector,
        )
        if not limit_check.allowed:
            return None, limit_check

        plan = TradePlan(
            entry_low=proposal.entry_low,
            entry_high=proposal.entry_high,
            stop_loss=proposal.stop_loss,
            targets=list(proposal.targets),
            target_fractions=list(proposal.target_fractions),
            trailing_stop_atr=proposal.trailing_stop_atr,
            time_stop_days=min(proposal.time_stop_days, self.config.signals.max_holding_days),
            expected_holding_days=proposal.expected_holding_days,
            shares=sizing.shares,
            notional_usd=sizing.notional_usd,
            risk_per_share=sizing.risk_per_share,
            reward_per_share=proposal.reward_per_share,
            dollar_risk=sizing.dollar_risk,
        )
        return plan, limit_check

    def _classify_status(
        self, ctx: StrategyContext, proposal: StrategyProposal
    ) -> SignalStatus:
        """Where price sits relative to the proposed entry zone.

        ``EXTENDED`` exists to stop the operator chasing: once price has run
        past the zone, the reward:risk that justified the trade no longer
        applies, and the signal says so instead of quietly staying green.
        """
        price = ctx.price
        atr = ctx.atr
        extended_by = self.config.signals.extended_threshold_atr * atr

        if proposal.entry_low <= price <= proposal.entry_high:
            return SignalStatus.ACTIONABLE
        if proposal.direction is Direction.LONG:
            if price > proposal.entry_high + extended_by:
                return SignalStatus.EXTENDED
            return SignalStatus.APPROACHING
        if price < proposal.entry_low - extended_by:
            return SignalStatus.EXTENDED
        return SignalStatus.APPROACHING

    def _freshness_hours(self, ctx: StrategyContext, session: dt.date) -> float:
        """Age of the latest bar relative to that session's close."""
        if not ctx.bars:
            return 999.0
        latest = ctx.bars[-1].session
        return max(0.0, (session - latest).days * 24.0)
