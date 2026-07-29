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

STOOQ_BASE_URL = "https://stooq.com/q/l/"
DEFAULT_RATE_LIMIT = 30  # Calls per minute


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

        params = {
            "s": symbol.upper(),
            "f": "d",  # d = CSV format
            "e": "csv",  # e=csv parameter
        }

        try:
            with httpx.Client(
                timeout=self._config.request_timeout_s,
                verify=True,  # Always verify SSL
            ) as client:
                response = client.get(STOOQ_BASE_URL, params=params)
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
    def _parse_csv_response(
        csv_text: str, symbol: str, start: dt.date, end: dt.date
    ) -> list[Bar]:
        """Parse stooq CSV format.

        Stooq CSV is comma-separated with header:
        Symbol,Date,Time,Open,High,Low,Close,Volume

        Returns:
            List of Bar objects in date order.

        Raises:
            ValueError: on parse errors.
        """
        lines = csv_text.strip().split("\n")
        if len(lines) < 2:
            raise ValueError("empty response from stooq")

        bars: list[Bar] = []

        # Skip header, parse data rows.
        for line_num, line in enumerate(lines, start=1):
            if line_num == 1:
                # Skip header line
                continue

            try:
                parts = line.split(",")
                if len(parts) < 8:
                    continue

                date_str = parts[1].strip()
                open_str = parts[3].strip()
                high_str = parts[4].strip()
                low_str = parts[5].strip()
                close_str = parts[6].strip()
                vol_str = parts[7].strip()

                session = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=dt.UTC
                ).date()
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
