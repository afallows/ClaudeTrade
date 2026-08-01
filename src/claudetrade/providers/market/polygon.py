"""Polygon.io grouped-daily market-data adapter: the WHOLE US equity market's
OHLCV in ONE request per trading date.

``GET https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}
?adjusted=true&apiKey=KEY`` returns a row for every US-listed stock that traded
that session (~10k rows). That inverts the cost model of every other bars
source in this codebase: TipRanks/Yahoo pay one HTTP call *per symbol* (a
~2,400-symbol universe refresh is thousands of calls and the reason the
owner's refresh took 5-10 minutes to reach ~33% -- QA handoff v3 finding F23),
while this adapter pays one call *per trading date* regardless of universe
size. A daily refresh is one or two grouped calls; a 2-year historical
backfill (``claudetrade db backfill``) is ~500 calls total.

**Posture -- deliberately different from tipranks/yahoo/stooq.** This is an
official, published, contracted REST API with an API key and documented
tiers, not an unauthenticated endpoint that happens to be reachable. The free
tier is ~5 requests/minute and end-of-day (EOD-delayed) data -- both fine for
a session-based swing scanner that refreshes after the close. The self-imposed
``RateLimiter`` (``PolygonConfig.rate_limit_per_minute``, default 5) paces
below the published ceiling; a 429 anyway raises ``RateLimitError`` carrying
the server's ``Retry-After`` so callers (the backfill command) can honour the
backoff rather than loop.

**Enabled-by-key semantics.** With no key resolvable this adapter is cleanly
unconfigured: ``status()`` reports it (with the exact setup instructions) and
every fetch raises ``NotConfiguredError``, which
``providers.registry.FallbackMarketProvider`` catches like any other
``ProviderError`` -- the cascade degrades to the fallbacks (tipranks/yahoo)
with no key configured, so the recommended config is safe to ship in
``config.example.toml`` even for an operator who has not created a key yet.
Key resolution order (first match wins):

1. ``POLYGON_API_KEY`` -- the plain environment variable every Polygon client
   library documents.
2. The standard credential store (``claudetrade.secrets``) under
   ``PolygonConfig.api_key_credential`` (default ``polygon_api_key``): i.e.
   ``CLAUDETRADE_SECRET_POLYGON_API_KEY`` or
   ``claudetrade secrets set polygon_api_key``.
3. ``[polygon] api_key`` in ``config.toml`` -- supported because a free-tier
   key is low-stakes, but discouraged (the config file is meant to be
   shareable); ``AppConfig.public_dict`` redacts it so it can never leak into
   the config hash, logs, or persisted run metadata.

**BARS SOURCE ONLY.** ``bulk_daily = True`` (a plain class attribute, the
same mechanism as ``TipRanksProvider.bars_last_resort``) is what
``data.ingest.DataIngestor.ingest_prices`` checks to narrow a refresh's
fetch window to sessions the database does not have yet. Reference data is
deliberately minimal: ``get_security_info`` returns nameless stubs (which
``FallbackMarketProvider.get_security_info`` treats as unfilled, so the
cascade sources real names/sectors/caps from TipRanks exactly as today),
``get_market_caps`` inherits the protocol's "not supported" default, and
``list_universe`` serves the packaged seed universes like every other live
adapter -- ``FallbackMarketProvider.list_universe`` is primary-only, so
returning ``[]`` here would silently empty a refresh's whole universe when
polygon is primary.

**Per-date on-disk response cache** (``paths.cache_dir/polygon/``, one JSON
file per (date, adjusted) pair): a cache hit costs ZERO HTTP calls and does
not even touch the rate limiter. Historical grouped responses are immutable,
so the cache has no TTL; two freshness rules keep it honest for recent dates:

* An **empty** response is never cached. For the current session it just
  means EOD data has not landed yet; for a past date it would mean an ad-hoc
  exchange closure this codebase's approximate calendar does not model --
  either way a permanently-cached empty answer is the one wrong outcome, and
  re-paying one call per rare empty date is cheap.
* The **current session's** response is only cached once the session is over
  (``utc_now() >= session_close_utc(date) + settle buffer``) -- an intraday
  grouped row is a partial-day aggregate that must never become the
  permanent cached answer. Older dates are always cacheable.

This is what makes ``ingest_prices``'s chunked calls cheap (the first chunk
fetches each date once; every later chunk is a cache hit) and what makes
``claudetrade db backfill`` resumable and idempotent (a re-run re-reads
already-fetched dates from disk).

**Symbol notation**: Polygon uses dot notation for US share classes
(``BRK.B``); this codebase uses hyphens (``BRK-B``). ``polygon_ticker``
applies the same deliberately narrow single-trailing-letter rule as
``providers.market.tipranks._US_CLASS_SHARE_RE`` -- nothing else is
rewritten. Canadian (TSX/TSXV) listings are simply not in a
``locale=us`` grouped response: they come back with no bars and the
registry cascade fills them from tipranks/yahoo, per symbol, exactly as the
``FallbackMarketProvider.get_daily_bars`` contract already provides.

**Sessions come from the REQUEST DATE, never the row's ``t`` epoch.** The
grouped endpoint is queried per date, so every row in a response belongs to
that date by construction; parsing ``t`` (epoch millis, whose wall-clock
placement varies with the aggregate window convention) would only reintroduce
the timezone misdating traps ``utils.timeutils`` exists to prevent.

**Adjusted prices**: ``adjusted=true`` (the default) makes the returned OHLC
series split-adjusted -- there is no separate raw-print + dividend-adjusted
pair on this endpoint, so ``Bar.adj_close`` is left ``None`` and
``Bar.effective_adj_close`` falls back to the (split-adjusted) close. That is
the honest representation: claiming the close as a dividend-adjusted series
would misstate what Polygon returned.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import httpx

from claudetrade.config import PolygonConfig
from claudetrade.data.universe import load_packaged_universe
from claudetrade.domain import Bar, SecurityInfo
from claudetrade.providers.base import (
    AuthenticationError,
    MarketDataProvider,
    NotConfiguredError,
    ProviderError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
)
from claudetrade.secrets import get_secret
from claudetrade.utils.timeutils import (
    current_trading_session,
    session_close_utc,
    trading_day_range,
    utc_now,
)

log = logging.getLogger(__name__)

GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date}"

#: Plain environment variable checked FIRST -- the name every Polygon client
#: library documents, so a key exported for any other Polygon tool just works.
ENV_API_KEY = "POLYGON_API_KEY"

DEFAULT_RATE_LIMIT = 5  # Calls/minute -- the documented free tier.
#: RateLimiter.acquire raises once a caller would wait longer than this. At
#: 5/min the pacing interval is 12s and this adapter's callers are
#: sequential-per-date, so a single acquire's wait is ~one interval; 90s
#: leaves headroom for a second thread (the dedicated benchmark fetch)
#: queueing behind a date fetch without tripping the limiter's own error.
_MAX_WAIT_S = 90.0

#: How long after a session's close its grouped response becomes cacheable --
#: EOD aggregates settle shortly after the bell; caching a response fetched
#: mid-settle would freeze a possibly-incomplete answer. Before this instant
#: the response is still served to the caller, just re-fetched next run.
_CACHE_SETTLE = dt.timedelta(hours=1)

#: Same deliberately narrow rule as ``tipranks._US_CLASS_SHARE_RE``: only a
#: single trailing letter after a dash is a share class (``BRK-B``); anything
#: else is never rewritten.
_US_CLASS_SHARE_RE = re.compile(r"^[A-Z]{1,5}-[A-Z]$")


def polygon_ticker(symbol: str) -> str:
    """Map this codebase's symbol notation to Polygon's.

    US share classes: our hyphen convention becomes Polygon's dot one
    (``BRK-B`` -> ``BRK.B``), via the same narrow single-letter-suffix rule
    tipranks uses; everything else passes through upper-cased. There is no
    exchange namespace to add -- the grouped URL already scopes to
    ``locale/us``, and non-US listings simply never match a response row.
    """
    stripped = symbol.strip().upper()
    if _US_CLASS_SHARE_RE.match(stripped):
        return stripped.replace("-", ".")
    return stripped


class PolygonProvider(MarketDataProvider):
    """Grouped-daily (whole-market-per-date) bars adapter over Polygon.io."""

    name = "polygon"
    #: Read by ``data.ingest.DataIngestor.ingest_prices``: this provider's
    #: cost model is per-DATE, not per-symbol, so a refresh can narrow its
    #: window to sessions the database is actually missing. A plain class
    #: attribute (like ``TipRanksProvider.bars_last_resort``), not a Protocol
    #: requirement -- every other provider simply lacks it.
    bulk_daily = True

    def __init__(
        self,
        config: PolygonConfig | None = None,
        *,
        cache_dir: Path | str | None = None,
    ) -> None:
        self._config = config or PolygonConfig()
        self._cache_dir = (Path(cache_dir) / "polygon") if cache_dir else None
        self._rate_limiter = RateLimiter(
            self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            name="polygon",
            max_wait_s=_MAX_WAIT_S,
        )
        #: Resolved once at construction (see the module docstring's key
        #: resolution order); a provider instance lives for one run, so a key
        #: change means a new process/config load anyway.
        self._api_key = self._resolve_api_key()
        self._lock = threading.Lock()
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None

    def _resolve_api_key(self) -> str | None:
        env = os.environ.get(ENV_API_KEY)
        if env:
            return env
        secret = get_secret(self._config.api_key_credential)
        if secret is not None:
            return secret.reveal()
        if self._config.api_key:
            return self._config.api_key
        return None

    # --- status -------------------------------------------------------------

    def status(self) -> ProviderStatus:
        configured = self._api_key is not None
        if configured:
            message = (
                "polygon.io grouped-daily EOD bars (whole US market per call); "
                f"free tier ~{DEFAULT_RATE_LIMIT}/min, EOD-delayed"
            )
        else:
            # The exact setup instructions, because "unconfigured" without a
            # fix is a support ticket: any one of the three resolution paths
            # enables the adapter on the next run.
            message = (
                "not configured: set POLYGON_API_KEY, or store the key with "
                "'claudetrade secrets set polygon_api_key' "
                "(env: CLAUDETRADE_SECRET_POLYGON_API_KEY), or set "
                "[polygon] api_key in config.toml. Bars degrade to the "
                "configured fallbacks until then."
            )
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=configured,
            configured=configured,
            message=message,
            last_error=self._last_error,
            last_success=self._last_success,
            #: Historical grouped dates ARE point-in-time-stable (immutable
            #: EOD aggregates), but the flag stays False to match the rest of
            #: the chain: the provider serves no reference data or delistings
            #: of its own, so a polygon-primary chain as a whole is no more
            #: point-in-time than a tipranks-primary one.
            supports_point_in_time=False,
            supports_delisted=False,
            rate_limit_per_minute=self._config.rate_limit_per_minute or DEFAULT_RATE_LIMIT,
            calls_made=self._calls,
            licence_note=(
                "Official polygon.io REST API (published, contracted, keyed) -- unlike the "
                "unauthenticated tipranks/yahoo endpoints. Free tier: ~5 requests/minute, "
                "end-of-day data, personal/research use per polygon.io's terms of service. "
                "Grouped-daily responses are cached on disk per date; historical data is "
                "immutable so cached dates are never re-fetched."
            ),
            capabilities={
                "daily_bars": True,
                "bulk_daily": True,
                "intraday": False,
                "market_caps": False,
                "security_info": False,
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
        """One grouped call per trading date in ``[start, end]`` (cache hits
        cost zero HTTP), filtered to the requested symbols.

        A symbol Polygon has nothing for (a TSX listing, a delisted name, a
        typo) gets an empty list per the protocol contract -- which is
        exactly what makes ``FallbackMarketProvider.get_daily_bars`` fill it
        from the next provider in the cascade.
        """
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        if not symbols:
            return out
        if self._api_key is None:
            raise NotConfiguredError(
                "polygon has no API key configured -- " + self.status().message,
                provider=self.name,
            )

        for date in trading_day_range(start, end):
            per_symbol = self.grouped_daily_bars(symbols, date, adjusted=adjusted)
            for symbol, bar in per_symbol.items():
                out[symbol].append(bar)

        for bars in out.values():
            bars.sort(key=lambda b: b.session)
        return out

    def grouped_daily_bars(
        self,
        symbols: list[str],
        date: dt.date,
        *,
        adjusted: bool = True,
        bypass_cache: bool = False,
    ) -> dict[str, Bar]:
        """One trading date's grouped bars, filtered to ``symbols``.

        The shared primitive under ``get_daily_bars`` and the
        ``claudetrade db backfill`` command -- both go through the same
        cache/fetch/parse path so a backfilled date is a free cache hit for
        the next refresh and vice versa. ``bypass_cache=True`` (backfill's
        ``--force``) re-fetches the date from the API and overwrites the
        cached copy, for when the operator wants restatements picked up.
        """
        rows = self._grouped_rows(date, adjusted=adjusted, bypass_cache=bypass_cache)
        if not rows:
            return {}

        wanted = {polygon_ticker(s): s for s in symbols}
        out: dict[str, Bar] = {}
        for row in rows:
            bar = _parse_grouped_row(row, date, wanted)
            if bar is not None:
                out[bar.symbol] = bar
        return out

    # --- cache/fetch plumbing ----------------------------------------------

    def _cache_path(self, date: dt.date, *, adjusted: bool) -> Path | None:
        if self._cache_dir is None:
            return None
        suffix = "" if adjusted else ".unadjusted"
        return self._cache_dir / f"{date.isoformat()}{suffix}.json"

    def _load_cached_rows(self, date: dt.date, *, adjusted: bool) -> list[dict[str, Any]] | None:
        """Cached rows for ``date``, or ``None`` on a miss. No TTL: a cached
        date is by construction final (see ``_cacheable``)."""
        path = self._cache_path(date, adjusted=adjusted)
        if path is None or not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            rows = record["results"]
        except (OSError, ValueError, KeyError, TypeError):
            # A truncated/corrupt cache file (an interrupted write, a manual
            # edit) is a miss, never an error -- the date is simply re-fetched.
            return None
        return rows if isinstance(rows, list) else None

    @staticmethod
    def _cacheable(date: dt.date, rows: list[dict[str, Any]]) -> bool:
        """Whether ``date``'s response may become the permanent cached answer.

        See the module docstring's cache rules: empty responses never (an
        unpublished current session or an unmodelled ad-hoc closure must stay
        re-checkable), and the current session only once it has closed and
        settled (an intraday grouped row is a partial-day aggregate).
        """
        if not rows:
            return False
        today = current_trading_session()
        if date < today:
            return True
        return utc_now() >= session_close_utc(date) + _CACHE_SETTLE

    def _store_cached_rows(
        self, date: dt.date, rows: list[dict[str, Any]], *, adjusted: bool
    ) -> None:
        path = self._cache_path(date, adjusted=adjusted)
        if path is None or not self._cacheable(date, rows):
            return
        record = {
            "fetched_at": utc_now().isoformat(),
            "date": date.isoformat(),
            "adjusted": adjusted,
            "results": rows,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            log.debug("failed to write polygon cache for %s", date, exc_info=True)

    def _grouped_rows(
        self, date: dt.date, *, adjusted: bool, bypass_cache: bool = False
    ) -> list[dict[str, Any]]:
        if not bypass_cache:
            cached = self._load_cached_rows(date, adjusted=adjusted)
            if cached is not None:
                return cached
        rows = self._fetch_grouped(date, adjusted=adjusted)
        self._store_cached_rows(date, rows, adjusted=adjusted)
        return rows

    def _fetch_grouped(self, date: dt.date, *, adjusted: bool) -> list[dict[str, Any]]:
        """One live grouped-daily HTTP call, with the standard error taxonomy.

        * 401/403 -> ``AuthenticationError`` (bad/revoked key or a plan the
          endpoint is not on; never retried automatically).
        * 429 -> ``RateLimitError`` carrying the server's ``Retry-After`` --
          the backfill command sleeps it off; a refresh degrades to fallbacks.
        * 5xx / network / timeout -> retryable ``ProviderError`` (an outage).
        * Non-JSON or an ``ERROR``-status envelope -> ``ProviderError``.

        An OK envelope with ``resultsCount: 0`` is a legitimate "no data for
        this date" (holiday mismatch, EOD not yet published) -- an empty
        list, never an exception.
        """
        if self._api_key is None:
            raise NotConfiguredError(
                "polygon has no API key configured -- " + self.status().message,
                provider=self.name,
            )
        self._rate_limiter.acquire()
        with self._lock:
            self._calls += 1

        url = GROUPED_URL.format(date=date.isoformat())
        params = {"adjusted": "true" if adjusted else "false", "apiKey": self._api_key}
        try:
            with httpx.Client(timeout=self._config.request_timeout_s, verify=True) as client:
                response = client.get(url, params=params)
        except httpx.ConnectError as exc:
            self._last_error = str(exc)
            raise ProviderError(
                f"network error connecting to polygon for {date}: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            self._last_error = str(exc)
            raise ProviderError(
                f"timeout fetching {date} from polygon: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                wait_s = float(retry_after)
            except (TypeError, ValueError):
                wait_s = 60.0
            self._last_error = f"rate limited (429) for {date}"
            raise RateLimitError(
                f"polygon rate limit reached fetching {date}",
                provider=self.name,
                retry_after_s=wait_s,
            )

        if response.status_code in (401, 403):
            self._last_error = f"HTTP {response.status_code} for {date}"
            raise AuthenticationError(
                f"polygon rejected the API key (HTTP {response.status_code}) for {date} -- "
                "check POLYGON_API_KEY / the stored polygon_api_key credential.",
                provider=self.name,
            )

        if response.status_code >= 500:
            self._last_error = f"HTTP {response.status_code} (outage) for {date}"
            raise ProviderError(
                f"polygon returned HTTP {response.status_code} for {date} -- an outage, retryable.",
                provider=self.name,
                retryable=True,
            )

        if response.status_code >= 400:
            self._last_error = f"HTTP {response.status_code} for {date}"
            raise ProviderError(
                f"polygon returned HTTP {response.status_code} for {date}",
                provider=self.name,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            self._last_error = f"non-JSON response for {date}"
            raise ProviderError(
                f"polygon returned a non-JSON body for {date}: {exc}",
                provider=self.name,
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError(
                f"polygon returned an unexpected payload shape for {date}",
                provider=self.name,
            )

        status = payload.get("status")
        if isinstance(status, str) and status.upper() == "ERROR":
            detail = payload.get("error") or payload.get("message") or "no detail"
            self._last_error = f"API error for {date}: {detail}"
            raise ProviderError(
                f"polygon reported an error for {date}: {detail}",
                provider=self.name,
            )

        with self._lock:
            self._last_success = utc_now()
        results = payload.get("results")
        if not isinstance(results, list):
            # "OK" with no results key at all is how a dateless/holiday query
            # comes back -- same bucket as resultsCount: 0.
            return []
        return [row for row in results if isinstance(row, dict)]

    # --- reference data (deliberately minimal -- bars source only) -----------

    def get_intraday_bars(
        self,
        symbols: list[str],  # noqa: ARG002
        start: dt.datetime,  # noqa: ARG002
        end: dt.datetime,  # noqa: ARG002
        *,
        interval_minutes: int = 5,  # noqa: ARG002
    ) -> dict[str, list[Bar]]:
        raise ProviderError(
            "polygon intraday bars are not implemented by this adapter",
            provider=self.name,
        )

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        """Nameless stubs only -- ``FallbackMarketProvider.get_security_info``
        treats an empty ``.name`` as unfilled, so the cascade sources real
        reference data from TipRanks exactly as it does today. Serving the
        packaged seed here instead (the yahoo/stooq degrade) would make the
        cascade stop at this provider and never reach TipRanks' live data."""
        return {s: SecurityInfo(symbol=s) for s in symbols}

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list[Any]]:
        # Not implemented by this adapter -- an honest empty result, same
        # convention as yahoo/tipranks.
        return {s: [] for s in symbols}

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """Packaged seed universes, same as stooq/yahoo/tipranks.

        NOT empty: ``FallbackMarketProvider.list_universe`` is primary-only
        (no cascade), so an empty list here would silently empty every
        refresh's universe whenever polygon is the configured primary.
        """
        securities = load_packaged_universe()
        if as_of is None:
            return securities
        return [s for s in securities if s.is_active_on(as_of)]

    # ``get_market_caps`` is deliberately NOT overridden: the protocol default
    # (an empty mapping, "not supported") makes ``DataIngestor.
    # enrich_market_caps`` skip straight past this provider to TipRanks.


def _parse_grouped_row(
    row: dict[str, Any], date: dt.date, wanted: dict[str, str]
) -> Bar | None:
    """One grouped-response row -> ``Bar``, or ``None`` when the row is not a
    requested symbol or is malformed.

    Grouped rows are terse: ``T`` ticker, ``o/h/l/c`` OHLC, ``v`` volume
    (``vw`` VWAP and ``n`` trade count are ignored). Every access is
    defensive -- one malformed row among ~10k must never fail the date.
    ``session`` is the request date, never parsed from the row's ``t`` epoch
    (see the module docstring).
    """
    ticker = row.get("T")
    if not isinstance(ticker, str):
        return None
    symbol = wanted.get(ticker.upper())
    if symbol is None:
        return None
    o, h, low_, c = row.get("o"), row.get("h"), row.get("l"), row.get("c")
    if o is None or h is None or low_ is None or c is None:
        return None
    try:
        o_f, h_f, l_f, c_f = float(o), float(h), float(low_), float(c)
    except (TypeError, ValueError):
        return None
    volume = row.get("v")
    try:
        vol_f = float(volume) if volume is not None else 0.0
    except (TypeError, ValueError):
        vol_f = 0.0
    return Bar(
        symbol=symbol,
        session=date,
        open=round(o_f, 4),
        high=round(h_f, 4),
        low=round(l_f, 4),
        close=round(c_f, 4),
        volume=round(vol_f, 1),
        # adjusted=true makes the whole OHLC series split-adjusted; there is
        # no separate dividend-adjusted close on this endpoint, so adj_close
        # honestly stays None and effective_adj_close falls back to close.
        adj_close=None,
        source="polygon",
    )
