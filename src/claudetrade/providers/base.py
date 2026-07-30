"""Provider protocols and shared plumbing.

Design rules every adapter must honour:

* **Fail soft.** Raise ``ProviderError`` rather than arbitrary vendor
  exceptions; callers degrade to a fallback provider instead of crashing.
* **Respect the vendor.** Adapters own their own rate limiting and must not
  attempt to bypass authentication, paywalls, quotas, or anti-bot measures.
  If a source cannot be accessed within its terms, the adapter reports itself
  unavailable -- it does not work around the restriction.
* **No look-ahead.** ``as_of`` is honoured where a vendor supports point-in-time
  queries; where it does not, the adapter declares that in ``status()`` so the
  backtester can account for it.
* **Timezone-aware.** All datetimes in and out are UTC-aware.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from claudetrade.domain import (
    Bar,
    CorporateAction,
    EarningsEvent,
    SecurityInfo,
    SocialPost,
    SocialSource,
)


class ProviderError(RuntimeError):
    """Any failure to obtain data from an external source."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class RateLimitError(ProviderError):
    """The provider's published rate limit was reached.

    Raised *by our own limiter before sending*, or on a 429 response. Callers
    back off; they never retry in a tight loop.
    """

    def __init__(self, message: str, *, provider: str = "", retry_after_s: float = 60.0):
        super().__init__(message, provider=provider, retryable=True)
        self.retry_after_s = retry_after_s


class AuthenticationError(ProviderError):
    """Missing or rejected credentials. Never retried automatically."""


class NotConfiguredError(ProviderError):
    """The provider has no credentials configured and is cleanly disabled."""


@dataclass(slots=True)
class ProviderStatus:
    """Health and capability report shown on the dashboard.

    ``supports_point_in_time`` matters for backtest integrity: a provider that
    only serves *current* values (e.g. today's engagement counts on an old
    post) cannot be used to reconstruct history without introducing leakage.
    """

    name: str
    kind: str
    available: bool
    configured: bool
    message: str = ""
    last_success: dt.datetime | None = None
    last_error: str | None = None
    supports_point_in_time: bool = False
    supports_delisted: bool = False
    rate_limit_per_minute: int | None = None
    calls_made: int = 0
    licence_note: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


class RateLimiter:
    """Thread-safe token-bucket limiter.

    Adapters call ``acquire()`` before every request. The limiter blocks up to
    ``max_wait_s``; beyond that it raises ``RateLimitError`` so the caller can
    degrade rather than stall the UI.
    """

    def __init__(self, calls_per_minute: int, *, name: str = "", max_wait_s: float = 30.0):
        self.calls_per_minute = max(1, calls_per_minute)
        self.name = name
        self.max_wait_s = max_wait_s
        self._interval = 60.0 / self.calls_per_minute
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until the next call is permitted, or raise ``RateLimitError``."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > self.max_wait_s:
                raise RateLimitError(
                    f"rate limit for {self.name or 'provider'} would require "
                    f"{wait:.1f}s of waiting (limit {self.calls_per_minute}/min)",
                    provider=self.name,
                    retry_after_s=wait,
                )
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval

    def reset(self) -> None:
        with self._lock:
            self._next_allowed = 0.0


@runtime_checkable
class MarketDataProvider(Protocol):
    """Source of prices, reference data and corporate actions."""

    name: str

    def status(self) -> ProviderStatus:
        """Health, capability and licence report."""
        ...

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        """Daily OHLCV for each symbol over the inclusive ``[start, end]`` range.

        Symbols with no data are returned with an empty list rather than
        omitted, so callers can distinguish 'no data' from 'not requested'.
        """
        ...

    def get_intraday_bars(
        self,
        symbols: list[str],
        start: dt.datetime,
        end: dt.datetime,
        *,
        interval_minutes: int = 5,
    ) -> dict[str, list[Bar]]:
        """Intraday bars. May raise ``ProviderError`` when unsupported."""
        ...

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        """Reference data: name, exchange, sector, market cap, listing dates."""
        ...

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[CorporateAction]]:
        """Splits, dividends, symbol changes and delistings."""
        ...

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        """All securities the provider can serve, ideally point-in-time.

        Including names that were later delisted is what prevents survivorship
        bias; providers unable to do so must say so in ``status()``.
        """
        ...

    def get_market_caps(self, symbols: list[str]) -> dict[str, float]:  # noqa: ARG002
        """Optional bulk market-capitalisation lookup, in USD.

        This is an **optional capability**, not a required part of the
        interface every adapter must implement: the default body below
        returns an empty mapping ("not supported"), and every existing
        subclass (synthetic, csv, stooq) inherits that default unchanged --
        adding this method to the protocol does not require touching any of
        them. A provider that *can* look this up (see
        ``providers.market.yahoo.YahooMarketProvider``) overrides it.

        Returns:
            ``{symbol: market_cap_usd}`` for symbols this provider could
            price. A symbol this provider has no figure for is simply absent
            from the mapping -- never populated with a guess or a stale
            fallback value. Callers (``data.ingest.DataIngestor``) treat "not
            supported" and "supported but priced nothing" identically: both
            leave the symbol's cap unresolved, which is flagged in the
            data-quality report rather than silently dropped (ADR-0008
            Decision 3's data-quality risk note -- silently excluding a name
            whose cap cannot be established would reintroduce
            survivorship-style bias at the universe layer, the same failure
            mode ``UniverseSelector.for_session`` already guards against for
            delisted names).
        """
        return {}


@runtime_checkable
class EarningsProvider(Protocol):
    """Source of earnings calendar and surprise history."""

    name: str

    def status(self) -> ProviderStatus: ...

    def get_upcoming_earnings(
        self, symbols: list[str], *, through: dt.date | None = None
    ) -> dict[str, list[EarningsEvent]]:
        """Scheduled reports, with ``confirmed`` distinguishing fact from estimate."""
        ...

    def get_historical_earnings(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[EarningsEvent]]:
        """Past reports including actuals and surprise percentages.

        For backtesting, the returned ``as_of`` must reflect when the date was
        *known*, not when it was scraped -- otherwise the calendar leaks.
        """
        ...


@runtime_checkable
class SocialProvider(Protocol):
    """Source of social-media posts.

    Implementations must return text that has already been sanitised through
    ``claudetrade.utils.text.sanitize_social_text`` and authors that have been
    reduced to salted digests.
    """

    name: str
    source: SocialSource

    def status(self) -> ProviderStatus: ...

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Posts created in the window, newest first.

        ``symbols`` is a hint for query construction only; the caller still runs
        entity resolution over the returned text.
        """
        ...


@dataclass(slots=True)
class AIRequest:
    """One AI task. ``payload`` holds only sanitised, minimised text."""

    task: str  # "sentiment" | "ticker_context" | "catalyst" | "thesis" | "spam"
    payload: dict[str, Any]
    schema_name: str
    prompt_version: str = "v1"
    max_output_tokens: int = 900
    temperature: float = 0.0


@dataclass(slots=True)
class AIResponse:
    """Result of an AI call, always with full accounting metadata.

    ``parsed_ok=False`` means the model's output failed schema validation. The
    caller must then use ``fallback_used`` behaviour -- never the raw text.
    """

    task: str
    provider: str
    model: str
    prompt_version: str
    created_at: dt.datetime
    data: dict[str, Any] | None
    parsed_ok: bool
    confidence: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0
    fallback_used: str | None = None
    error: str | None = None
    cache_hit: bool = False


@runtime_checkable
class AIProvider(Protocol):
    """Provider-neutral LLM interface (OpenAI, Anthropic, or a null stub).

    Contract:

    * Requests carry only sanitised, fenced text -- never usernames or histories.
    * Responses are validated against a strict schema; malformed output is
      rejected, recorded, and replaced by the deterministic fallback.
    * No hidden reasoning is requested or stored -- concise structured labels
      and short evidence strings only.
    """

    name: str
    model: str

    def status(self) -> ProviderStatus: ...

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute one AI task. Must not raise for ordinary failures."""
        ...

    def complete_batch(self, requests: list[AIRequest]) -> list[AIResponse]:
        """Execute several tasks, batching where the vendor supports it."""
        ...


@runtime_checkable
class BrokerProvider(Protocol):
    """Order routing. Only the paper broker is implemented in this release.

    A live adapter must additionally verify ``TradingModeConfig.mode == 'live'``
    and ``live_trading_authorised`` before transmitting anything.
    """

    name: str
    is_live: bool

    def status(self) -> ProviderStatus: ...

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]: ...

    def cancel_all(self) -> int:
        """Emergency kill switch: cancel every working order. Returns the count."""
        ...
