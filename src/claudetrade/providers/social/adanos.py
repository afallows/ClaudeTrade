"""Adanos (``adanos.org``) pre-aggregated buzz and sentiment across X,
Reddit, Polymarket and financial news.

Same family as ``providers.social.apewisdom``: a hosted aggregator that
serves the finished per-ticker tally rather than individual posts. Adanos is
richer than ApeWisdom in one load-bearing way -- it reports real polarity
(``sentiment_score``, ``bullish_pct``, ``bearish_pct``) alongside volume, and
does so across four separate feeds, refreshed hourly.

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
    GET {site_base_url}/proxy-news/trending?limit=N&from=YYYY-MM-DD&to=YYYY-MM-DD

Official authenticated API (header ``X-API-Key: sk_live_...``), same row
shapes::

    GET {official_base_url}/x/stocks/v1/trending
    GET {official_base_url}/reddit/stocks/v1/trending
    GET {official_base_url}/polymarket/stocks/v1/trending
    GET {official_base_url}/news/stocks/v1/trending

The Polymarket and news *official* paths are inferred from the X/Reddit
pattern, not independently confirmed -- if either 404s at runtime, only that
one feed degrades (see ``_get_json``'s 404-in-official-mode handling); the X
and Reddit feeds are unaffected. The news *site* endpoint (``proxy-news``)
was itself confirmed live 2026-08-02.

News rows have one further difference worth flagging: they carry
``source_count`` (distinct news outlets reporting on the ticker) instead of
an engagement total -- there is no upvotes/likes/liquidity analogue for a
news aggregation. ``source_count`` is stored through the same
``AdanosSnapshot.engagement`` field the other three feeds use for their own
per-platform engagement number (see ``_ENGAGEMENT_FIELD`` below and
``domain.AdanosSnapshot.engagement``'s docstring) -- one more platform-
specific meaning for a column that already does double duty, not a fourth
column, since the value is honestly a single float either way.

**Two access modes.** Default is keyless "site" mode: one request per
enabled feed per collection cycle, honest User-Agent, paced by the shared
``providers.base.RateLimiter`` -- the same courtesy-budget class as
ApeWisdom's few calls/cycle. "Official" mode activates only when BOTH
``config.prefer_official_api`` is true AND ``config.api_key_credential``
resolves to a real key; it is gated by a persistent monthly budget (see
``_MonthlyBudgetStore``) that fails closed once the reserve floor is reached
rather than ever falling back to hammering the site proxy harder than its
normal one-request-per-feed-per-cycle cadence.

**Hybrid mode -- funding the free tier without burning it (owner's explicit
requirement: "I want to utilize the free tier and then use the official api
ALONG with the site mode").** Trending collection (``fetch_snapshots``)
NEVER spends the official-API budget unless ``config.prefer_official_api``
is true: the site endpoints serve the exact same trending rows keylessly, so
there is no reason to ever pay for them. Separately, and regardless of
``prefer_official_api``, whenever ``config.api_key_credential`` resolves to
a real key this provider ALSO exposes two on-demand, per-ticker official
calls that only make sense with a key at all --
:meth:`AdanosProvider.fetch_stock_detail` (full per-ticker detail: daily
trend, sentiment breakdown, top mentions/authors) and
:meth:`AdanosProvider.fetch_explain` (the vendor's AI trend explanation,
cached 6h server-side). Both are ALWAYS budget-guarded through the same
``_MonthlyBudgetStore`` trending's official mode uses -- the free tier's
~250 requests/month is meant to fund exactly this on-demand research use,
not bulk collection the site proxy already covers for free, and both refuse
with a structured, non-raising ``{"accepted": false, "reason": ...}`` payload
(naming the reset date) rather than ever silently degrading to an
unmetered call. See ``config.AdanosConfig.enrich_top_candidates`` /
``enrich_enabled`` for the one automatic consumer of this budget (bounded
top-candidate enrichment after a scan, wired in ``pipeline.Pipeline
._enrich_adanos_top_candidates``) and :meth:`AdanosProvider.budget_status`
for the read-only, free status surfaced by ``mcp_server.get_adanos_budget``.

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
    "news": "proxy-news/trending",
}

#: Official-API path segments, relative to ``config.official_base_url``.
_OFFICIAL_PATHS = {
    "x": "x/stocks/v1/trending",
    "reddit": "reddit/stocks/v1/trending",
    "polymarket": "polymarket/stocks/v1/trending",
    "news": "news/stocks/v1/trending",
}

#: Which row field carries the "how many times" count -- x/reddit/news share
#: ``mentions``, polymarket reports ``trade_count`` instead.
_COUNT_FIELD = {"x": "mentions", "reddit": "mentions", "polymarket": "trade_count", "news": "mentions"}

#: Which row field carries the engagement total -- x/reddit report
#: ``total_upvotes``, polymarket reports ``total_liquidity``, and news
#: reports ``source_count`` (distinct outlets, its nearest analogue to an
#: engagement number) instead.
_ENGAGEMENT_FIELD = {
    "x": "total_upvotes",
    "reddit": "total_upvotes",
    "polymarket": "total_liquidity",
    "news": "source_count",
}


def _stock_base(platform: str) -> str:
    """The official per-platform base path for on-demand stock endpoints
    (``{base}/stock/{ticker}``, ``.../stock/{ticker}/explain``), derived
    from ``_OFFICIAL_PATHS`` by dropping its ``/trending`` suffix -- e.g.
    ``x/stocks/v1/trending`` -> ``x/stocks/v1`` -- since the vendor shares
    one base path across every endpoint in a platform's family (trending,
    stock detail, explain; see ``docs/adanos-api-reference.md``)."""
    return _OFFICIAL_PATHS[platform].rsplit("/trending", 1)[0]


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


def _next_month_reset_date(now: dt.datetime | None = None) -> str:
    """ISO date of the 1st of next calendar month -- the reset date named in
    an on-demand budget-exhausted refusal (see ``AdanosProvider
    ._budget_refusal_reason``/``budget_status``'s ``resets_hint``). Note the
    vendor's OWN quota window resets per account, not per calendar month
    (``docs/adanos-api-reference.md``); this is an approximation consistent
    with how ``_MonthlyBudgetStore`` already keys its local counter by
    calendar month, not a claim about the vendor's actual reset instant."""
    current = now or utc_now()
    if current.month == 12:
        nxt = current.replace(year=current.year + 1, month=1, day=1)
    else:
        nxt = current.replace(month=current.month + 1, day=1)
    return nxt.date().isoformat()


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
        #: Resolved once at construction, and now UNCONDITIONALLY (mirrors
        #: ``providers.market.polygon.PolygonProvider``): a provider instance
        #: lives for one run, so a credential change means a new process.
        #: Unlike before hybrid mode, this no longer depends on
        #: ``config.prefer_official_api`` -- on-demand detail/explain calls
        #: (see the module docstring's Hybrid mode section) are available
        #: whenever a key resolves at all, independent of which mode
        #: trending collection uses.
        self._api_key = self._resolve_api_key()
        self.mode = "official" if (config.prefer_official_api and self._api_key) else "site"
        #: Root for this installation's on-disk caches (budget state below,
        #: and the top-candidate detail-enrichment cache under
        #: ``adanos/detail/`` -- see ``enrich_top_candidates``). ``None``
        #: only in tests that construct a provider with no ``cache_dir``.
        self._cache_dir = Path(cache_dir) if cache_dir else None
        budget_path = (self._cache_dir / "adanos" / "monthly_budget.json") if self._cache_dir else None
        self._budget = _MonthlyBudgetStore(budget_path)
        self._lock = threading.Lock()
        self._calls = 0
        self._last_error: str | None = None
        self._last_success: dt.datetime | None = None
        #: Per-feed failure messages from the most recent ``fetch_snapshots``
        #: call, keyed ``"adanos_x"``/``"adanos_reddit"``/``"adanos_polymarket"``/
        #: ``"adanos_news"`` -- read by ``data.ingest.DataIngestor.ingest_adanos`` and folded
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
        if self.config.feed_news:
            platforms.append("news")
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
                "news": self.config.feed_news,
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

    # --- hybrid mode: on-demand official calls ------------------------------
    #
    # Independent of ``self.mode`` (the trending-collection selector above):
    # these methods are available whenever ``self._api_key`` resolves at
    # all, regardless of ``config.prefer_official_api``. See the module
    # docstring's Hybrid mode section -- the owner's explicit requirement is
    # that bulk trending never spends the free tier, while on-demand
    # per-ticker research always can, budget permitting.

    def budget_status(self) -> dict[str, Any]:
        """Read-only, free -- calling this never counts against the budget.

        Current official-API monthly budget state plus whether a key
        resolves at all on this installation. Surfaced by
        ``mcp_server.get_adanos_budget`` and folded into every
        ``fetch_stock_detail``/``fetch_explain`` response (success or
        refusal alike) so a caller always knows how much quota is left.
        """
        used, month = self._budget.snapshot()
        return {
            "key_resolved": self._api_key is not None,
            "budget": self.config.monthly_budget,
            "used": used,
            "remaining": max(0, self.config.monthly_budget - used),
            "reserve": self.config.monthly_reserve,
            "month": month,
            "resets_hint": _next_month_reset_date(),
        }

    def _budget_refusal_reason(self) -> str | None:
        """``None`` when there is budget to spend one more on-demand call;
        otherwise the refusal message naming the reset date -- the same
        reserve-floor rule ``_enforce_budget_or_raise`` applies to trending's
        official mode, applied here independent of ``self.mode``."""
        used, month = self._budget.snapshot()
        remaining = self.config.monthly_budget - used
        if remaining <= self.config.monthly_reserve:
            return (
                f"adanos official-API monthly budget exhausted (used {used}/"
                f"{self.config.monthly_budget}, reserve {self.config.monthly_reserve}) for "
                f"{month}; resets {_next_month_reset_date()}"
            )
        return None

    def _refusal(self, symbol: str, platform: str, reason: str) -> dict[str, Any]:
        """The structured, ``accepted: false`` shape ``fetch_stock_detail``/
        ``fetch_explain`` return (never raise) for the two EXPECTED refusal
        cases -- no key, or budget at the reserve floor -- so a caller (an
        MCP client, or ``enrich_top_candidates`` below) can branch on
        ``accepted`` without a try/except. A genuine HTTP-level failure
        (401/403/429/404/5xx) still raises a typed ``ProviderError``
        subclass, same taxonomy as ``_get_json``/``fetch_snapshots``, since
        that is unexpected rather than an ordinary, budget-aware refusal."""
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": reason,
            "budget": self.budget_status(),
        }

    def _detail_cache_path(self, symbol: str, session: dt.date) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / "adanos" / "detail" / f"{symbol}-{session.isoformat()}.json"

    def cached_detail(
        self, symbol: str, session: dt.date, *, platform: str | None = None
    ) -> dict[str, Any] | None:
        """A same-session top-candidate enrichment cache entry for
        ``symbol``, or ``None`` when absent, unreadable, or written for a
        different platform than requested. Reading this NEVER spends quota
        -- it is what lets ``mcp_server.get_adanos_detail`` answer "cached
        from enrichment, no quota spent" for a symbol ``Pipeline.scan``
        already enriched this session (see ``enrich_top_candidates``
        below)."""
        path = self._detail_cache_path(symbol.strip().upper(), session)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if platform is not None and data.get("platform") != platform.strip().lower():
            return None
        return data

    def _send_request(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        """The network send shared by trending's ``_get_json`` and the
        on-demand ``_ondemand_request`` below -- rate-limited, no retries,
        wrapped into ``ProviderError`` on a transport failure. Everything
        status-code-specific is left to the caller, since trending and
        on-demand calls read different meanings into the same HTTP codes
        (e.g. site-mode 401 is a block signal, official-mode 401 is a
        credential error)."""
        self._limiter.acquire()
        try:
            if self._client is not None:
                return self._client.get(url, headers=headers, timeout=self.config.request_timeout_s)
            with httpx.Client(
                timeout=self.config.request_timeout_s, follow_redirects=True, headers=headers
            ) as client:
                return client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"adanos request failed: {exc}", provider=self.name, retryable=True
            ) from exc

    def _ondemand_request(
        self, url: str, *, headers: dict[str, str], platform: str, ticker: str, kind: str
    ) -> Any:
        """One on-demand official call (stock detail / explain) -- ALWAYS
        official regardless of ``self.mode``, since these endpoints have no
        keyless site-proxy equivalent (only the four trending endpoints do
        -- see ``docs/adanos-api-reference.md``). The caller must already
        have confirmed there is budget to spend (``_budget_refusal_reason``)
        -- this only sends the request, records it against the monthly
        budget, and translates the response. No retries."""
        try:
            response = self._send_request(url, headers=headers)
        except ProviderError:
            self._last_error = f"network error for {kind} ({ticker}/{platform})"
            raise

        with self._lock:
            self._calls += 1
        self._record_official_call(response)

        if response.status_code == 401:
            self._last_error = f"HTTP 401 for {kind} ({ticker}/{platform})"
            raise AuthenticationError(
                f"adanos rejected the API key (HTTP 401) for {kind} of {ticker} -- check the "
                f"{self.config.api_key_credential} credential",
                provider=self.name,
            )
        if response.status_code == 403:
            self._last_error = f"HTTP 403 for {kind} ({ticker}/{platform})"
            raise ProviderError(
                f"adanos refused {kind} for {ticker}: the requested history window exceeds "
                "this account's tier depth (HTTP 403) -- request a shorter from/to range or "
                "upgrade the tier",
                provider=self.name,
            )
        if response.status_code == 429:
            self._last_error = f"HTTP 429 for {kind} ({ticker}/{platform})"
            raise RateLimitError(
                f"adanos burst rate limit reached for {kind} of {ticker}",
                provider=self.name,
                retry_after_s=60.0,
            )
        if response.status_code == 404:
            self._last_error = f"HTTP 404 for {kind} ({ticker}/{platform})"
            raise ProviderError(
                f"adanos has no {kind} for {ticker} on {platform} (HTTP 404) -- unsupported "
                "or unknown ticker",
                provider=self.name,
            )
        if response.status_code >= 500:
            self._last_error = f"HTTP {response.status_code} for {kind} ({ticker}/{platform})"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for {kind} of {ticker}",
                provider=self.name,
                retryable=True,
            )
        if response.status_code >= 400:
            self._last_error = f"HTTP {response.status_code} for {kind} ({ticker}/{platform})"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for {kind} of {ticker}",
                provider=self.name,
            )

        try:
            payload = response.json()
        except Exception as exc:
            self._last_error = f"non-JSON response for {kind} ({ticker}/{platform})"
            raise SourceBlockedError(
                f"adanos returned a non-JSON body for {kind} of {ticker} -- unexpected for a "
                "documented JSON API, treated as a possible block/challenge response",
                provider=self.name,
            ) from exc

        with self._lock:
            self._last_success = utc_now()
        return payload

    def fetch_stock_detail(
        self,
        ticker: str,
        platform: str = "x",
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """One ticker's full official-API detail: daily trend, sentiment
        breakdown, top mentions/authors/subreddits/tweets -- whatever the
        vendor's ``GET /stock/{ticker}`` returns for ``platform``, parsed
        defensively (missing keys tolerated, nothing invented) and passed
        through under ``raw``, alongside a normalized header block
        (``buzz_score``/``sentiment_score``/``bullish_pct``/``bearish_pct``/
        ``mentions``). **Spends one official-API request** -- always
        budget-guarded (``_budget_refusal_reason``), regardless of
        ``self.mode``: this is the on-demand half of hybrid mode, available
        whenever ``self._api_key`` resolves at all (see the module
        docstring). Returns a structured ``{"accepted": false, "reason":
        ...}`` refusal (never raises) when no key resolves or the budget is
        at its reserve floor; raises the usual ``ProviderError`` taxonomy
        for a genuine HTTP failure (401/403/429/404/5xx).
        """
        symbol = ticker.strip().upper()
        platform = platform.strip().lower()
        if platform not in _OFFICIAL_PATHS:
            return self._refusal(
                symbol,
                platform,
                f"unsupported platform '{platform}' -- expected one of {sorted(_OFFICIAL_PATHS)}",
            )
        if self._api_key is None:
            return self._refusal(
                symbol,
                platform,
                "no adanos_api_key credential resolves on this installation -- on-demand "
                "detail spends the official-API quota and requires a key even though bulk "
                "trending collection never does",
            )
        reason = self._budget_refusal_reason()
        if reason is not None:
            return self._refusal(symbol, platform, reason)

        params = []
        if from_date:
            params.append(f"from={from_date}")
        if to_date:
            params.append(f"to={to_date}")
        query = ("?" + "&".join(params)) if params else ""
        base = self.config.official_base_url.rstrip("/")
        url = f"{base}/{_stock_base(platform)}/stock/{symbol}{query}"
        headers = {
            "X-API-Key": self._api_key,
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        payload = self._ondemand_request(
            url, headers=headers, platform=platform, ticker=symbol, kind="stock detail"
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "accepted": True,
            "symbol": symbol,
            "platform": platform,
            "fetched_at": utc_now().isoformat(),
            "buzz_score": _as_float(data.get("buzz_score")),
            "sentiment_score": _as_float(data.get("sentiment_score")),
            "bullish_pct": _as_float(data.get("bullish_pct")),
            "bearish_pct": _as_float(data.get("bearish_pct")),
            "mentions": _as_int(data.get(_COUNT_FIELD.get(platform, "mentions"))),
            "raw": data,
            "budget": self.budget_status(),
        }

    def fetch_explain(self, ticker: str, platform: str = "x") -> dict[str, Any]:
        """The vendor's AI trend explanation (llama-3.1-8b, cached
        server-side for 6h) for one ticker. **Spends one official-API
        request** -- even a cache hit on the VENDOR's side still spends
        this installation's quota, since the vendor still counts the call;
        the returned ``cached``/``generated_at`` fields say whether the text
        was freshly generated or served from the vendor's own cache. Same
        budget-guard/refusal/error-taxonomy contract as
        ``fetch_stock_detail``.
        """
        symbol = ticker.strip().upper()
        platform = platform.strip().lower()
        if platform not in _OFFICIAL_PATHS:
            return self._refusal(
                symbol,
                platform,
                f"unsupported platform '{platform}' -- expected one of {sorted(_OFFICIAL_PATHS)}",
            )
        if self._api_key is None:
            return self._refusal(
                symbol,
                platform,
                "no adanos_api_key credential resolves on this installation -- on-demand "
                "explain spends the official-API quota and requires a key even though bulk "
                "trending collection never does",
            )
        reason = self._budget_refusal_reason()
        if reason is not None:
            return self._refusal(symbol, platform, reason)

        base = self.config.official_base_url.rstrip("/")
        url = f"{base}/{_stock_base(platform)}/stock/{symbol}/explain"
        headers = {
            "X-API-Key": self._api_key,
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        payload = self._ondemand_request(
            url, headers=headers, platform=platform, ticker=symbol, kind="explain"
        )
        data = payload if isinstance(payload, dict) else {}
        return {
            "accepted": True,
            "symbol": symbol,
            "platform": platform,
            "explanation": data.get("explanation"),
            "cached": data.get("cached"),
            "generated_at": data.get("generated_at"),
            "budget": self.budget_status(),
        }

    def enrich_top_candidates(self, symbols: list[str], *, session: dt.date) -> int:
        """Bounded, best-effort post-scan enrichment: up to
        ``config.enrich_top_candidates`` of ``symbols`` (already ordered
        best-first and de-duplicated by the caller -- see
        ``pipeline.Pipeline._enrich_adanos_top_candidates``), ONE
        ``fetch_stock_detail`` call each on ``config.detail_platform_default``,
        cached to ``cache_dir/adanos/detail/{symbol}-{session}.json``.

        Effective only when ``config.enrich_enabled`` and an
        ``adanos_api_key`` credential resolves -- bulk trending collection is
        never affected either way. Skips (INFO log, not an error) a symbol
        already cached for this session -- the whole point of the cache is
        that a re-scan of the same session, or a later
        ``AdanosProvider.cached_detail``/``mcp_server.get_adanos_detail``
        call for the same symbol, must not spend quota twice. Stops early,
        rather than skipping one-by-one, once the budget guard refuses --
        the remaining symbols are simply not enriched this session.

        **Never raises.** A scan must never fail because enrichment
        degraded; any per-symbol failure (network, vendor error, disk) is
        logged and the loop continues to the next symbol. Returns the number
        of NEW official calls actually made (i.e. quota actually spent), for
        logging/tests.
        """
        if self._cache_dir is None:
            log.info("adanos enrichment skipped: no cache_dir configured")
            return 0
        if self._api_key is None:
            log.info("adanos enrichment skipped: no adanos_api_key credential resolves")
            return 0
        if not self.config.enrich_enabled or self.config.enrich_top_candidates <= 0:
            return 0

        platform = self.config.detail_platform_default
        spent = 0
        try:
            for raw_symbol in symbols[: self.config.enrich_top_candidates]:
                symbol = raw_symbol.strip().upper()
                if not symbol:
                    continue
                path = self._detail_cache_path(symbol, session)
                if path is None:
                    continue
                if path.exists():
                    log.info(
                        "adanos enrichment: %s already cached for session %s; skipping",
                        symbol,
                        session,
                    )
                    continue
                reason = self._budget_refusal_reason()
                if reason is not None:
                    log.info("adanos enrichment: budget-guarded, stopping early (%s)", reason)
                    break
                try:
                    result = self.fetch_stock_detail(symbol, platform=platform)
                except ProviderError as exc:
                    log.info("adanos enrichment: %s failed (%s); skipping", symbol, exc)
                    continue
                if not result.get("accepted", True):
                    log.info(
                        "adanos enrichment: %s refused (%s)", symbol, result.get("reason")
                    )
                    continue
                result["enriched_at_session"] = session.isoformat()
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(result), encoding="utf-8")
                except OSError:
                    log.debug(
                        "adanos enrichment: failed to persist cache for %s",
                        symbol,
                        exc_info=True,
                    )
                    continue
                spent += 1
        except Exception:  # pragma: no cover - defensive, see docstring
            log.warning(
                "adanos enrichment raised unexpectedly; scan is unaffected", exc_info=True
            )
        return spent


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
