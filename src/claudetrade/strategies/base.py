"""Strategy interface and the point-in-time context handed to strategies.

The central integrity rule of this application lives here.

``StrategyContext`` is constructed for a specific ``session`` and contains
**only** information that was observable at that session's close:

* ``bars`` ends at ``session`` -- the constructor truncates anything later.
* ``features`` were computed from those bars alone.
* ``sentiment`` aggregates only posts created at or before the session close.
* ``earnings`` entries were filtered by their ``as_of`` knowledge date.

A strategy therefore *cannot* see the future even if it tries: there is no
handle to reach it through. ``StrategyContext.assert_no_lookahead()`` re-checks
the invariant and is called by the backtester on every construction, so a
regression in the data layer surfaces as a test failure rather than as an
implausibly good equity curve.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from claudetrade.config import AppConfig
from claudetrade.domain import (
    AnalystSnapshot,
    Bar,
    Direction,
    EarningsEvent,
    InstitutionalScorePoint,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)

if TYPE_CHECKING:
    # Deferred: importing ``data.adanos_read`` at module scope would make the
    # `data` package depend on `strategies` (already true, see
    # ``data.context``) AND `strategies` depend on `data` -- harmless given
    # Python's submodule-import semantics (neither module's import touches
    # the other's still-executing ``__init__``), but ``from __future__
    # import annotations`` above means this type is never evaluated at
    # runtime anyway, so there is no reason to pay for the real import.
    from claudetrade.data.adanos_read import AttentionAggregate


class LookaheadError(AssertionError):
    """Raised when data dated after the decision session reaches a strategy."""


#: ``SymbolSentiment.source`` prefix used by aggregator ATTENTION rows -- see
#: ``providers.social.apewisdom`` and ``data.ingest.ingest_attention``. These
#: rows are mention/upvote tallies over a community, with no post text, no
#: authors and no polarity, stored in the same table as the polarity
#: aggregates purely because the table is keyed by (symbol, session, source).
ATTENTION_SOURCE_PREFIX = "apewisdom:"


def is_attention_only(snapshot: SymbolSentiment) -> bool:
    """Whether ``snapshot`` carries attention volume but no polarity at all.

    The single most important design rule in the sentiment subsystem is that
    attention and polarity are separate axes, and this predicate is where the
    two kinds of stored row are told apart. An attention row's polarity
    columns are not "measured as zero" -- they were never measured, and
    averaging them into a polarity aggregate would silently pull every
    reading toward neutral in proportion to how many communities the
    aggregator happened to cover. Its ``unique_authors`` is 0 for the same
    reason, which is why these rows must also stay out of
    ``manipulation_risk``, ``bot_risk`` and ``duplicate_ratio``, all of which
    are derived from post-level identity and text this source does not have.

    Both the ``source`` prefix and the ``attention_only`` label are checked so
    a row survives either being relabelled or losing its label.
    """
    return snapshot.source.startswith(ATTENTION_SOURCE_PREFIX) or bool(
        snapshot.labels.get("attention_only")
    )


@dataclass(slots=True)
class StrategyContext:
    """Everything a strategy is permitted to know on one session.

    Args:
        session: The decision date. Signals produced from this context may only
            be executed on a later bar (see ``BacktestConfig.execution_delay_bars``).
        symbol: Security under evaluation.
        bars: Ascending daily bars, last element dated ``session``.
        features: Indicator values as of ``session``.
        sentiment: The COMBINED (``source == "all"``) aggregate as of
            ``session``, or ``None`` when the social sources are disabled or
            the sample was too small.
        sentiment_by_source: Per-source polarity snapshots for ``session``,
            keyed by ``SymbolSentiment.source`` ("all", "reddit", "x",
            "news", ...). Each is an independently stored row aggregated from
            that source's own posts. This exists so scoring can weight each
            source from ITS OWN evidence: the combined row used to be handed
            to every per-source scoring slot, which let one sample fill
            several independently-weighted slots and read as corroboration
            between sources that had never been compared. Attention-only rows
            are never in here -- see ``attention_by_source``.
        attention_by_source: Aggregator ATTENTION snapshots for ``session``
            (``apewisdom:<community>``), keyed by source. Mention volume
            only: no polarity, no authors, no text. Consumed by the attention
            axis alone.
        attention_history: Per-source attention snapshots, ascending and
            ending at ``session``, keyed by source. Needed because an
            aggregator's counts live on a completely different scale from
            this application's own fetches (~100x), so a reading is only
            usable once ranked against its OWN history.
        sentiment_history: Recent daily COMBINED sentiment, ascending, ending
            at ``session``. Per-source and attention rows are deliberately
            excluded: strategies percentile-rank today's combined snapshot
            against this series, and interleaving rows from other sources
            would rank a value against a distribution it does not belong to.
        analyst_history: Stored TipRanks ``AnalystSnapshot`` rows, ascending,
            ending at ``session`` (ADR-0009). Feeds
            ``signals.scoring._analyst_sentiment_score``; the last element is
            "current", the one before it (if any) is "previous" for
            ``data.analyst.analyst_delta``'s coverage-change kicker.
        institutional_history: Stored ``InstitutionalScorePoint`` rows
            (snapshot + its ingest-time ``score``), ascending, ending at
            ``session`` (ADR-0009). Feeds
            ``signals.scoring._institutional_sentiment_score``, which reads
            the last element's ``score`` straight off the row rather than
            recomputing it.
        adanos_history: Stored cross-platform Adanos ``AttentionAggregate``
            rows (``data.adanos_read``), ascending, ending at ``session``
            (ADR-0009). Feeds ``signals.scoring
            ._cross_source_attention_score``, which reads the last
            element's own ``trend_history`` for the buzz-percentile
            sub-component rather than needing several sessions of this list.
        earnings: Known earnings events, past and scheduled.
        security: Reference data.
        regime: Market environment on ``session``.
        benchmark_features: Features of the broad-market benchmark.
        sector_features: Features of the applicable sector ETF.
        data_warnings: Outstanding data-quality problems for this symbol.
    """

    session: dt.date
    symbol: str
    bars: list[Bar]
    features: dict[str, float]
    security: SecurityInfo
    regime: RegimeState
    sentiment: SymbolSentiment | None = None
    sentiment_by_source: dict[str, SymbolSentiment] = field(default_factory=dict)
    attention_by_source: dict[str, SymbolSentiment] = field(default_factory=dict)
    attention_history: dict[str, list[SymbolSentiment]] = field(default_factory=dict)
    sentiment_history: list[SymbolSentiment] = field(default_factory=list)
    analyst_history: list[AnalystSnapshot] = field(default_factory=list)
    institutional_history: list[InstitutionalScorePoint] = field(default_factory=list)
    adanos_history: list[AttentionAggregate] = field(default_factory=list)
    earnings: list[EarningsEvent] = field(default_factory=list)
    benchmark_features: dict[str, float] = field(default_factory=dict)
    sector_features: dict[str, float] = field(default_factory=dict)
    data_warnings: list[str] = field(default_factory=list)
    config: AppConfig | None = None

    def __post_init__(self) -> None:
        # Series inputs are *truncated* rather than rejected: passing a full
        # history and having it clipped to the decision session is a normal,
        # safe calling pattern, and clipping is what guarantees the strategy
        # only ever sees the past.
        if self.bars and self.bars[-1].session > self.session:
            self.bars = [b for b in self.bars if b.session <= self.session]
        if self.sentiment_history:
            self.sentiment_history = [
                s for s in self.sentiment_history if s.session <= self.session
            ]
        if self.attention_history:
            self.attention_history = {
                source: [s for s in series if s.session <= self.session]
                for source, series in self.attention_history.items()
            }
        # ADR-0009: the three new histories get the identical truncate-
        # silently treatment as sentiment_history/attention_history above --
        # over-supplying a full history and having it clipped here is the
        # same safe, normal calling pattern, not an error.
        if self.analyst_history:
            self.analyst_history = [
                a for a in self.analyst_history if a.as_of_session <= self.session
            ]
        if self.institutional_history:
            self.institutional_history = [
                p for p in self.institutional_history if p.session <= self.session
            ]
        if self.adanos_history:
            self.adanos_history = [
                a for a in self.adanos_history if a.session <= self.session
            ]
        # Everything that cannot be safely clipped -- a single sentiment
        # snapshot, an earnings row's knowledge date, the regime -- is validated
        # here, at construction. Leaving that to an explicit
        # assert_no_lookahead() call meant a caller who forgot it could build
        # and use a leaking context; now no such context can exist.
        self.assert_no_lookahead()

    # --- accessors --------------------------------------------------------

    @property
    def last_bar(self) -> Bar:
        if not self.bars:
            raise ValueError(f"no bars available for {self.symbol} on {self.session}")
        return self.bars[-1]

    @property
    def price(self) -> float:
        """Latest observable close."""
        return self.last_bar.close

    @property
    def atr(self) -> float:
        """Average true range, falling back to a fraction of price if absent."""
        value = self.features.get("atr_14", 0.0)
        if value > 0:
            return value
        return max(self.price * 0.02, 0.01)

    def feature(self, name: str, default: float = 0.0) -> float:
        """Feature lookup that tolerates absent indicators (short history)."""
        value = self.features.get(name)
        if value is None:
            return default
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            return default
        # NaN propagates silently through comparisons; treat it as missing.
        return default if fvalue != fvalue else fvalue

    def has_feature(self, name: str) -> bool:
        value = self.features.get(name)
        return value is not None and float(value) == float(value)

    def polarity_for_source(self, source: str) -> SymbolSentiment | None:
        """This session's polarity snapshot for one source, or ``None``.

        ``None`` means "that source did not report a usable sample", which is
        NOT the same as "that source reported neutral": the caller must drop
        the source's weight rather than score it 50, and must not substitute
        another source's row. Attention-only rows can never be returned here
        even if one were mis-keyed into ``sentiment_by_source``, because they
        carry no polarity to return.
        """
        snapshot = self.sentiment_by_source.get(source)
        if snapshot is None or is_attention_only(snapshot):
            return None
        return snapshot

    def next_earnings(self) -> EarningsEvent | None:
        """The soonest earnings event on or after ``session``."""
        upcoming = [e for e in self.earnings if e.report_date >= self.session]
        return min(upcoming, key=lambda e: e.report_date) if upcoming else None

    def last_earnings(self) -> EarningsEvent | None:
        """The most recent earnings event strictly before ``session``."""
        past = [e for e in self.earnings if e.report_date < self.session]
        return max(past, key=lambda e: e.report_date) if past else None

    def days_to_earnings(self) -> int | None:
        """Calendar days until the next earnings report, or ``None`` if unknown.

        For an unconfirmed (estimated) date the *earliest* plausible date is
        used, so the earnings guard errs toward caution.
        """
        event = self.next_earnings()
        if event is None:
            return None
        pad = 0
        if not event.confirmed and self.config is not None:
            pad = self.config.earnings.estimated_date_uncertainty_days
        return (event.report_date - dt.timedelta(days=pad) - self.session).days

    def days_since_earnings(self) -> int | None:
        event = self.last_earnings()
        if event is None:
            return None
        return (self.session - event.report_date).days

    # --- integrity --------------------------------------------------------

    def assert_no_lookahead(self) -> None:
        """Verify that nothing in the context post-dates the decision session.

        Raises:
            LookaheadError: on any violation. The backtester calls this for
                every context it builds.
        """
        if self.bars and self.bars[-1].session > self.session:
            raise LookaheadError(
                f"{self.symbol}: bar dated {self.bars[-1].session} exceeds session {self.session}"
            )
        if self.sentiment is not None and self.sentiment.session > self.session:
            raise LookaheadError(
                f"{self.symbol}: sentiment dated {self.sentiment.session} "
                f"exceeds session {self.session}"
            )
        # Per-source and attention snapshots are single readings for THIS
        # session, so -- like ``sentiment`` above and unlike the histories --
        # they cannot be safely clipped; a future-dated one is a bug upstream.
        for label, snapshot in (*self.sentiment_by_source.items(), *self.attention_by_source.items()):
            if snapshot.session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: {label} sentiment dated {snapshot.session} "
                    f"exceeds session {self.session}"
                )
        for snapshot in self.sentiment_history:
            if snapshot.session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: sentiment history dated {snapshot.session} "
                    f"exceeds session {self.session}"
                )
        for source, series in self.attention_history.items():
            for snapshot in series:
                if snapshot.session > self.session:
                    raise LookaheadError(
                        f"{self.symbol}: {source} attention history dated "
                        f"{snapshot.session} exceeds session {self.session}"
                    )
        # ADR-0009: the three new histories get the identical belt-and-braces
        # check as sentiment_history/attention_history above.
        for snapshot in self.analyst_history:
            if snapshot.as_of_session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: analyst history dated {snapshot.as_of_session} "
                    f"exceeds session {self.session}"
                )
        for point in self.institutional_history:
            if point.session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: institutional history dated {point.session} "
                    f"exceeds session {self.session}"
                )
        for aggregate in self.adanos_history:
            if aggregate.session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: adanos history dated {aggregate.session} "
                    f"exceeds session {self.session}"
                )
        # The regime may legitimately be absent (unclassified session); only a
        # regime dated in the future is a leak.
        if self.regime is not None and self.regime.session > self.session:
            raise LookaheadError(
                f"regime dated {self.regime.session} exceeds session {self.session}"
            )
        for event in self.earnings:
            # A *scheduled* future report is legitimate knowledge; an
            # unannounced one is not. Providers stamp ``as_of`` with the date
            # the calendar entry became public.
            if event.as_of is not None and event.as_of.date() > self.session:
                raise LookaheadError(
                    f"{self.symbol}: earnings entry for {event.report_date} was only "
                    f"known from {event.as_of.date()}, after session {self.session}"
                )


@dataclass(slots=True)
class StrategyProposal:
    """A strategy's raw idea, before risk sizing and portfolio-level vetting.

    Prices here are *levels*, not orders. The signal engine converts a proposal
    into a ``Signal`` by attaching position size, expiry, scores and a thesis,
    and may still reject it on liquidity, heat or reward:risk grounds.
    """

    strategy: str
    strategy_version: str
    direction: Direction
    entry_low: float
    entry_high: float
    stop_loss: float
    targets: list[float]
    target_fractions: list[float] = field(default_factory=list)
    expected_holding_days: int = 10
    time_stop_days: int = 15
    trailing_stop_atr: float | None = None
    #: 0-100 conviction *from this strategy's own criteria only*. Cross-cutting
    #: components (liquidity, regime, manipulation risk) are scored centrally.
    setup_score: float = 50.0
    evidence: list[str] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    thesis_hint: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Structural sanity checks.

        Raises:
            ValueError: on an incoherent proposal (stop on the wrong side,
                inverted entry zone, target behind the entry). A strategy bug
                that produced a negative-risk trade would otherwise show up as
                free money in the backtest.
        """
        if self.direction is Direction.FLAT:
            raise ValueError("a proposal must be long or short; return None to decline")
        if self.entry_high < self.entry_low:
            raise ValueError(f"{self.strategy}: entry_high {self.entry_high} < entry_low")
        if not self.targets:
            raise ValueError(f"{self.strategy}: at least one target is required")
        ref = (self.entry_low + self.entry_high) / 2.0
        if self.direction is Direction.LONG:
            if self.stop_loss >= self.entry_low:
                raise ValueError(
                    f"{self.strategy}: long stop {self.stop_loss} must sit below "
                    f"entry_low {self.entry_low}"
                )
            if any(t <= ref for t in self.targets):
                raise ValueError(f"{self.strategy}: long targets must exceed the entry reference")
        else:
            if self.stop_loss <= self.entry_high:
                raise ValueError(
                    f"{self.strategy}: short stop {self.stop_loss} must sit above "
                    f"entry_high {self.entry_high}"
                )
            if any(t >= ref for t in self.targets):
                raise ValueError(f"{self.strategy}: short targets must sit below the entry")
        if self.target_fractions:
            if len(self.target_fractions) != len(self.targets):
                raise ValueError(f"{self.strategy}: target_fractions must align with targets")
            total = sum(self.target_fractions)
            if not 0.99 <= total <= 1.01:
                raise ValueError(f"{self.strategy}: target_fractions must sum to 1 (got {total})")
        if self.time_stop_days <= 0:
            raise ValueError(f"{self.strategy}: time_stop_days must be positive")

    @property
    def risk_per_share(self) -> float:
        ref = (self.entry_low + self.entry_high) / 2.0
        return abs(ref - self.stop_loss)

    @property
    def reward_per_share(self) -> float:
        """Reward to the *final* target, the conservative reading of R:R."""
        ref = (self.entry_low + self.entry_high) / 2.0
        return abs(self.targets[-1] - ref) if self.targets else 0.0

    @property
    def reward_risk_ratio(self) -> float:
        risk = self.risk_per_share
        return self.reward_per_share / risk if risk > 0 else 0.0


@dataclass(frozen=True, slots=True)
class StrategyRejection:
    """Why a strategy declined, kept for diagnostics on the Scanner screen."""

    strategy: str
    symbol: str
    reason: str
    detail: str = ""
    #: Optional structured numbers behind the human-readable ``detail`` --
    #: e.g. ``{"bars_available": 3, "bars_required": 80}`` for an
    #: ``insufficient_history`` decline. ``detail`` stays the display string;
    #: this is what lets ``signals.engine.ScanFunnel`` aggregate *quantities*
    #: per (strategy, reason) ("median 3/80 bars") instead of only counting
    #: occurrences, without parsing prose. ``None`` (the default) means the
    #: decline carries no structured numbers, exactly as before this field.
    metrics: dict[str, float] | None = None


class Strategy(ABC):
    """Base class for all strategies.

    Subclasses implement :meth:`evaluate` and declare ``name``, ``version`` and
    ``description``. ``version`` is stamped onto every signal, so changing the
    rules invalidates comparison with older signals rather than silently
    re-labelling them.
    """

    name: str = "unnamed"
    version: str = "v1"
    description: str = ""
    direction_bias: Direction = Direction.LONG
    #: Minimum bars of history before the strategy is willing to act.
    min_history_bars: int = 60
    #: Whether the strategy is allowed to hold through an earnings report.
    permits_earnings_risk: bool = False
    #: Whether the strategy requires social sentiment to function at all.
    requires_sentiment: bool = True

    def __init__(self, config: AppConfig):
        self.config = config
        self._rejections: list[StrategyRejection] = []

    # --- interface --------------------------------------------------------

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> StrategyProposal | None:
        """Assess one symbol on one session.

        Returns:
            A proposal, or ``None`` to decline. Declining is the common case and
            is not an error -- call :meth:`decline` to record why.
        """

    # --- helpers for subclasses ------------------------------------------

    def decline(
        self,
        ctx: StrategyContext,
        reason: str,
        detail: str = "",
        metrics: dict[str, float] | None = None,
    ) -> None:
        """Record why this symbol was passed over (diagnostics only).

        Args:
            ctx: The context being declined.
            reason: Stable, normalised reason code (what the funnel counts).
            detail: Human-readable specifics with this candidate's own numbers.
            metrics: Optional structured counterpart of ``detail`` (see
                ``StrategyRejection.metrics``) so the funnel can aggregate the
                numbers, not just the count.
        """
        self._rejections.append(
            StrategyRejection(
                strategy=self.name,
                symbol=ctx.symbol,
                reason=reason,
                detail=detail,
                metrics=metrics,
            )
        )

    def drain_rejections(self) -> list[StrategyRejection]:
        """Return and clear accumulated rejections."""
        out = self._rejections
        self._rejections = []
        return out

    def has_sufficient_history(self, ctx: StrategyContext) -> bool:
        return len(ctx.bars) >= self.min_history_bars

    def earnings_blocked(self, ctx: StrategyContext) -> bool:
        """Whether an entry is forbidden by proximity to earnings.

        Applies unless the strategy explicitly permits event risk. An
        *unconfirmed* date is widened by the configured uncertainty window
        before the comparison, so an estimate is treated as the earlier bound.
        """
        if self.permits_earnings_risk:
            return False
        days = ctx.days_to_earnings()
        if days is None:
            return False
        buffer = self.config.filters.block_entry_within_days_of_earnings
        return 0 <= days <= buffer

    def sentiment_available(self, ctx: StrategyContext) -> bool:
        """Whether the sentiment sample is large enough to rely on."""
        if ctx.sentiment is None:
            return False
        cfg = self.config.sentiment
        return (
            ctx.sentiment.post_count >= cfg.min_posts_for_signal
            and ctx.sentiment.unique_authors >= cfg.min_unique_authors_for_signal
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} {self.name}@{self.version}>"
