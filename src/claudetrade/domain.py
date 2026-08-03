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


@dataclass(frozen=True, slots=True)
class AnalystConsensusPoint:
    """One dated point of TipRanks' ``consensusOverTime`` series.

    A trailing history of the Buy/Hold/Sell split and blended price target,
    as TipRanks itself computed and reported it at that date -- this
    application never recomputes or backfills a point, it only stores what
    was served. ``consensus`` is the same 1-5 opaque rating scale as
    ``AnalystSnapshot.consensus_rating`` (see that field's docstring for the
    scale-direction caveat).
    """

    date: dt.date
    buy: int
    hold: int
    sell: int
    consensus: int | None = None
    price_target: float | None = None


@dataclass(frozen=True, slots=True)
class AnalystRatingAction:
    """One analyst's single rating action from TipRanks' ``experts[].ratings[]``.

    ``rating_id``/``rating_label``: TipRanks' own 1/2/3 rating code, and a
    best-effort label derived from it. Confirmed against the committed
    fixtures, not merely assumed: the INTC fixture's Vivek Arya row is
    literally titled "Buy Rating Reaffirmed" under ``rating_id=1``, and the
    non-analyst StockTwits row in ``notRankedExperts`` is titled
    "...-Bearish" under ``rating_id=3`` -- so ``1="buy"``, ``3="sell"`` are
    confirmed and ``2="hold"`` follows by elimination (also consistent with
    every ``nB``/``nH``/``nS`` ordering seen). Any other value is stored with
    ``rating_label=None`` rather than guessed.

    ``action_id``/``action_label``: TipRanks does NOT document this code
    anywhere reachable from this adapter. Only two values are confirmed from
    the fixtures' own headline text: ``action_id=3`` on the TECK.B fixture's
    Brian MacArthur row, titled "upgraded to Outperform from Market Perform"
    ("upgrade"), and ``action_id=5`` on three separate rows whose titles
    read as a maintained rating ("Buy Rating Reaffirmed", a same-firm price
    target raise with no rating change) ("reiterate"). ``action_id=8``
    appears only on the excluded non-analyst StockTwits row and is left
    unmapped. Every other value this adapter has never seen (an initiation,
    a downgrade, ...) is stored as the raw ``action_id`` with
    ``action_label=None`` -- never guessed silently, per the module's own
    ADR-0008 Decision 1 posture of never fabricating meaning for an
    unconfirmed field.
    """

    date: dt.date
    firm: str
    analyst_name: str
    rating_id: int | None = None
    rating_label: str | None = None
    action_id: int | None = None
    action_label: str | None = None
    price_target: float | None = None
    old_price_target: float | None = None
    analyst_stars: float | None = None
    analyst_success_rate: float | None = None
    included_in_consensus: bool = False


@dataclass(slots=True)
class AnalystSnapshot:
    """TipRanks-sourced analyst-sentiment snapshot for one symbol, one session.

    Harvested entirely from fields already present in the ``dataForTicker``
    ``overview`` payload ``providers.market.tipranks.TipRanksProvider``
    fetches (and caches) for reference data, market caps and earnings --
    this domain object adds no new HTTP calls of its own. See
    ``providers.market.tipranks_analyst`` for the parser and the fixture
    cross-references behind every field mapping below.

    ``buy_count``/``hold_count``/``sell_count``/``analyst_count`` come from
    ``overview.latestRankedConsensus`` (``nB``/``nH``/``nS``) -- the
    RANKED-analyst subset TipRanks itself distinguishes from the broader,
    unranked pool in ``overview.consensuses[]`` (confirmed different on the
    INTC fixture: ranked ``nH=23`` vs. the unranked row's ``nH=24``).
    ``analyst_count`` is deliberately the sum of these same three ranked
    counts, not ``overview.numOfAnalysts`` (a much larger, all-time/global
    TipRanks figure unrelated to this one symbol's current coverage) -- so
    the four numbers are always internally consistent with each other.

    ``consensus_rating``/``consensus_rate`` come from the
    ``overview.consensuses[]`` row selected by ``isLatest == 1`` (and
    ``bench == 1`` when more than one such row is present -- both fixtures
    only ever carry a single row satisfying both, so multi-row selection is
    exercised defensively, not against a captured real case).
    ``consensus_rating`` is TipRanks' own opaque 1-5 scale; this adapter
    stores it as reported without asserting a Strong-Buy-to-Strong-Sell
    direction, since that direction is not independently confirmed from
    either fixture (the raw counts on both do not obviously order by it).

    ``price_target_mean``/``high``/``low``/``currency`` come from
    ``overview.ptConsensus[]``, preferring a ``bench == 1`` row (mirroring
    the ``consensuses`` selection) and falling back to whatever row is
    present when none is -- both fixtures carry exactly one row, with
    ``bench == 0``, so that fallback is the path actually exercised today.

    ``last_eps_surprise_pct``/``next_earnings_estimate_eps`` are the same
    ``portfolioHoldingData.lastReportedEps.surprise`` /
    ``nextEarningsReport.eps`` fields ``TipRanksProvider``'s own earnings
    mapping already reads (see ``_map_earnings_event``) -- duplicated onto
    this object so a caller wanting the analyst picture does not also have
    to separately query ``EarningsEventRow``.

    An empty/no-coverage symbol (no ``consensuses``, no ``experts``, no
    ``ptConsensus``) parses to ``None`` from
    ``tipranks_analyst.parse_analyst_snapshot`` rather than an all-zero
    instance of this class -- see that function's docstring. This class
    itself carries no "did this symbol have coverage" flag because an
    instance of it, by construction, only ever exists for a covered symbol.
    """

    symbol: str
    as_of_session: dt.date
    consensus_rating: int | None = None
    buy_count: int = 0
    hold_count: int = 0
    sell_count: int = 0
    consensus_rate: float | None = None
    price_target_mean: float | None = None
    price_target_high: float | None = None
    price_target_low: float | None = None
    price_target_currency: str | None = None
    analyst_count: int = 0
    consensus_over_time: list[AnalystConsensusPoint] = field(default_factory=list)
    recent_rating_actions: list[AnalystRatingAction] = field(default_factory=list)
    last_eps_surprise_pct: float | None = None
    next_earnings_estimate_eps: float | None = None
    fetched_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class InsiderTransactionMonth:
    """One monthly aggregate row from TipRanks' ``overview.
    corporateInsiderTransactions[]``.

    TipRanks reports both a RAW tally (``trans_buy_amount``/
    ``trans_sell_amount``, every Form-4-reportable transaction) and an
    "informative" subset (``informative_buy_amount``/
    ``informative_sell_amount`` -- transactions TipRanks itself judges
    meaningful, e.g. open-market buys/sells, as opposed to option exercises,
    gifts, or scheduled 10b5-1 plan sales that carry little signal about the
    insider's own view). See ``providers.market.tipranks_institutional`` for
    exactly how this module prefers the informative figure and falls back to
    the raw one only when informative is ``None`` for that side.
    """

    month: int
    year: int
    shares_bought: int | None = None
    insiders_buy_count: int = 0
    shares_sold: int | None = None
    insiders_sell_count: int = 0
    trans_buy_count: int = 0
    trans_sell_count: int = 0
    trans_buy_amount: float | None = None
    trans_sell_amount: float | None = None
    informative_buy_count: int = 0
    informative_sell_count: int = 0
    informative_buy_amount: float | None = None
    informative_sell_amount: float | None = None


@dataclass(frozen=True, slots=True)
class InsiderTransaction:
    """One individual insider's transaction, from ``overview.insiders[]`` --
    evidence-level detail (as opposed to :class:`InsiderTransactionMonth`'s
    monthly aggregates), kept for display and audit, not for scoring.

    ``action``/``operation_description``: TipRanks' own numeric ``action``
    code is NOT independently confirmed by either committed fixture (unlike
    ``tipranks_analyst``'s ``ratingId``/``actionId``, which have headline-text
    confirmation) -- it is stored raw, alongside the vendor's own
    human-readable ``insiderOperationDescription`` string (e.g. ``"Buy"``,
    ``"Grant/Award/Other Disposal"``), which this module trusts as display
    text without re-deriving a label from the numeric code.
    """

    name: str
    is_officer: bool = False
    is_director: bool = False
    is_ten_percent_owner: bool = False
    officer_title: str | None = None
    action: int | None = None
    operation_description: str | None = None
    amount: float | None = None
    number_of_shares: int | None = None
    r_date: dt.date | None = None
    estimated_shares_value: float | None = None
    link: str | None = None


@dataclass(frozen=True, slots=True)
class HedgeFundHoldingQuarter:
    """One quarterly point from ``overview.hedgeFundData.holdingsByTime[]``.

    Institutional 13F holdings are SEC-lagged by construction -- a quarter's
    row is only ever published weeks after the quarter closes, so the most
    recent row here is routinely 1-3 months stale even on the day it first
    appears. ``is_complete`` is the vendor's own flag for whether a quarter's
    figures are still being amended (seen ``True`` on every row in both
    committed fixtures; the flag exists in the schema for a not-yet-fully-
    reported quarter, which neither fixture happens to carry).
    """

    date: dt.date
    holding_amount: int | None = None
    institution_holding_percentage: float | None = None
    net_shares_change: int | None = None
    number_of_shares_bought: int | None = None
    number_of_shares_sold: int | None = None
    is_complete: bool = False


@dataclass(frozen=True, slots=True)
class HedgeFundHolderMove:
    """One notable institutional holder's latest reported move, from
    ``overview.hedgeFundData.institutionalHoldings[]``.

    ``action``: TipRanks' own numeric code, NOT independently confirmed by
    either fixture -- stored raw, same posture as ``InsiderTransaction.action``.
    ``stars``: the vendor's own track-record rating for this manager (higher
    is better), unrelated to ``change``/``change_amount`` (the position move
    itself).
    """

    manager_name: str
    institution_name: str
    action: int | None = None
    effective_date: dt.date | None = None
    value: float | None = None
    change_pct: float | None = None
    change_amount: float | None = None
    percentage_of_portfolio: float | None = None
    stars: float | None = None
    is_active: bool = True


@dataclass(slots=True)
class InstitutionalSnapshot:
    """TipRanks-sourced insider/hedge-fund ("institutional") sentiment
    snapshot for one symbol, one session.

    Harvested from the SAME ``dataForTicker`` ``overview`` payload
    ``providers.market.tipranks.TipRanksProvider`` already fetches/caches for
    reference data, market caps, earnings and (as of the prior feature)
    analyst consensus -- this object adds no new HTTP calls of its own. See
    ``providers.market.tipranks_institutional`` for the parser, the fixture
    cross-references behind each field mapping, and ``institutional_score``
    for the pure scoring function built on top of this object.

    ``insider_net_3m_usd`` is this module's OWN derived figure -- summed from
    ``insider_monthly``'s most recent (up to three) rows, preferring each
    month's ``informative_*_amount`` over its raw ``trans_*_amount`` per
    ``InsiderTransactionMonth``'s own docstring -- NOT the vendor's own
    ``overview.insiderslast3MonthsSum`` figure, which is kept separately as
    ``insider_net_3m_usd_vendor`` for display/cross-check. The two will
    usually be close but need not match exactly: the vendor total's own
    informative-vs-raw mixing rule is not documented, so this module computes
    its own figure rather than trust an opaque one for scoring.

    ``insider_confidence_stock_score``/``insider_confidence_sector_score``
    come from ``overview.insidrConfidenceSignal`` (the vendor's own typo,
    preserved in the raw field name only, not here). Best-effort CONFIRMED
    0..1 scale (see ``tipranks_institutional``'s module docstring): both
    committed fixtures show a value below the 0.5 midpoint (INTC 0.29, TECK.B
    0.08) alongside a negative ``insider_net_3m_usd_vendor`` on both -- i.e.
    lower-than-midpoint tracks net insider SELLING on both available
    fixtures, the same direction ``hedgeFundData.sentiment``'s independently
    documented 0..1 scale uses. Not vendor-documented, so treated as
    best-effort corroborating evidence rather than an authoritative label.

    An empty/no-institutional-content symbol (no insider transactions, no
    insider confidence signal, no hedge-fund data at all) parses to ``None``
    from ``tipranks_institutional.parse_institutional_snapshot`` rather than
    an all-``None`` instance of this class -- see that function's docstring.

    **Not fed into ``signals.scoring.ComponentScores`` or any strategy.**
    This is a read-only research overlay (Streamlit ticker-detail block, the
    ``get_institutional_sentiment`` MCP tool) -- see ``institutional_score``'s
    own docstring for the same caveat stated at the scoring-function level.
    """

    symbol: str
    as_of_session: dt.date
    insider_monthly: list[InsiderTransactionMonth] = field(default_factory=list)
    insider_net_3m_usd: float | None = None
    insider_net_3m_usd_vendor: float | None = None
    insider_confidence_stock_score: float | None = None
    insider_confidence_sector_score: float | None = None
    insider_confidence_raw_score: int | None = None
    num_of_insiders: int | None = None
    recent_insider_transactions: list[InsiderTransaction] = field(default_factory=list)
    hedge_fund_sentiment: float | None = None
    hedge_fund_trend_action: int | None = None
    hedge_fund_trend_value: float | None = None
    hedge_fund_holdings_by_quarter: list[HedgeFundHoldingQuarter] = field(default_factory=list)
    notable_holder_moves: list[HedgeFundHolderMove] = field(default_factory=list)
    market_cap_usd: float | None = None
    fetched_at: dt.datetime | None = None


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
class SymbolAttention:
    """How much a community is *talking about* one symbol -- not what it says.

    Deliberately separate from :class:`SymbolSentiment` and from
    :class:`SocialPost`. Aggregator sources (ApeWisdom, see
    ``providers.social.apewisdom``) publish per-ticker mention and upvote
    counts with no post text, no authors and no timestamps. Forcing them
    into ``SocialPost`` would mean inventing all three, which is precisely
    the fabricated-data failure the synthetic providers were removed for:
    ``unique_authors``, ``bot_risk``, ``duplicate_ratio`` and
    ``manipulation_risk`` are all computed *from* post-level identity and
    text, so synthesising posts would not add attention data, it would
    corrupt the manipulation model with confident-looking fiction.

    Attention is also a strictly separate axis from polarity -- see
    ``sentiment.aggregation``'s note that counting mentions toward
    bullishness "is exactly the mistake this module exists to avoid". These
    observations therefore carry no sentiment field at all: there is nothing
    in a mention count that says which way anyone is leaning.

    ``mentions_prev`` is the source's own trailing comparison (ApeWisdom's
    ``mentions_24h_ago``); it is what makes an attention *change* readable
    from a single fetch, without this application needing its own history of
    a corpus it never sees.
    """

    symbol: str
    community: str
    mentions: int = 0
    upvotes: int = 0
    mentions_prev: int | None = None
    rank: int | None = None
    rank_prev: int | None = None
    name: str = ""
    observed_at: dt.datetime | None = None

    @property
    def mention_acceleration(self) -> float:
        """Fractional change in mentions against the source's own baseline.

        Clipped to the same +/-10 band ``sentiment.aggregation`` uses for its
        post-rate version so the two are on one scale. ``None``/zero baseline
        yields 0.0 rather than an invented infinity: a symbol appearing for
        the first time has unknown acceleration, not infinite acceleration.
        """
        if not self.mentions_prev:
            return 0.0
        change = (self.mentions - self.mentions_prev) / self.mentions_prev
        return max(-10.0, min(10.0, change))


@dataclass(slots=True)
class AdanosSnapshot:
    """One platform's pre-aggregated buzz/sentiment reading for one symbol.

    Distinct from :class:`SymbolAttention` in the one way that matters:
    Adanos (``adanos.org``) publishes real polarity alongside volume --
    ``sentiment_score``, ``bullish_pct``, ``bearish_pct`` -- where ApeWisdom
    publishes mention counts only. It is still a *pre-aggregated* reading,
    exactly like ApeWisdom's: a ticker, a score, a percentage split, with no
    underlying post text, author or timestamp. Forcing it into
    :class:`SocialPost` would mean inventing all three, the same fabrication
    ``providers.social.hosted_api`` warns against and ``SymbolAttention``'s
    own docstring explains at length -- so this gets its own shape and its
    own storage path (``db.models.AdanosSnapshotRow``), never
    ``symbol_sentiment_daily``'s ``"all"`` aggregate that strategies score
    against.

    ``platform`` is one of ``"x"``, ``"reddit"``, ``"polymarket"``, ``"news"``
    -- Adanos's four source feeds, each fetched and stored separately because
    the same ticker can read very differently across them.

    ``trend_history`` is the vendor's own 7-point trailing buzz series
    (oldest first), carried through unmodified rather than recomputed from
    local history -- this application has no independent way to verify it,
    so it is stored as reported, not treated as ground truth for scoring.
    """

    symbol: str
    platform: str
    company_name: str = ""
    buzz_score: float = 0.0
    mentions: int = 0
    trend: str = ""
    sentiment_score: float | None = None
    bullish_pct: float | None = None
    bearish_pct: float | None = None
    #: Platform-specific engagement number: ``total_upvotes`` (x/reddit),
    #: ``total_liquidity`` (polymarket), or ``source_count`` -- distinct news
    #: outlets reporting on the ticker -- for the ``"news"`` platform, which
    #: has no upvotes/likes/liquidity analogue. See
    #: ``providers.social.adanos._ENGAGEMENT_FIELD``.
    engagement: float = 0.0
    trend_history: list[float] = field(default_factory=list)
    observed_at: dt.datetime | None = None


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
