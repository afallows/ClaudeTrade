"""Pydantic response/request models for every ``webapi`` endpoint.

Every field here is either a direct pass-through of an existing domain type
(``claudetrade.domain``) or a deliberate re-shaping for JSON:

* ``float("inf")`` (an "undefined -- no losses yet" ratio) is never sent as a
  JSON number -- standard ``JSON.parse`` on the frontend rejects the bare
  ``Infinity`` token. Ratios that can be infinite carry a ``float | None``
  field (``None`` = undefined) *and* a pre-formatted ``*_display`` string
  (``"n/a"`` / ``"∞"`` / the number) so the frontend never has to
  reinvent that formatting rule or guess why a number is missing.
* Every "is this unavailable, and why" case (no equity history, sample too
  small to be significant, no rejected candidates this process) is a named
  field with a human-readable reason -- never a bare ``0`` or empty list with
  no explanation, per the app's "render unavailable-with-reason honestly"
  rule.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


class ComponentScoresOut(BaseModel):
    technical_setup: float
    price_momentum: float
    volume_confirmation: float
    reddit_sentiment: float
    x_sentiment: float
    sentiment_acceleration: float
    attention_acceleration: float
    catalyst_quality: float
    earnings_risk: float
    liquidity: float
    market_regime: float
    manipulation_risk: float
    data_confidence: float


class TradePlanOut(BaseModel):
    entry_low: float
    entry_high: float
    stop_loss: float
    targets: list[float]
    reward_risk_ratio: float
    shares: int
    notional_usd: float
    risk_per_share: float
    reward_per_share: float
    time_stop_days: int
    expected_holding_days: int


class SignalRowOut(BaseModel):
    """One Screener grid row -- deliberately flat for AG Grid column binding."""

    signal_id: str
    symbol: str
    company_name: str
    strategy: str
    direction: str
    status: str
    regime: str
    overall_score: float
    #: ``overall_score`` re-ranked by any accepted research revisions (see
    #: ``signals.scoring.adjusted_overall``). Equals ``overall_score`` exactly
    #: when ``has_research`` is False -- never null/omitted, so the frontend
    #: can always read this field without a fallback.
    effective_score: float
    #: Whether at least one research revision exists for this signal.
    has_research: bool
    confidence: float
    reward_risk_ratio: float
    entry_low: float
    entry_high: float
    stop_loss: float
    days_to_earnings: int | None
    session: dt.date
    created_at: dt.datetime


class ResearchRevisionOut(BaseModel):
    """One entry from ``signals.research.ResearchLedger`` (latest or history).

    Mirrors the dict shape ``ResearchLedger.latest_research_revisions``/
    ``research_history`` return -- ``thesis``/``invalidation`` of ``None``
    means "the engine's own text is unchanged by this revision", not "blank".
    """

    revision: int
    created_at: dt.datetime
    actor: str
    thesis: str | None
    invalidation: list[str] | None
    score_adjustments: dict[str, float]
    rationale: str
    sources: list[str]


class SignalDetailOut(SignalRowOut):
    """Everything ``SignalRowOut`` has, plus the ticker-detail/thesis fields."""

    components: ComponentScoresOut
    plan: TradePlanOut
    thesis: str
    invalidation: list[str]
    exit_conditions: list[str]
    risks: list[str]
    evidence: list[str]
    next_earnings_date: dt.date | None
    data_warnings: list[str]
    #: The latest research revision, if any -- ``None`` means no research has
    #: been submitted for this signal yet.
    research: ResearchRevisionOut | None = None
    #: Every research revision for this signal, oldest first. Empty when
    #: ``research`` is ``None``.
    research_history: list[ResearchRevisionOut] = Field(default_factory=list)


class SignalListOut(BaseModel):
    signals: list[SignalRowOut]
    total: int


class RejectedCandidateOut(BaseModel):
    symbol: str
    strategy: str
    stage: str
    reasons: list[str]
    reason_codes: list[str] = Field(default_factory=list)


class NearMissOut(BaseModel):
    """One below-threshold candidate close enough to be worth a second look.

    ``metric``/``threshold``/``margin`` mirror ``signals.engine.NearMiss``:
    ``margin`` is ``metric - threshold`` (negative; closer to zero is closer
    to clearing the bar). ``weakest_components``/``strongest_components`` are
    the 3 lowest/highest-valued entries from whichever component breakdown
    produced the rejection -- a strategy's own setup score, or the engine's
    blended score -- each as ``[label, value]``.
    """

    symbol: str
    strategy: str
    reason_code: str
    metric: float
    threshold: float
    margin: float
    overall_score: float | None
    confidence: float | None
    weakest_components: list[tuple[str, float]]
    strongest_components: list[tuple[str, float]]


class ScanFunnelOut(BaseModel):
    """Aggregated ``why zero (or few) signals`` breakdown for the last scan.

    See ``signals.engine.ScanFunnel``: ``by_reason``/``by_strategy_reason``
    are full counts across every rejection in the scan (small, bounded by the
    fixed set of reason codes the code produces); ``near_misses`` is
    deliberately capped (``top_n``, default 20) rather than every
    below-threshold candidate.
    """

    top_n: int
    total_rejections: int
    by_reason: dict[str, int]
    by_strategy_reason: dict[str, dict[str, int]]
    near_misses: list[NearMissOut]


class RejectedResponse(BaseModel):
    """Near-miss candidates from the last scan run in *this server process*.

    ``ScanResult.rejected`` is never persisted (see ``signals/ledger.py``'s
    module docstring) -- so, exactly like the Streamlit Scanner's expander,
    this is only ever populated by a scan that ran in this process, and says
    so honestly when it hasn't. ``funnel`` is the aggregated counterpart of
    ``rejected`` (see ``ScanFunnelOut``) and follows the same availability
    rule -- ``None`` until a scan has run here, never a misleadingly-empty
    funnel.
    """

    available: bool
    reason: str | None = None
    generated_at: dt.datetime | None = None
    evaluated_symbols: int = 0
    rejected: list[RejectedCandidateOut] = Field(default_factory=list)
    funnel: ScanFunnelOut | None = None


class ScanRequest(BaseModel):
    session: dt.date | None = None
    lookback_days: int = 400
    generate_thesis: bool = False


class ScanResponse(BaseModel):
    session: dt.date
    evaluated_symbols: int
    signal_count: int
    rejected_count: int
    warnings: list[str]


class RefreshRequest(BaseModel):
    start: dt.date | None = None
    end: dt.date | None = None


class RefreshResponse(BaseModel):
    universe_size: int
    sentiment_rows: int
    degraded_sources: dict[str, str]
    warnings: list[str]


# --------------------------------------------------------------------------
# Ticker detail
# --------------------------------------------------------------------------


class BarOut(BaseModel):
    session: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float | None


class SentimentPointOut(BaseModel):
    session: dt.date
    post_count: int
    unique_authors: int
    engagement_weighted: float
    bull_bear_ratio: float
    manipulation_risk: float
    confidence: float


class IndicatorsOut(BaseModel):
    """Chart overlays, computed by ``claudetrade.features.indicators`` (never
    reimplemented in the frontend) and aligned index-for-index with ``bars``.

    Warm-up rows (not enough history yet for a given window) are ``None``,
    matching the pandas ``NaN`` the indicator functions themselves emit.
    """

    sma_20: list[float | None]
    sma_50: list[float | None]
    sma_200: list[float | None]
    rsi_14: list[float | None]
    bollinger_upper: list[float | None]
    bollinger_lower: list[float | None]


class TickerDetailOut(BaseModel):
    symbol: str
    bars: list[BarOut]
    indicators: IndicatorsOut
    sentiment: list[SentimentPointOut]
    earnings_dates: list[dt.date]
    current_signal: SignalDetailOut | None
    signal_history: list[SignalRowOut]
    price_note: str | None = None
    sentiment_note: str | None = None


# --------------------------------------------------------------------------
# AI Analysis configuration (Configuration screen's "AI Analysis" section)
# --------------------------------------------------------------------------


class AIConfigOut(BaseModel):
    """Current effective AI-provider selection, plus enough to render the
    Configuration screen's "AI Analysis" section without a second round trip:
    each provider's operator-configurable default model (shown as the model
    field's placeholder when ``model`` is empty) and per-provider credential
    names (already present in ``/api/system/credentials`` too, repeated here
    for convenience).

    ``persisted`` is always ``false`` from this endpoint's own GET/PUT --
    see ``PUT /api/system/ai-config``'s docstring for why a selection here
    is honestly scoped to "takes effect immediately, for this running
    server process" rather than a config.toml rewrite.
    """

    provider: str
    model: str
    anthropic_default_model: str
    openai_default_model: str
    anthropic_api_key_credential: str
    openai_api_key_credential: str


class AIConfigUpdate(BaseModel):
    provider: str = Field(pattern="^(anthropic|openai|none)$")
    #: Empty string means "use the selected provider's own default".
    model: str = ""


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


class RegimeCardOut(BaseModel):
    regime: str
    label: str
    as_of_session: dt.date | None
    has_data: bool


class StatusRibbonOut(BaseModel):
    last_refresh: dt.datetime | None
    last_scan: dt.datetime | None
    symbols_with_data: int


class ProviderStatusOut(BaseModel):
    name: str
    kind: str
    available: bool
    configured: bool
    supports_point_in_time: bool
    message: str


class DashboardOut(BaseModel):
    regime: RegimeCardOut
    top_longs: list[SignalRowOut]
    top_shorts: list[SignalRowOut]
    status: StatusRibbonOut
    providers: list[ProviderStatusOut]


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------


class PaperAccountOut(BaseModel):
    equity: float
    cash: float
    realised_pnl: float
    kill_switch_engaged: bool


class PaperPositionOut(BaseModel):
    trade_id: str
    symbol: str
    direction: str
    shares: int
    entry_price: float
    last_price: float
    unrealised_pnl: float
    unrealised_pct: float
    days_held: int
    needs_attention: list[str]


class ClosedTradeOut(BaseModel):
    trade_id: str
    symbol: str
    direction: str
    exit_session: dt.date | None
    outcome: str | None
    net_pnl: float
    r_multiple: float
    reason: str | None


class EquityPointOut(BaseModel):
    session: dt.date
    equity: float


class PaperAccountResponse(BaseModel):
    account: PaperAccountOut
    positions: list[PaperPositionOut]
    closed_trades: list[ClosedTradeOut]
    equity_curve: list[EquityPointOut]
    equity_curve_note: str | None = None


class PerformanceOut(BaseModel):
    closed_trades: int
    open_trades: int
    win_loss_ratio: float | None
    win_loss_display: str
    win_rate: float | None
    expectancy: float | None
    average_win: float | None
    average_loss: float | None
    profit_factor: float | None
    profit_factor_display: str
    max_drawdown_pct: float | None
    max_drawdown_note: str | None
    is_significant: bool
    significance_reason: str | None
    warnings: list[str]


class PaperOpenRequest(BaseModel):
    signal_id: str


class PaperOpenResponse(BaseModel):
    accepted: bool
    #: "filled" | "rejected" | "not_fillable"
    status: str
    order_id: str | None
    symbol: str
    direction: str
    requested_shares: int
    filled_shares: int
    fill_price: float | None
    fill_session: dt.date | None
    reasons: list[str]
    message: str


__all__ = [
    "AIConfigOut",
    "AIConfigUpdate",
    "BarOut",
    "ClosedTradeOut",
    "ComponentScoresOut",
    "DashboardOut",
    "EquityPointOut",
    "IndicatorsOut",
    "NearMissOut",
    "PaperAccountOut",
    "PaperAccountResponse",
    "PaperOpenRequest",
    "PaperOpenResponse",
    "PaperPositionOut",
    "PerformanceOut",
    "ProviderStatusOut",
    "RefreshRequest",
    "RefreshResponse",
    "RegimeCardOut",
    "RejectedCandidateOut",
    "RejectedResponse",
    "ResearchRevisionOut",
    "ScanFunnelOut",
    "ScanRequest",
    "ScanResponse",
    "SentimentPointOut",
    "SignalDetailOut",
    "SignalListOut",
    "SignalRowOut",
    "StatusRibbonOut",
    "TickerDetailOut",
    "TradePlanOut",
]
