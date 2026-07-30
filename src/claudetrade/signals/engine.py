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
from collections.abc import Iterable
from dataclasses import dataclass, field

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
                )
                if signal is not None:
                    candidates.append((signal, signal.overall_score))

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
    ) -> Signal | None:
        try:
            proposal = strategy.evaluate(ctx)
        except Exception as exc:  # one bad symbol must not abort the scan
            log.exception("strategy %s failed on %s: %s", strategy.name, ctx.symbol, exc)
            rejected.append(
                RejectedCandidate(ctx.symbol, strategy.name, "strategy", [f"error: {exc}"])
            )
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
                    )
                )
            return None

        try:
            proposal.validate()
        except ValueError as exc:
            log.error("invalid proposal from %s for %s: %s", strategy.name, ctx.symbol, exc)
            rejected.append(
                RejectedCandidate(ctx.symbol, strategy.name, "strategy", [str(exc)])
            )
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
                RejectedCandidate(ctx.symbol, strategy.name, "gates", breakdown.gate_failures)
            )
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
                )
            )
            return None

        plan, limit_check = self._build_plan(
            ctx=ctx, proposal=proposal, regime=regime, state=state
        )
        if plan is None:
            rejected.append(
                RejectedCandidate(
                    ctx.symbol,
                    strategy.name,
                    "sizing" if limit_check is None else "limits",
                    limit_check.breaches if limit_check else ["position sized to zero shares"],
                )
            )
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
