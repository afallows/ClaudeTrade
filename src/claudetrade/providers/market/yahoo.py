"""Daily OHLCV and market-cap reference data from Yahoo Finance's undocumented
public JSON endpoints (``query1.finance.yahoo.com``).

**This is a fallback provider, not the primary one.** It exists for one thing
the free stooq endpoint cannot do at all: establish a real, current market
capitalisation per symbol (see ``get_market_caps``), which is what ADR-0008
Decision 3's runtime universe filter needs to enforce the owner's ">= $1B
market cap" rule. ``market_data.fallbacks`` is the intended place to add
``"yahoo"`` alongside (or instead of) ``"csv"`` once it is registered with a
name in ``providers.registry`` -- that registration is a deliberately separate
change (outside this module's remit) from having a complete, independently
testable adapter here.

Honesty about what this is:

* **Undocumented, unofficial API.** These are the same JSON endpoints Yahoo
  Finance's own web frontend calls, not a published, contracted API. There is
  no SLA, no rate-limit guarantee, and the shape can change without notice.
  This mirrors this project's posture elsewhere (see stooq's module
  docstring): a free source used conservatively, not depended upon for
  anything the application cannot degrade gracefully without.
* **No redistribution rights.** Personal/research use only, same posture as
  stooq's free tier.
* **Real data or nothing.** ``get_market_caps`` returns only symbols Yahoo's
  own quote response actually carried a ``marketCap`` figure for; a symbol
  it has nothing for is simply absent from the returned mapping, never
  filled in with an estimate, a stale value, or zero.
* **No bulk universe/reference-data endpoint** in this free tier either --
  ``list_universe`` serves the same packaged seed universes as stooq (see
  ``data.universe.load_packaged_universe``).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from claudetrade.config import MarketDataConfig
from claudetrade.data.universe import load_packaged_universe
from claudetrade.domain import Bar, SecurityInfo
from claudetrade.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimiter,
)

log = logging.getLogger(__name__)

#: Historical daily bars ("chart") endpoint. Single symbol per request; the
#: quote endpoint below is the one that supports a batch of symbols.
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
#: Batched quote endpoint -- this is where ``marketCap`` lives.
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
DEFAULT_RATE_LIMIT = 30  # Calls per minute -- conservative, undocumented endpoint.

#: Canadian (TSX/TSXV) listings carry a ``.TO`` suffix on Yahoo. Matched
#: case-insensitively against ``SecurityInfo.exchange`` / the ``exchange``
#: kwarg, same convention as ``StooqMarketProvider``.
CA_SUFFIX = ".TO"
CA_EXCHANGES = frozenset({"TSX", "TSXV"})

_EXCHANGE_MAP_CACHE: dict[str, str] | None = None


def _default_exchange_map() -> dict[str, str]:
    """Symbol -> exchange, derived from the packaged seed universes.

    Same rationale and caching behaviour as
    ``providers.market.stooq._default_exchange_map``: this is only consulted
    when the caller does not pass ``exchange`` explicitly.
    """
    global _EXCHANGE_MAP_CACHE
    if _EXCHANGE_MAP_CACHE is None:
        try:
            securities = load_packaged_universe()
        except Exception:
            log.warning("could not load packaged universe for yahoo's exchange map", exc_info=True)
            securities = []
        _EXCHANGE_MAP_CACHE = {s.symbol.upper(): s.exchange.upper() for s in securities if s.exchange}
    return _EXCHANGE_MAP_CACHE


class YahooMarketProvider(MarketDataProvider):
    """Fallback market-data adapter over Yahoo Finance's public JSON endpoints."""

    name = "yahoo"

    def __init__(self, config: MarketDataConfig | None = None) -> None:
        self._config = config or MarketDataConfig()
        self._rate_limiter = RateLimiter(
            self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            name="yahoo",
            max_wait_s=30.0,
        )
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Symbols Yahoo returned no chart data for on the most recent
        #: call(s) -- mirrors ``StooqMarketProvider._not_found`` so callers
        #: can introspect the per-symbol degrade without it failing the batch.
        self._not_found: set[str] = set()

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=True,  # Always available to attempt; actual call may fail.
            configured=True,
            message="undocumented query1.finance.yahoo.com endpoints; fallback provider",
            supports_point_in_time=False,
            supports_delisted=False,
            rate_limit_per_minute=self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            calls_made=self._calls,
            last_error=self._last_error,
            last_success=self._last_success,
            licence_note=(
                "Undocumented Yahoo Finance JSON endpoints (the same ones the Yahoo Finance "
                "web frontend calls) -- not a published/contracted API, no SLA, no redistribution "
                "rights. Personal/research use only; unsuitable for commercial use. Fallback "
                "provider, not primary: use for market-cap enrichment and as a secondary bar "
                "source when the primary is unavailable, not as the sole market-data source."
            ),
            capabilities={
                "daily_bars": True,
                "intraday": False,
                "market_caps": True,
                "corporate_actions": False,
            },
        )

    # --- bars ---------------------------------------------------------------

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        """Fetch daily bars. Unknown/errored symbols degrade to an empty list
        and the rest of the batch continues -- same contract as
        ``StooqMarketProvider.get_daily_bars``."""
        self._calls += 1
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            try:
                self._rate_limiter.acquire()
                bars = self._fetch_chart(symbol, start, end, adjusted=adjusted)
                out[symbol] = bars
                self._last_success = dt.datetime.now(tz=dt.UTC)
            except _YahooNoDataError as exc:
                log.info("yahoo has no chart data for %s: %s", symbol, exc)
                self._last_error = str(exc)
                self._not_found.add(symbol)
                out[symbol] = []
            except ProviderError:
                self._last_error = f"failed to fetch {symbol}"
                raise
            except Exception as exc:
                self._last_error = str(exc)
                raise ProviderError(
                    f"failed to fetch {symbol} from yahoo: {exc}",
                    provider=self.name,
                    retryable=True,
                ) from exc
        return out

    def _fetch_chart(
        self, symbol: str, start: dt.date, end: dt.date, *, adjusted: bool
    ) -> list[Bar]:
        params = {
            "period1": str(int(dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC).timestamp())),
            "period2": str(int(dt.datetime.combine(end, dt.time.max, tzinfo=dt.UTC).timestamp())),
            "interval": "1d",
            "events": "div,splits",
        }
        url = YAHOO_CHART_URL.format(symbol=self.yahoo_symbol(symbol))
        response = self._get(url, params, symbol)
        return self._parse_chart_response(response.json(), symbol, start, end, adjusted=adjusted)

    @staticmethod
    def _parse_chart_response(
        payload: dict[str, Any], symbol: str, start: dt.date, end: dt.date, *, adjusted: bool
    ) -> list[Bar]:
        chart = payload.get("chart", {})
        error = chart.get("error")
        if error:
            raise _YahooNoDataError(f"yahoo chart error for {symbol}: {error}")
        results = chart.get("result") or []
        if not results:
            raise _YahooNoDataError(f"yahoo returned no chart result for {symbol}")

        result = results[0]
        timestamps = result.get("timestamp") or []
        quote_list = (result.get("indicators", {}).get("quote") or [{}])
        quote = quote_list[0] if quote_list else {}
        adjclose_list = result.get("indicators", {}).get("adjclose") or []
        adjclose = (adjclose_list[0].get("adjclose") if adjclose_list else None) or []

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        bars: list[Bar] = []
        for i, ts in enumerate(timestamps):
            try:
                o, h, low_, c = opens[i], highs[i], lows[i], closes[i]
                if o is None or h is None or low_ is None or c is None:
                    # Yahoo pads holidays/halts with nulls rather than omitting
                    # the row; a null OHLC is not a usable bar.
                    continue
                session = dt.datetime.fromtimestamp(ts, tz=dt.UTC).date()
                if not (start <= session <= end):
                    continue
                adj = adjclose[i] if i < len(adjclose) and adjusted else None
                bars.append(
                    Bar(
                        symbol=symbol,
                        session=session,
                        open=round(float(o), 4),
                        high=round(float(h), 4),
                        low=round(float(low_), 4),
                        close=round(float(c), 4),
                        volume=round(float(volumes[i]), 1) if i < len(volumes) and volumes[i] is not None else 0.0,
                        adj_close=round(float(adj), 6) if adj is not None else None,
                        source="yahoo",
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                log.warning("skipping yahoo chart row %d for %s: %s", i, symbol, exc)
                continue

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
            "yahoo intraday bars are not implemented by this adapter",
            provider=self.name,
        )

    # --- market caps ----------------------------------------------------------

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """Bulk market-capitalisation lookup via the batched quote endpoint.

        Batches respect ``MarketDataConfig.max_symbols_per_request``. A
        symbol Yahoo's response has no positive ``marketCap`` figure for is
        simply absent from the returned mapping -- this is the one method in
        the codebase that must never invent a number, since it exists
        specifically to feed the ADR-0008 Decision 3 ">= $1B" universe floor.
        """
        out: dict[str, float] = {}
        batch_size = max(1, self._config.max_symbols_per_request)
        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i : i + batch_size]
            try:
                self._rate_limiter.acquire()
                response = self._get(
                    YAHOO_QUOTE_URL,
                    {"symbols": ",".join(self.yahoo_symbol(s) for s in chunk)},
                    ",".join(chunk),
                )
                self._last_success = dt.datetime.now(tz=dt.UTC)
            except ProviderError as exc:
                # A batch failing to fetch degrades those symbols to
                # "unresolved", not a hard failure of the whole call -- the
                # data-quality layer is where "unresolved" gets surfaced.
                log.warning("yahoo market-cap batch failed for %s: %s", chunk, exc)
                self._last_error = str(exc)
                continue

            payload = response.json()
            quote_response = payload.get("quoteResponse", {})
            if quote_response.get("error"):
                log.warning("yahoo quote error for %s: %s", chunk, quote_response["error"])
                continue

            by_yahoo_symbol = {self.yahoo_symbol(s): s for s in chunk}
            for row in quote_response.get("result") or []:
                cap = row.get("marketCap")
                y_symbol = row.get("symbol", "")
                original = by_yahoo_symbol.get(y_symbol, y_symbol)
                if isinstance(cap, (int, float)) and cap > 0:
                    out[original] = float(cap)
        return out

    # --- reference data -------------------------------------------------------

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        """Reference data from the same batched quote endpoint used for caps.

        Falls back to the packaged seed universe for a symbol the quote
        endpoint returned nothing usable for -- same degrade pattern as
        ``StooqMarketProvider.get_security_info``.
        """
        out: dict[str, SecurityInfo] = {}
        by_symbol = {s.symbol: s for s in load_packaged_universe()}
        batch_size = max(1, self._config.max_symbols_per_request)
        for i in range(0, len(symbols), batch_size):
            chunk = symbols[i : i + batch_size]
            try:
                self._rate_limiter.acquire()
                response = self._get(
                    YAHOO_QUOTE_URL,
                    {"symbols": ",".join(self.yahoo_symbol(s) for s in chunk)},
                    ",".join(chunk),
                )
            except ProviderError:
                continue
            payload = response.json()
            by_yahoo_symbol = {self.yahoo_symbol(s): s for s in chunk}
            for row in payload.get("quoteResponse", {}).get("result") or []:
                original = by_yahoo_symbol.get(row.get("symbol", ""), row.get("symbol", ""))
                cap = row.get("marketCap")
                out[original] = SecurityInfo(
                    symbol=original,
                    name=row.get("longName") or row.get("shortName") or "",
                    exchange=_yahoo_exchange_to_internal(row.get("fullExchangeName", "")),
                    sector=row.get("sector", ""),
                    market_cap_usd=float(cap) if isinstance(cap, (int, float)) and cap > 0 else None,
                )
        for symbol in symbols:
            if symbol not in out:
                out[symbol] = by_symbol.get(symbol.upper(), SecurityInfo(symbol=symbol))
        return out

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list[Any]]:
        # Not implemented by this adapter -- an honest empty result, not a
        # fabricated "no actions occurred" claim; corporate actions for a
        # symbol needing them should come from a provider that supports it.
        return {s: [] for s in symbols}

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """No bulk reference-data endpoint in this free tier either -- serves
        the packaged seed universes, same as ``StooqMarketProvider.list_universe``."""
        securities = load_packaged_universe()
        if as_of is None:
            return securities
        return [s for s in securities if s.is_active_on(as_of)]

    # --- shared plumbing --------------------------------------------------------

    def _get(self, url: str, params: dict[str, str], symbol_label: str):
        try:
            with httpx.Client(
                timeout=self._config.request_timeout_s,
                verify=True,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; claudetrade research use)"},
            ) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"network error connecting to yahoo for {symbol_label}: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"timeout fetching {symbol_label} from yahoo: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ProviderError(
                    f"rate limited by yahoo for {symbol_label}",
                    provider=self.name,
                    retryable=True,
                ) from exc
            raise ProviderError(
                f"yahoo returned {exc.response.status_code} for {symbol_label}",
                provider=self.name,
                retryable=True,
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"error fetching {symbol_label} from yahoo: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

    @staticmethod
    def yahoo_symbol(symbol: str, exchange: str | None = None) -> str:
        """Map an exchange ticker to Yahoo's namespaced form.

        Yahoo expects a bare ticker for US listings (``AAPL``) and a ``.TO``
        suffix for Canadian (TSX/TSXV) ones (``SHOP`` -> ``SHOP.TO``). Share
        classes already use this codebase's hyphen convention (``BRK-B``),
        which happens to be Yahoo's own convention too, so no remapping is
        needed there. A symbol that already carries a market suffix (a dot
        anywhere in it) is passed through untouched.

        Same exchange-resolution fallback as ``StooqMarketProvider.stooq_symbol``:
        pass ``exchange`` explicitly when known, otherwise fall back to the
        packaged seed universes' symbol -> exchange map, defaulting to a bare
        (US) symbol for anything not found there.
        """
        stripped = symbol.strip().upper()
        if "." in stripped:
            return stripped
        exch = (exchange or _default_exchange_map().get(stripped, "")).upper()
        if exch in CA_EXCHANGES:
            return f"{stripped}{CA_SUFFIX}"
        return stripped


class _YahooNoDataError(ProviderError):
    """Yahoo has no chart data for this specific symbol (unknown ticker,
    delisted, or an empty result). Degrades that one symbol to an empty bar
    list rather than aborting the whole batch -- mirrors
    ``providers.market.stooq.SymbolNotFoundError``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="yahoo", retryable=False)


def _yahoo_exchange_to_internal(full_exchange_name: str) -> str:
    """Map Yahoo's ``fullExchangeName`` strings to this codebase's exchange
    codes (``NASDAQ``/``NYSE``/``AMEX``/``TSX``). Anything unrecognised is
    passed through as-is rather than dropped, so an unmapped exchange name is
    still visible for a human to correct rather than silently blanked."""
    name = (full_exchange_name or "").strip()
    lowered = name.lower()
    if "nasdaq" in lowered or lowered in {"nms", "ngm", "ncm"}:
        return "NASDAQ"
    if "nyse american" in lowered or lowered == "asemex":
        return "AMEX"
    if lowered == "nyq" or "new york stock exchange" in lowered or lowered == "nyse":
        return "NYSE"
    if "toronto" in lowered or lowered == "tor":
        return "TSX"
    return name
