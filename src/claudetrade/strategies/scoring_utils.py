"""Shared score-accumulation helpers for the ADR-0007 Decision 2 strategies.

Every strategy in this package used to be an AND-chain of absolute-threshold
gates: the first unmet condition returned ``None`` and nothing else about the
candidate was ever recorded. Across 46 symbols x 5 strategies on a fully
populated dataset that produced one proposal. This module supplies the
building blocks the rewritten strategies use instead:

* :class:`ScoreAccumulator` -- each condition contributes a weighted, partial
  -credit component (a "how well is this met", not a "is this met") to a
  running score, with a human-readable breakdown kept alongside it so a
  near-miss decline can say exactly which components fell short and by how
  much (see ``Strategy.decline``).
* :func:`ramp_up` / :func:`ramp_down` -- smooth linear scoring instead of a
  hard cutoff, so a condition met at 90% of the target contributes 90% of its
  points rather than zero.
* :func:`percentile_rank` / :func:`zscore` -- a symbol's OWN trailing history
  as the reference frame for a condition, computed strictly from values dated
  at or before the decision session (callers pass already-truncated history;
  see ``StrategyContext.sentiment_history``, which the context builder never
  populates with future sessions). This is the on-the-fly counterpart to the
  bar-derived percentile FEATURES baked into
  ``features.feature_builder`` (``*_pctl_120`` columns, built with the
  already-causal ``features.indicators.rolling_percentile``) -- the two exist
  side by side because bar history is available as engineered features while
  sentiment history is only available as a list of past snapshots on the
  context.

Sources (see docs/decisions/ADR-0007-reference-component-adoption.md,
Decision 2): the score-accumulation SHAPE (weighted contributions, a small
hard-veto list, threshold-based emission) is patterned -- ideas only, no code
-- on gr8monk3ys/trading-bot ``strategies/momentum/signals.py::_generate_signal``
(GPL/MIT license conflict; nothing from that file is reproduced here, only
its structural idea). The percentile-rank-vs-own-history technique is
adapted with attribution from gsaini/financial-research-analyst-agent
``src/tools/peer_comparison.py`` (percentile scoring, lines ~431-457) and
``src/tools/anomaly_detector.py`` (rolling z-scores), both MIT, and from
Mattbusel/Reddit-Options-Trader-ROT- ``src/rot/analytics/iv_rank.py``
(52-week self-history rank shape), MIT.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


def ramp_up(value: float, low: float, high: float) -> float:
    """Linear ramp: 0.0 at/below ``low``, 1.0 at/above ``high``, clamped.

    Used for "the bigger/higher this is, the better" conditions. Replaces a
    hard ``value >= threshold`` gate with partial credit for values that
    nearly clear the bar, which is what turns an AND-chain of cliffs into a
    score that accumulates.
    """
    if high <= low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def ramp_down(value: float, high: float, low: float) -> float:
    """Linear ramp: 1.0 at/below ``low``, 0.0 at/above ``high``, clamped.

    The mirror image of :func:`ramp_up`, for "the smaller/lower this is, the
    better" conditions (an oversold percentile, a quiet down-volume reading).
    """
    if high <= low:
        return 1.0 if value <= low else 0.0
    return max(0.0, min(1.0, (high - value) / (high - low)))


def band_credit(value: float, low: float, high: float, taper: float) -> float:
    """1.0 for ``value`` inside ``[low, high]``, ramping to 0.0 over ``taper``
    beyond each edge.

    Used for "sweet spot" conditions -- a pullback that is neither too
    shallow nor too deep, an RSI reading that is reset but not broken -- where
    both an absolute miss on the low side and on the high side should cost
    points, but a near-miss on either edge should not zero the condition out.
    """
    return min(ramp_up(value, low - taper, low), ramp_down(value, high + taper, high))


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Percentile rank (0.0-1.0) of ``value`` within ``history``, itself included.

    ``history`` must already be causal -- every element known at or before the
    decision session -- this function performs no date filtering of its own.
    Mirrors the trailing-window, include-self convention of
    ``features.indicators.rolling_percentile`` so the two percentile
    definitions used across the codebase agree. Returns 0.5 (neutral) on an
    empty or all-NaN sample rather than raising, so a thin history degrades a
    score instead of crashing a scan.
    """
    values = [v for v in history if v == v]  # drop NaN (x != x is the NaN test)
    if not values:
        return 0.5
    # A constant history equal to the value itself carries no ranking
    # information, but "count <= value" would report 1.0 -- the TOP percentile
    # for a value that is merely the same as every other observation. During
    # the degenerate-sentiment incident every acceleration series was exactly
    # 0.0, and this saturation silently awarded full percentile credit to
    # dead-flat data. Neutral (0.5) is the honest answer.
    if all(v == value for v in values):
        return 0.5
    at_or_below = sum(1 for v in values if v <= value)
    return at_or_below / len(values)


def zscore(history: Sequence[float], value: float) -> float:
    """Population z-score of ``value`` within ``history``, itself included.

    Returns 0.0 (neutral) when fewer than two observations are available or
    the sample has ~zero variance, rather than dividing by zero.
    """
    values = [v for v in history if v == v]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = variance**0.5
    if std <= 1e-9:
        return 0.0
    return (value - mean) / std


@dataclass(slots=True)
class ScoreAccumulator:
    """Weighted score accumulation with a readable breakdown, per candidate.

    Structural model (pattern only -- see module docstring for attribution):
    a ``baseline`` plus a sequence of independently-scored conditions, each
    contributing up to its own ``max_points`` in proportion to how strongly it
    is met (``fraction`` in ``[0, 1]``, typically produced by ``ramp_up`` /
    ``ramp_down`` / :func:`percentile_rank`). A condition that is not met at
    all contributes zero -- it costs the candidate the opportunity, not a
    penalty -- while :meth:`penalty` exists for the few conditions that are a
    genuine negative signal (elevated hype, squeeze chatter) rather than
    merely absent evidence.

    The resulting :attr:`score` is clamped to ``[0, 100]`` and is what
    strategies use directly as ``StrategyProposal.setup_score``; comparing it
    against ``config.calibration.proposal_score_threshold`` is what decides
    whether the strategy emits a proposal at all.
    """

    baseline: float = 50.0
    parts: list[str] = field(default_factory=list)
    _delta: float = 0.0

    def add(self, label: str, fraction: float, max_points: float) -> float:
        """Score one condition. ``fraction`` is clamped to ``[0, 1]`` first."""
        frac = max(0.0, min(1.0, fraction))
        points = frac * max_points
        self._delta += points
        self.parts.append(f"{label}={points:+.1f}/{max_points:.0f}")
        return points

    def penalty(self, label: str, points: float) -> float:
        """A soft, already-signed adjustment (e.g. a risk-flag deduction)."""
        self._delta += points
        self.parts.append(f"{label}={points:+.1f}")
        return points

    @property
    def raw(self) -> float:
        """Baseline plus every contribution, unclamped (for diagnostics)."""
        return self.baseline + self._delta

    @property
    def score(self) -> float:
        return max(0.0, min(100.0, self.raw))

    @property
    def breakdown(self) -> str:
        """Human-readable component list, newest-last, for decline() detail."""
        return "; ".join(self.parts)

    def summary(self, threshold: float) -> str:
        return f"score {self.score:.1f}/100 (need {threshold:.1f}): {self.breakdown}"
