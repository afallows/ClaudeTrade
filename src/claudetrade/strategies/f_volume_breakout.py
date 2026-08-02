"""Strategy F -- Volume-Confirmed Breakout (the unconfirmed-sentiment path).

Thesis: the same breakout ``sentiment_breakout`` looks for -- price through a
clustered resistance level, on volume that ranks high against the symbol's own
history, in a trending context -- but *without* a positive social sample
standing behind it. Price and volume are the whole case.

Why this is a separate strategy rather than a branch
-----------------------------------------------------
``sentiment_breakout`` used to carry this path internally: with
``requires_sentiment = False`` it took a candidate with no social sample at
all, or with actively negative sentiment, lost a few points, and emitted the
trade under the name "sentiment breakout". That is a false claim about why the
trade exists, and it was invisible everywhere it mattered -- the ledger, the
scan output, and every per-strategy backtest statistic pooled two different
edges with two different failure modes under one label.

Splitting them costs nothing in coverage (this strategy takes exactly the
candidates the other now declines, via the shared
:func:`~claudetrade.strategies.a_sentiment_breakout.breakout_sentiment_is_confirming`
predicate) and buys three things: an honest name on every signal, separate
win-rate statistics for a thesis that genuinely has a different edge, and the
ability to disable one without the other in
``config.signals.enabled_strategies``.

The mechanics are deliberately NOT duplicated -- they are inherited from
:class:`~claudetrade.strategies.a_sentiment_breakout.BreakoutStrategyBase`, so
a change to how a breakout is found or shaped cannot apply to one strategy and
not the other.

What differs, beyond the name
------------------------------
* **Mutual exclusivity.** A candidate whose sentiment *does* confirm is
  declined here as ``sentiment_confirmed_elsewhere`` -- it belongs to
  ``sentiment_breakout``, and emitting both would put the same setup on the
  list twice under contradictory justifications.
* **Contrary sentiment is a scored negative, not merely absent evidence.**
  "Nobody is talking about it" and "the people talking about it are bearish"
  are different situations and must not score alike: the first is this
  strategy's ordinary case, the second is contradicted by the only social
  evidence available and takes an explicit penalty plus a risk line. It is
  still not a veto -- price and volume are the thesis here, and a modest
  bearish murmur should cost the candidate rank, not its existence.
* **No sentiment points are available at all**, so this strategy's ceiling is
  the 29 points of sentiment weight lower than ``sentiment_breakout``'s. That
  is the intended asymmetry rather than an oversight: a confirmed breakout
  should outrank an unconfirmed one, all else equal. Confidence falls too --
  ``signals.scoring._data_confidence_score`` already discounts a candidate
  scored without a sentiment row -- so these signals must clear the same bars
  on thinner evidence.
"""

from __future__ import annotations

from claudetrade.domain import SymbolSentiment
from claudetrade.strategies.a_sentiment_breakout import (
    BreakoutStrategyBase,
    breakout_sentiment_is_confirming,
)
from claudetrade.strategies.base import StrategyContext
from claudetrade.strategies.registry import register_strategy
from claudetrade.strategies.scoring_utils import ScoreAccumulator, ramp_up


@register_strategy
class VolumeBreakoutStrategy(BreakoutStrategyBase):
    name = "volume_breakout"
    version = "v1"
    description = "Breakout above resistance on volume, with no sentiment confirmation available"
    #: False, and honestly so: this strategy is *defined* by the absence of a
    #: confirming social sample, so the sentiment-quality hard gates in
    #: ``signals.scoring.apply_hard_gates`` must not veto it for a thin
    #: sample. Manipulation risk still applies -- that gate is not
    #: conditioned on ``requires_sentiment``, and a coordinated promotion is
    #: a reason to avoid a name whatever produced the setup.
    requires_sentiment = False

    #: Penalty when a usable sample exists and leans NEGATIVE. Sized below
    #: every core price/volume component so it re-ranks rather than
    #: eliminates, and below ``PENALTY_EXTENSION`` because a bearish murmur
    #: is weaker evidence against a breakout than the breakout already having
    #: run away from its level.
    PENALTY_CONTRARY_SENTIMENT = -12.0

    def _sentiment_precondition(self, ctx: StrategyContext) -> bool:
        confirming, _reason, _detail, _metrics = breakout_sentiment_is_confirming(self, ctx)
        if confirming:
            # Not a failure: the candidate is good, it simply has a better
            # -evidenced home. Recorded with its own reason code so the funnel
            # shows the hand-off explicitly rather than as an unexplained gap
            # between the two strategies' counts.
            self.decline(
                ctx,
                "sentiment_confirmed_elsewhere",
                "positive sentiment confirmation available; scored as sentiment_breakout",
            )
            return False
        return True

    def _score_sentiment(
        self,
        ctx: StrategyContext,
        sentiment: SymbolSentiment | None,
        score: ScoreAccumulator,
        evidence: list[str],
        risks: list[str],
    ) -> None:
        if sentiment is None or not self.sentiment_available(ctx):
            evidence.append("No usable social sample: scored on price and volume alone")
            risks.append(
                "No sentiment confirmation for this breakout; the volume is the only "
                "evidence that anyone funded the move"
            )
            return

        # A usable sample exists and did not confirm (that is why this
        # strategy, not the other, is evaluating it). Only a genuinely
        # negative reading is penalised -- a sample sitting between the
        # positive floor and zero is ambivalent, which is absent evidence,
        # not contrary evidence.
        polarity = min(sentiment.raw_sentiment, sentiment.unique_author_sentiment)
        if polarity < 0.0:
            score.penalty(
                "contrary_sentiment",
                self.PENALTY_CONTRARY_SENTIMENT * ramp_up(-polarity, 0.0, 0.4),
            )
            risks.append(
                f"The social sample leans bearish (raw {sentiment.raw_sentiment:+.2f}, "
                f"per-author {sentiment.unique_author_sentiment:+.2f}) against a long breakout"
            )
            evidence.append(
                "Breakout is on price and volume only; the available sentiment disagrees"
            )
        else:
            evidence.append(
                f"Social sample present but not confirming (raw {sentiment.raw_sentiment:+.2f}, "
                f"per-author {sentiment.unique_author_sentiment:+.2f}); scored on price and volume"
            )
            risks.append("No positive sentiment confirmation for this breakout")

    def _thesis_suffix(self) -> str:
        return ", with no confirming social sample behind it"
