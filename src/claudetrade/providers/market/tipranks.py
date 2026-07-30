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

**GetQuotes batching (optional, off by default)**: TipRanks' CIBC integration
also exposes
``https://marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=TSE:A,TSE:B,...``
-- a batch quote lookup that can reduce Canadian cap enrichment to a handful
of calls instead of one per symbol. A real probe from the owner's machine
(a batch of 7 mixed US/TSX tickers) confirmed the endpoint works and that
requested tickers are echoed back exactly as sent (``TSE:`` notation
included), but this repository still has no committed fixture of the raw
response body, so ``_parse_getquotes_response`` stays a defensive,
best-effort parser (every field access wrapped, any unexpected shape returns
an empty result rather than raising) and the feature stays gated behind
``TipRanksConfig.use_getquotes_batch`` (default ``False``).

**Confirmed currency trap (do not "fix" this without re-checking the real
probe first)**: GetQuotes' own ``marketCap`` field is in the listing's
*local* currency, not USD -- the probe showed ``TSE:TECK.B`` reporting
``marketCap`` 40,620,412,377 (CAD) alongside an ``exchangeRate`` of
~0.712, while ``dataForTicker`` gives ``marketCapUSD`` 28,913,081,465 (USD),
and ``40,620,412,377 * 0.712 ~= 28.9B`` -- consistent. A US entry's
``exchangeRate`` is 1. Consequently ``_parse_getquotes_response`` never
returns a raw, un-converted ``marketCap`` for a non-USD listing: it uses
``marketCapUSD`` when present, else ``marketCap * exchangeRate`` when both
are present and positive, and contributes nothing for a row missing both.
Canadian market-cap coverage never depends on this succeeding regardless:
``dataForTicker`` (with the ``TSE:SYMBOL`` notation) is the primary path for
every symbol, US and Canadian alike; GetQuotes is purely a call-count
optimisation layered on top, and any failure in it -- a bad response shape,
a network error, anything -- is caught and logged, falling straight back to
the per-symbol ``dataForTicker`` path with no user-visible effect beyond one
extra batch call having been wasted.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from claudetrade.config import TipRanksConfig
from claudetrade.data.universe import load_packaged_universe
from claudetrade.domain import (
    Bar,
    DataQualityIssue,
    DataQualitySeverity,
    EarningsEvent,
    EarningsSession,
    SecurityInfo,
)
from claudetrade.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.utils.timeutils import trading_days_between, utc_now

log = logging.getLogger(__name__)

WIDGET_BASE_URL = "https://widgets.tipranks.com/api/etoro/dataForTicker"
#: Price-only fallback which remains available for some instruments (notably
#: closed-end funds) that the richer ``dataForTicker`` endpoint does not know.
HISTORICAL_PRICES_URL = "https://widgets.tipranks.com/api/etoro/historicalprices"
#: Optional batching path -- see the module docstring's GetQuotes section.
#: UNVERIFIED response shape; off by default.
GETQUOTES_BASE_URL = "https://marketsv3.tipranks.com/api/quotes/GetQuotes"

DEFAULT_RATE_LIMIT = 30  # Calls per minute -- conservative, unauthenticated endpoint.
_USER_AGENT = "Mozilla/5.0 (compatible; claudetrade research use)"

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

    US listings are passed through bare (``INTC``). Canadian (TSX/TSXV)
    listings get a ``TSE:`` prefix with this codebase's hyphenated
    share-class convention rewritten to TipRanks' dotted one
    (``TECK-B`` -> ``TSE:TECK.B``) -- confirmed against the committed TECK.B
    fixture, whose ``overview.ticker`` echoes exactly this form back.

    Exchange resolution: pass ``exchange`` explicitly when known, otherwise
    fall back to the packaged seed universes' symbol -> exchange column,
    same convention as stooq/yahoo's own symbol-mapping helpers.
    """
    stripped = symbol.strip().upper()
    exch = (exchange or _default_exchange_map().get(stripped, "")).upper()
    if exch in CA_EXCHANGES:
        return f"TSE:{stripped.replace('-', '.')}"
    return stripped


def _is_ca_symbol(symbol: str, exchange: str | None = None) -> bool:
    exch = (exchange or _default_exchange_map().get(symbol.strip().upper(), "")).upper()
    return exch in CA_EXCHANGES


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


def _parse_getquotes_response(payload: Any, requested_tickers: list[str]) -> dict[str, float]:
    """Defensive parser for the optional GetQuotes batch endpoint.

    The endpoint's reachability and rough field set (ticker, marketCap,
    exchangeRate, ...) were confirmed against a real probe (see the module
    docstring), but this repository has no committed fixture of the exact
    response body, so every field access here stays defensive; any shape
    that does not match the confirmed/guessed structure yields an empty
    result rather than raising, so a wrong guess degrades to "this
    optimisation contributed nothing" rather than breaking the caller.

    CONFIRMED currency trap: ``marketCap`` is in the listing's own local
    currency, not USD -- ``marketCapUSD`` is preferred when present;
    otherwise ``marketCap * exchangeRate`` is used (both must be present and
    positive). A row with neither usable combination contributes nothing --
    a raw, un-converted non-USD ``marketCap`` is never returned.
    """
    out: dict[str, float] = {}
    if not isinstance(payload, dict):
        return out
    rows = payload.get("quotes") or payload.get("result") or payload.get("data")
    if not isinstance(rows, list):
        return out
    requested = set(requested_tickers)
    for row in rows:
        try:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker") or row.get("symbol")
            if ticker not in requested:
                continue

            cap_usd = row.get("marketCapUSD")
            if isinstance(cap_usd, (int, float)) and cap_usd > 0:
                out[ticker] = float(cap_usd)
                continue

            cap_local = row.get("marketCap")
            rate = row.get("exchangeRate")
            if (
                isinstance(cap_local, (int, float))
                and cap_local > 0
                and isinstance(rate, (int, float))
                and rate > 0
            ):
                out[ticker] = float(cap_local) * float(rate)
        except Exception:
            continue
    return out


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

    def __init__(self, config: TipRanksConfig | None = None, *, cache_dir: Path | str | None = None) -> None:
        self._config = config or TipRanksConfig()
        self._cache_dir = (Path(cache_dir) / "tipranks") if cache_dir else None
        self._rate_limiter = RateLimiter(
            self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            name="tipranks",
            max_wait_s=30.0,
        )
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Symbols TipRanks returned an empty/null overview for on the most
        #: recent call(s) -- mirrors ``StooqMarketProvider._not_found``.
        self._not_found: set[str] = set()
        #: Accumulated close-only-bars data-quality warnings, drained by
        #: ``drain_quality_warnings()``. Always logged too, so a caller that
        #: never drains this still sees the degrade in the logs.
        self._quality_warnings: list[DataQualityIssue] = []

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

    def _load_cached_overview(self, cache_key: str) -> dict[str, Any] | None:
        """Return a cached ``overview`` (possibly ``{}`` for a cached "unknown
        ticker" result) if still within the TTL, else ``None`` (cache miss)."""
        path = self._cache_path(cache_key)
        if path is None or not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            fetched_date = dt.date.fromisoformat(record["fetched_date"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        today = dt.datetime.now(tz=dt.UTC).date()
        ttl = max(1, self._config.cache_ttl_trading_days)
        if trading_days_between(fetched_date, today) >= ttl:
            return None
        overview = record.get("overview")
        return overview if isinstance(overview, dict) else None

    def _store_cached_overview(self, cache_key: str, overview: dict[str, Any]) -> None:
        path = self._cache_path(cache_key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "fetched_date": dt.datetime.now(tz=dt.UTC).date().isoformat(),
                        "overview": overview,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            log.debug("failed to write tipranks cache for %s", cache_key, exc_info=True)

    def _fetch_overview(self, symbol: str, exchange: str | None = None) -> dict[str, Any] | None:
        """Return the raw ``overview`` object for ``symbol``, via cache or a
        live fetch. Returns ``None`` for an unknown ticker -- an HTTP 404
        (confirmed by a real probe: a garbage ticker gets a clean 404) or an
        empty/null ``overview`` on an otherwise-2xx response -- that degrades
        this one symbol, not the whole call.

        Raises:
            SourceBlockedError: non-JSON body, missing ``overview`` key,
                401/403, or any other unexpected/non-2xx response.
            RateLimitError: HTTP 429, or the local limiter's own budget.
            ProviderError: HTTP 5xx (an outage, retryable) or a network/
                timeout failure.
        """
        ticker_param = tipranks_ticker(symbol, exchange)
        cache_key = _safe_cache_key(ticker_param)

        cached = self._load_cached_overview(cache_key)
        if cached is not None:
            return cached or None

        self._rate_limiter.acquire()
        self._calls += 1
        try:
            response = self._get(ticker_param)
        except _TipRanksNotFoundError:
            # HTTP 404 -- confirmed "unknown ticker", not an outage. Same
            # degrade-and-cache treatment as an empty/null overview below.
            log.info("tipranks has no data for %s (%s) -- HTTP 404, unknown ticker", symbol, ticker_param)
            self._last_success = utc_now()
            self._not_found.add(symbol)
            self._store_cached_overview(cache_key, {})
            return None

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

        self._last_success = utc_now()
        overview = payload.get("overview")
        if not overview:
            log.info("tipranks has no overview for %s (%s) -- unknown ticker", symbol, ticker_param)
            self._not_found.add(symbol)
            self._store_cached_overview(cache_key, {})
            return None

        self._store_cached_overview(cache_key, overview)
        return overview

    def _get(self, ticker_param: str) -> httpx.Response:
        return self._get_url(WIDGET_BASE_URL, ticker_param)

    def _get_url(self, url: str, ticker_param: str) -> httpx.Response:
        """Fetch one widget endpoint with the adapter's common safeguards."""
        try:
            with httpx.Client(
                timeout=self._config.request_timeout_s,
                verify=True,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = client.get(url, params={"ticker": ticker_param})
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

    def _fetch_historical_bars(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> list[Bar]:
        """Try TipRanks' OHLCV history when its richer overview is absent.

        TipRanks serves this endpoint for some valid funds for which
        ``dataForTicker`` returns 404.  A miss remains local to the symbol;
        malformed/block responses retain the common fail-closed behaviour.
        """
        ticker_param = tipranks_ticker(symbol)
        self._rate_limiter.acquire()
        self._calls += 1
        try:
            response = self._get_url(HISTORICAL_PRICES_URL, ticker_param)
        except _TipRanksNotFoundError:
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"tipranks returned non-JSON history for {symbol}: {exc}",
                provider=self.name,
            ) from exc
        if not isinstance(payload, list):
            raise SourceBlockedError(
                f"tipranks history for {symbol} had an unexpected shape",
                provider=self.name,
            )

        bars: list[Bar] = []
        for row in payload:
            try:
                session = dt.datetime.fromisoformat(row["date"]).date()
                if not start <= session <= end:
                    continue
                bars.append(
                    Bar(
                        symbol=symbol,
                        session=session,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0),
                        adj_close=_maybe_float(row.get("price")),
                        source="tipranks",
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort(key=lambda bar: bar.session)
        if bars:
            self._last_success = utc_now()
        return bars

    # --- bars (last resort only) ----------------------------------------------

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,  # noqa: ARG002 - close-only; there is nothing to adjust.
    ) -> dict[str, list[Bar]]:
        """Close-only bars from ``overview.prices``. LAST RESORT ONLY --
        see the module docstring and ``bars_last_resort``."""
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            overview = self._fetch_overview(symbol)
            if overview is None:
                out[symbol] = self._fetch_historical_bars(symbol, start, end)
                continue
            bars = self._parse_close_only_bars(symbol, overview, start, end)
            out[symbol] = bars
            if bars:
                message = f"{symbol}: close-only bars from tipranks; volume/ATR/gap features degraded"
                log.warning(message)
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

    # --- market caps ------------------------------------------------------------

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """Bulk market-cap lookup. First tries the optional GetQuotes batch
        for Canadian symbols (if enabled), then ``dataForTicker`` per symbol
        for anything left unresolved -- see the module docstring."""
        out: dict[str, float] = {}

        if self._config.use_getquotes_batch:
            ca_symbols = [s for s in symbols if _is_ca_symbol(s)]
            if len(ca_symbols) > 1:
                try:
                    out.update(self._get_market_caps_via_getquotes(ca_symbols))
                except Exception:
                    log.debug(
                        "tipranks GetQuotes batch optimisation failed; falling back to "
                        "per-symbol dataForTicker",
                        exc_info=True,
                    )

        for symbol in symbols:
            if symbol in out:
                continue
            overview = self._fetch_overview(symbol)
            if overview is None:
                continue
            cap = _preferred_market_cap(overview)
            if cap is not None:
                out[symbol] = cap
        return out

    def _get_market_caps_via_getquotes(self, ca_symbols: list[str]) -> dict[str, float]:
        """OPTIONAL, UNVERIFIED batching path -- see the module docstring."""
        out: dict[str, float] = {}
        batch_size = max(1, self._config.getquotes_batch_size)
        for i in range(0, len(ca_symbols), batch_size):
            chunk = ca_symbols[i : i + batch_size]
            ticker_by_param = {tipranks_ticker(s): s for s in chunk}
            self._rate_limiter.acquire()
            try:
                with httpx.Client(
                    timeout=self._config.request_timeout_s,
                    verify=True,
                    follow_redirects=True,
                    headers={"User-Agent": _USER_AGENT},
                ) as client:
                    response = client.get(
                        GETQUOTES_BASE_URL, params={"tickers": ",".join(ticker_by_param)}
                    )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                log.debug("tipranks GetQuotes batch request failed", exc_info=True)
                continue
            parsed = _parse_getquotes_response(payload, list(ticker_by_param))
            for ticker_param, cap in parsed.items():
                original = ticker_by_param.get(ticker_param)
                if original:
                    out[original] = cap
        return out

    # --- reference data -----------------------------------------------------------

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        out: dict[str, SecurityInfo] = {}
        by_symbol = {s.symbol: s for s in load_packaged_universe()}
        for symbol in symbols:
            overview = self._fetch_overview(symbol)
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
        for symbol in symbols:
            overview = self._fetch_overview(symbol)
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
        for symbol in symbols:
            overview = self._fetch_overview(symbol)
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
