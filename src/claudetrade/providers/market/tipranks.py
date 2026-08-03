"""TipRanks unauthenticated partner-widget API adapter.

Reads ``https://widgets.tipranks.com/api/etoro/dataForTicker?ticker={SYMBOL}``
-- a keyless, unauthenticated JSON endpoint (the same one TipRanks' eToro
integration widget calls) that returns a rich ``overview`` object per symbol:
company reference data, a real market cap, an analyst consensus, and --
critically for this application -- the next scheduled earnings report and the
last reported one. One HTTP call per symbol serves all four of this adapter's
capabilities (earnings, market caps, reference data, and last-resort close
bars), which is why they are implemented on a single class sharing one
fetch/cache path rather than four separate adapters.

**Honest ToS posture (ADR-0008 Decision 1)**: this is not a published,
contracted API. It is an unauthenticated endpoint that happens to be
reachable and returns useful data; TipRanks could restrict, reshape, rate
limit, or withdraw it at any time with no notice and no deprecation window.
This mirrors the posture this codebase already applies to stooq (free CSV
endpoint), Yahoo (undocumented chart/quote JSON) and Stocktwits (keyless
public stream): personal/research use only, conservative self-imposed rate
limiting, and a **fail-closed** response to anything that looks like a
block, challenge, or unexpected shape -- see ``_get`` and the module-level
docstring section on failure modes below. Nothing here bypasses
authentication, defeats a paywall, or solves a challenge.

**This sandbox cannot reach ``widgets.tipranks.com`` or
``marketsv3.tipranks.com``** (egress is fully blocked). This adapter was
built entirely against two fixtures the project owner captured from their
own machine and committed to the repo:

* ``tests/fixtures/tipranks/dataForTicker_INTC.json`` -- a US (NASDAQ) listing.
* ``tests/fixtures/tipranks/dataForTicker_TECK_B.json`` -- a Canadian (TSX)
  listing, requested as ``ticker=TSE:TECK.B``.

Both are read in full before writing this module (see each file's
``_fixture_note``); every field access below is defensive (``.get()`` with a
fallback, guarded type checks) because a real-world response will carry many
more fields than either fixture shows, and a field this adapter does not
consume must never be able to break parsing of the fields it does.

**Symbol notation** (``tipranks_ticker``): a bare US ticker is passed through
unchanged (``AAPL``); a Canadian (TSX/TSXV) one is rewritten to
``TSE:<SYMBOL-WITH-DOTS>`` -- this codebase's own hyphenated share-class
convention (``TECK-B``) becomes TipRanks' dotted one (``TSE:TECK.B``), the
form the TECK.B fixture confirms the endpoint actually accepts and echoes
back verbatim in ``overview.ticker``. Exchange resolution follows the same
convention as ``StooqMarketProvider.stooq_symbol`` / mapping-table approach:
pass ``exchange`` explicitly when known, otherwise fall back to the packaged
seed universes' symbol -> exchange column.

**Earnings ticker mismatch (confirmed from the TECK.B fixture)**:
``overview.portfolioHoldingData.nextEarningsReport.ticker`` and
``lastReportedEps.ticker`` come back as the *US cross-listing* ticker
(``"TECK"``), not the ``TSE:TECK.B`` symbol that was actually requested.
Every earnings event this adapter returns is therefore keyed by the symbol
*this adapter's own caller asked for* (the loop variable), never by any
``ticker`` field found inside the response body -- those inner ticker fields
are informational only and are never used for identity.

**timeOfDay is provisional.** Two confirmed values from the owner's brief
(``1`` = before market open, ``4`` = after market close) plus a third
observed empirically in the TECK.B fixture (``2``, on a *confirmed* historical
report) that is not independently documented anywhere reachable from here.
By elimination against the common vendor convention (1/2/4 already accounted
for as BMO/?/AMC), ``2`` is mapped to ``EarningsSession.DURING`` as a
best-effort, UNVERIFIED guess -- flagged here and in ``docs/api-providers.md``
so it can be corrected once TipRanks' own documentation (if any) or further
real captures confirm it. Any other value maps to ``EarningsSession.UNKNOWN``
rather than raising -- an unrecognised code must never fail the parse.

**Market cap currency (owner-simplified rule)**: prefer ``overview.
marketCapUSD`` when present and positive; otherwise use ``overview.marketCap``
as-is, with **no currency gating** -- the ``>= $1B`` universe floor is
currency-agnostic by explicit owner instruction (a nominal $1B/CAD1B both
clear it). Nested blocks that also happen to carry a ``marketCap`` field
(``portfolioHoldingData.nextDividendDate.marketCap`` in the TECK.B fixture,
which is in CAD and a *different* figure from the top-level cap) are never
used as a cap source -- only the top-level ``overview`` fields are.
``claudetrade.domain.SecurityInfo`` has no currency field, so the currency id
itself is not persisted; this is a documented limitation, not an oversight
(``domain.py`` is outside this change's boundary). A related, one-line
limitation worth stating plainly: bars for a TSX listing are in that
listing's own currency (CAD) -- fine for this application's own per-symbol
math (ATR, gaps, returns are all currency-internal), but relative-strength
comparisons against the USD benchmark (SPY) mix currencies uncorrected.

**Daily bars are close-only and LAST RESORT ONLY.**
``overview.prices`` is a list of ``{"date", "d", "p"}`` -- a closing print per
session, nothing else. Synthesising fake open/high/low/volume would silently
corrupt every downstream ATR/gap/volume feature, so this adapter never does
that: ``get_daily_bars`` emits ``Bar(open=high=low=close=p, volume=0)`` and
records a ``DataQualityIssue`` (WARNING, category ``close_only_bars``) for
every symbol it degrades this way, via ``drain_quality_warnings()`` --
logged unconditionally either way, so a degraded bar series is never silent
even if nothing downstream happens to drain the queue yet. ``bars_last_resort
= True`` (a plain class attribute, not a Protocol requirement) is the flag
``providers.registry.FallbackMarketProvider.get_daily_bars`` checks to defer
this provider to the very end of the per-call bars cascade, even when it is
configured as the primary provider -- see that method's docstring for the
exact mechanism.

**Fail-closed rules (ADR-0008 Decision 1)**, checked in this order. A real
probe from the owner's machine confirmed the two status-code-based ones
(404, and a clean unauthenticated 200) empirically, so these are no longer
guesses:

* **HTTP 404** -> confirmed "this ticker is not known to TipRanks" (an
  ordinary garbage-symbol probe returned a clean 404, not an error page).
  This degrades *that one symbol only* -- caught internally as
  ``_TipRanksNotFoundError`` and mapped to the same "no data for this
  symbol" outcome as an empty/null ``overview`` (see below) -- and must
  never be treated as an outage or spend a retry budget on it.
* **HTTP 401/403** -> ``SourceBlockedError`` (a real block/challenge signal).
* **HTTP 429** -> ``RateLimitError`` (a quantity signal, not a block).
* **HTTP 5xx** -> ``ProviderError(retryable=True)`` -- an outage, not a
  block: this aborts the current call (same as a network/timeout failure)
  so the caller's own retry/backoff and fallback-provider behaviour applies,
  rather than being fail-closed for the rest of the cycle the way a genuine
  block is.
* Any other unexpected status (>= 400, not one of the above), or a non-JSON
  response body -> ``SourceBlockedError`` (unexpected shape, treated as a
  possible block).
* A well-formed JSON response with no ``"overview"`` key at all -> unexpected
  shape -> ``SourceBlockedError``.
* A well-formed JSON response whose ``"overview"`` is present but empty/null
  -> an ordinary "unknown ticker" outcome (the same bucket as the 404 case
  above); degrades *that one symbol* to "no data" and continues the rest of
  the batch, exactly like ``StooqMarketProvider``'s and
  ``YahooMarketProvider``'s per-symbol "not found" handling.

Every capability shares one response cache keyed by the TipRanks ticker
parameter, stored as one JSON file per symbol under
``paths.cache_dir/tipranks/`` with a **1-trading-day TTL**
(``TipRanksConfig.cache_ttl_trading_days``, checked via
``utils.timeutils.trading_days_between`` so the cache survives a weekend but
invalidates on the next real trading session) -- this is what keeps a
scan-universe refresh of thousands of symbols to one call per symbol per
trading day rather than one call per symbol per *capability* per run.

**GetQuotes batching -- CONFIRMED, and now the PRIMARY market-cap path
(owner directive, confirmed by live probes)**: TipRanks' CIBC integration
also exposes
``https://marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=A,B,C,...``
-- a genuinely batched quote lookup (one HTTP call serves every ticker in
the request, unlike ``dataForTicker``'s one-call-per-symbol) that returns a
real-time snapshot per ticker: current-session OHLCV plus caps, not
history. The envelope shape is now CONFIRMED from the owner's own probes,
not guessed:

* Top level: ``{"quotes": [...], "errors": [...], "metadata": {"count",
  "success", "errors"}}``.
* Each ``quotes[]`` row: ``{ticker, currency, exchangeRate, isomic,
  marketName, price, open, low, high, volume, changeAmount, changePercent,
  lastTradeDate, lastClose, marketCap, realTimeMarketCap, isRealTime,
  isMarketOpen, isPremarket, isAfterMarket, prePostMarket,
  lastCacheUpdate}``.
* Requested tickers are echoed back exactly as sent, including ``TSE:``
  notation for Canadian names -- the same ``tipranks_ticker`` mapping
  ``dataForTicker`` uses (see above) is reused for GetQuotes' ``tickers``
  parameter, comma-joined per chunk.
* A ticker TipRanks has nothing for comes back either in ``errors[]`` or
  simply absent from ``quotes[]`` -- both are treated identically by this
  adapter: skip that one symbol, never fatal to the rest of the batch (see
  ``_parse_getquotes_envelope``).

This repository still has no committed fixture of a *raw* captured response
body (the owner's probe output was relayed as a confirmed field list/shape,
not pasted verbatim) -- see each ``tests/fixtures/tipranks/getquotes_*.json``
file's own ``_fixture_note`` for that provenance distinction. Every field
access below stays defensive regardless (any unexpected shape degrades to
"contributes nothing" rather than raising), the same posture as every other
parser in this module.

``get_quotes(symbols)`` is the shared batched primitive two capabilities are
built on:

1. **Market caps (the speed win)** -- ``get_market_caps`` tries
   ``get_quotes`` for the WHOLE requested symbol list first (chunked at
   ``TipRanksConfig.getquotes_batch_size``, default 200 -- so a ~2,400-symbol
   universe refresh costs roughly a dozen GetQuotes calls instead of one
   ``dataForTicker`` call per symbol), then falls back to the existing
   per-symbol ``dataForTicker`` path (via ``_resolve_map``) for only
   whatever GetQuotes did not cover. ``TipRanksConfig.use_getquotes_batch``
   defaults to ``True`` now -- GetQuotes is the primary path, not an opt-in
   optimisation layered on top of it, per the owner's explicit direction
   ("make TipRanks the primary market-data workhorse via its BATCHED
   GetQuotes endpoint"). Setting it ``False`` disables ``get_quotes``
   entirely (a single flag check inside ``get_quotes`` itself, so every
   caller -- market caps and the current-session bar below -- is gated
   uniformly): the adapter then behaves exactly as it did before this
   change, one ``dataForTicker`` call per symbol.
2. **Current-session bar** -- ``get_current_session_bars`` turns each
   resolved quote into one ``Bar`` for *today's* (or the most recently
   completed) session, using ``price`` as the close. This is a distinct
   capability from ``get_daily_bars`` (which stays close-only/last-resort,
   unaffected by any of this): callers that want a fresher "today" bar than
   Yahoo's historical chart typically offers call this directly and merge
   it in themselves -- see ``data.ingest.DataIngestor.ingest_prices``'s
   conservative merge (append-only, dedupe-by-session-date, documented
   there in full).

**CONFIRMED currency trap (do not "fix" this without re-checking the real
probe first)**: GetQuotes has no ``marketCapUSD`` field at all (unlike
``dataForTicker``'s ``overview``) -- ``marketCap``/``realTimeMarketCap`` are
always in the listing's *local* currency. The probe showed ``TSE:TECK.B``
reporting a CAD ``marketCap`` alongside a non-1 ``exchangeRate``, and that
``local_cap * exchangeRate`` recovers the same USD figure
``dataForTicker.marketCapUSD`` reports directly; a US entry's
``exchangeRate`` is confirmed to be 1. The exact rule this adapter applies,
in ``_getquotes_market_cap_usd``:

1. Prefer ``realTimeMarketCap`` over ``marketCap`` when both are present and
   positive (the fresher of the two current-session figures).
2. If ``currency`` is ``"USD"`` (or absent, treated as USD) -> use that raw
   cap as-is.
3. Otherwise (non-USD currency) -> multiply by ``exchangeRate`` if it is
   present and positive; if not, this row contributes **nothing** -- a raw,
   un-normalised non-USD cap is never returned by this adapter, from either
   endpoint.

Market-cap coverage never depends on GetQuotes succeeding: ``dataForTicker``
(with the ``TSE:SYMBOL`` notation) remains the fallback path for every
symbol, US and Canadian alike, for whatever GetQuotes did not resolve; any
failure in the GetQuotes sweep -- a bad response shape, a network error, a
whole chunk failing -- is caught and logged, falling straight back to the
per-symbol path with no user-visible effect beyond the wasted batch call(s).
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from claudetrade.config import TipRanksConfig
from claudetrade.data.universe import load_packaged_universe
from claudetrade.domain import (
    AnalystSnapshot,
    Bar,
    DataQualityIssue,
    DataQualitySeverity,
    EarningsEvent,
    EarningsSession,
    InstitutionalSnapshot,
    SecurityInfo,
)
from claudetrade.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
    SourceBlockedError,
    parallel_map,
)
from claudetrade.providers.market.tipranks_analyst import parse_analyst_snapshot
from claudetrade.providers.market.tipranks_institutional import parse_institutional_snapshot
from claudetrade.utils.timeutils import trading_days_between, utc_now

log = logging.getLogger(__name__)

WIDGET_BASE_URL = "https://widgets.tipranks.com/api/etoro/dataForTicker"
#: Fallback probe for a symbol ``dataForTicker`` has no analyst/overview
#: coverage for -- see the module docstring's "prices_only" section. Verbatim
#: owner-captured response committed at
#: ``tests/fixtures/tipranks/historicalprices_MHD.json`` (a closed-end fund).
HISTORICALPRICES_BASE_URL = "https://widgets.tipranks.com/api/etoro/historicalprices"
#: Batched real-time quote lookup -- see the module docstring's GetQuotes
#: section. Envelope shape CONFIRMED by the owner's live probes; primary
#: market-cap path by default (``TipRanksConfig.use_getquotes_batch``).
GETQUOTES_BASE_URL = "https://marketsv3.tipranks.com/api/quotes/GetQuotes"

DEFAULT_RATE_LIMIT = 60  # Calls per minute -- see TipRanksConfig.rate_limit_per_minute.
DEFAULT_MAX_WORKERS = 8  # See MarketDataConfig.max_workers.
_USER_AGENT = "Mozilla/5.0 (compatible; claudetrade research use)"

#: A US-listed dash share-class suffix TipRanks expects as a dot instead
#: (``BRK-B`` -> ``BRK.B``, ``BF-B`` -> ``BF.B``) -- confirmed by the owner's
#: live refresh log ("tipranks has no data for BRK-B", "BF-B": plain 404s
#: under this codebase's own hyphenated notation). Deliberately narrow: only a
#: single trailing letter after the dash matches, so a symbol like ``LILAP``
#: (no dash at all) or a hypothetical multi-letter suffix is never mistaken
#: for a share class and rewritten. Canadian (TSX/TSXV) listings have their
#: own, separately-handled dot conversion below (``TECK-B`` -> ``TSE:TECK.B``)
#: and never reach this branch.
_US_CLASS_SHARE_RE = re.compile(r"^[A-Z]{1,5}-[A-Z]$")

#: Progress logging cadence (item 6): a long-running refresh across the whole
#: universe must never look hung. Whichever of these triggers first.
_PROGRESS_LOG_EVERY_N = 100
_PROGRESS_LOG_EVERY_S = 60.0

#: Canadian (TSX/TSXV) listings are requested with a TipRanks "TSE:" prefix
#: and the exchange's own dotted share-class notation (confirmed against the
#: TECK.B fixture: our "TECK-B" -> their "TSE:TECK.B").
CA_EXCHANGES = frozenset({"TSX", "TSXV"})

#: PROVISIONAL -- see the module docstring's "timeOfDay is provisional"
#: section. Only 1, 2 and 4 have been observed (across two fixture captures);
#: anything else maps to UNKNOWN rather than raising.
_TIME_OF_DAY_MAP: dict[int, EarningsSession] = {
    1: EarningsSession.BEFORE_OPEN,
    2: EarningsSession.DURING,  # UNVERIFIED: inferred by elimination, not confirmed.
    4: EarningsSession.AFTER_CLOSE,
}

_EXCHANGE_MAP_CACHE: dict[str, str] | None = None


def _default_exchange_map() -> dict[str, str]:
    """Symbol -> exchange, derived from the packaged seed universes.

    Same rationale and caching behaviour as
    ``providers.market.stooq._default_exchange_map`` / ``providers.market.
    yahoo._default_exchange_map``: consulted only when the caller does not
    pass ``exchange`` explicitly.
    """
    global _EXCHANGE_MAP_CACHE
    if _EXCHANGE_MAP_CACHE is None:
        try:
            securities = load_packaged_universe()
        except Exception:
            log.warning(
                "could not load packaged universe for tipranks's exchange map", exc_info=True
            )
            securities = []
        _EXCHANGE_MAP_CACHE = {
            s.symbol.upper(): s.exchange.upper() for s in securities if s.exchange
        }
    return _EXCHANGE_MAP_CACHE


def tipranks_ticker(symbol: str, exchange: str | None = None) -> str:
    """Map an exchange ticker to TipRanks' ``dataForTicker`` notation.

    Canadian (TSX/TSXV) listings get a ``TSE:`` prefix with this codebase's
    hyphenated share-class convention rewritten to TipRanks' dotted one
    (``TECK-B`` -> ``TSE:TECK.B``) -- confirmed against the committed TECK.B
    fixture, whose ``overview.ticker`` echoes exactly this form back.

    US listings are passed through bare EXCEPT for a dash-suffixed single-
    letter share class, which TipRanks also expects in dot notation
    (``BRK-B`` -> ``BRK.B``, ``BF-B`` -> ``BF.B``) -- confirmed by a real
    refresh log showing plain 404s for both under this codebase's own dash
    convention. Yahoo, by contrast, wants the dash form for these same
    symbols (see ``YahooMarketProvider.yahoo_symbol``) -- this mapping is
    local to this module and never touches that one. Only a genuine
    single-letter class suffix matches (``_US_CLASS_SHARE_RE``); a symbol
    like ``LILAP`` (no dash) is left untouched.

    Exchange resolution: pass ``exchange`` explicitly when known, otherwise
    fall back to the packaged seed universes' symbol -> exchange column,
    same convention as stooq/yahoo's own symbol-mapping helpers.
    """
    stripped = symbol.strip().upper()
    exch = (exchange or _default_exchange_map().get(stripped, "")).upper()
    if exch in CA_EXCHANGES:
        return f"TSE:{stripped.replace('-', '.')}"
    if _US_CLASS_SHARE_RE.match(stripped):
        return stripped.replace("-", ".")
    return stripped


def _safe_cache_key(ticker_param: str) -> str:
    """Filesystem-safe cache filename stem for a TipRanks ticker parameter."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", ticker_param)


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _preferred_market_cap(overview: dict[str, Any]) -> float | None:
    """Prefer ``marketCapUSD``; else raw ``marketCap`` -- no currency gating.

    Owner-simplified rule: the >= $1B universe floor is currency-agnostic, so
    a CAD-denominated ``marketCap`` on a TSX listing is used as-is when
    ``marketCapUSD`` is absent, never withheld or flagged for its currency.
    Only the top-level ``overview`` fields are consulted -- nested blocks
    (e.g. ``portfolioHoldingData.nextDividendDate.marketCap``, confirmed in
    the TECK.B fixture to carry a *different*, CAD-only figure) are never
    used as a cap source.
    """
    cap = overview.get("marketCapUSD")
    if not (isinstance(cap, (int, float)) and cap > 0):
        cap = overview.get("marketCap")
    if isinstance(cap, (int, float)) and cap > 0:
        return float(cap)
    return None


def _tipranks_exchange_to_internal(market_field: str) -> str:
    """Map TipRanks' ``overview.market`` string to this codebase's exchange
    codes. Observed values are inconsistently cased (``"NASDAQ"`` for INTC,
    ``"tsx"`` for TECK.B), so this always upper-cases before matching.
    Anything unrecognised is passed through upper-cased rather than dropped,
    so an unmapped exchange name stays visible instead of silently blanked.
    """
    normalised = (market_field or "").strip().upper()
    if normalised in {"TSX", "TSE"}:
        return "TSX"
    if normalised in {"NASDAQ", "NYSE", "AMEX"}:
        return normalised
    return normalised


def _parse_getquotes_envelope(
    payload: Any, requested_params: list[str]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Parse the CONFIRMED GetQuotes envelope: ``{"quotes": [...], "errors":
    [...], "metadata": {...}}`` -- see the module docstring's GetQuotes
    section for the full confirmed field list.

    Returns ``(quotes_by_ticker_param, errored_ticker_params)``. A ticker
    param present in neither ``quotes`` nor ``errors`` is simply not covered
    by this response -- the caller treats that identically to an explicit
    error (skip that one symbol, never fatal to the rest of the batch).
    Every access stays defensive: an unexpected shape (wrong type at any
    level, a row that isn't a dict, ...) degrades to that row/response
    contributing nothing rather than raising, per ADR-0008 Decision 1 -- a
    surprising field must never be able to break the caller's own fallback
    path.
    """
    quotes: dict[str, dict[str, Any]] = {}
    errors: set[str] = set()
    if not isinstance(payload, dict):
        return quotes, errors
    requested = set(requested_params)

    rows = payload.get("quotes")
    if isinstance(rows, list):
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                ticker = row.get("ticker")
                if not isinstance(ticker, str) or ticker not in requested:
                    continue
                quotes[ticker] = row
            except Exception:
                continue

    error_rows = payload.get("errors")
    if isinstance(error_rows, list):
        for row in error_rows:
            try:
                if isinstance(row, str):
                    if row in requested:
                        errors.add(row)
                elif isinstance(row, dict):
                    ticker = row.get("ticker")
                    if isinstance(ticker, str) and ticker in requested:
                        errors.add(ticker)
            except Exception:
                continue

    return quotes, errors


def _getquotes_market_cap_usd(quote: dict[str, Any]) -> float | None:
    """USD-normalised market cap from one GetQuotes row -- see the module
    docstring's "CONFIRMED currency trap" section for the full rationale.

    Exact rule, in order:

    1. Prefer ``realTimeMarketCap`` over ``marketCap`` when both are present
       and positive (the fresher current-session figure).
    2. ``currency == "USD"`` (or the field is absent, treated as USD) -> the
       raw cap is already USD, used as-is.
    3. Any other ``currency`` -> multiply by ``exchangeRate`` if it is
       present and positive.
    4. Non-USD with no usable ``exchangeRate`` -> contributes nothing. A raw,
       un-normalised non-USD cap is never returned.
    """
    cap = quote.get("realTimeMarketCap")
    if not (isinstance(cap, (int, float)) and cap > 0):
        cap = quote.get("marketCap")
    if not (isinstance(cap, (int, float)) and cap > 0):
        return None

    currency = quote.get("currency")
    is_usd = currency is None or (isinstance(currency, str) and currency.strip().upper() == "USD")
    if is_usd:
        return float(cap)

    rate = quote.get("exchangeRate")
    if isinstance(rate, (int, float)) and rate > 0:
        return float(cap) * float(rate)
    return None


def _getquotes_session_bar(symbol: str, quote: dict[str, Any]) -> Bar | None:
    """Build one current-session ``Bar`` from a GetQuotes row -- see the
    module docstring's "current-session bar" section.

    The session date comes from ``lastTradeDate`` (parsed to a date), never
    from ``utc_now()`` -- deriving it from wall-clock time would misdate a
    bar for an exchange in a different timezone, or during a pre/post-market
    snapshot. ``price`` is GetQuotes' own field for the latest traded print
    and is used as this bar's close -- a live intraday value while the
    market is open, the final close once it isn't; either way it belongs to
    the session ``lastTradeDate`` names, never assumed to be a prior day's.
    Missing/unparseable open, high, low, price, or date -> ``None`` (no bar
    synthesised from a partial row).
    """
    date_str = quote.get("lastTradeDate")
    if not isinstance(date_str, str) or not date_str:
        return None
    try:
        session = dt.datetime.fromisoformat(date_str).date()
    except ValueError:
        return None

    o, h, low_, price = quote.get("open"), quote.get("high"), quote.get("low"), quote.get("price")
    if any(v is None for v in (o, h, low_, price)):
        return None
    try:
        o_f, h_f, l_f, c_f = float(o), float(h), float(low_), float(price)
    except (TypeError, ValueError):
        return None

    volume = quote.get("volume")
    try:
        vol_f = float(volume) if volume is not None else 0.0
    except (TypeError, ValueError):
        vol_f = 0.0

    return Bar(
        symbol=symbol,
        session=session,
        open=round(o_f, 4),
        high=round(h_f, 4),
        low=round(l_f, 4),
        close=round(c_f, 4),
        volume=round(vol_f, 1),
        adj_close=None,
        source="tipranks_getquotes",
    )


@dataclass(slots=True)
class _Resolution:
    """One symbol's resolved state from ``TipRanksProvider._resolve`` --
    see that method's docstring for what each ``state`` value means."""

    state: str  # "found" | "unknown" | "prices_only"
    overview: dict[str, Any] | None = None
    #: Raw ``historicalprices`` rows, only populated for ``"prices_only"``.
    bars_rows: list[dict[str, Any]] | None = None
    #: Whether this resolution came from the on-disk cache rather than a
    #: live fetch this call -- used only for the progress log's
    #: fetched/cached split, never for correctness.
    from_cache: bool = False


class _TipRanksNotFoundError(ProviderError):
    """TipRanks has no data for this specific symbol -- a confirmed clean
    HTTP 404 for an unknown/garbage ticker (see the module docstring's
    fail-closed rules). Degrades that one symbol to "no data" rather than
    aborting the whole batch/call -- mirrors
    ``providers.market.stooq.SymbolNotFoundError`` and
    ``providers.market.yahoo._YahooNoDataError``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="tipranks", retryable=False)


class TipRanksProvider(MarketDataProvider):
    """Market-data + earnings adapter over TipRanks' unauthenticated widget API.

    Implements ``MarketDataProvider`` (earnings methods are added directly
    onto this same class below rather than through a second adapter, since
    ``EarningsProvider`` is a structural ``Protocol`` -- ``get_upcoming_earnings``
    / ``get_historical_earnings`` satisfy it without a separate inheritance
    declaration) so that earnings, market caps, reference data and last-resort
    bars all share one fetch/cache path per symbol.
    """

    name = "tipranks"
    #: See the module docstring: this tells
    #: ``providers.registry.FallbackMarketProvider.get_daily_bars`` to defer
    #: this provider to the very end of the bars cascade regardless of its
    #: position in ``market_data.provider``/``fallbacks``.
    bars_last_resort = True

    def __init__(
        self,
        config: TipRanksConfig | None = None,
        *,
        cache_dir: Path | str | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._config = config or TipRanksConfig()
        self._cache_dir = (Path(cache_dir) / "tipranks") if cache_dir else None
        self._rate_limiter = RateLimiter(
            self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            name="tipranks",
            max_wait_s=30.0,
        )
        #: See ``providers.base.parallel_map``: worker threads for the
        #: per-symbol fetch loop, all sharing ``self._rate_limiter`` above (a
        #: thread-safe limiter), so the enforced calls/minute ceiling is
        #: global across workers, not per thread.
        self._max_workers = max(1, max_workers or DEFAULT_MAX_WORKERS)
        #: Guards every piece of mutable state below that a worker thread can
        #: touch concurrently (everything except the per-symbol cache files,
        #: which are already one file per unique symbol).
        self._lock = threading.Lock()
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Symbols TipRanks returned an empty/null overview for on the most
        #: recent call(s) -- mirrors ``StooqMarketProvider._not_found``. Also
        #: populated for the "prices_only" state (see ``_Resolution``): both
        #: mean "no dataForTicker/analyst coverage", which is what this set
        #: has always meant.
        self._not_found: set[str] = set()
        #: Accumulated close-only-bars / sparse-bars data-quality warnings,
        #: drained by ``drain_quality_warnings()``. Always logged too, so a
        #: caller that never drains this still sees the degrade in the logs.
        self._quality_warnings: list[DataQualityIssue] = []
        #: Optional per-symbol progress hook ``(done, total) -> None``, set by
        #: the caller that owns a user-visible progress surface (see
        #: ``DataIngestor.ingest_securities``). The every-100-symbols log
        #: above it is for the console; without this hook the webapi's
        #: refresh-status endpoint only ever saw a phase's 0% and 100%,
        #: which is what left the UI banner stuck at 0/N for a 40-minute
        #: securities pass while the console counted normally.
        self.on_symbol_progress: Callable[[int, int], None] | None = None

    # --- status -------------------------------------------------------------

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=True,
            configured=True,
            message="unauthenticated widgets.tipranks.com dataForTicker endpoint; primary source",
            last_error=self._last_error,
            last_success=self._last_success,
            supports_point_in_time=False,
            supports_delisted=False,
            rate_limit_per_minute=self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            calls_made=self._calls,
            licence_note=(
                "Unauthenticated partner-widget JSON endpoint (widgets.tipranks.com), not a "
                "published/contracted API -- no SLA, could be restricted or withdrawn without "
                "notice. Personal/research use only, per ADR-0008; fails closed (stops calling "
                "for the rest of the cycle) on any block/challenge/unexpected-shape signal "
                "rather than retrying, evading, or rotating identity."
            ),
            capabilities={
                "daily_bars": True,
                "bars_last_resort": True,
                "intraday": False,
                "market_caps": True,
                "security_info": True,
                "earnings": True,
                "corporate_actions": False,
                "getquotes_batch_enabled": self._config.use_getquotes_batch,
                "getquotes_current_bar": self._config.use_getquotes_batch,
                #: See ``get_analyst_snapshots`` -- harvested from the same
                #: ``overview`` responses at zero additional HTTP cost.
                "analyst_sentiment": True,
            },
        )

    def drain_quality_warnings(self) -> list[DataQualityIssue]:
        """Pop and return every accumulated close-only-bars warning.

        Not wired into ``data.ingest.DataIngestor`` by this change (that
        module's boundary here is the market-cap accounting fix only) --
        exposed as a plain, documented extension point for a future caller to
        pick up. The degrade is never silent regardless: every occurrence is
        also emitted as a ``log.warning`` at the point it happens.
        """
        out = self._quality_warnings
        self._quality_warnings = []
        return out

    # --- shared fetch/cache ---------------------------------------------------

    def _cache_path(self, cache_key: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{cache_key}.json"

    def _load_cache_record(self, cache_key: str) -> dict[str, Any] | None:
        """Return the raw cached record for ``cache_key`` if it exists and is
        still within its state's TTL, else ``None`` (cache miss/expired).

        The record's ``state`` (item 3/4) selects which TTL applies: a
        "found" record uses the short ``cache_ttl_trading_days`` (a real,
        analyst-covered symbol may get real updates daily); an "unknown" or
        "prices_only" record uses the much longer ``unknown_ticker_ttl_days``
        -- neither state is going to change from one day to the next, so
        there is no reason to keep paying a round trip for it every refresh.
        """
        path = self._cache_path(cache_key)
        if path is None or not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            fetched_date = dt.date.fromisoformat(record["fetched_date"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        today = dt.datetime.now(tz=dt.UTC).date()
        state = record.get("state", "found")
        ttl = (
            max(1, self._config.unknown_ticker_ttl_days)
            if state in ("unknown", "prices_only")
            else max(1, self._config.cache_ttl_trading_days)
        )
        if trading_days_between(fetched_date, today) >= ttl:
            return None
        return record

    def _store_cache_record(
        self,
        cache_key: str,
        *,
        state: str,
        overview: dict[str, Any] | None = None,
        historical_prices: list[dict[str, Any]] | None = None,
    ) -> None:
        path = self._cache_path(cache_key)
        if path is None:
            return
        record: dict[str, Any] = {
            "fetched_date": dt.datetime.now(tz=dt.UTC).date().isoformat(),
            "state": state,
        }
        if overview is not None:
            record["overview"] = overview
        if historical_prices is not None:
            record["historical_prices"] = historical_prices
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            log.debug("failed to write tipranks cache for %s", cache_key, exc_info=True)

    def _resolve(self, symbol: str, exchange: str | None = None) -> _Resolution:
        """Resolve ``symbol`` to one of three states, via cache or a live
        fetch -- the single path every capability (market caps, security
        info, earnings, bars) goes through, so the cache stays consistent
        across all of them:

        * ``"found"`` -- ``dataForTicker`` has a real ``overview``.
        * ``"unknown"`` -- neither ``dataForTicker`` nor the
          ``historicalprices`` fallback probe (item 4) has anything for this
          symbol; a genuinely unknown/delisted/renamed ticker.
        * ``"prices_only"`` -- ``dataForTicker`` has nothing (a 404 or a
          null/empty overview), but ``historicalprices`` returned real rows:
          the symbol exists (typically a closed-end fund with no analyst
          coverage) and can serve bars as a last resort, but never a market
          cap, security info beyond the packaged seed, or earnings.

        Raises:
            SourceBlockedError: non-JSON body, missing ``overview`` key,
                401/403, or any other unexpected/non-2xx response.
            RateLimitError: HTTP 429, or the local limiter's own budget.
            ProviderError: HTTP 5xx (an outage, retryable) or a network/
                timeout failure.
        """
        ticker_param = tipranks_ticker(symbol, exchange)
        cache_key = _safe_cache_key(ticker_param)

        cached = self._load_cache_record(cache_key)
        if cached is not None:
            state = cached.get("state", "found")
            if state == "prices_only":
                rows = cached.get("historical_prices")
                return _Resolution(
                    state="prices_only",
                    bars_rows=rows if isinstance(rows, list) else [],
                    from_cache=True,
                )
            if state == "unknown":
                return _Resolution(state="unknown", from_cache=True)
            overview = cached.get("overview")
            return _Resolution(
                state="found",
                overview=overview if isinstance(overview, dict) else None,
                from_cache=True,
            )

        self._rate_limiter.acquire()
        with self._lock:
            self._calls += 1
        try:
            response = self._get(ticker_param)
        except _TipRanksNotFoundError:
            # HTTP 404 -- confirmed "unknown ticker", not an outage. Try the
            # historicalprices fallback before finalising the cache state.
            log.info(
                "tipranks has no data for %s (%s) -- HTTP 404, unknown ticker", symbol, ticker_param
            )
            return self._resolve_unknown_via_fallback(symbol, ticker_param, cache_key)

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"tipranks returned non-JSON for {symbol} ({ticker_param}): {exc}",
                provider=self.name,
            ) from exc

        if not isinstance(payload, dict) or "overview" not in payload:
            raise SourceBlockedError(
                f"tipranks response for {symbol} ({ticker_param}) had no 'overview' field -- "
                "unexpected shape, fail-closed per ADR-0008 Decision 1.",
                provider=self.name,
            )

        with self._lock:
            self._last_success = utc_now()
        overview = payload.get("overview")
        if not overview:
            log.info("tipranks has no overview for %s (%s) -- unknown ticker", symbol, ticker_param)
            return self._resolve_unknown_via_fallback(symbol, ticker_param, cache_key)

        self._store_cache_record(cache_key, state="found", overview=overview)
        return _Resolution(state="found", overview=overview)

    def _resolve_unknown_via_fallback(
        self, symbol: str, ticker_param: str, cache_key: str
    ) -> _Resolution:
        """``dataForTicker`` had nothing for ``symbol`` -- try
        ``historicalprices`` (item 4) before caching it as fully unknown. A
        ``dataForTicker`` SUCCESS never reaches this method at all, so a
        success never calls ``historicalprices``, by construction.
        """
        with self._lock:
            self._last_success = utc_now()
            self._not_found.add(symbol)
        rows = self._fetch_historicalprices_rows(ticker_param)
        if rows:
            log.info(
                "tipranks: %s (%s) has no dataForTicker coverage but historicalprices "
                "returned %d row(s) -- caching as prices_only (bars-only, last resort)",
                symbol, ticker_param, len(rows),
            )
            self._store_cache_record(cache_key, state="prices_only", historical_prices=rows)
            return _Resolution(state="prices_only", bars_rows=rows)
        self._store_cache_record(cache_key, state="unknown")
        return _Resolution(state="unknown")

    def _fetch_historicalprices_rows(self, ticker_param: str) -> list[dict[str, Any]]:
        """Raw ``historicalprices`` rows for ``ticker_param``, or ``[]`` if
        this symbol has nothing there either (a 404 -- the same fail-soft
        treatment as ``dataForTicker``'s own unknown-ticker case; this is a
        fallback PROBE, not a second fail-open path). A genuine block/outage
        signal (401/403/429/5xx/unexpected shape) still raises, via ``_get``.
        """
        self._rate_limiter.acquire()
        with self._lock:
            self._calls += 1
        try:
            response = self._get(ticker_param, base_url=HISTORICALPRICES_BASE_URL)
        except _TipRanksNotFoundError:
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"tipranks historicalprices returned non-JSON for {ticker_param}: {exc}",
                provider=self.name,
            ) from exc
        if not isinstance(payload, list):
            log.debug("tipranks historicalprices returned a non-list payload for %s", ticker_param)
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _fetch_overview(self, symbol: str, exchange: str | None = None) -> dict[str, Any] | None:
        """Back-compat convenience wrapper over ``_resolve``: the ``overview``
        for a "found" symbol, else ``None`` ("unknown" or "prices_only" --
        neither has analyst/overview data)."""
        return self._resolve(symbol, exchange).overview

    def _resolve_map(
        self,
        symbols: list[str],
        *,
        progress_offset: int = 0,
        progress_total: int | None = None,
    ) -> dict[str, _Resolution]:
        """Resolve every symbol in ``symbols`` -- in parallel, see
        ``providers.base.parallel_map`` -- logging progress every
        ``_PROGRESS_LOG_EVERY_N`` symbols or ``_PROGRESS_LOG_EVERY_S``
        seconds, whichever comes first (item 6), so a long refresh across a
        whole universe never looks hung.

        ``progress_offset``/``progress_total`` let a caller that already
        completed other work before this call (``get_market_caps``'s
        GetQuotes sweep, resolving most of the universe before this
        per-symbol ``dataForTicker`` fallback pass even starts) keep the
        EXTERNAL ``on_symbol_progress`` hook's ``done`` value monotonically
        increasing across both phases, rather than resetting to 0 and making
        the caller's progress surface (the webapi refresh-status banner)
        visibly jump backwards partway through a refresh. The console log
        line below is unaffected -- it always describes this call's own
        ``symbols`` count, which stays accurate for what is actually
        happening in this pass.
        """
        total = len(symbols)
        if total == 0:
            return {}
        hook_total = progress_total if progress_total is not None else total
        counts = {"done": 0, "fetched": 0, "cached": 0, "unknown": 0}
        last_log = time.monotonic()
        progress_lock = threading.Lock()

        def _resolve_one(symbol: str) -> _Resolution:
            nonlocal last_log
            resolution = self._resolve(symbol)
            with progress_lock:
                counts["done"] += 1
                counts["cached" if resolution.from_cache else "fetched"] += 1
                if resolution.state == "unknown":
                    counts["unknown"] += 1
                now = time.monotonic()
                due = (
                    counts["done"] == total
                    or counts["done"] % _PROGRESS_LOG_EVERY_N == 0
                    or (now - last_log) >= _PROGRESS_LOG_EVERY_S
                )
                if due:
                    last_log = now
                    log.info(
                        "market data: %d/%d symbols (%d fetched, %d cached, %d unknown)",
                        counts["done"], total, counts["fetched"], counts["cached"], counts["unknown"],
                    )
                hook = self.on_symbol_progress
                if hook is not None:
                    try:
                        hook(progress_offset + counts["done"], hook_total)
                    except Exception:  # progress must never fail a fetch
                        log.debug("symbol-progress hook raised; continuing", exc_info=True)
            return resolution

        return parallel_map(symbols, _resolve_one, max_workers=self._max_workers)

    def _get(
        self,
        ticker_param: str,
        *,
        base_url: str = WIDGET_BASE_URL,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform one GET, translating expected failure modes into
        ``ProviderError`` subclasses per the module docstring's fail-closed
        rules.

        ``ticker_param`` is used verbatim as the ``?ticker=`` query value for
        every ``dataForTicker``/``historicalprices`` call (the default), and
        purely as a LABEL for logging/error messages when the caller passes
        its own ``params`` (GetQuotes' ``?tickers=A,B,C`` batch parameter --
        see ``_fetch_getquotes_chunk``): the same status-code handling below
        applies identically to both endpoints, since none of it is specific
        to what the request parameters actually are.
        """
        try:
            with httpx.Client(
                timeout=self._config.request_timeout_s,
                verify=True,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = client.get(base_url, params=params or {"ticker": ticker_param})
        except httpx.ConnectError as exc:
            self._last_error = str(exc)
            raise ProviderError(
                f"network error connecting to tipranks for {ticker_param}: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            self._last_error = str(exc)
            raise ProviderError(
                f"timeout fetching {ticker_param} from tipranks: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

        # CONFIRMED by a real probe: an unknown/garbage ticker gets a clean
        # HTTP 404, not an error page -- this degrades only the one symbol
        # (see _fetch_overview), never treated as a block or an outage.
        if response.status_code == 404:
            raise _TipRanksNotFoundError(f"tipranks has no data for {ticker_param} (HTTP 404)")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                wait_s = float(retry_after)
            except (ValueError, TypeError):
                wait_s = 60.0
            self._last_error = f"rate limited (429) for {ticker_param}"
            raise RateLimitError(
                f"tipranks rate limit reached for {ticker_param}",
                provider=self.name,
                retry_after_s=wait_s,
            )

        if response.status_code in (401, 403):
            self._last_error = f"HTTP {response.status_code} for {ticker_param}"
            raise SourceBlockedError(
                f"tipranks denied access for {ticker_param} (HTTP {response.status_code}) -- "
                "fail-closed per ADR-0008 Decision 1: no retry, no fingerprint/proxy rotation.",
                provider=self.name,
            )

        if 500 <= response.status_code < 600:
            # An outage, not a block: retryable, and must not disable the
            # source for the rest of the cycle the way a real block does.
            self._last_error = f"HTTP {response.status_code} (outage) for {ticker_param}"
            raise ProviderError(
                f"tipranks returned HTTP {response.status_code} for {ticker_param} -- "
                "treated as an outage, not a block.",
                provider=self.name,
                retryable=True,
            )

        if response.status_code >= 400:
            self._last_error = f"HTTP {response.status_code} for {ticker_param}"
            raise SourceBlockedError(
                f"tipranks returned HTTP {response.status_code} for {ticker_param} -- "
                "fail-closed per ADR-0008 Decision 1.",
                provider=self.name,
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            self._last_error = f"unexpected content-type {content_type!r} for {ticker_param}"
            raise SourceBlockedError(
                f"tipranks returned unexpected content-type {content_type!r} for {ticker_param} "
                "(possible block/challenge page) -- fail-closed per ADR-0008 Decision 1.",
                provider=self.name,
            )
        return response

    # --- bars (last resort only) ----------------------------------------------

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,  # noqa: ARG002 - neither source needs an adjustment toggle: close-only has nothing to adjust, historicalprices already reports both close and adjusted close.
    ) -> dict[str, list[Bar]]:
        """Bars for the bars-last-resort cascade position. LAST RESORT ONLY --
        see the module docstring and ``bars_last_resort``.

        Two sources, preferring real OHLCV whenever it is available:

        * A "found" symbol (real analyst/overview coverage) -- close-only
          bars from ``overview.prices``, as before (no volume/high/low).
        * A "prices_only" symbol (no analyst coverage, but
          ``historicalprices`` has real OHLCV -- typically a closed-end
          fund; item 4) -- real bars from that endpoint, preferred over
          close-only synthesis. Its cadence in practice is downsampled
          (biweekly, not daily); see ``_is_sparse`` for the cadence guard
          that flags this rather than serving it silently as daily data.
        * An "unknown" symbol contributes an empty list.
        """
        out: dict[str, list[Bar]] = {}
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            if resolution is None or resolution.state == "unknown":
                out[symbol] = []
                continue

            if resolution.state == "found":
                bars = self._parse_close_only_bars(symbol, resolution.overview or {}, start, end)
                out[symbol] = bars
                if bars:
                    message = f"{symbol}: close-only bars from tipranks; volume/ATR/gap features degraded"
                    log.warning(message)
                    with self._lock:
                        self._quality_warnings.append(
                            DataQualityIssue(
                                detected_at=utc_now(),
                                severity=DataQualitySeverity.WARNING,
                                category="close_only_bars",
                                symbol=symbol,
                                session=None,
                                message=message,
                            )
                        )
                continue

            # prices_only -- real OHLCV from historicalprices, preferred
            # over close-only synthesis, subject to the cadence guard.
            bars, sparse = self._parse_historicalprices_bars(
                symbol, resolution.bars_rows or [], start, end
            )
            out[symbol] = bars
            if sparse:
                message = (
                    f"{symbol}: historicalprices series is downsampled (median gap > 4 "
                    "calendar days) over the requested range -- returned as-is, "
                    "un-interpolated; not a continuous daily series"
                )
                log.warning(message)
                with self._lock:
                    self._quality_warnings.append(
                        DataQualityIssue(
                            detected_at=utc_now(),
                            severity=DataQualitySeverity.WARNING,
                            category="sparse_bars",
                            symbol=symbol,
                            session=None,
                            message=message,
                        )
                    )
            elif bars:
                log.info(
                    "%s: bars from tipranks historicalprices (no analyst/overview coverage)",
                    symbol,
                )
        return out

    @staticmethod
    def _parse_close_only_bars(
        symbol: str, overview: dict[str, Any], start: dt.date, end: dt.date
    ) -> list[Bar]:
        bars: list[Bar] = []
        for row in overview.get("prices") or []:
            try:
                date_str = row.get("date")
                price = row.get("p")
                if not date_str or price is None:
                    continue
                session = dt.datetime.fromisoformat(date_str).date()
                if not (start <= session <= end):
                    continue
                p = round(float(price), 4)
            except (AttributeError, TypeError, ValueError):
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    session=session,
                    open=p,
                    high=p,
                    low=p,
                    close=p,
                    volume=0.0,
                    adj_close=None,
                    source="tipranks",
                )
            )
        bars.sort(key=lambda b: b.session)
        return bars

    @staticmethod
    def _parse_historicalprices_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Parse raw ``historicalprices`` rows per the module docstring's
        schema facts: ``volume == 0`` rows are Jan-1 holiday padding and are
        dropped; ``price`` (the dividend/split-adjusted close) maps to
        ``adj_close``, ``close`` maps to ``close``. Deduped and sorted by
        date -- a duplicate date keeps the last row seen."""
        by_date: dict[dt.date, dict[str, Any]] = {}
        for row in rows:
            try:
                volume = row.get("volume")
                if volume is None or float(volume) == 0.0:
                    continue
                date_str = row.get("date")
                if not date_str:
                    continue
                session = dt.datetime.fromisoformat(date_str).date()
                close = row.get("close")
                price = row.get("price")
                if close is None or price is None:
                    continue
                close_f = float(close)
                open_ = row.get("open")
                high = row.get("high")
                low = row.get("low")
                by_date[session] = {
                    "session": session,
                    "open": float(open_) if open_ is not None else close_f,
                    "high": float(high) if high is not None else close_f,
                    "low": float(low) if low is not None else close_f,
                    "close": close_f,
                    "adj_close": float(price),
                    "volume": float(volume),
                }
            except (TypeError, ValueError):
                continue
        return [by_date[d] for d in sorted(by_date)]

    @staticmethod
    def _is_sparse(rows: list[dict[str, Any]]) -> bool:
        """Cadence guard (item 4): the committed MHD fixture's real cadence
        is biweekly (~14 calendar days between rows), not daily. A median
        inter-row gap over 4 calendar days means the series is downsampled
        and must be flagged rather than served as ordinary daily bars.
        Fewer than two rows cannot have a gap at all -- never flagged."""
        if len(rows) < 2:
            return False
        dates = sorted(r["session"] for r in rows)
        gaps = sorted((b - a).days for a, b in itertools.pairwise(dates))
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        return median > 4

    def _parse_historicalprices_bars(
        self, symbol: str, rows: list[dict[str, Any]], start: dt.date, end: dt.date
    ) -> tuple[list[Bar], bool]:
        """Rows within ``[start, end]`` as ``Bar`` objects, plus whether that
        in-range subset triggers the cadence guard. Real OHLCV throughout --
        this is never close-only synthesis."""
        parsed = self._parse_historicalprices_rows(rows)
        in_range = [r for r in parsed if start <= r["session"] <= end]
        sparse = self._is_sparse(in_range)
        bars = [
            Bar(
                symbol=symbol,
                session=r["session"],
                open=round(r["open"], 4),
                high=round(r["high"], 4),
                low=round(r["low"], 4),
                close=round(r["close"], 4),
                volume=round(r["volume"], 1),
                adj_close=round(r["adj_close"], 6),
                source="tipranks",
            )
            for r in in_range
        ]
        return bars, sparse

    def get_intraday_bars(
        self,
        symbols: list[str],  # noqa: ARG002
        start: dt.datetime,  # noqa: ARG002
        end: dt.datetime,  # noqa: ARG002
        *,
        interval_minutes: int = 5,  # noqa: ARG002
    ) -> dict[str, list[Bar]]:
        raise ProviderError(
            "tipranks intraday bars are not implemented by this adapter",
            provider=self.name,
        )

    # --- GetQuotes (batched real-time quotes) ------------------------------------

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Batched real-time quote snapshot via GetQuotes, keyed by the
        CALLER's own symbol (not the TipRanks ticker param) -- see the
        module docstring's GetQuotes section for the confirmed envelope
        shape and the two capabilities built on top of this
        (``get_market_caps``, ``get_current_session_bars``).

        A no-op (returns ``{}`` without any HTTP call) when
        ``TipRanksConfig.use_getquotes_batch`` is ``False`` -- the single
        place this flag is checked, so every caller of this method is gated
        by it uniformly. ``symbols`` is chunked into batches of
        ``TipRanksConfig.getquotes_batch_size`` (default 200); each chunk's
        symbols are mapped to TipRanks' ticker notation with the same
        ``tipranks_ticker`` helper ``dataForTicker`` uses (TSX ->
        ``TSE:SYMBOL``), comma-joined into one ``?tickers=`` request. A
        symbol GetQuotes has no data for (in ``errors[]`` or simply absent
        from ``quotes[]``) is omitted from the result -- never fatal to the
        rest of the batch or to this method's own caller. Progress is
        reported through ``on_symbol_progress`` after each chunk completes,
        same mechanism as ``_resolve_map`` (item 6).
        """
        out: dict[str, dict[str, Any]] = {}
        if not symbols or not self._config.use_getquotes_batch:
            return out

        batch_size = max(1, self._config.getquotes_batch_size)
        unique_symbols = list(dict.fromkeys(symbols))  # de-dup, preserve order
        param_by_symbol = {s: tipranks_ticker(s) for s in unique_symbols}
        total = len(unique_symbols)
        done = 0

        for i in range(0, total, batch_size):
            chunk_symbols = unique_symbols[i : i + batch_size]
            param_to_symbol: dict[str, str] = {
                param_by_symbol[s]: s for s in chunk_symbols
            }
            try:
                quotes_by_param = self._fetch_getquotes_chunk(list(param_to_symbol))
            except Exception:
                log.warning(
                    "tipranks GetQuotes chunk of %d symbol(s) failed; skipping this chunk "
                    "(falls back to per-symbol dataForTicker for anything it would have "
                    "resolved)",
                    len(chunk_symbols),
                    exc_info=True,
                )
                quotes_by_param = {}

            for param, quote in quotes_by_param.items():
                symbol = param_to_symbol.get(param)
                if symbol:
                    out[symbol] = quote

            done += len(chunk_symbols)
            hook = self.on_symbol_progress
            if hook is not None:
                try:
                    hook(done, total)
                except Exception:  # progress must never fail a fetch
                    log.debug("symbol-progress hook raised during GetQuotes; continuing", exc_info=True)

        return out

    def _fetch_getquotes_chunk(self, ticker_params: list[str]) -> dict[str, dict[str, Any]]:
        """One GetQuotes HTTP call for up to ``getquotes_batch_size``
        tickers. Fail-closed rules from ``_get`` apply identically to this
        endpoint (401/403 -> ``SourceBlockedError``, 429 -> ``RateLimitError``,
        5xx -> retryable ``ProviderError``) -- all propagate to the caller
        (``get_quotes``), which logs and skips just this one chunk rather
        than aborting the whole sweep.
        """
        self._rate_limiter.acquire()
        with self._lock:
            self._calls += 1
        label = f"<GetQuotes batch of {len(ticker_params)} tickers>"
        try:
            response = self._get(
                label,
                base_url=GETQUOTES_BASE_URL,
                params={"tickers": ",".join(ticker_params)},
            )
        except _TipRanksNotFoundError:
            # Not a per-symbol "unknown ticker" signal on this endpoint (that
            # semantic is specific to dataForTicker) -- an HTTP 404 here just
            # means this chunk returned nothing.
            log.info("tipranks GetQuotes returned HTTP 404 for a %d-symbol batch", len(ticker_params))
            return {}

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"tipranks GetQuotes returned non-JSON for a batch of {len(ticker_params)} "
                f"tickers: {exc}",
                provider=self.name,
            ) from exc

        quotes, errors = _parse_getquotes_envelope(payload, ticker_params)
        if errors:
            log.info(
                "tipranks GetQuotes reported errors for %d/%d ticker(s) in this batch",
                len(errors), len(ticker_params),
            )
        return quotes

    def get_current_session_bars(self, symbols: list[str]) -> dict[str, Bar]:
        """Today's (current-session) OHLCV bar per symbol, from GetQuotes --
        a REAL-TIME SNAPSHOT of the session currently in progress (or the
        most recently completed one when the market is closed), never a
        historical series. This is a capability distinct from
        ``get_daily_bars`` (close-only, last-resort, unaffected by any of
        this) and is not part of the ``MarketDataProvider`` protocol --
        callers that want it merge it in explicitly; see
        ``data.ingest.DataIngestor.ingest_prices``'s conservative
        append-only, dedupe-by-session-date merge for the documented rule.

        A symbol GetQuotes has no quote for, or whose quote is missing any
        of open/high/low/price/lastTradeDate, is simply omitted -- never
        synthesised (see ``_getquotes_session_bar``).
        """
        out: dict[str, Bar] = {}
        for symbol, quote in self.get_quotes(symbols).items():
            bar = _getquotes_session_bar(symbol, quote)
            if bar is not None:
                out[symbol] = bar
        return out

    # --- market caps ------------------------------------------------------------

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """Bulk market-cap lookup, in USD.

        GetQuotes (batched, whole-universe) is tried FIRST -- see the module
        docstring's "GetQuotes batching" section -- covering the large
        majority of a refresh in a handful of calls (a no-op, network-call-
        free, when ``TipRanksConfig.use_getquotes_batch`` is ``False``);
        ``dataForTicker`` per symbol (the pre-existing path, via
        ``_resolve_map``) then fills in anything GetQuotes did not cover --
        a symbol in its ``errors[]``, a chunk that failed outright, or the
        feature disabled entirely. Any GetQuotes failure (bad response
        shape, network error, anything) is caught inside ``get_quotes``
        itself and never prevents this fallback pass from running.
        """
        out: dict[str, float] = {}
        overall_total = len(symbols)

        quotes = self.get_quotes(symbols)
        via_getquotes = 0
        for symbol, quote in quotes.items():
            cap = _getquotes_market_cap_usd(quote)
            if cap is not None:
                out[symbol] = cap
                via_getquotes += 1

        remaining = [s for s in symbols if s not in out]
        # progress_offset/progress_total keep the on_symbol_progress hook's
        # `done` value climbing from where the GetQuotes sweep above left
        # off, instead of resetting to 0 for this fallback pass and making
        # the caller's progress surface jump backwards -- see
        # _resolve_map's own docstring.
        resolved = self._resolve_map(
            remaining,
            progress_offset=overall_total,
            progress_total=overall_total,
        )
        via_fallback = 0
        for symbol in remaining:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            if overview is None:
                continue
            cap = _preferred_market_cap(overview)
            if cap is not None:
                out[symbol] = cap
                via_fallback += 1

        if quotes or self._config.use_getquotes_batch:
            log.info(
                "market caps: %d via GetQuotes batches, %d via dataForTicker fallback, "
                "%d unresolved",
                via_getquotes, via_fallback, overall_total - len(out),
            )
        return out

    # --- reference data -----------------------------------------------------------

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        out: dict[str, SecurityInfo] = {}
        by_symbol = {s.symbol: s for s in load_packaged_universe()}
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            if overview is None:
                out[symbol] = by_symbol.get(symbol.upper(), SecurityInfo(symbol=symbol))
                continue
            company_data = overview.get("companyData") or {}
            out[symbol] = SecurityInfo(
                symbol=symbol,
                name=overview.get("companyName") or overview.get("companyFullName") or "",
                exchange=_tipranks_exchange_to_internal(overview.get("market", "")),
                sector=company_data.get("sector") or "",
                industry=company_data.get("industry") or "",
                market_cap_usd=_preferred_market_cap(overview),
            )
        return out

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list[Any]]:
        # No corporate-actions coverage in this adapter -- an honest empty
        # result, not a fabricated "no actions occurred" claim.
        return {s: [] for s in symbols}

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """No bulk reference-data/screener endpoint -- serves the packaged
        seed universes, same as stooq/yahoo."""
        securities = load_packaged_universe()
        if as_of is None:
            return securities
        return [s for s in securities if s.is_active_on(as_of)]

    # --- earnings (EarningsProvider protocol, structural) --------------------------

    def get_upcoming_earnings(
        self, symbols: list[str], *, through: dt.date | None = None
    ) -> dict[str, list[EarningsEvent]]:
        """Scheduled reports from ``portfolioHoldingData.nextEarningsReport``.

        TipRanks' widget exposes exactly one upcoming report per symbol (not
        a multi-quarter calendar) -- an honest, narrower capability than the
        synthetic generator's full quarterly series or a hand-maintained CSV.
        """
        if through is None:
            through = dt.datetime.now(tz=dt.UTC).date()
        out: dict[str, list[EarningsEvent]] = {}
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            events: list[EarningsEvent] = []
            if overview:
                holding = overview.get("portfolioHoldingData") or {}
                block = holding.get("nextEarningsReport")
                event = self._map_earnings_event(symbol, block) if block else None
                if event is not None and event.report_date > through:
                    events.append(event)
            out[symbol] = events
        return out

    def get_historical_earnings(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[EarningsEvent]]:
        """Past report from ``portfolioHoldingData.lastReportedEps``.

        Only the single most recently reported quarter is available from this
        endpoint -- not a multi-quarter history. A symbol whose last report
        falls outside ``[start, end]`` contributes nothing for this call.
        """
        out: dict[str, list[EarningsEvent]] = {}
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            events: list[EarningsEvent] = []
            if overview:
                holding = overview.get("portfolioHoldingData") or {}
                block = holding.get("lastReportedEps")
                event = self._map_earnings_event(symbol, block) if block else None
                if event is not None and start <= event.report_date <= end:
                    events.append(event)
            out[symbol] = events
        return out

    @staticmethod
    def _map_earnings_event(symbol: str, block: dict[str, Any]) -> EarningsEvent | None:
        """Map one ``nextEarningsReport``/``lastReportedEps`` block onto
        ``EarningsEvent``. Always keyed by ``symbol`` (the caller's own
        request), never by ``block["ticker"]`` -- see the module docstring's
        "earnings ticker mismatch" section; the TECK.B fixture confirms that
        inner field is the US cross-listing ticker, not the requested symbol.
        """
        date_str = block.get("date")
        if not date_str:
            return None
        try:
            report_date = dt.datetime.fromisoformat(date_str).date()
        except ValueError:
            return None

        confirmed = bool(block.get("isConfirmed"))
        time_of_day = block.get("timeOfDay")
        session = _TIME_OF_DAY_MAP.get(time_of_day, EarningsSession.UNKNOWN)
        if time_of_day is not None and time_of_day not in _TIME_OF_DAY_MAP:
            log.info(
                "tipranks: unrecognised timeOfDay=%r for %s; session left UNKNOWN",
                time_of_day, symbol,
            )

        surprise = _maybe_float(block.get("surprise"))
        as_of = (
            dt.datetime.combine(report_date, dt.time(20, 0), tzinfo=dt.UTC)
            if confirmed
            else utc_now()
        )
        return EarningsEvent(
            symbol=symbol,
            report_date=report_date,
            session=session,
            confirmed=confirmed,
            eps_estimate=_maybe_float(block.get("eps")),
            eps_actual=_maybe_float(block.get("reportedEPS")),
            revenue_estimate=_maybe_float(block.get("salesEstimate")),
            revenue_actual=_maybe_float(block.get("totalRevenue")),
            surprise_pct=(surprise * 100.0 if surprise is not None else None),
            source="tipranks",
            as_of=as_of,
        )

    # --- analyst sentiment (zero-new-calls harvest of the same overview) -----------

    def get_analyst_snapshots(
        self, symbols: list[str], *, as_of_session: dt.date | None = None
    ) -> dict[str, AnalystSnapshot]:
        """Analyst-consensus snapshots parsed from the SAME ``overview``
        responses ``get_market_caps``/``get_security_info``/the earnings
        methods above already fetch or serve from cache -- this issues no
        additional HTTP call of its own; it always goes through
        ``_resolve_map``, the one shared fetch/cache path every capability
        on this class uses. Calling this after (or before, or instead of)
        those other capabilities for the same ``symbols`` within one
        process/session costs nothing extra, because a symbol resolved once
        is cached for the rest of the configured TTL (see the module
        docstring).

        A symbol with no analyst-coverage layer at all (unknown ticker,
        ``prices_only`` state, or a real "found" overview with no
        consensus/expert data) is simply absent from the returned dict --
        see ``providers.market.tipranks_analyst.parse_analyst_snapshot``'s
        own "returns None" contract, which this method never overrides with
        a placeholder.

        Args:
            as_of_session: The session every produced snapshot is stamped
                with. Defaults to today (UTC date) when not given -- callers
                doing a same-session refresh should pass the actual trading
                session explicitly (see ``data.ingest.DataIngestor
                .ingest_analyst_snapshots``) so the stored row's ``session``
                matches the rest of that refresh's rows exactly, rather than
                drifting from a UTC-vs-trading-session mismatch near a
                session boundary.
        """
        out: dict[str, AnalystSnapshot] = {}
        if not symbols:
            return out
        session_date = as_of_session or dt.datetime.now(tz=dt.UTC).date()
        fetched_at = utc_now()
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            if overview is None:
                continue
            try:
                snapshot = parse_analyst_snapshot(overview, symbol, session_date, fetched_at)
            except Exception:
                # Parsing is defensive throughout (see tipranks_analyst's own
                # docstring); an exception here would be a genuine bug in
                # this adapter, not a vendor-shape surprise -- logged and
                # skipped rather than aborting the rest of the batch, same
                # per-symbol-degrades posture as every other capability on
                # this class.
                log.warning("analyst snapshot parsing failed for %s", symbol, exc_info=True)
                continue
            if snapshot is not None:
                out[symbol] = snapshot
        return out

    # --- institutional sentiment (zero-new-calls harvest of the same overview) ----

    def get_institutional_snapshots(
        self, symbols: list[str], *, as_of_session: dt.date | None = None
    ) -> dict[str, InstitutionalSnapshot]:
        """Insider/hedge-fund ("institutional") sentiment snapshots parsed
        from the SAME ``overview`` responses ``get_analyst_snapshots`` (and
        every other capability on this class) already fetch or serve from
        cache -- this issues no additional HTTP call of its own; it always
        goes through ``_resolve_map``, the one shared fetch/cache path every
        capability on this class uses.

        A symbol with no institutional content at all (unknown ticker,
        ``prices_only`` state, or a real "found" overview with no insider or
        hedge-fund data) is simply absent from the returned dict -- see
        ``providers.market.tipranks_institutional.parse_institutional_snapshot``'s
        own "returns None" contract, which this method never overrides with
        a placeholder.

        Args:
            as_of_session: The session every produced snapshot is stamped
                with. Defaults to today (UTC date) when not given -- callers
                doing a same-session refresh should pass the actual trading
                session explicitly (see ``data.ingest.DataIngestor
                .ingest_institutional_snapshots``) so the stored row's
                ``session`` matches the rest of that refresh's rows exactly,
                rather than drifting from a UTC-vs-trading-session mismatch
                near a session boundary.
        """
        out: dict[str, InstitutionalSnapshot] = {}
        if not symbols:
            return out
        session_date = as_of_session or dt.datetime.now(tz=dt.UTC).date()
        fetched_at = utc_now()
        resolved = self._resolve_map(symbols)
        for symbol in symbols:
            resolution = resolved.get(symbol)
            overview = resolution.overview if resolution else None
            if overview is None:
                continue
            try:
                snapshot = parse_institutional_snapshot(overview, symbol, session_date, fetched_at)
            except Exception:
                # Parsing is defensive throughout (see tipranks_institutional's
                # own docstring); an exception here would be a genuine bug in
                # this adapter, not a vendor-shape surprise -- logged and
                # skipped rather than aborting the rest of the batch, same
                # per-symbol-degrades posture as every other capability on
                # this class.
                log.warning("institutional snapshot parsing failed for %s", symbol, exc_info=True)
                continue
            if snapshot is not None:
                out[symbol] = snapshot
        return out
