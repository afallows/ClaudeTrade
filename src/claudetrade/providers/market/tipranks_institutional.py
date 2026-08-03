"""Insider/hedge-fund ("institutional") sentiment parsing and scoring over
TipRanks' ``dataForTicker`` ``overview`` payload.

Sibling to ``providers.market.tipranks_analyst`` -- same "call it once, use
it for everything" posture: ``providers.market.tipranks.TipRanksProvider``
already fetches (and caches, one file per symbol, full response body) the
``overview`` object for every symbol it resolves. That payload carries a
separate insider-transaction and hedge-fund-holdings layer this adapter
previously discarded entirely, alongside the analyst-consensus layer the
prior feature already harvests. This module reads MORE of the same dict --
**zero additional HTTP calls**. The cache-compatibility fact ``tipranks_
analyst``'s module docstring establishes (the on-disk cache stores the FULL
``overview`` verbatim, never trimmed) was re-confirmed, not re-derived, for
this module: nothing about that caching path changed between the two
features.

Every field access below is defensive (``.get()``, guarded ``isinstance``
checks) -- an unrecognised or reshaped field must never break the rest of
the snapshot, and a genuinely uncovered symbol must parse to ``None`` (see
:func:`parse_institutional_snapshot`) rather than an all-``None`` object.

**Fixture cross-references** (``tests/fixtures/tipranks/dataForTicker_INTC
.json`` and ``..._TECK_B.json`` -- both read in full before writing this
module):

* Both fixtures carry REAL insider and hedge-fund data -- there is no
  observed "nulls path" for the institutional block specifically (unlike
  some other TipRanks sub-payloads). ``corporateInsiderTransactions[]``,
  ``insiders[]``, ``insidrConfidenceSignal``, ``hedgeFundData`` (including
  ``holdingsByTime[]``/``institutionalHoldings[]``), ``numOfInsiders`` and
  ``marketCapUSD`` are all present and populated on both. The two fixtures
  differ in *scale* and *currency* (TECK.B's insider amounts are CAD via
  ``currencyTypeId=2`` on individual ``insiders[]`` rows, though the
  headline ``insiderslast3MonthsSum``/monthly-aggregate amounts carry no
  explicit per-row currency and are stored as reported, same as
  ``tipranks_analyst`` does for undocumented-scale fields) and in
  transaction volume (TECK.B's ``corporateInsiderTransactions[]`` rows are
  sell-heavy across all three months; INTC's May row is buy-heavy). The
  "no institutional content at all" path exercised by this module's tests
  uses a synthetic empty/absent payload, not either committed fixture.
* ``insidrConfidenceSignal.stockScore``: NOT documented by TipRanks. Treated
  as a 0..1 scale (mapped to -1..+1 via ``2x - 1`` for scoring) on the
  strength of two consistent, independent signals rather than vendor
  documentation: (1) it is the SAME number as ``overview.portfolioHolding
  Data.insiderSentimentData.stockScore`` on both fixtures (0.29 / 0.08), a
  field co-located with ``hedgeFundSentimentData.score`` (CONFIRMED 0..1,
  see below) under one parent object, suggesting a shared scale convention;
  (2) both fixtures' values sit below the 0.5 midpoint alongside a negative
  ``insiderslast3MonthsSum`` (net insider SELLING) -- i.e. the direction is
  internally consistent on both available data points. Not proof, but two
  independent lines of corroborating evidence, stated honestly as
  best-effort rather than vendor-confirmed.
* ``hedgeFundData.sentiment``: CONFIRMED 0..1 scale from the payload
  description itself and cross-checked against ``overview.portfolioHolding
  Data.hedgeFundSentimentData.score``, which carries the identical value
  (0.83 on INTC) alongside a ``rating`` field -- the same "raw score +
  opaque rating" shape ``tipranks_analyst``'s ``consensus_rating`` already
  established a precedent for treating as vendor-opaque.
* ``corporateInsiderTransactions[].informative*`` vs. raw ``trans*``
  fields: both present on every monthly row in both fixtures (never one
  without the other structurally, though either can be individually
  ``null`` on a given row -- e.g. INTC's July 2026 row has
  ``transBuyAmount: null`` alongside ``informativeBuyAmount: 0.0``). This
  module prefers the informative figure per side (buy/sell independently)
  and falls back to the raw figure only when the informative one is
  ``None`` for that side -- see :func:`_insider_net_3m_usd`.
* ``action``/``insiderOperationId``/``insiderOperationTypeId`` (on
  ``insiders[]``) and ``action`` (on ``hedgeFundData.institutionalHoldings
  []``): NOT documented anywhere reachable from this adapter, and unlike
  ``tipranks_analyst``'s ``ratingId``/``actionId``, no headline text in
  either institutional fixture confirms a mapping. Stored raw
  (``InsiderTransaction.action``/``HedgeFundHolderMove.action``) rather than
  guessed, per the same "never fabricate meaning for an unconfirmed field"
  posture (ADR-0008 Decision 1) ``tipranks_analyst`` already follows for its
  own undocumented code. TipRanks' own human-readable
  ``insiderOperationDescription`` string IS trusted as display text (it is
  vendor-authored prose, not a code this adapter would be inventing a label
  for).

**Scoring.** :func:`institutional_score` is a pure function over an already-
parsed :class:`~claudetrade.domain.InstitutionalSnapshot` -- no I/O, no
network, no database access, safe to call for every stored snapshot at read
time. It lives in this module (rather than a separate file) because every
constant it uses is a TipRanks-specific scale decision (the 0..1 vendor
scales, the vendor's own quarterly SEC-lag cadence) that only makes sense
read alongside the parser producing the values it operates on; it gets its
own dedicated test file (``tests/test_institutional_score.py``) despite the
shared module, exactly as the coordinator's build plan specified.

**Not fed into ``signals.scoring.ComponentScores`` or any strategy.** This
score (and the snapshot it is built from) is a read-only research overlay --
the Streamlit ticker-detail "Institutional sentiment" block and the
``get_institutional_sentiment`` MCP tool -- never a scan/backtest input. See
``domain.InstitutionalSnapshot``'s own docstring for the same caveat.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from claudetrade.domain import (
    HedgeFundHolderMove,
    HedgeFundHoldingQuarter,
    InsiderTransaction,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)

# --------------------------------------------------------------------------
# Parsing bounds
# --------------------------------------------------------------------------

#: Longest ``insider_monthly`` list stored per snapshot, most-recent last
#: (chronological, mirroring ``tipranks_analyst.CONSENSUS_OVER_TIME_MAX``'s
#: convention). Both fixtures carry 3 rows; this is a generous multiple
#: bounding the stored JSON column without starving a real monthly trend.
INSIDER_MONTHLY_MAX = 12

#: Longest ``recent_insider_transactions`` list stored per snapshot, ranked
#: by ``|estimated_shares_value|`` descending (most notable trades first --
#: see :func:`_parse_recent_insider_transactions`).
RECENT_INSIDER_TRANSACTIONS_MAX = 5

#: Longest ``hedge_fund_holdings_by_quarter`` series stored per snapshot,
#: date-ascending. TECK.B's fixture already carries 9 quarterly rows; this
#: caps at roughly 5 years of quarterly SEC-lagged history.
HEDGE_FUND_HOLDINGS_MAX = 20

#: Longest ``notable_holder_moves`` list stored per snapshot, ranked by
#: ``|change_amount|`` descending (the biggest reported position moves).
NOTABLE_HOLDER_MOVES_MAX = 5

# --------------------------------------------------------------------------
# Small defensive parsing helpers (mirrors tipranks_analyst.py's own)
# --------------------------------------------------------------------------


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Parsing: overview -> InstitutionalSnapshot
# --------------------------------------------------------------------------

#: How many of the most-recent monthly ``corporateInsiderTransactions[]``
#: buckets feed ``insider_net_3m_usd`` -- one bucket per calendar month, so
#: 3 buckets approximates "trailing 3 months" the same way the vendor's own
#: (unused-for-scoring) ``insiderslast3MonthsSum`` field name implies.
INSIDER_NET_FLOW_MONTHS = 3


def _parse_insider_monthly(rows: Any) -> list[InsiderTransactionMonth]:
    """``overview.corporateInsiderTransactions[]`` -> a bounded,
    chronologically-ascending list. A row missing ``month``/``year`` is
    dropped outright (there is nothing to key it by); every other field
    tolerates ``None``.
    """
    if not isinstance(rows, list):
        return []
    out: list[InsiderTransactionMonth] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        month = _maybe_int(row.get("month"))
        year = _maybe_int(row.get("year"))
        if month is None or year is None:
            continue
        out.append(
            InsiderTransactionMonth(
                month=month,
                year=year,
                shares_bought=_maybe_int(row.get("sharesBought")),
                insiders_buy_count=_maybe_int(row.get("insidersBuyCount")) or 0,
                shares_sold=_maybe_int(row.get("sharesSold")),
                insiders_sell_count=_maybe_int(row.get("insidersSellCount")) or 0,
                trans_buy_count=_maybe_int(row.get("transBuyCount")) or 0,
                trans_sell_count=_maybe_int(row.get("transSellCount")) or 0,
                trans_buy_amount=_maybe_float(row.get("transBuyAmount")),
                trans_sell_amount=_maybe_float(row.get("transSellAmount")),
                informative_buy_count=_maybe_int(row.get("informativeBuyCount")) or 0,
                informative_sell_count=_maybe_int(row.get("informativeSellCount")) or 0,
                informative_buy_amount=_maybe_float(row.get("informativeBuyAmount")),
                informative_sell_amount=_maybe_float(row.get("informativeSellAmount")),
            )
        )
    out.sort(key=lambda m: (m.year, m.month))
    return out[-INSIDER_MONTHLY_MAX:]


def _insider_net_3m_usd(monthly: list[InsiderTransactionMonth]) -> float | None:
    """This module's own trailing net insider $ flow, summed over the
    ``INSIDER_NET_FLOW_MONTHS`` most recent monthly buckets present.

    Each side (buy/sell) of each month prefers ``informative_*_amount``,
    falling back to the raw ``trans_*_amount`` only when the informative
    figure is ``None`` for that side (see the module docstring's fixture
    cross-reference). A month contributing neither a buy nor a sell figure
    (both ``None`` on both the informative and raw pair) is skipped rather
    than treated as a zero -- a silent zero here would understate a real
    but unreported month exactly as much as overstate a genuinely quiet
    one. Returns ``None`` only when EVERY inspected month contributed
    nothing at all -- never a fabricated 0.0 standing in for "no insider
    data".
    """
    if not monthly:
        return None
    newest_first = sorted(monthly, key=lambda m: (m.year, m.month), reverse=True)
    window = newest_first[:INSIDER_NET_FLOW_MONTHS]
    total = 0.0
    contributed = False
    for row in window:
        buy = row.informative_buy_amount if row.informative_buy_amount is not None else row.trans_buy_amount
        sell = (
            row.informative_sell_amount
            if row.informative_sell_amount is not None
            else row.trans_sell_amount
        )
        if buy is None and sell is None:
            continue
        total += (buy or 0.0) - (sell or 0.0)
        contributed = True
    return total if contributed else None


def _parse_recent_insider_transactions(rows: Any) -> list[InsiderTransaction]:
    """``overview.insiders[]`` -> the ``RECENT_INSIDER_TRANSACTIONS_MAX``
    largest transactions by ``|estimatedSharesValue|`` -- evidence rows for
    display, not an input to the scoring axis (which reads the monthly
    aggregates instead)."""
    if not isinstance(rows, list):
        return []
    out: list[InsiderTransaction] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        officer_title = row.get("officerTitle")
        operation_description = row.get("insiderOperationDescription")
        link = row.get("link")
        out.append(
            InsiderTransaction(
                name=name,
                is_officer=bool(row.get("isOfficer")),
                is_director=bool(row.get("isDirector")),
                is_ten_percent_owner=bool(row.get("isTenPercentOwner")),
                officer_title=str(officer_title) if officer_title else None,
                action=_maybe_int(row.get("action")),
                operation_description=str(operation_description) if operation_description else None,
                amount=_maybe_float(row.get("amount")),
                number_of_shares=_maybe_int(row.get("numberOfShares")),
                r_date=_parse_date(row.get("rDate")),
                estimated_shares_value=_maybe_float(row.get("estimatedSharesValue")),
                link=str(link) if link else None,
            )
        )
    out.sort(
        key=lambda t: abs(t.estimated_shares_value) if t.estimated_shares_value is not None else -1.0,
        reverse=True,
    )
    return out[:RECENT_INSIDER_TRANSACTIONS_MAX]


def _parse_hedge_fund_holdings(rows: Any) -> list[HedgeFundHoldingQuarter]:
    """``overview.hedgeFundData.holdingsByTime[]`` -> a bounded,
    date-ascending series. A row missing its date contributes nothing."""
    if not isinstance(rows, list):
        return []
    out: list[HedgeFundHoldingQuarter] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = _parse_date(row.get("date"))
        if date is None:
            continue
        out.append(
            HedgeFundHoldingQuarter(
                date=date,
                holding_amount=_maybe_int(row.get("holdingAmount")),
                institution_holding_percentage=_maybe_float(row.get("institutionHoldingPercentage")),
                net_shares_change=_maybe_int(row.get("netSharesChange")),
                number_of_shares_bought=_maybe_int(row.get("numberOfSharesBought")),
                number_of_shares_sold=_maybe_int(row.get("numberOfSharesSold")),
                is_complete=bool(row.get("isComplete")),
            )
        )
    out.sort(key=lambda h: h.date)
    return out[-HEDGE_FUND_HOLDINGS_MAX:]


def _parse_notable_holder_moves(rows: Any) -> list[HedgeFundHolderMove]:
    """``overview.hedgeFundData.institutionalHoldings[]`` -> the
    ``NOTABLE_HOLDER_MOVES_MAX`` largest reported moves by
    ``|changeAmount|``."""
    if not isinstance(rows, list):
        return []
    out: list[HedgeFundHolderMove] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        manager = str(row.get("managerName") or "").strip()
        institution = str(row.get("institutionName") or "").strip()
        if not manager and not institution:
            continue
        out.append(
            HedgeFundHolderMove(
                manager_name=manager,
                institution_name=institution,
                action=_maybe_int(row.get("action")),
                effective_date=_parse_date(row.get("effectiveDate")),
                value=_maybe_float(row.get("value")),
                change_pct=_maybe_float(row.get("change")),
                change_amount=_maybe_float(row.get("changeAmount")),
                percentage_of_portfolio=_maybe_float(row.get("percentageOfPortfolio")),
                stars=_maybe_float(row.get("stars")),
                is_active=bool(row.get("isActive", True)),
            )
        )
    out.sort(
        key=lambda h: abs(h.change_amount) if h.change_amount is not None else -1.0,
        reverse=True,
    )
    return out[:NOTABLE_HOLDER_MOVES_MAX]


def parse_institutional_snapshot(
    overview: dict[str, Any] | None,
    symbol: str,
    as_of_session: dt.date,
    fetched_at: dt.datetime,
) -> InstitutionalSnapshot | None:
    """Build one ``InstitutionalSnapshot`` from a ``dataForTicker``
    ``overview`` -- the SAME dict ``TipRanksProvider._resolve`` already
    fetched (or served from cache); this function performs no I/O.

    Returns ``None`` when the symbol has no institutional content at all:
    no insider-transaction rows (monthly or individual), no insider
    confidence signal, no vendor 3-month insider sum, no insider count, and
    no hedge-fund data (sentiment, trend, holdings-by-quarter, or notable
    holder moves) -- an empty, all-``None`` snapshot must never be stored.
    A symbol with genuinely no ``overview`` at all also returns ``None``
    here, via the same guard.
    """
    if not isinstance(overview, dict):
        return None

    insider_monthly = _parse_insider_monthly(overview.get("corporateInsiderTransactions"))
    recent_insider_transactions = _parse_recent_insider_transactions(overview.get("insiders"))

    confidence = overview.get("insidrConfidenceSignal")
    confidence = confidence if isinstance(confidence, dict) else {}
    confidence_stock = _maybe_float(confidence.get("stockScore"))
    confidence_sector = _maybe_float(confidence.get("sectorScore"))
    confidence_raw = _maybe_int(confidence.get("score"))

    hedge_fund = overview.get("hedgeFundData")
    hedge_fund = hedge_fund if isinstance(hedge_fund, dict) else {}
    holdings_by_quarter = _parse_hedge_fund_holdings(hedge_fund.get("holdingsByTime"))
    notable_holder_moves = _parse_notable_holder_moves(hedge_fund.get("institutionalHoldings"))
    hf_sentiment = _maybe_float(hedge_fund.get("sentiment"))
    hf_trend_action = _maybe_int(hedge_fund.get("trendAction"))
    hf_trend_value = _maybe_float(hedge_fund.get("trendValue"))

    insider_net_vendor = _maybe_float(overview.get("insiderslast3MonthsSum"))
    num_of_insiders = _maybe_int(overview.get("numOfInsiders"))

    has_content = (
        bool(insider_monthly)
        or bool(recent_insider_transactions)
        or confidence_stock is not None
        or confidence_sector is not None
        or confidence_raw is not None
        or insider_net_vendor is not None
        or num_of_insiders is not None
        or hf_sentiment is not None
        or hf_trend_action is not None
        or bool(holdings_by_quarter)
        or bool(notable_holder_moves)
    )
    if not has_content:
        return None

    return InstitutionalSnapshot(
        symbol=symbol,
        as_of_session=as_of_session,
        insider_monthly=insider_monthly,
        insider_net_3m_usd=_insider_net_3m_usd(insider_monthly),
        insider_net_3m_usd_vendor=insider_net_vendor,
        insider_confidence_stock_score=confidence_stock,
        insider_confidence_sector_score=confidence_sector,
        insider_confidence_raw_score=confidence_raw,
        num_of_insiders=num_of_insiders,
        recent_insider_transactions=recent_insider_transactions,
        hedge_fund_sentiment=hf_sentiment,
        hedge_fund_trend_action=hf_trend_action,
        hedge_fund_trend_value=hf_trend_value,
        hedge_fund_holdings_by_quarter=holdings_by_quarter,
        notable_holder_moves=notable_holder_moves,
        market_cap_usd=_maybe_float(overview.get("marketCapUSD")),
        fetched_at=fetched_at,
    )


# --------------------------------------------------------------------------
# Scoring: InstitutionalSnapshot -> InstitutionalScoreResult
# --------------------------------------------------------------------------

#: Insider axis's base share of the blended score, before either axis's
#: own staleness discount is applied. Weighted ABOVE the hedge-fund axis
#: per the task spec: insider transactions are filed within days (SEC
#: Form 4, T+2) and are a single individual's real capital commitment,
#: whereas the hedge-fund axis is a quarterly-lagged, fund-level aggregate
#: that can be materially stale the moment it is published (see
#: ``HEDGE_FUND_STALENESS_FULL_DECAY_DAYS`` below).
INSIDER_AXIS_BASE_WEIGHT = 0.65

#: Hedge-fund axis's base share -- see ``INSIDER_AXIS_BASE_WEIGHT``.
HEDGE_FUND_AXIS_BASE_WEIGHT = 0.35

#: Within the insider axis: how much the market-cap-normalized $ flow
#: component counts vs. the vendor's own confidence-signal component. The
#: flow figure is this module's own computation from primary transaction
#: data (hard evidence); the confidence signal is a single vendor-opaque
#: number corroborating it (see the module docstring's stockScore
#: cross-reference) -- hard evidence carries the larger half.
INSIDER_FLOW_COMPONENT_WEIGHT = 0.6
INSIDER_CONFIDENCE_COMPONENT_WEIGHT = 0.4

#: Within the hedge-fund axis: the vendor's own ``sentiment`` figure is
#: itself understood to already synthesize broader holdings history than
#: one quarter's flow, so it carries the larger half -- mirroring the
#: insider axis's evidence/confidence split above for consistency, not
#: because the two axes are otherwise symmetric.
HEDGE_FUND_SENTIMENT_COMPONENT_WEIGHT = 0.6
HEDGE_FUND_FLOW_COMPONENT_WEIGHT = 0.4

#: Gain applied to ``netSharesChange / holdingAmount`` before clamping to
#: [-1, 1]. A single quarter's institutional rebalancing of roughly a third
#: of the aggregate reported holding (ratio magnitude ~0.33) is already a
#: large, unusual move for a broad fund-level aggregate (as opposed to one
#: activist's concentrated stake) -- this gain maps that magnitude to the
#: clamp boundary. Both committed fixtures' latest quarters (INTC +7.0%,
#: TECK.B -11.3%) land well inside the boundary at this gain, giving room
#: for a real outlier quarter to still read as more extreme than a routine
#: one.
HEDGE_FUND_FLOW_RATIO_GAIN = 3.0

#: Insider axis staleness: linear decay from full weight (newest monthly
#: bucket dated this session) to zero once the newest bucket is this many
#: days old. TipRanks' insider feed is MONTHLY cadence (both fixtures), so
#: going a full quarter (~90 days) without a fresh bucket means the feed
#: itself has gone quiet for this symbol, not merely that insiders were --
#: the axis fades out over that single quarter rather than the two full
#: quarters the vendor-lagged hedge-fund axis gets (see below).
INSIDER_STALENESS_FULL_DECAY_DAYS = 90.0

#: Hedge-fund axis staleness: linear decay from full weight (latest
#: quarter dated this session) to zero at this many days old -- ~2 calendar
#: quarters (91 days each), directly implementing the task spec's "near
#: zero at 2 quarters old" requirement. A single stale quarter (~91 days,
#: roughly unavoidable given SEC 13F filing lag) still carries about half
#: weight; two stale quarters carries none.
HEDGE_FUND_STALENESS_FULL_DECAY_DAYS = 182.0


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _log_damped_flow_ratio(net_usd: float | None, market_cap_usd: float | None) -> float | None:
    """Signed-log-dollar-flow divided by log-market-cap, clamped to
    [-1, 1] -- the insider axis's "market-cap normalized with log damping"
    component.

    A RAW ratio (``net_usd / market_cap_usd``) would be dominated by
    company size: even a large, meaningful insider transaction is a
    vanishingly small fraction of a mega-cap's market cap (INTC's
    -$2.49M/3m against a $435B cap is a ratio of about -6e-6), so a linear
    ratio would read every mega-cap as permanently ~0 regardless of real
    insider activity. Putting BOTH the flow and the cap on a log scale
    (``sign(net) * log1p(|net|)`` over ``log1p(cap)``) compares them in
    "orders of magnitude" terms instead, so a meaningful flow relative to a
    company's OWN size scale still registers -- confirmed against both
    fixtures, which land at economically distinguishable, non-degenerate
    magnitudes under this formula (~-0.55 for INTC, ~-0.66 for TECK.B's
    vendor-reported sum) despite one raw ratio being roughly two orders of
    magnitude smaller than the other and both raw ratios being individually
    unusable directly.

    Returns ``None`` when either input is missing or the market cap is not
    positive (nothing to normalize against).
    """
    if net_usd is None or market_cap_usd is None or market_cap_usd <= 0:
        return None
    if net_usd == 0:
        return 0.0
    signed_log = math.copysign(math.log1p(abs(net_usd)), net_usd)
    log_cap = math.log1p(market_cap_usd)
    if log_cap <= 0:
        return None
    return _clamp(signed_log / log_cap)


def _scaled_zero_one(value: float | None) -> float | None:
    """Maps a vendor 0..1 scale (``hedgeFundData.sentiment`` /
    ``insidrConfidenceSignal.stockScore``, see the module docstring's
    confirmation evidence) onto -1..+1."""
    if value is None:
        return None
    return _clamp(2.0 * value - 1.0)


def _hedge_fund_flow_component(latest_quarter: HedgeFundHoldingQuarter | None) -> float | None:
    if latest_quarter is None:
        return None
    change = latest_quarter.net_shares_change
    holding = latest_quarter.holding_amount
    if change is None or holding is None or holding <= 0:
        return None
    return _clamp((change / holding) * HEDGE_FUND_FLOW_RATIO_GAIN)


def _weighted_blend(components: list[tuple[float | None, float]]) -> float | None:
    """Weighted average of whichever ``(value, weight)`` pairs have a real
    value, renormalized over just those present -- an absent component
    redistributes its weight to the other(s) rather than pulling the blend
    toward zero. ``None`` when nothing is present."""
    present = [(value, weight) for value, weight in components if value is not None]
    if not present:
        return None
    total_weight = sum(weight for _, weight in present)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in present) / total_weight


def _insider_axis_age_days(monthly: list[InsiderTransactionMonth], as_of: dt.date) -> int | None:
    """Age, in days, of the newest monthly insider bucket -- the first of
    that bucket's month, since a monthly aggregate has no finer-grained
    date. ``None`` when there is no monthly data at all."""
    if not monthly:
        return None
    newest = max(monthly, key=lambda m: (m.year, m.month))
    try:
        newest_date = dt.date(newest.year, newest.month, 1)
    except ValueError:
        return None
    return (as_of - newest_date).days


def _staleness_weight(age_days: int | None, full_decay_days: float) -> float:
    """1.0 at age 0, linearly down to 0.0 at ``full_decay_days`` (and
    beyond) -- ``None`` age (no dated evidence at all) is 0.0."""
    if age_days is None:
        return 0.0
    if age_days <= 0:
        return 1.0
    return _clamp(1.0 - age_days / full_decay_days, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class InstitutionalScoreResult:
    """The full, transparent output of :func:`institutional_score` -- not
    just the blended number, but every subscore/weight/age that produced
    it, so a caller (UI, MCP tool) can show its work rather than present a
    single opaque figure.

    ``score`` is the blended [-1, +1] figure, or ``None`` when BOTH axes
    are absent (no usable data at all) or fully staleness-decayed to zero
    weight -- never a fabricated 0.0 standing in for "no institutional
    signal". ``insider_subscore``/``hedge_fund_subscore`` are each
    independently ``None`` only when that axis itself has no usable
    component (see ``_insider_axis``/``_hedge_fund_axis``); a subscore can
    be a real, present value even when its own ``*_weight_applied`` has
    decayed toward 0.0 from staleness -- the two are reported separately on
    purpose, so "there IS a number, it's just old" is distinguishable from
    "there is no number at all".
    """

    score: float | None
    insider_subscore: float | None
    insider_weight_applied: float
    insider_age_days: int | None
    hedge_fund_subscore: float | None
    hedge_fund_weight_applied: float
    hedge_fund_age_days: int | None


def _insider_axis(
    snapshot: InstitutionalSnapshot, as_of: dt.date
) -> tuple[float | None, int | None]:
    flow_component = _log_damped_flow_ratio(snapshot.insider_net_3m_usd, snapshot.market_cap_usd)
    confidence_component = _scaled_zero_one(snapshot.insider_confidence_stock_score)
    axis_value = _weighted_blend(
        [
            (flow_component, INSIDER_FLOW_COMPONENT_WEIGHT),
            (confidence_component, INSIDER_CONFIDENCE_COMPONENT_WEIGHT),
        ]
    )
    age_days = _insider_axis_age_days(snapshot.insider_monthly, as_of)
    return axis_value, age_days


def _hedge_fund_axis(
    snapshot: InstitutionalSnapshot, as_of: dt.date
) -> tuple[float | None, int | None]:
    latest_quarter = (
        snapshot.hedge_fund_holdings_by_quarter[-1]
        if snapshot.hedge_fund_holdings_by_quarter
        else None
    )
    sentiment_component = _scaled_zero_one(snapshot.hedge_fund_sentiment)
    flow_component = _hedge_fund_flow_component(latest_quarter)
    axis_value = _weighted_blend(
        [
            (sentiment_component, HEDGE_FUND_SENTIMENT_COMPONENT_WEIGHT),
            (flow_component, HEDGE_FUND_FLOW_COMPONENT_WEIGHT),
        ]
    )
    age_days = (as_of - latest_quarter.date).days if latest_quarter is not None else None
    return axis_value, age_days


def institutional_score(snapshot: InstitutionalSnapshot, as_of: dt.date) -> InstitutionalScoreResult:
    """The blended, staleness-discounted [-1, +1] institutional sentiment
    score for ``snapshot`` as of ``as_of`` -- pure function, no I/O.

    Two axes, each itself a weighted blend of two components (see the
    module-level weight constants for the rationale behind every number):

    * **Insider axis** (``INSIDER_AXIS_BASE_WEIGHT``): market-cap-normalized,
      log-damped net informative buy/sell $ flow over the trailing 3
      monthly buckets (:func:`_log_damped_flow_ratio` over
      ``snapshot.insider_net_3m_usd``), blended with the vendor's own
      ``insidrConfidenceSignal.stockScore``. Staleness keys off the newest
      monthly bucket's age (:func:`_insider_axis_age_days`), fully decayed
      by ``INSIDER_STALENESS_FULL_DECAY_DAYS``.
    * **Hedge-fund axis** (``HEDGE_FUND_AXIS_BASE_WEIGHT``): the vendor's own
      0..1 ``hedgeFundData.sentiment``, blended with the latest quarter's
      ``netSharesChange`` relative to its ``holdingAmount``. Staleness keys
      off the latest quarter's own date, fully decayed by
      ``HEDGE_FUND_STALENESS_FULL_DECAY_DAYS`` (~2 quarters, per spec).

    The two axes are blended by their STALENESS-DISCOUNTED weights
    (``base_weight * staleness_weight``), renormalized over whichever
    axis/axes actually have a value -- an absent (or fully stale) axis
    redistributes its weight to the other rather than pulling the blend
    toward zero, and BOTH absent/fully-stale yields ``score=None``, never a
    fabricated 0.0. See :class:`InstitutionalScoreResult` for the full
    per-axis breakdown this function always returns alongside the blended
    score.

    **Not fed into ``signals.scoring.ComponentScores`` or any strategy** --
    see the module docstring.
    """
    insider_subscore, insider_age_days = _insider_axis(snapshot, as_of)
    hedge_fund_subscore, hedge_fund_age_days = _hedge_fund_axis(snapshot, as_of)

    insider_weight_applied = (
        INSIDER_AXIS_BASE_WEIGHT * _staleness_weight(insider_age_days, INSIDER_STALENESS_FULL_DECAY_DAYS)
        if insider_subscore is not None
        else 0.0
    )
    hedge_fund_weight_applied = (
        HEDGE_FUND_AXIS_BASE_WEIGHT
        * _staleness_weight(hedge_fund_age_days, HEDGE_FUND_STALENESS_FULL_DECAY_DAYS)
        if hedge_fund_subscore is not None
        else 0.0
    )

    total_weight = insider_weight_applied + hedge_fund_weight_applied
    score: float | None = None
    if total_weight > 0.0:
        numerator = 0.0
        if insider_subscore is not None:
            numerator += insider_subscore * insider_weight_applied
        if hedge_fund_subscore is not None:
            numerator += hedge_fund_subscore * hedge_fund_weight_applied
        score = _clamp(numerator / total_weight)

    return InstitutionalScoreResult(
        score=score,
        insider_subscore=insider_subscore,
        insider_weight_applied=insider_weight_applied,
        insider_age_days=insider_age_days,
        hedge_fund_subscore=hedge_fund_subscore,
        hedge_fund_weight_applied=hedge_fund_weight_applied,
        hedge_fund_age_days=hedge_fund_age_days,
    )


__all__ = [
    "HEDGE_FUND_AXIS_BASE_WEIGHT",
    "HEDGE_FUND_FLOW_COMPONENT_WEIGHT",
    "HEDGE_FUND_FLOW_RATIO_GAIN",
    "HEDGE_FUND_HOLDINGS_MAX",
    "HEDGE_FUND_SENTIMENT_COMPONENT_WEIGHT",
    "HEDGE_FUND_STALENESS_FULL_DECAY_DAYS",
    "INSIDER_AXIS_BASE_WEIGHT",
    "INSIDER_CONFIDENCE_COMPONENT_WEIGHT",
    "INSIDER_FLOW_COMPONENT_WEIGHT",
    "INSIDER_MONTHLY_MAX",
    "INSIDER_NET_FLOW_MONTHS",
    "INSIDER_STALENESS_FULL_DECAY_DAYS",
    "NOTABLE_HOLDER_MOVES_MAX",
    "RECENT_INSIDER_TRANSACTIONS_MAX",
    "InstitutionalScoreResult",
    "institutional_score",
    "parse_institutional_snapshot",
]
