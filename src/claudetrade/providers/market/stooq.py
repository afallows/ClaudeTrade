"""Free daily OHLCV from stooq.com via published CSV endpoint.

Real data source with proper rate limiting and error handling. Does NOT scrape
HTML or bypass access controls, and does not solve any anti-bot challenge it
encounters (see below). If the source is unavailable, the provider cleanly
reports itself unavailable rather than working around the limitation.

**NOT a default fallback any more.** Two separate real-world findings drove
that, both from the owner's own machine:

1. **Diagnosis of the "stooq returned 404 for AAPL/META/PG" refresh
   failure**: the request URL/params were already correct -- ``stooq_symbol()``
   lower-cases the ticker and adds the exchange-appropriate ``.us``/``.to``
   suffix, and that mapping was (and always had been) applied on the real
   ``get_daily_bars`` fetch path (``_fetch_symbol`` passes
   ``self.stooq_symbol(symbol)`` straight into the request params; exercised
   end-to-end by ``tests/test_providers.py::
   test_stooq_requests_the_history_endpoint_with_a_bounded_range`` and the
   URL-assertion tests added alongside this fix). What was actually missing
   was a ``User-Agent`` header: the client sent none at all, so httpx's own
   generic default went out on the wire, and stooq's edge answered that
   default with a 404 rather than the CSV. A browser-like ``User-Agent``
   (``_USER_AGENT``) fixes that specific symptom.
2. **A subsequent live probe (both a US and a TSX symbol, correctly
   ``.us``/``.to``-suffixed, with the ``User-Agent`` fix from (1) already in
   place) found stooq now answers with HTTP 200 and an HTML, JavaScript
   SHA-256 proof-of-work challenge page (posts to ``/__verify``, then
   reloads) instead of the CSV body -- an anti-bot wall, not a request
   defect.** Per ADR-0008 Decision 1 this application never solves a
   challenge, so this is fail-closed: ``_looks_like_challenge_page`` detects
   the HTML response (note it is HTTP 200 -- status code alone cannot
   distinguish it from a real CSV response) and raises
   ``SourceBlockedError`` before the body ever reaches the CSV parser, rather
   than parsing garbage or silently returning nothing. Since this makes the
   endpoint unusable without manual intervention on an unknown cadence,
   ``stooq`` has been removed from the default ``market_data.fallbacks``
   (see ``config.MarketDataConfig``) -- it remains fully registered and
   usable as an explicit opt-in for an operator whose network path to
   stooq.com is not challenged.

Limitations:

- Free endpoint only: no redistribution rights.
- Daily data only (no intraday).
- No delisted security coverage.
- Rate limited to reasonable frequency.
- Requires active network access (will fail offline).
- May be behind an anti-bot JavaScript challenge depending on the requesting
  network's reputation with stooq's edge (see finding 2 above) -- this
  adapter detects and fails closed rather than working around it; there is
  no code-level fix for this the application can apply.
- No bulk reference-data/universe endpoint in the free tier: ``list_universe``
  serves the packaged seed universes (see ``data.universe.load_packaged_universe``)
  rather than a live listing from stooq itself.
- Canadian (TSX/TSXV) coverage is real but partial and unverified from this
  sandbox: stooq mirrors TSX-listed names under a ``.to`` suffix for many, but
  not all, tickers. Run ``claudetrade probe`` and a small ``claudetrade refresh``
  to confirm coverage for the specific symbols you care about before relying on
  it -- see docs/api-providers.md.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from claudetrade.config import MarketDataConfig
from claudetrade.data.universe import load_packaged_universe, load_stooq_universe
from claudetrade.domain import Bar, SecurityInfo
from claudetrade.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimiter,
    SourceBlockedError,
)

log = logging.getLogger(__name__)

#: Historical daily CSV endpoint. NOT ``/q/l/`` -- that is the *last quote*
#: endpoint and returns a single row, which would silently give the whole
#: application one bar of history per symbol instead of a price series.
STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/"
DEFAULT_RATE_LIMIT = 30  # Calls per minute

#: Stooq's edge answers a request with no (or a generic/default) User-Agent
#: with a 404 rather than serving the CSV -- see the ``headers=`` comment in
#: ``_fetch_symbol`` for the diagnosis behind this.
_USER_AGENT = "Mozilla/5.0 (compatible; claudetrade research use)"

#: Stooq namespaces its symbols by market. US listings carry a ``.us`` suffix,
#: so a bare ``AAPL`` resolves to nothing.
US_SUFFIX = ".us"
#: Canadian (TSX/TSXV) listings carry a ``.to`` suffix on stooq.
CA_SUFFIX = ".to"
#: Exchanges routed to the Canadian suffix. Matched case-insensitively against
#: ``SecurityInfo.exchange`` / the ``exchange`` kwarg passed to ``stooq_symbol``.
CA_EXCHANGES = frozenset({"TSX", "TSXV"})

#: Stooq answers a request it cannot serve with this literal body rather than an
#: HTTP error, so it has to be detected in the payload.
_NO_DATA_MARKERS = ("N/D", "No data", "Exceeded the daily hits limit")

#: Markers of stooq's HTML/JavaScript anti-bot challenge page (HTTP 200, so
#: the status code alone cannot distinguish it from a real CSV response).
_CHALLENGE_MARKERS = ("<!doctype", "<html")


def _looks_like_challenge_page(response: object) -> bool:
    """Whether ``response`` looks like stooq's browser-challenge page rather
    than a CSV body -- checked by content-type first (cheap), then by the
    first few bytes of the body (covers a challenge page served without an
    explicit ``text/html`` content-type)."""
    content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
    if "html" in content_type:
        return True
    text_start = response.text.lstrip()[:32].lower()  # type: ignore[attr-defined]
    return text_start.startswith(_CHALLENGE_MARKERS)


class SymbolNotFoundError(ProviderError):
    """Stooq has no data for this specific symbol (unknown ticker or exhausted
    quota, both signalled by the same HTTP-200 plain-text body).

    Deliberately narrower than ``ProviderError``: raised only for the one
    symbol affected, so ``get_daily_bars`` can skip it and keep fetching the
    rest of the batch rather than aborting every symbol in the request over
    one bad ticker.
    """


class _StooqNoDataError(ValueError):
    """Internal: the parsed response says stooq has nothing for this symbol.

    Distinct from a genuinely malformed/unexpected response shape (wrong
    header, corrupt rows), which stays a hard failure -- that indicates a
    parser/endpoint mismatch bug, not an ordinary "unknown ticker" outcome.
    """


_EXCHANGE_MAP_CACHE: dict[str, str] | None = None


def _default_exchange_map() -> dict[str, str]:
    """Symbol -> exchange, derived from the packaged seed universes.

    Used to decide the stooq suffix (``.us`` vs ``.to``) for a bare symbol when
    the caller does not pass ``exchange`` explicitly -- which is the normal
    path, since ``MarketDataProvider.get_daily_bars`` takes plain ticker
    strings with no exchange context. Built once per process and cached; a
    failure to load the packaged files degrades to an empty map (every symbol
    then falls back to the ``.us`` default), not an exception.
    """
    global _EXCHANGE_MAP_CACHE
    if _EXCHANGE_MAP_CACHE is None:
        try:
            securities = load_packaged_universe()
        except Exception:
            log.warning("could not load packaged universe for stooq's exchange map", exc_info=True)
            securities = []
        _EXCHANGE_MAP_CACHE = {s.symbol.upper(): s.exchange.upper() for s in securities if s.exchange}
    return _EXCHANGE_MAP_CACHE


class StooqMarketProvider(MarketDataProvider):
    """Free daily OHLCV from stooq.com published endpoint."""

    name = "stooq"

    def __init__(self, config: MarketDataConfig | None = None) -> None:
        """Initialize Stooq provider.

        Args:
            config: MarketDataConfig with rate_limit_per_minute.
        """
        self._config = config or MarketDataConfig()
        self._rate_limiter = RateLimiter(
            self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            name="stooq",
            max_wait_s=30.0,
        )
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Symbols that came back "no data" (unknown ticker / quota) on the
        #: most recent call(s), for callers/tests that want to introspect the
        #: per-symbol degrade without it having failed the whole batch.
        self._not_found: set[str] = set()

    def status(self) -> ProviderStatus:
        """Health and capability report.

        Stooq has significant limitations and is marked as unsuitable for
        commercial use.
        """
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=True,  # Always available to attempt; actual call may fail.
            configured=True,
            message=(
                "free stooq.com daily-history endpoint (opt-in only; not a default "
                "fallback -- may be behind an anti-bot browser challenge); symbol coverage "
                "comes from the packaged seed universe (not a live Stooq listing)"
                + (f"; last error: {self._last_error}" if self._last_error else "")
            ),
            supports_point_in_time=False,
            supports_delisted=False,
            rate_limit_per_minute=self._config.rate_limit_per_minute or
                                   DEFAULT_RATE_LIMIT,
            calls_made=self._calls,
            last_error=self._last_error,
            last_success=self._last_success,
            licence_note=(
                "Free endpoint from stooq.com. No redistribution rights. "
                "Does not cover delisted securities. Unsuitable for commercial use. "
                "Rate limit enforced; requests outside terms are rejected."
            ),
            capabilities={
                "daily_bars": True,
                "intraday": False,
                "delisted": False,
                "bulk_universe": False,
            },
        )

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,  # noqa: ARG002
    ) -> dict[str, list[Bar]]:
        """Fetch daily bars. May raise ProviderError on network failure.

        An unknown symbol (or a per-symbol quota message) degrades that one
        symbol to an empty bar list and continues with the rest of the batch
        rather than aborting -- ``SymbolNotFoundError`` is the one exception
        this loop treats as "skip and carry on"; every other ``ProviderError``
        (network failure, rate limit, malformed response) still aborts the
        whole call, since those affect every symbol in the batch identically
        and retrying the same request piecemeal would not help.
        """
        self._calls += 1
        out: dict[str, list[Bar]] = {}

        for symbol in symbols:
            try:
                self._rate_limiter.acquire()
                bars = self._fetch_symbol(symbol, start, end)
                out[symbol] = bars
                self._last_success = dt.datetime.now(tz=dt.UTC)
            except SymbolNotFoundError as exc:
                log.info("stooq has no data for %s: %s", symbol, exc)
                self._last_error = str(exc)
                self._not_found.add(symbol)
                out[symbol] = []
                continue
            except SourceBlockedError:
                # Preserve the specific "blocked (browser challenge)" marker
                # _fetch_symbol already set on self._last_error -- the
                # generic ProviderError branch below would otherwise
                # overwrite it with a less informative message.
                raise
            except ProviderError:
                self._last_error = f"failed to fetch {symbol}"
                raise
            except Exception as exc:
                self._last_error = str(exc)
                raise ProviderError(
                    f"failed to fetch {symbol} from stooq: {exc}",
                    provider=self.name,
                    retryable=True,
                ) from exc

        return out

    def _fetch_symbol(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> list[Bar]:
        """Fetch bars for one symbol from stooq CSV endpoint.

        Raises:
            SymbolNotFoundError: stooq has no data for this specific symbol.
            ProviderError: on network failure or a malformed response.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ProviderError(
                "httpx not installed; cannot fetch from stooq",
                provider=self.name,
                retryable=False,
            ) from exc

        # d1/d2 bound the range server-side so a five-year request does not pull
        # the full history for every symbol.
        params = {
            "s": self.stooq_symbol(symbol),
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",  # daily interval
        }

        try:
            with httpx.Client(
                timeout=self._config.request_timeout_s,
                verify=True,  # Always verify SSL
                follow_redirects=True,
                # Root cause of the 404s a real Windows refresh hit for
                # AAPL/META/PG/etc (see module docstring): this client sent no
                # ``User-Agent`` at all, which meant httpx's own generic
                # default (``python-httpx/<version>``) went out on the wire.
                # Stooq's edge answers requests carrying that default with a
                # 404 rather than a normal response -- the symbol/suffix
                # mapping below was, and always has been, correct (``s`` is
                # already lower-cased and suffixed by ``stooq_symbol`` right
                # here); a browser-like ``User-Agent`` is what was actually
                # missing from the request.
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = client.get(STOOQ_HISTORY_URL, params=params)
                response.raise_for_status()
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"network error connecting to stooq for {symbol}: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"timeout fetching {symbol} from stooq: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ProviderError(
                    f"rate limited by stooq for {symbol}",
                    provider=self.name,
                    retryable=True,
                ) from exc
            raise ProviderError(
                f"stooq returned {exc.response.status_code} for {symbol}",
                provider=self.name,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise ProviderError(
                f"error fetching {symbol} from stooq: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

        # Anti-bot / browser-challenge detection (HTTP 200, HTML body) -- must
        # be checked BEFORE CSV parsing, since the challenge page is served
        # with a normal 200 status, not an error status. Confirmed against a
        # real probe from the owner's machine: stooq now answers both a US
        # and a TSX symbol request with a SHA-256 proof-of-work JavaScript
        # challenge page (POSTs to "/__verify", then reloads) instead of the
        # CSV history body. Per ADR-0008 Decision 1 this fails closed -- no
        # PoW is ever solved, no retry loop -- rather than being silently fed
        # into the CSV parser.
        if _looks_like_challenge_page(response):
            self._last_error = "blocked (browser challenge)"
            raise SourceBlockedError(
                f"stooq served a browser-challenge/anti-bot page for {symbol} (HTTP 200, "
                "HTML body, not CSV) -- fail-closed per ADR-0008 Decision 1: no "
                "proof-of-work solving, no retry loop, no fingerprint/proxy rotation.",
                provider=self.name,
            )

        # Parse CSV response.
        try:
            bars = self._parse_csv_response(response.text, symbol, start, end)
            return bars
        except _StooqNoDataError as exc:
            raise SymbolNotFoundError(str(exc), provider=self.name, retryable=False) from exc
        except Exception as exc:
            raise ProviderError(
                f"malformed stooq response for {symbol}: {exc}",
                provider=self.name,
                retryable=False,
            ) from exc

    @staticmethod
    def stooq_symbol(symbol: str, exchange: str | None = None) -> str:
        """Map an exchange ticker to stooq's namespaced form.

        Stooq expects ``aapl.us`` for US listings and ``shop.to`` for Canadian
        (TSX/TSXV) ones; a bare ``AAPL`` returns no data. A symbol that already
        carries a market suffix (a dot anywhere in it) is passed through
        untouched, so an explicit non-US/CA suffix (e.g. ``BMW.DE``) still
        works.

        The US/CA choice is driven by the exchange: pass ``exchange``
        explicitly when the caller has it, otherwise this falls back to the
        packaged seed universes' symbol -> exchange mapping (see
        ``_default_exchange_map``) and defaults to ``.us`` for anything not
        found there -- ``MarketDataProvider.get_daily_bars`` receives only bare
        ticker strings with no exchange context, so that fallback is the
        common path in practice.
        """
        lowered = symbol.strip().lower()
        if "." in lowered:
            return lowered
        exch = (exchange or _default_exchange_map().get(symbol.strip().upper(), "")).upper()
        suffix = CA_SUFFIX if exch in CA_EXCHANGES else US_SUFFIX
        return f"{lowered}{suffix}"

    @staticmethod
    def _parse_csv_response(
        csv_text: str, symbol: str, start: dt.date, end: dt.date
    ) -> list[Bar]:
        """Parse stooq's historical daily CSV.

        The history endpoint returns::

            Date,Open,High,Low,Close,Volume
            2024-01-02,187.15,188.44,183.89,185.64,82488700

        Six columns, no ``Symbol`` or ``Time`` -- unlike the last-quote endpoint,
        whose eight-column shape this parser previously assumed.

        Returns:
            Bars in ascending date order, restricted to ``[start, end]``.

        Raises:
            _StooqNoDataError: an empty response or a recognised no-data
                marker -- stooq has nothing for this symbol (unknown ticker or
                exhausted quota). A ``ValueError`` subclass, so callers that
                only catch the base class (existing tests included) still see
                it; ``_fetch_symbol`` catches the subclass specifically to
                degrade per-symbol rather than failing the whole batch.
            ValueError: an unexpected/malformed shape (e.g. the last-quote
                endpoint's header) -- a real parser/endpoint mismatch, not an
                ordinary "no data" outcome.
        """
        text = csv_text.strip()
        if not text:
            raise _StooqNoDataError("empty response from stooq")
        for marker in _NO_DATA_MARKERS:
            if text.startswith(marker):
                # Stooq signals "unknown symbol" and "quota exhausted" with a
                # 200 response and a plain-text body, so this is not an
                # HTTP-level error and must be caught here.
                raise _StooqNoDataError(f"stooq returned no data for {symbol}: {text[:60]!r}")

        lines = text.split("\n")
        if len(lines) < 2:
            raise _StooqNoDataError(f"stooq returned no rows for {symbol}: {text[:60]!r}")

        header = [h.strip().lower() for h in lines[0].split(",")]
        # The last-quote endpoint's header is a superset of the history one
        # (Symbol,Date,Time,Open,...), so a plain "are the columns present?"
        # check accepts it and yields exactly one bar. Reject it explicitly:
        # silently treating a quote as a price series is the failure this
        # parser exists to prevent.
        if "symbol" in header or "time" in header:
            raise ValueError(
                f"unexpected stooq header for {symbol}: {header}. This is the last-quote "
                "layout from /q/l/, not the daily history from /q/d/l/; parsing it would "
                "yield a single bar instead of a price series."
            )
        try:
            idx = {name: header.index(name) for name in ("date", "open", "high", "low", "close")}
        except ValueError as exc:
            raise ValueError(f"unexpected stooq header for {symbol}: {header}") from exc
        # Some instruments (indices) legitimately carry no volume column.
        volume_idx = header.index("volume") if "volume" in header else None

        bars: list[Bar] = []
        for line_num, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            try:
                parts = line.split(",")
                if len(parts) < len(header):
                    continue

                date_str = parts[idx["date"]].strip()
                open_str = parts[idx["open"]].strip()
                high_str = parts[idx["high"]].strip()
                low_str = parts[idx["low"]].strip()
                close_str = parts[idx["close"]].strip()
                vol_str = parts[volume_idx].strip() if volume_idx is not None else "0"

                session = dt.date.fromisoformat(date_str)
                if not (start <= session <= end):
                    continue

                bar = Bar(
                    symbol=symbol,
                    session=session,
                    open=float(open_str),
                    high=float(high_str),
                    low=float(low_str),
                    close=float(close_str),
                    volume=float(vol_str),
                    adj_close=None,  # Stooq doesn't provide adj_close in free endpoint
                    source="stooq",
                )
                bars.append(bar)
            except Exception as exc:
                log.warning("skipping stooq row %d: %s", line_num, exc)
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
            "stooq free endpoint does not support intraday data",
            provider=self.name,
        )

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        # Stooq doesn't provide a bulk reference data endpoint in the free
        # tier. Fill in what the packaged seed universes know (name, exchange,
        # sector) for symbols that are in them; anything else gets a minimal
        # stub rather than being omitted.
        by_symbol = {s.symbol: s for s in load_packaged_universe()}
        return {s: by_symbol.get(s.upper(), SecurityInfo(symbol=s)) for s in symbols}

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list[Any]]:
        # Stooq doesn't cover corporate actions.
        return {s: [] for s in symbols}

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """US and TSX stocks with bootstrap market caps of at least $1B.

        Stooq's free endpoint has no bulk reference-data/universe listing of
        its own, so without this a real-data refresh (``market_data.provider =
        "stooq"``) would have nothing to fetch unless symbols were supplied
        explicitly on the command line -- exactly the "very limited" out-of-box
        experience this seed exists to fix. The packaged lists carry no
        listing/delisting dates, so ``as_of`` never excludes anything; that is
        an honest reflection of what a hand-curated *current* seed list can
        promise, not point-in-time coverage.
        """
        securities = load_stooq_universe()
        if as_of is None:
            return securities
        return [s for s in securities if s.is_active_on(as_of)]
