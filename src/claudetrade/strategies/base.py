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
from typing import Any

from claudetrade.config import AppConfig
from claudetrade.domain import (
    Bar,
    Direction,
    EarningsEvent,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)


class LookaheadError(AssertionError):
    """Raised when data dated after the decision session reaches a strategy."""


@dataclass(slots=True)
class StrategyContext:
    """Everything a strategy is permitted to know on one session.

    Args:
        session: The decision date. Signals produced from this context may only
            be executed on a later bar (see ``BacktestConfig.execution_delay_bars``).
        symbol: Security under evaluation.
        bars: Ascending daily bars, last element dated ``session``.
        features: Indicator values as of ``session``.
        sentiment: Aggregated sentiment as of ``session``, or ``None`` when the
            social sources are disabled or the sample was too small.
        sentiment_history: Recent daily sentiment, ascending, ending at ``session``.
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
    sentiment_history: list[SymbolSentiment] = field(default_factory=list)
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
        for snapshot in self.sentiment_history:
            if snapshot.session > self.session:
                raise LookaheadError(
                    f"{self.symbol}: sentiment history dated {snapshot.session} "
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

    def decline(self, ctx: StrategyContext, reason: str, detail: str = "") -> None:
        """Record why this symbol was passed over (diagnostics only)."""
        self._rejections.append(
            StrategyRejection(strategy=self.name, symbol=ctx.symbol, reason=reason, detail=detail)
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
