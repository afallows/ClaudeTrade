"""Free daily OHLCV from stooq.com via published CSV endpoint.

Real data source with proper rate limiting and error handling. Does NOT scrape
HTML or bypass access controls. If the source is unavailable, the provider
cleanly reports itself unavailable rather than working around the limitation.

Limitations:
- Free endpoint only: no redistribution rights.
- Daily data only (no intraday).
- No delisted security coverage.
- Rate limited to reasonable frequency.
- Requires active network access (will fail offline).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from claudetrade.config import MarketDataConfig
from claudetrade.domain import Bar, SecurityInfo
from claudetrade.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderStatus,
    RateLimiter,
)

log = logging.getLogger(__name__)

#: Historical daily CSV endpoint. NOT ``/q/l/`` -- that is the *last quote*
#: endpoint and returns a single row, which would silently give the whole
#: application one bar of history per symbol instead of a price series.
STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/"
DEFAULT_RATE_LIMIT = 30  # Calls per minute

#: Stooq namespaces its symbols by market. US listings carry a ``.us`` suffix,
#: so a bare ``AAPL`` resolves to nothing.
US_SUFFIX = ".us"

#: Stooq answers a request it cannot serve with this literal body rather than an
#: HTTP error, so it has to be detected in the payload.
_NO_DATA_MARKERS = ("N/D", "No data", "Exceeded the daily hits limit")


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
            message="free stooq.com endpoint; may require network access",
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
        """Fetch daily bars. May raise ProviderError on network failure."""
        self._calls += 1
        out: dict[str, list[Bar]] = {}

        for symbol in symbols:
            try:
                self._rate_limiter.acquire()
                bars = self._fetch_symbol(symbol, start, end)
                out[symbol] = bars
                self._last_success = dt.datetime.now(tz=dt.UTC)
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
            ProviderError: on network failure or malformed response.
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

        # Parse CSV response.
        try:
            bars = self._parse_csv_response(response.text, symbol, start, end)
            return bars
        except Exception as exc:
            raise ProviderError(
                f"malformed stooq response for {symbol}: {exc}",
                provider=self.name,
                retryable=False,
            ) from exc

    @staticmethod
    def stooq_symbol(symbol: str) -> str:
        """Map an exchange ticker to stooq's namespaced form.

        Stooq expects ``aapl.us``; a bare ``AAPL`` returns no data. A symbol that
        already carries a market suffix is passed through untouched so non-US
        listings still work.
        """
        lowered = symbol.strip().lower()
        return lowered if "." in lowered else f"{lowered}{US_SUFFIX}"

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
            ValueError: on an empty response or a recognised no-data marker.
        """
        text = csv_text.strip()
        if not text:
            raise ValueError("empty response from stooq")
        for marker in _NO_DATA_MARKERS:
            if text.startswith(marker):
                # Stooq signals "unknown symbol" and "quota exhausted" with a
                # 200 response and a plain-text body, so this is not an
                # HTTP-level error and must be caught here.
                raise ValueError(f"stooq returned no data for {symbol}: {text[:60]!r}")

        lines = text.split("\n")
        if len(lines) < 2:
            raise ValueError(f"stooq returned no rows for {symbol}: {text[:60]!r}")

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
        # Stooq doesn't provide a bulk reference data endpoint in the free tier.
        # Return minimal stubs.
        return {s: SecurityInfo(symbol=s) for s in symbols}

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list[Any]]:
        # Stooq doesn't cover corporate actions.
        return {s: [] for s in symbols}

    def list_universe(
        self, *, as_of: dt.date | None = None  # noqa: ARG002
    ) -> list[SecurityInfo]:
        # Stooq doesn't provide a universe list in the free endpoint.
        # Caller must supply symbols explicitly.
        return []
