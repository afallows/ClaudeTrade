"""Shared domain vocabulary.

Every subsystem -- providers, features, sentiment, strategies, backtest, paper
trading, UI -- speaks in these types. They are deliberately plain
(``dataclass`` / ``Enum``) so they can cross module boundaries without dragging
in SQLAlchemy or pandas.

Persistence models in ``claudetrade.db.models`` mirror these shapes; conversion
helpers live alongside the persistence layer, not here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Direction(StrEnum):
    """Trade direction. ``FLAT`` means the engine explicitly declines to trade."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short, 0 for flat."""
        return {Direction.LONG: 1, Direction.SHORT: -1, Direction.FLAT: 0}[self]


class SignalStatus(StrEnum):
    """Lifecycle of a generated signal.

    ``EXPIRED`` exists so a missed entry cannot linger indefinitely and be
    'triggered' later at a conveniently favourable price.
    """

    ACTIONABLE = "actionable"  # entry zone is live right now
    APPROACHING = "approaching"  # price near, but not yet in, the entry zone
    EXTENDED = "extended"  # price ran past the zone; chasing is not permitted
    TRIGGERED = "triggered"  # an order was placed against this signal
    EXPIRED = "expired"  # lapsed without triggering
    REJECTED = "rejected"  # failed a hard filter or risk control


class ExitReason(StrEnum):
    """Why a position was closed. Every closed trade carries exactly one."""

    STOP_LOSS = "stop_loss"
    GAP_THROUGH_STOP = "gap_through_stop"
    TARGET = "target"
    PARTIAL_TARGET = "partial_target"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    TECHNICAL_INVALIDATION = "technical_invalidation"
    MOVING_AVERAGE_EXIT = "moving_average_exit"
    SENTIMENT_DETERIORATION = "sentiment_deterioration"
    NEGATIVE_CATALYST = "negative_catalyst"
    PRE_EARNINGS_EXIT = "pre_earnings_exit"
    DELISTED = "delisted"
    RISK_LIMIT = "risk_limit"
    KILL_SWITCH = "kill_switch"
    END_OF_BACKTEST = "end_of_backtest"
    MANUAL = "manual"


class TradeOutcome(StrEnum):
    """Classification used by the win/loss ratio.

    ``BREAKEVEN`` is reported separately and excluded from both numerator and
    denominator, so it can neither pad the win count nor hide a loss.
    """

    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"


class MarketRegime(StrEnum):
    BULL_QUIET = "bull_quiet"
    BULL_VOLATILE = "bull_volatile"
    NEUTRAL = "neutral"
    BEAR_VOLATILE = "bear_volatile"
    BEAR_QUIET = "bear_quiet"
    UNKNOWN = "unknown"

    @property
    def is_risk_on(self) -> bool:
        return self in {MarketRegime.BULL_QUIET, MarketRegime.BULL_VOLATILE}


class EarningsSession(StrEnum):
    BEFORE_OPEN = "bmo"
    AFTER_CLOSE = "amc"
    DURING = "during"
    UNKNOWN = "unknown"


class SocialSource(StrEnum):
    REDDIT = "reddit"
    X = "x"
    NEWS = "news"
    #: Stocktwits public symbol-stream API (ADR-0008 Decision 1 source
    #: expansion). Additive member; existing values are unchanged.
    STOCKTWITS = "stocktwits"
    OTHER = "other"


class DataQualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class OrderStatus(StrEnum):
    """Lifecycle of a broker order.

    Closed on purpose: every ``claudetrade.brokers.base.BrokerProvider``
    implementation -- paper today, a live adapter later -- must report one of
    these values and nothing else, so callers can reason about "is this order
    still live?" without knowing which broker placed it. Values match the
    strings already persisted by the paper broker (``PaperOrderRow.status``:
    ``"working"`` / ``"filled"``), so this enum documents behaviour that
    already existed rather than migrating it.
    """

    NEW = "new"  # constructed, not yet sent to a venue
    ACCEPTED = "accepted"  # acknowledged by the venue, not yet working
    WORKING = "working"  # live and eligible to fill
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"  # never became live: risk guard, venue reject, bad request
    EXPIRED = "expired"  # time in force lapsed unfilled
    ERROR = "error"  # broker/transport failure; status is unknown, not "safe"


#: Orders in one of these states can still fill, be cancelled, or be
#: modified. Anything else is terminal. Kept as a frozenset (not a property
#: on the enum) so a caller can test membership without importing the enum
#: class itself -- ``status in ACTIVE_STATUSES`` reads the same everywhere.
ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.WORKING,
        OrderStatus.PARTIALLY_FILLED,
    }
)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar.

    ``session`` is the exchange trading date. ``adj_close`` is split- and
    dividend-adjusted; ``close`` is the raw print. Strategies signal on adjusted
    series (continuity) but size and fill on raw prices (tradability).
    """

    symbol: str
    session: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float | None = None
    source: str = "unknown"

    @property
    def effective_adj_close(self) -> float:
        return self.adj_close if self.adj_close is not None else self.close

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def dollar_volume(self) -> float:
        return self.typical_price * self.volume

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class SecurityInfo:
    """Reference data for a listed security.

    ``delisted_date`` is populated for dead names and is what makes an unbiased
    backtest possible -- delisted securities stay in the universe rather than
    being quietly removed.
    """

    symbol: str
    name: str = ""
    exchange: str = ""
    sector: str = ""
    industry: str = ""
    market_cap_usd: float | None = None
    shares_outstanding: float | None = None
    is_etf: bool = False
    is_leveraged_or_inverse: bool = False
    listed_date: dt.date | None = None
    delisted_date: dt.date | None = None
    former_symbols: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def is_active_on(self, session: dt.date) -> bool:
        """Whether the security was listed and trading on ``session``."""
        if self.listed_date and session < self.listed_date:
            return False
        return not (self.delisted_date and session >= self.delisted_date)


@dataclass(frozen=True, slots=True)
class CorporateAction:
    symbol: str
    session: dt.date
    kind: str  # "split" | "dividend" | "symbol_change" | "delisting"
    ratio: float | None = None
    amount: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """An earnings report.

    ``confirmed`` distinguishes an exchange/company-confirmed date from a
    vendor estimate. Estimated dates are widened by a configurable uncertainty
    window before being used as a trading guard.
    """

    symbol: str
    report_date: dt.date
    session: EarningsSession = EarningsSession.UNKNOWN
    confirmed: bool = False
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    surprise_pct: float | None = None
    source: str = "unknown"
    as_of: dt.datetime | None = None

    @property
    def has_surprise(self) -> bool:
        return self.surprise_pct is not None

    def effective_risk_date_range(self, uncertainty_days: int) -> tuple[dt.date, dt.date]:
        """Date window to treat as earnings-risk-bearing.

        A confirmed date is a point; an estimate is widened symmetrically.
        """
        if self.confirmed:
            return (self.report_date, self.report_date)
        pad = dt.timedelta(days=uncertainty_days)
        return (self.report_date - pad, self.report_date + pad)


# --------------------------------------------------------------------------
# Social data
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SocialPost:
    """A sanitised social-media post or comment.

    ``text`` has already passed through ``utils.text.sanitize_social_text``:
    usernames and URLs are placeholders and instruction-like sequences are
    neutralised. ``author_hash`` is a salted digest -- the raw username is not
    retained.
    """

    source: SocialSource
    external_id: str
    created_at: dt.datetime  # timezone-aware UTC
    text: str
    community: str = ""
    score: int = 0
    num_comments: int = 0
    num_reposts: int = 0
    num_replies: int = 0
    author_hash: str = ""
    author_age_days: float | None = None
    author_karma: float | None = None
    author_followers: float | None = None
    is_comment: bool = False
    parent_id: str | None = None
    is_removed: bool = False
    is_crosspost: bool = False
    crosspost_parent: str | None = None
    text_hash: str = ""
    duplicate_group: str | None = None
    injection_risk: float = 0.0
    fetched_at: dt.datetime | None = None
    raw_ref: str | None = None
    #: Self-declared sentiment label the *source itself* attaches to a post
    #: (currently only Stocktwits, whose authors may tag a message "Bullish"
    #: / "Bearish"), normalised to ``"bullish"``/``"bearish"``/``None``.
    #: This is a PRIOR HINT, never a substitute for classification: a
    #: self-declared label is evidence a human asserted it, not truth about
    #: the post's actual sentiment, so the ensemble classifier still runs on
    #: ``text`` unconditionally. Sources with no such concept (Reddit, X,
    #: news) always leave this ``None``.
    sentiment_prior: str | None = None
    #: Reddit's own native post-type flair (``link_flair_text`` in the
    #: listing payload -- e.g. ``"DD"``, ``"YOLO"``, ``"News"``, ``"Meme"``),
    #: captured verbatim as Reddit sent it. Like ``sentiment_prior`` above,
    #: this is a PRIOR HINT the author/community attached, not a
    #: classification we vouch for -- ``sentiment.classifiers``/
    #: ``sentiment.aggregation`` treat it as a small, non-dominant nudge
    #: only, never a substitute for scoring ``text``. ``None`` when the post
    #: carries no flair or the source has no such concept (only Reddit does
    #: today). Idea: native-field capture inspired by
    #: reddit-stock-ai-agent-recommendation (MIT) -- no code copied, only
    #: the observation that Reddit already hands this field to us for free.
    flair: str | None = None

    @property
    def engagement(self) -> float:
        """Single engagement figure comparable across sources."""
        return float(
            max(0, self.score)
            + max(0, self.num_comments)
            + max(0, self.num_reposts)
            + max(0, self.num_replies)
        )


@dataclass(frozen=True, slots=True)
class TickerMention:
    """A resolved reference to a symbol inside a post.

    ``confidence`` is the entity-resolution confidence, not sentiment strength.
    Mentions below the configured threshold are dropped before aggregation --
    counting ``AI`` or ``AII`` every time someone writes about artificial
    intelligence would otherwise dominate the signal.
    """

    post_external_id: str
    symbol: str
    confidence: float
    method: str  # "cashtag" | "company_name" | "symbol_context" | "alias"
    matched_text: str = ""
    context: str = ""


@dataclass(slots=True)
class SentimentScores:
    """Multi-label output of the sentiment ensemble for one post/symbol pair.

    Labels are independent probabilities, not a softmax: a post can be both
    hyped and fearful, or sarcastic *and* bearish.
    """

    bullish: float = 0.0
    bearish: float = 0.0
    neutral: float = 0.0
    uncertainty: float = 0.0
    sarcasm: float = 0.0
    fear: float = 0.0
    hype: float = 0.0
    fomo: float = 0.0
    capitulation: float = 0.0
    earnings_speculation: float = 0.0
    product_catalyst: float = 0.0
    regulatory_catalyst: float = 0.0
    rumour: float = 0.0
    short_squeeze: float = 0.0
    pump_and_dump: float = 0.0
    position_disclosure: float = 0.0
    #: Options-chatter split -- independent 0-1 magnitudes (mirroring the
    #: bullish/bearish pair, not a single signed score) for how much the
    #: text reads as call-side vs. put-side options talk ("bought calls",
    #: "100c" strike shorthand vs. "puts", "100p"). Like bullish/bearish,
    #: a post can hit both (a spread, or hedged commentary) or neither.
    #: Idea (the calls/puts split itself): Stocksera
    #: (``scheduled_tasks/reddit/stocks/scrape_discussion_thread.py``, MIT)
    #: -- reimplemented against our own lexicon conventions, not copied.
    options_call: float = 0.0
    options_put: float = 0.0
    coordinated: float = 0.0
    confidence: float = 0.0
    classifier: str = "rules"

    @property
    def polarity(self) -> float:
        """Net directional sentiment in [-1, 1], damped by sarcasm.

        Sarcasm inverts surface polarity, so rather than trusting the sign we
        shrink the magnitude toward zero as sarcasm rises -- an ambiguous post
        should contribute little, not contribute confidently backwards.
        """
        raw = self.bullish - self.bearish
        return raw * (1.0 - 0.7 * self.sarcasm)


@dataclass(slots=True)
class SymbolSentiment:
    """Aggregated, time-decayed sentiment for one symbol on one session."""

    symbol: str
    session: dt.date
    source: str = "all"
    post_count: int = 0
    comment_count: int = 0
    unique_authors: int = 0
    raw_sentiment: float = 0.0
    engagement_weighted: float = 0.0
    credibility_weighted: float = 0.0
    unique_author_sentiment: float = 0.0
    sentiment_acceleration: float = 0.0
    mention_acceleration: float = 0.0
    bull_bear_ratio: float = 1.0
    dispersion: float = 0.0
    source_concentration: float = 0.0
    duplicate_ratio: float = 0.0
    bot_risk: float = 0.0
    manipulation_risk: float = 0.0
    confidence: float = 0.0
    hype: float = 0.0
    fear: float = 0.0
    capitulation: float = 0.0
    catalyst_quality: float = 0.0
    total_engagement: float = 0.0
    labels: dict[str, float] = field(default_factory=dict)

    @property
    def is_sufficient(self) -> bool:
        """Whether the sample is large enough to be used at full weight."""
        return self.post_count >= 5 and self.unique_authors >= 3


# --------------------------------------------------------------------------
# Signals and trades
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ComponentScores:
    """Per-component 0-100 scores backing the overall signal score.

    ``earnings_risk`` and ``manipulation_risk`` are *inverted* -- 100 means
    'no concern' -- so that every component points the same way and the
    weighted sum needs no special cases.
    """

    technical_setup: float = 50.0
    price_momentum: float = 50.0
    volume_confirmation: float = 50.0
    reddit_sentiment: float = 50.0
    x_sentiment: float = 50.0
    sentiment_acceleration: float = 50.0
    attention_acceleration: float = 50.0
    catalyst_quality: float = 50.0
    earnings_risk: float = 100.0
    liquidity: float = 50.0
    market_regime: float = 50.0
    manipulation_risk: float = 100.0
    data_confidence: float = 50.0

    def as_dict(self) -> dict[str, float]:
        return {
            "technical_setup": self.technical_setup,
            "price_momentum": self.price_momentum,
            "volume_confirmation": self.volume_confirmation,
            "reddit_sentiment": self.reddit_sentiment,
            "x_sentiment": self.x_sentiment,
            "sentiment_acceleration": self.sentiment_acceleration,
            "attention_acceleration": self.attention_acceleration,
            "catalyst_quality": self.catalyst_quality,
            "earnings_risk": self.earnings_risk,
            "liquidity": self.liquidity,
            "market_regime": self.market_regime,
            "manipulation_risk": self.manipulation_risk,
            "data_confidence": self.data_confidence,
        }


@dataclass(slots=True)
class TradePlan:
    """The executable part of a signal: where to get in, out, and how much."""

    entry_low: float
    entry_high: float
    stop_loss: float
    targets: list[float] = field(default_factory=list)
    #: Fraction of the position to exit at each target; must align with targets.
    target_fractions: list[float] = field(default_factory=list)
    trailing_stop_atr: float | None = None
    time_stop_days: int = 10
    expected_holding_days: int = 10
    shares: int = 0
    notional_usd: float = 0.0
    risk_per_share: float = 0.0
    reward_per_share: float = 0.0
    dollar_risk: float = 0.0

    @property
    def entry_reference(self) -> float:
        """Mid-point of the entry zone, used for sizing and R:R quoting."""
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def reward_risk_ratio(self) -> float:
        if self.risk_per_share <= 0:
            return 0.0
        return self.reward_per_share / self.risk_per_share


@dataclass(slots=True)
class Signal:
    """A complete, self-describing research signal.

    Once written to the ledger a signal is immutable: corrections are recorded
    as appended revisions, never as edits. That is what makes it impossible to
    improve the reported win/loss ratio by silently deleting failed signals.
    """

    signal_id: str
    created_at: dt.datetime
    session: dt.date
    symbol: str
    company_name: str
    strategy: str
    direction: Direction
    status: SignalStatus
    reference_price: float
    price_as_of: dt.datetime
    overall_score: float
    confidence: float
    components: ComponentScores
    plan: TradePlan
    regime: MarketRegime = MarketRegime.UNKNOWN
    next_earnings_date: dt.date | None = None
    days_to_earnings: int | None = None
    earnings_confirmed: bool = False
    thesis: str = ""
    invalidation: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    data_freshness_hours: float = 0.0
    data_warnings: list[str] = field(default_factory=list)
    expires_after: dt.date | None = None
    #: Reproducibility triple.
    code_version: str = ""
    config_hash: str = ""
    strategy_version: str = ""
    data_snapshot_hash: str = ""
    ai_metadata: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tradable(self) -> bool:
        return self.direction is not Direction.FLAT and self.status in {
            SignalStatus.ACTIONABLE,
            SignalStatus.APPROACHING,
        }


@dataclass(slots=True)
class Fill:
    """A single simulated execution."""

    session: dt.date
    price: float
    shares: int
    commission: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    is_partial: bool = False
    note: str = ""

    @property
    def gross_notional(self) -> float:
        return self.price * self.shares

    @property
    def total_cost(self) -> float:
        return self.commission + self.fees


@dataclass(slots=True)
class Trade:
    """A completed or open position, with everything needed to grade it.

    MFE/MAE are tracked in R-multiples as well as percentages so that outcomes
    stay comparable across names with very different volatility.
    """

    trade_id: str
    signal_id: str
    symbol: str
    strategy: str
    direction: Direction
    entry_session: dt.date
    entry_price: float
    shares: int
    stop_loss: float
    targets: list[float] = field(default_factory=list)
    exit_session: dt.date | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    fills: list[Fill] = field(default_factory=list)
    commission_total: float = 0.0
    fees_total: float = 0.0
    slippage_total: float = 0.0
    borrow_cost_total: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    initial_risk_per_share: float = 0.0
    thesis_intact_at_exit: bool | None = None
    regime_at_entry: MarketRegime = MarketRegime.UNKNOWN
    sector: str = ""
    market_cap_bucket: str = ""
    days_to_earnings_at_entry: int | None = None
    confidence_at_entry: float = 0.0
    sentiment_source: str = "none"
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_session is None

    @property
    def holding_days(self) -> int:
        if self.exit_session is None:
            return 0
        return (self.exit_session - self.entry_session).days

    @property
    def gross_pnl(self) -> float:
        """P&L before costs. Zero while the trade is open."""
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.shares * self.direction.sign

    @property
    def net_pnl(self) -> float:
        """P&L after commissions, regulatory fees and borrow."""
        if self.exit_price is None:
            return 0.0
        return self.gross_pnl - self.commission_total - self.fees_total - self.borrow_cost_total

    @property
    def net_return_pct(self) -> float:
        """Net return on the position's own notional, in percent."""
        notional = abs(self.entry_price * self.shares)
        if notional <= 0:
            return 0.0
        return 100.0 * self.net_pnl / notional

    @property
    def r_multiple(self) -> float:
        """Net P&L expressed in units of initially-risked dollars.

        R is the honest unit for grading a strategy: a +0.2R win and a -1.0R
        loss cannot be made to look equivalent by counting trades.
        """
        risk = abs(self.initial_risk_per_share * self.shares)
        if risk <= 0:
            return 0.0
        return self.net_pnl / risk

    def outcome(self, breakeven_threshold_pct: float = 0.05) -> TradeOutcome:
        """Classify the trade for win/loss accounting.

        Args:
            breakeven_threshold_pct: Net returns inside +/- this percentage are
                treated as breakeven and excluded from both counts.

        Raises:
            ValueError: if the trade is still open. Open trades are never
                classified -- letting a loser sit open would inflate the ratio.
        """
        if self.is_open:
            raise ValueError(
                f"trade {self.trade_id} is still open and cannot be classified; "
                "close it (a time stop always will) before computing win/loss"
            )
        ret = self.net_return_pct
        if abs(ret) <= breakeven_threshold_pct:
            return TradeOutcome.BREAKEVEN
        return TradeOutcome.WIN if ret > 0 else TradeOutcome.LOSS


@dataclass(frozen=True, slots=True)
class DataQualityIssue:
    """A recorded defect in input data.

    These are not just logged: unresolved ``ERROR``-severity issues suppress
    high-confidence signals for the affected symbol.
    """

    detected_at: dt.datetime
    severity: DataQualitySeverity
    category: str
    symbol: str | None
    session: dt.date | None
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RegimeState:
    """Classified market environment on a given session."""

    session: dt.date
    regime: MarketRegime
    trend_score: float = 0.0
    breadth: float = 0.5
    volatility_percentile: float = 0.5
    realised_vol_annual: float = 0.0
    risk_appetite: float = 0.0
    leading_sectors: list[str] = field(default_factory=list)
    lagging_sectors: list[str] = field(default_factory=list)
    benchmark_return_20d: float = 0.0
    #: Multipliers the signal engine applies in this environment.
    size_multiplier: float = 1.0
    score_threshold_adjustment: float = 0.0
    max_positions_multiplier: float = 1.0
    long_short_bias: float = 0.0
    notes: list[str] = field(default_factory=list)
