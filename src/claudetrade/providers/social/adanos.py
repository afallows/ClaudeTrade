"""Adanos (``adanos.org``) pre-aggregated buzz and sentiment across X,
Reddit and Polymarket.

Same family as ``providers.social.apewisdom``: a hosted aggregator that
serves the finished per-ticker tally rather than individual posts. Adanos is
richer than ApeWisdom in one load-bearing way -- it reports real polarity
(``sentiment_score``, ``bullish_pct``, ``bearish_pct``) alongside volume, and
does so across three separate platforms, refreshed hourly.

**This is PRE-AGGREGATED data. Never fabricate ``SocialPost`` rows from it.**
See the warning in ``providers.social.hosted_api``'s module docstring for
the general failure mode this avoids: a vendor-computed score has no
underlying post text, author or timestamp, and inventing those to satisfy
the ``SocialProvider`` protocol would feed the manipulation model
confident-looking fiction rather than real evidence. This module returns
``domain.AdanosSnapshot`` rows through :meth:`AdanosProvider.fetch_snapshots`
instead, and the ingest path (``data.ingest.DataIngestor.ingest_adanos``)
stores them in their own table (``db.models.AdanosSnapshotRow``) -- never
``symbol_sentiment_daily``, whose ``"all"`` aggregate strategies score
against, and never ``SymbolAttention``/``ingest_attention`` either: that path
hard-codes an ``apewisdom:<community>`` source label and has no columns for
polarity, so forcing Adanos through it would both mislabel the rows and
silently drop the one thing Adanos adds over ApeWisdom.

**Verified API contract** (captured live 2026-08-02 against the vendor's own
public pages and documented official API; see ``config.AdanosConfig`` and
``docs/api-providers.md`` for the full write-up):

Site-level keyless endpoints (what adanos.org's own web pages call -- JSON
arrays, no auth)::

    GET {site_base_url}/proxy-x/trending?limit=N&from=YYYY-MM-DD&to=YYYY-MM-DD
    GET {site_base_url}/proxy/trending?limit=N&from=YYYY-MM-DD&to=YYYY-MM-DD
    GET {site_base_url}/proxy-polymarket/trending?limit=N&from=YYYY-MM-DD&to=YYYY-MM-DD

Official authenticated API (header ``X-API-Key: sk_live_...``), same row
shapes::

    GET {official_base_url}/x/stocks/v1/trending
    GET {official_base_url}/reddit/stocks/v1/trending
    GET {official_base_url}/polymarket/stocks/v1/trending

The Polymarket *official* path is inferred from the X/Reddit pattern, not
independently confirmed -- if it 404s at runtime, only that feed degrades
(see ``_get_json``'s 404-in-official-mode handling); the X and Reddit feeds
are unaffected.

**Two access modes.** Default is keyless "site" mode: one request per
enabled feed per collection cycle, honest User-Agent, paced by the shared
``providers.base.RateLimiter`` -- the same courtesy-budget class as
ApeWisdom's few calls/cycle. "Official" mode activates only when BOTH
``config.prefer_official_api`` is true AND ``config.api_key_credential``
resolves to a real key; it is gated by a persistent monthly budget (see
``_MonthlyBudgetStore``) that fails closed once the reserve floor is reached
rather than ever falling back to hammering the site proxy harder than its
normal one-request-per-feed-per-cycle cadence.

**Licensing** (adanos.org/terms, checked 2026-08-02): commercial use is
permitted subject to the vendor's terms; raw API data may not be
redistributed as a competing service, and rate limits may not be
circumvented. Site-proxy mode is the vendor's own public page endpoint, used
at page-equivalent cadence; an API key is the guaranteed-compliant path and
is preferred whenever one is configured.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx

from claudetrade.config import AdanosConfig
from claudetrade.domain import AdanosSnapshot
from claudetrade.providers.base import (
    AuthenticationError,
    ProviderError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.secrets import get_secret
from claudetrade.utils.timeutils import utc_now

log = logging.getLogger(__name__)

#: Ticker strings Adanos may emit that are not equities this app screens --
#: same list ``providers.social.apewisdom`` filters, duplicated rather than
#: imported so this module has no dependency on that one (see the module
#: docstring: Adanos is its own first-class module, not an extension).
_NON_EQUITY = frozenset({"BTC", "ETH", "DOGE", "SHIB", "XRP", "SOL", "ADA", "USDT"})

#: Site-proxy path segments, relative to ``config.site_base_url``.
_SITE_PATHS = {
    "x": "proxy-x/trending",
    "reddit": "proxy/trending",
    "polymarket": "proxy-polymarket/trending",
}

#: Official-API path segments, relative to ``config.official_base_url``.
_OFFICIAL_PATHS = {
    "x": "x/stocks/v1/trending",
    "reddit": "reddit/stocks/v1/trending",
    "polymarket": "polymarket/stocks/v1/trending",
}

#: Which row field carries the "how many times" count -- x/reddit share
#: ``mentions``, polymarket reports ``trade_count`` instead.
_COUNT_FIELD = {"x": "mentions", "reddit": "mentions", "polymarket": "trade_count"}

#: Which row field carries the engagement total -- x/reddit report
#: ``total_upvotes``, polymarket reports ``total_liquidity`` instead.
_ENGAGEMENT_FIELD = {"x": "total_upvotes", "reddit": "total_upvotes", "polymarket": "total_liquidity"}


def _current_month(now: dt.datetime | None = None) -> str:
    current = now or utc_now()
    return f"{current.year:04d}-{current.month:02d}"


def _seconds_until_next_month(now: dt.datetime | None = None) -> float:
    current = now or utc_now()
    if current.month == 12:
        nxt = current.replace(
            year=current.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        nxt = current.replace(month=current.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (nxt - current).total_seconds())


class _MonthlyBudgetStore:
    """Persists official-mode request counts across process restarts.

    A single small JSON file under ``paths.cache_dir/adanos/`` -- there is no
    existing DB table for a cross-run request budget (``SymbolFetchHealth``
    is the nearest precedent but is symbol-keyed, not a fit here) -- mirrors
    ``providers.market.polygon.PolygonProvider``'s on-disk cache-under-
    ``cache_dir`` posture. This is best-effort, not a race-free distributed
    counter: it exists to keep official mode from silently blowing through a
    250/month free tier, and ``record_call`` self-corrects from the vendor's
    own ``X-RateLimit-Remaining-Monthly`` header whenever a response supplies
    one, which bounds any drift from a missed or duplicated local increment
    to at most one cycle.

    Falls back to an in-process dict when ``path`` is ``None`` (no
    ``cache_dir`` given): budget gating still works for the life of one
    process, it just does not survive a restart.
    """

    def __init__(self, path: Path | None):
        self._path = path
        self._lock = threading.Lock()
        self._mem_state: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if self._path is None:
            return dict(self._mem_state)
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        if self._path is None:
            self._mem_state = dict(state)
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            log.debug("failed to persist adanos budget state", exc_info=True)

    def snapshot(self) -> tuple[int, str]:
        """``(used, month)`` for the current calendar month.

        A month rollover resets ``used`` to 0 and persists the reset
        immediately, so a budget exhausted in one month never bleeds into
        the next.
        """
        with self._lock:
            month = _current_month()
            state = self._load()
            if state.get("month") != month:
                state = {"month": month, "used": 0}
                self._save(state)
            return int(state.get("used", 0)), month

    def record_call(self, *, month: str, remaining_hint: int | None, budget: int) -> int:
        """Count one request just sent, self-correcting from the vendor's own
        remaining-count header when given. Returns the resulting ``used``."""
        with self._lock:
            state = self._load()
            if state.get("month") != month:
                state = {"month": month, "used": 0}
            used = int(state.get("used", 0)) + 1
            if remaining_hint is not None:
                used = max(used, budget - remaining_hint)
            state = {"month": month, "used": used}
            self._save(state)
            return used


class AdanosProvider:
    """Reads pre-aggregated per-ticker buzz/sentiment from Adanos."""

    name = "adanos"
    kind = "attention"

    def __init__(
        self,
        config: AdanosConfig,
        *,
        client: httpx.Client | None = None,
        cache_dir: Path | str | None = None,
    ):
        self.config = config
        #: Injected in tests via ``httpx.MockTransport``; ``None`` means this
        #: provider opens (and closes) its own client per fetch, matching
        #: ``apewisdom``'s posture of holding no long-lived connection.
        self._client = client
        self._limiter = RateLimiter(config.calls_per_minute, name="adanos", max_wait_s=30.0)
        #: Resolved once at construction (mirrors
        #: ``providers.market.polygon.PolygonProvider``): a provider instance
        #: lives for one run, so a credential change means a new process.
        self._api_key = self._resolve_api_key() if config.prefer_official_api else None
        self.mode = "official" if (config.prefer_official_api and self._api_key) else "site"
        budget_path = (Path(cache_dir) / "adanos" / "monthly_budget.json") if cache_dir else None
        self._budget = _MonthlyBudgetStore(budget_path)
        self._lock = threading.Lock()
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Per-feed failure messages from the most recent ``fetch_snapshots``
        #: call, keyed ``"adanos_x"``/``"adanos_reddit"``/``"adanos_polymarket"``
        #: -- read by ``data.ingest.DataIngestor.ingest_adanos`` and folded
        #: into ``IngestReport.provider_failures`` / ``degraded_sources``, so
        #: one feed's outage is visible by name rather than merged into a
        #: single opaque "adanos failed" entry.
        self.last_feed_failures: dict[str, str] = {}

    def _resolve_api_key(self) -> str | None:
        secret = get_secret(self.config.api_key_credential)
        return secret.reveal() if secret is not None else None

    def _enabled_platforms(self) -> list[str]:
        platforms = []
        if self.config.feed_x:
            platforms.append("x")
        if self.config.feed_reddit:
            platforms.append("reddit")
        if self.config.feed_polymarket:
            platforms.append("polymarket")
        return platforms

    # --- status ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        """Report provider status: configured/available, mode, and -- in
        official mode -- remaining monthly budget."""
        platforms = self._enabled_platforms()
        configured = self.config.enabled and bool(platforms)
        if self.mode == "official":
            used, month = self._budget.snapshot()
            remaining = max(0, self.config.monthly_budget - used)
            message = (
                f"Adanos official API ({', '.join(platforms) or 'no feeds enabled'}); "
                f"budget remaining {remaining}/{self.config.monthly_budget} for {month} "
                f"(reserve {self.config.monthly_reserve})"
            )
        elif configured:
            message = f"Adanos site-proxy (keyless) mode ({', '.join(platforms)})"
        else:
            message = "disabled or no feeds configured"
        return ProviderStatus(
            name=self.name,
            kind="attention",
            available=configured,
            configured=configured,
            message=message,
            last_success=self._last_success,
            last_error=self._last_error,
            #: Same rationale as ApeWisdom: an hourly-refreshed rolling
            #: "current" snapshot with no history endpoint cannot be used to
            #: reconstruct a past date without look-ahead.
            supports_point_in_time=False,
            rate_limit_per_minute=self.config.calls_per_minute,
            calls_made=self._calls,
            licence_note=(
                "Adanos.org: commercial use permitted subject to the vendor's terms; raw API "
                "data may not be redistributed as a competing service and rate limits may not "
                "be circumvented. Local personal-research use only; no redistribution. "
                "Site-proxy mode is the vendor's own public page endpoint, used at "
                "page-equivalent cadence -- an API key is the guaranteed-compliant path and is "
                "preferred whenever one is configured."
            ),
            capabilities={
                "x": self.config.feed_x,
                "reddit": self.config.feed_reddit,
                "polymarket": self.config.feed_polymarket,
                "official_mode": self.mode == "official",
            },
        )

    # --- fetch ------------------------------------------------------------

    def fetch_snapshots(self) -> list[AdanosSnapshot]:
        """Every enabled feed's current buzz/sentiment snapshot, best-effort.

        One feed failing (network, rate limit, budget exhaustion, schema
        drift) is logged, recorded in ``last_feed_failures`` and skipped
        rather than aborting the rest -- this is a supplementary source that
        must never take a refresh down. Returns an empty list -- never
        raises -- when disabled or when every feed fails.
        """
        self.last_feed_failures = {}
        if not self.config.enabled:
            return []

        observed_at = utc_now()
        platforms = self._enabled_platforms()
        out: list[AdanosSnapshot] = []
        for platform in platforms:
            try:
                out.extend(self._fetch_platform(platform, observed_at))
            except ProviderError as exc:
                self.last_feed_failures[f"adanos_{platform}"] = str(exc)
                log.warning("adanos %s feed failed: %s", platform, exc)
            except Exception:  # pragma: no cover - defensive
                self.last_feed_failures[f"adanos_{platform}"] = "unexpected error"
                log.warning("adanos %s feed raised unexpectedly", platform, exc_info=True)
        if out:
            log.info(
                "adanos: %d snapshot row(s) across %d feed(s) (%s mode)",
                len(out),
                len(platforms),
                self.mode,
            )
        return out

    def _fetch_platform(self, platform: str, observed_at: dt.datetime) -> list[AdanosSnapshot]:
        url, headers = self._build_request(platform)
        payload = self._get_json(url, headers=headers, platform=platform)
        return _parse_feed_rows(payload, platform=platform, observed_at=observed_at)

    def _build_request(self, platform: str) -> tuple[str, dict[str, str]]:
        today = utc_now().date().isoformat()
        params = f"limit={max(1, self.config.limit)}&from={today}&to={today}"
        if self.mode == "official":
            base = self.config.official_base_url.rstrip("/")
            url = f"{base}/{_OFFICIAL_PATHS[platform]}?{params}"
            headers = {
                "X-API-Key": self._api_key or "",
                "User-Agent": self.config.user_agent,
                "Accept": "application/json",
            }
        else:
            base = self.config.site_base_url.rstrip("/")
            url = f"{base}/{_SITE_PATHS[platform]}?{params}"
            headers = {"User-Agent": self.config.user_agent, "Accept": "application/json"}
        return url, headers

    def _enforce_budget_or_raise(self) -> None:
        """Fail closed, with no request sent, once the monthly reserve floor
        is reached -- never a fallback to extra site-proxy calls to
        compensate (see the module docstring)."""
        used, month = self._budget.snapshot()
        remaining = self.config.monthly_budget - used
        if remaining <= self.config.monthly_reserve:
            raise RateLimitError(
                f"adanos official-API monthly budget exhausted (used {used}/"
                f"{self.config.monthly_budget}, reserve {self.config.monthly_reserve}); "
                f"failing closed for the rest of {month} rather than falling back to extra "
                "site-proxy calls",
                provider=self.name,
                retry_after_s=_seconds_until_next_month(),
            )

    def _record_official_call(self, response: httpx.Response) -> None:
        month = _current_month()
        header = response.headers.get("X-RateLimit-Remaining-Monthly")
        hint: int | None = None
        if header is not None:
            try:
                hint = int(header)
            except ValueError:
                hint = None
        self._budget.record_call(month=month, remaining_hint=hint, budget=self.config.monthly_budget)

    def _get_json(self, url: str, *, headers: dict[str, str], platform: str) -> Any:
        if self.mode == "official":
            self._enforce_budget_or_raise()

        self._limiter.acquire()
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers, timeout=self.config.request_timeout_s)
            else:
                with httpx.Client(
                    timeout=self.config.request_timeout_s, follow_redirects=True, headers=headers
                ) as client:
                    response = client.get(url)
        except httpx.HTTPError as exc:
            self._last_error = f"network error for {platform}: {exc}"
            raise ProviderError(
                f"adanos request failed for the {platform} feed: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

        with self._lock:
            self._calls += 1
        if self.mode == "official":
            self._record_official_call(response)

        if response.status_code == 429:
            self._last_error = f"HTTP 429 for {platform}"
            raise RateLimitError(
                f"adanos rate limit reached for the {platform} feed",
                provider=self.name,
                retry_after_s=60.0,
            )
        if response.status_code in (401, 403):
            self._last_error = f"HTTP {response.status_code} for {platform}"
            if self.mode == "official":
                raise AuthenticationError(
                    f"adanos rejected the API key (HTTP {response.status_code}) for the "
                    f"{platform} feed -- check the {self.config.api_key_credential} credential",
                    provider=self.name,
                )
            raise SourceBlockedError(
                f"adanos site endpoint answered HTTP {response.status_code} for the {platform} "
                "feed -- unexpected for a keyless page-proxy endpoint, treated as a block signal",
                provider=self.name,
            )
        if response.status_code == 404 and self.mode == "official":
            # The official Polymarket path is inferred, not confirmed live
            # (see the module docstring) -- a 404 there degrades cleanly,
            # same as any other single-feed failure, rather than treating an
            # unconfirmed endpoint shape as a hard outage.
            self._last_error = f"HTTP 404 for {platform} (official)"
            raise ProviderError(
                f"adanos official API returned 404 for the {platform} feed; endpoint shape may "
                "differ from the inferred pattern",
                provider=self.name,
            )
        if response.status_code >= 500:
            self._last_error = f"HTTP {response.status_code} for {platform}"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for the {platform} feed",
                provider=self.name,
                retryable=True,
            )
        if response.status_code >= 400:
            self._last_error = f"HTTP {response.status_code} for {platform}"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for the {platform} feed",
                provider=self.name,
            )

        try:
            payload = response.json()
        except Exception as exc:
            self._last_error = f"non-JSON response for {platform}"
            raise SourceBlockedError(
                f"adanos returned a non-JSON body for the {platform} feed -- unexpected for a "
                "documented JSON API, treated as a possible block/challenge response",
                provider=self.name,
            ) from exc

        with self._lock:
            self._last_success = utc_now()
        return payload


def _extract_rows(payload: Any) -> list | None:
    """The documented shape is a bare JSON array; tolerate a
    ``{"results"/"data"/"rows": [...]}`` envelope too, ``None`` otherwise."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _parse_feed_rows(payload: Any, *, platform: str, observed_at: dt.datetime) -> list[AdanosSnapshot]:
    """Convert one feed's response into snapshot rows.

    Unlike ``apewisdom._parse_results`` (which returns ``[]`` for any
    unusable payload), a top-level shape the vendor never documented is
    treated as parse-drift and raises ``ProviderError`` -- Adanos is a
    documented, versioned API, so a shape change here is worth surfacing as a
    failure rather than silently degrading to no data. Individual malformed
    or junk-ticker *rows* within an otherwise well-shaped response are still
    skipped rather than fatal, same posture as ApeWisdom.
    """
    rows = _extract_rows(payload)
    if rows is None:
        raise ProviderError(
            f"adanos returned an unexpected payload shape for the {platform} feed",
            provider="adanos",
        )

    count_field = _COUNT_FIELD[platform]
    engagement_field = _ENGAGEMENT_FIELD[platform]
    out: list[AdanosSnapshot] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("ticker") or "").strip().upper()
        # Same non-equity filter as ApeWisdom: Adanos's own site aggregates
        # crypto tickers alongside equities, and this application screens US
        # equities only. Symbols unknown to the universe are dropped later by
        # the securities join in ``data.ingest.DataIngestor.ingest_adanos``.
        if not symbol or symbol in _NON_EQUITY:
            continue
        out.append(
            AdanosSnapshot(
                symbol=symbol,
                platform=platform,
                company_name=str(entry.get("company_name") or "").strip(),
                buzz_score=_as_float(entry.get("buzz_score")) or 0.0,
                mentions=_as_int(entry.get(count_field)) or 0,
                trend=str(entry.get("trend") or ""),
                sentiment_score=_as_float(entry.get("sentiment_score")),
                bullish_pct=_as_float(entry.get("bullish_pct")),
                bearish_pct=_as_float(entry.get("bearish_pct")),
                engagement=_as_float(entry.get(engagement_field)) or 0.0,
                trend_history=_as_float_list(entry.get("trend_history")),
                observed_at=observed_at,
            )
        )
    return out


def _as_int(value: Any) -> int | None:
    """Coerce an API field to ``int``; ``None`` when it isn't a number.

    Same posture as ``apewisdom._as_int``: counts may arrive as JSON numbers
    or numeric strings depending on endpoint version.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a count
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    """Coerce an API field to ``float``; ``None`` when it isn't a number or
    is explicitly ``null`` (Adanos reports ``sentiment_score`` as ``null``
    for a row with no measurable polarity -- that must stay absent, not
    become a fabricated 0.0)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_float_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        parsed = _as_float(item)
        if parsed is not None:
            out.append(parsed)
    return out


__all__ = ["AdanosProvider"]
