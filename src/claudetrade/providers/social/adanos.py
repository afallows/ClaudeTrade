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

**The site proxy also mirrors the official API's per-ticker/service routes**
(confirmed live 2026-08-03 by both the owner and a follow-up TSLA probe),
not just ``/trending``::

    GET {site_base_url}/{proxy_base}/stock/{ticker}?days=N
    GET {site_base_url}/{proxy_base}/stock/{ticker}/explain
    GET {site_base_url}/{proxy_base}/market-sentiment

where ``proxy_base`` is the same per-platform segment ``_SITE_PATHS`` uses
for trending (``proxy-x``, ``proxy``, ``proxy-polymarket``, ``proxy-news``).
Verified live: ``/stock`` and ``/market-sentiment`` on all FOUR bases;
``/stock/.../explain`` on ``proxy``/``proxy-x``/``proxy-news`` only --
Polymarket's explain route is CONFIRMED ABSENT (``{"error": "Not found"}``,
official API only), not merely unconfirmed, so ``_site_explain`` skips the
network round-trip for that one platform+kind combination rather than
probing a route known not to exist. ``/compare`` is NOT proxied either
(``{"error": "Not found"}``) -- official API only, not wired here. See
``docs/adanos-api-reference.md`` for the full verified/unverified table.

``/stock/{ticker}`` returns a COMMON header (``ticker``, ``company_name``,
``found``, ``buzz_score``, ``sentiment_score``, ``positive_count``/
``negative_count``/``neutral_count``, ``trend``, ``bullish_pct``/
``bearish_pct``, ``period_days``, ``daily_trend`` -- a list of per-day
``{date, <activity>, sentiment_score, buzz_score, bullish_pct,
bearish_pct}`` objects) plus per-base extras: X adds ``mentions``/
``unique_tweets``/``total_upvotes`` and ``top_tweets`` (``text_snippet``,
``sentiment_score``); Reddit adds ``mentions``/``unique_posts``/
``subreddit_count``/``total_upvotes`` and ``top_subreddits``; News adds
``mentions``/``source_count`` (no engagement field) and ``top_sources``
plus ``top_mentions`` (``text_snippet``); Polymarket adds ``trade_count``/
``market_count``/``current_market_count``/``unique_traders``/
``total_liquidity``. ``daily_trend``'s per-day activity key follows the
same split (``mentions`` for X/Reddit/News, ``trade_count`` for
Polymarket) -- and its length is NOT guaranteed to be 7: a quiet day can be
omitted (observed on News), so nothing here assumes a fixed window length.
``/market-sentiment`` follows the same common-header-plus-per-base-extras
pattern, with ``trend_history`` (7 entries) and ``drivers`` (top 5,
``{ticker, buzz_score, sentiment_score}`` plus the per-base activity field)
replacing ``daily_trend``.

Two response shapes matter for these on-demand routes, and they are NOT the
same signal (see ``_is_structured_vendor_answer``):

* ``{"found": false, ...}`` (HTTP 200) means the vendor recognises and
  tracks this ticker but has no data for it in the requested window -- a
  normal, structured "no data" answer, not a failure.
* ``{"detail": {"error_code": "unsupported_ticker", "message": ...}}``
  (observed under HTTP 404, but the body is checked regardless of status)
  means the vendor does not track this ticker AT ALL -- a different,
  equally normal "unsupported ticker" answer.

Both end the ladder (see below) at whichever rung produced them; the other
rung is not tried, since the vendor has already given a definitive answer.
By contrast, ``{"error": "...", ...}`` (HTTP 200 *or* a non-2xx status,
and lacking both ``found`` and a ``detail.error_code``) means the ROUTE
ITSELF does not exist on that proxy base -- endpoint-absent, not data. This
IS a rung failure and triggers the ladder's fallback.

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

**Hybrid mode -- site-first on-demand calls, official as fallback (revised
2026-08-03: the site proxy turned out to mirror the on-demand routes too,
not just trending).** Trending collection (``fetch_snapshots``) NEVER spends
the official-API budget unless ``config.prefer_official_api`` is true: the
site endpoints serve the exact same trending rows keylessly, so there is no
reason to ever pay for them -- this part is unchanged.

The three on-demand calls --
:meth:`AdanosProvider.fetch_stock_detail` (full per-ticker detail: daily
trend, sentiment breakdown, top mentions/authors),
:meth:`AdanosProvider.fetch_explain` (the vendor's AI trend explanation,
cached 6h server-side), and
:meth:`AdanosProvider.fetch_market_sentiment` (service-level buzz/sentiment
snapshot with top momentum drivers, not per-ticker) -- now run the SAME
two-rung ladder as trending, independent of ``self.mode``:

1. **Site rung first** (free, no budget touch) -- the mirrored
   ``{proxy_base}/stock/{ticker}``, ``.../explain``, ``.../market-sentiment``
   routes described above. No API key required.
2. **Official rung as fallback**, tried only when the site rung fails (HTTP
   error, block, an ``{"error": ...}`` body, schema drift -- see the two
   response shapes above) AND ``config.api_key_credential`` resolves to a
   real key AND the monthly budget has room. Budget-guarded exactly like
   before (see ``_MonthlyBudgetStore`` / ``_budget_refusal_reason``): fails
   closed at the reserve floor rather than ever making extra site calls to
   compensate.

``config.prefer_official_api = true`` reverses the rung order for these
three calls too (official first, site fallback) -- consistent with what it
already means for trending. If BOTH rungs fail (or the only available rung
fails), the method returns a structured, non-raising
``{"accepted": false, "reason": ...}`` refusal naming both failures -- these
methods no longer raise ``ProviderError`` for an ordinary HTTP failure, since
the whole point of the ladder is that one rung's failure is not fatal.
Either structured vendor "no" (``{"found": false}`` or ``{"detail":
{"error_code": "unsupported_ticker", ...}}`` -- see above) from either rung
is handled as its own structured, non-raising refusal (``accepted: false``
with a reason distinguishing the two cases), not an exception and not a
trigger to try the other rung.

Every envelope (success, structured "no", or both-rungs-failed refusal) now
carries ``mode: "site" | "official" | None`` (which rung actually answered;
``None`` only in the both-failed case) and ``quota_spent: bool`` (whether an
official request actually reached the vendor this call -- ``False`` for a
site-mode answer, and also ``False`` when the official rung was
pre-emptively skipped for lacking a key or budget) alongside the existing
``budget`` block (present regardless of mode, so a caller -- the MCP layer
in particular -- can always report remaining budget).

**Net effect: normal operation (no key configured, or a key configured but
the site rung healthy) never spends the official-API budget for on-demand
calls either.** The free tier's ~250 requests/month is now purely a
fallback/durability reserve for when the site proxy is down or rate-limited,
not the primary path -- see ``docs/api-providers.md``'s "Hybrid mode /
spending the free tier" for the revised budget arithmetic. See
``config.AdanosConfig.enrich_scope`` / ``enrich_top_candidates`` /
``enrich_max_symbols_per_scan`` / ``enrich_delay_seconds`` / ``enrich_enabled``
for the one automatic consumer of this ladder --
:meth:`AdanosProvider.enrich_candidates`, wired in
``pipeline.Pipeline._enrich_adanos_candidates`` after every scan. Default
scope (``"all"``) enriches every distinct signal symbol, best-scoring first,
capped by ``enrich_max_symbols_per_scan`` and paced by
``enrich_delay_seconds`` between actual network calls; ``"top_n"`` restores
the previous top-N-only behaviour. A symbol the vendor definitively refuses
as unsupported (``error_code: "unsupported_ticker"``, never the quieter
``{"found": false}``) is memoised for a 30-trading-day re-probe horizon (see
``_UnsupportedTickerStore``) so a scan never re-asks about it every day.
This, too, now runs keylessly via the site rung by default, and each
successful enrichment also feeds ``db.models.AdanosSnapshotRow`` (via the
``on_snapshot`` callback the pipeline supplies), reusing the exact same
``(session, platform, symbol)`` upsert path ``data.ingest.DataIngestor
.ingest_adanos`` uses for trending -- so a symbol outside the top-100
trending feeds still shows up in the Screener's attention columns. See
:meth:`AdanosProvider.budget_status` for the read-only, free status surfaced
by ``mcp_server.get_adanos_budget``.

**Text sanitisation.** ``fetch_stock_detail``'s ``raw`` passthrough may
include third-party text snippets (``top_tweets``/``top_mentions`` etc.,
under a ``text``/``text_snippet``/``snippet`` key, platform-dependent). These
are untrusted external text same as any Reddit/X post body -- see
``utils.text.sanitize_social_text`` -- and are run through that same
sanitiser (URL/email/phone/username stripping, prompt-injection
neutralisation) before being returned; every other field in ``raw`` passes
through untouched.

**Licensing** (adanos.org/terms, checked 2026-08-02): commercial use is
permitted subject to the vendor's terms; raw API data may not be
redistributed as a competing service, and rate limits may not be
circumvented. Site-proxy mode is the vendor's own public page endpoint, used
at page-equivalent cadence; an API key is the guaranteed-compliant path and
is preferred whenever one is configured.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable
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
from claudetrade.utils.text import sanitize_social_text
from claudetrade.utils.timeutils import next_trading_day, utc_now

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
_COUNT_FIELD = {
    "x": "mentions",
    "reddit": "mentions",
    "polymarket": "trade_count",
    "news": "mentions",
}

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


def _site_base(platform: str) -> str:
    """The site-proxy per-platform base path for on-demand routes (mirrors
    ``_stock_base`` but for ``site_base_url``) -- e.g. ``proxy-x/trending``
    -> ``proxy-x`` -- since the vendor's proxy shares the same base segment
    across trending, stock detail, explain and market-sentiment for a given
    platform (see the module docstring's verified facts)."""
    return _SITE_PATHS[platform].rsplit("/trending", 1)[0]


def _days_param(from_date: str | None, to_date: str | None) -> int | None:
    """Best-effort translation of an explicit ``from``/``to`` range into the
    site proxy's ``days``-back window parameter -- the mirrored on-demand
    endpoint takes ``days``, not ``from``/``to`` like the official API and
    like this provider's own trending requests (see the module docstring's
    verified facts). Returns ``None`` (the vendor's own default, observed as
    7) when either bound is missing or unparseable -- a bare ``to_date`` with
    no ``from_date`` (or vice versa) is not enough information to compute a
    day count, so this falls back to the default rather than guessing."""
    if not from_date or not to_date:
        return None
    try:
        start = dt.date.fromisoformat(from_date)
        end = dt.date.fromisoformat(to_date)
    except ValueError:
        return None
    days = (end - start).days
    return days if days > 0 else None


def _is_structured_vendor_answer(payload: dict[str, Any]) -> bool:
    """``True`` for either shape the site proxy uses to give a definitive
    "no" for an on-demand lookup, observed live 2026-08-03 -- distinct from
    an endpoint-absent ``{"error": ...}`` body, and NOT a rung failure (see
    ``AdanosProvider._site_get``/the module docstring):

    * ``{"found": false, ...}`` -- a ticker the vendor tracks, but with no
      data in the requested window.
    * ``{"detail": {"error_code": "unsupported_ticker", ...}}`` -- a ticker
      the vendor does not track at all.
    """
    if payload.get("found") is False:
        return True
    detail = payload.get("detail")
    return isinstance(detail, dict) and detail.get("error_code") == "unsupported_ticker"


#: Keys under which a vendor detail payload may carry untrusted third-party
#: text (``top_tweets``/``top_mentions``/``top_posts``/``top_articles`` etc,
#: platform-dependent field name) -- these get the same sanitisation applied
#: to any other Reddit/X post text (see ``utils.text.sanitize_social_text``)
#: before being returned under ``raw``. Every other field is vendor-computed
#: numbers/labels and passes through untouched.
_SNIPPET_KEYS = frozenset({"text", "text_snippet", "snippet"})


def _sanitize_raw_snippets(value: Any) -> Any:
    """Recursively sanitise any string found under a known snippet-bearing
    key inside a vendor on-demand payload passed through under ``raw``.

    Untrusted third-party text (a tweet, a Reddit post, a news headline)
    reaching a downstream prompt unsanitised is exactly the failure mode
    ``utils.text.sanitize_social_text`` exists to prevent for
    ``SocialPost`` rows elsewhere in this codebase; Adanos's own
    ``top_tweets``/``top_mentions``-style snippets are no more trustworthy
    just because they arrived pre-aggregated. Nothing here invents or drops
    a vendor-computed number -- only string values under one of
    ``_SNIPPET_KEYS`` are rewritten, everything else (including strings under
    other keys, e.g. ``trend``) passes through unchanged.
    """
    if isinstance(value, dict):
        return {
            key: sanitize_social_text(item)
            if key in _SNIPPET_KEYS and isinstance(item, str)
            else _sanitize_raw_snippets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_raw_snippets(item) for item in value]
    return value


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
        nxt = current.replace(
            month=current.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
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


class _UnsupportedTickerStore:
    """Persists symbols the vendor has told us, DEFINITIVELY, it does not
    track at all -- an ``unsupported_ticker`` refusal (see
    ``_structured_answer_reason``), never the quieter ``{"found": false}``
    "tracked but no data right now" answer, which can flip hourly and is
    deliberately NOT memoised here (see ``AdanosProvider.enrich_candidates``).

    Same persistence posture as ``_MonthlyBudgetStore`` immediately above: one
    small JSON file under ``cache_dir/adanos/unsupported.json``, falling back
    to an in-process dict when no ``cache_dir`` is configured. Exists purely
    so post-scan enrichment never re-asks about the same unsupported symbol
    every day -- reading/writing it is cheap and local, never a network call.

    Each memoised symbol carries a re-probe horizon of 30 TRADING days (not
    calendar days, via ``utils.timeutils.next_trading_day`` -- vendors
    occasionally gain coverage for a previously-unsupported ticker, so the
    memo is a durable skip, not a permanent one.
    """

    def __init__(self, path: Path | None):
        self._path = path
        self._lock = threading.Lock()
        self._mem_state: dict[str, str] = {}

    def _load(self) -> dict[str, str]:
        if self._path is None:
            return dict(self._mem_state)
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, str]) -> None:
        if self._path is None:
            self._mem_state = dict(state)
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            log.debug("failed to persist adanos unsupported-ticker memo", exc_info=True)

    def is_memoized(self, symbol: str, *, today: dt.date) -> bool:
        """``True`` when ``symbol`` was memoised as unsupported and the
        30-trading-day re-probe horizon has not elapsed as of ``today`` --
        callers should skip the network call entirely in that case."""
        state = self._load()
        expiry = state.get(symbol)
        if expiry is None:
            return False
        try:
            expiry_date = dt.date.fromisoformat(expiry)
        except ValueError:
            return False
        return today < expiry_date

    def memoize(self, symbol: str, *, today: dt.date) -> None:
        """Record ``symbol`` as unsupported as of ``today``, re-probable
        after the 30-trading-day horizon."""
        with self._lock:
            state = self._load()
            state[symbol] = next_trading_day(today, skip=30).isoformat()
            self._save(state)


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
        #: the candidate detail-enrichment cache under ``adanos/detail/``,
        #: and the unsupported-ticker memo below -- see
        #: ``enrich_candidates``). ``None`` only in tests that construct a
        #: provider with no ``cache_dir``.
        self._cache_dir = Path(cache_dir) if cache_dir else None
        budget_path = (
            (self._cache_dir / "adanos" / "monthly_budget.json") if self._cache_dir else None
        )
        self._budget = _MonthlyBudgetStore(budget_path)
        unsupported_path = (
            (self._cache_dir / "adanos" / "unsupported.json") if self._cache_dir else None
        )
        #: See ``_UnsupportedTickerStore`` -- consulted/updated only by
        #: ``enrich_candidates``, never by ``fetch_stock_detail`` itself (an
        #: interactive/on-demand caller always gets a live answer).
        self._unsupported = _UnsupportedTickerStore(unsupported_path)
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
        self._budget.record_call(
            month=month, remaining_hint=hint, budget=self.config.monthly_budget
        )

    def _get_json(self, url: str, *, headers: dict[str, str], platform: str) -> Any:
        if self.mode == "official":
            self._enforce_budget_or_raise()

        self._limiter.acquire()
        try:
            if self._client is not None:
                response = self._client.get(
                    url, headers=headers, timeout=self.config.request_timeout_s
                )
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

    # --- hybrid mode: on-demand calls, site-first with official fallback ----
    #
    # Independent of ``self.mode`` (the trending-collection selector above):
    # these methods try the keyless site proxy first (free, no key needed)
    # and fall back to the official API only when the site rung fails and a
    # key resolves with budget to spend -- ``config.prefer_official_api``
    # reverses the rung order, same as it does for trending. See the module
    # docstring's Hybrid mode section.

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

    def _refusal(
        self,
        symbol: str,
        platform: str,
        reason: str,
        *,
        mode: str | None = None,
        quota_spent: bool = False,
        unsupported_ticker: bool = False,
    ) -> dict[str, Any]:
        """The structured, ``accepted: false`` envelope every on-demand
        refusal shares -- preflight (unsupported platform), both-rungs-failed
        (see ``_ladder_refusal``), and unknown-ticker (``_unknown_ticker_
        refusal``) alike -- so a caller (an MCP client, or
        ``enrich_candidates`` below) can branch on ``accepted`` without a
        try/except and always finds ``mode``/``quota_spent``/``budget`` keys
        present regardless of which refusal shape it got. ``mode``/
        ``quota_spent`` stay at their defaults (``None``/``False``) for a
        preflight refusal, since no rung was ever attempted.

        ``unsupported_ticker`` is ``True`` only for the specific vendor "no"
        naming an ``unsupported_ticker`` error code (see
        ``_structured_answer_reason``) -- the one refusal shape
        ``enrich_candidates`` memoises via ``_UnsupportedTickerStore``,
        distinct from every other refusal (including the quieter
        ``{"found": false}`` answer), which is never memoised."""
        return {
            "accepted": False,
            "symbol": symbol,
            "platform": platform,
            "reason": reason,
            "mode": mode,
            "quota_spent": quota_spent,
            "unsupported_ticker": unsupported_ticker,
            "budget": self.budget_status(),
        }

    def _rung_order(self) -> list[str]:
        """``["site", "official"]`` by default -- the site proxy is tried
        first since it is free and keyless. ``config.prefer_official_api``
        reverses this, consistent with what it already means for trending
        (see the module docstring)."""
        return ["official", "site"] if self.config.prefer_official_api else ["site", "official"]

    def _official_unavailable_reason(self) -> str | None:
        """``None`` when the official rung is worth attempting (a key
        resolves and there is budget); otherwise why it was skipped WITHOUT
        sending a request -- folded into the ladder's failure list so a
        both-rungs-failed refusal can name this even though no HTTP call was
        ever made for it."""
        if self._api_key is None:
            return (
                "no adanos_api_key credential resolves on this installation -- the official "
                "fallback is unavailable (site-first on-demand calls still work without one)"
            )
        return self._budget_refusal_reason()

    def _run_ladder(
        self, *, site_fn: Callable[[], Any], official_fn: Callable[[], Any]
    ) -> tuple[str | None, Any, dict[str, str], bool]:
        """Try ``site_fn``/``official_fn`` in ``_rung_order()``, stopping at
        the first rung that returns a payload without raising.

        Returns ``(mode, payload, failures, quota_spent)``:

        * ``mode`` -- which rung answered (``"site"`` or ``"official"``), or
          ``None`` if every available rung failed.
        * ``payload`` -- that rung's decoded JSON body, or ``None`` when
          ``mode`` is ``None``.
        * ``failures`` -- ``{"site": reason, "official": reason}`` for
          whichever rungs did NOT answer (a rung that was never tried because
          an earlier one already answered is simply absent from this dict).
        * ``quota_spent`` -- ``True`` iff an official request actually
          reached the vendor during this call (determined by diffing the
          budget counter, not merely by whether the official rung was
          attempted -- a pre-flight refusal from ``_official_unavailable_
          reason`` never sends a request at all, so it must not count).

        Any ``ProviderError`` (its whole subclass family -- rate limit,
        auth, source-blocked, or a bare schema-drift/HTTP-status failure)
        from either ``_fn`` is caught here and recorded, never re-raised --
        the ladder's entire point is that one rung's failure is not fatal.
        """
        failures: dict[str, str] = {}
        quota_spent = False
        for rung in self._rung_order():
            if rung == "official":
                reason = self._official_unavailable_reason()
                if reason is not None:
                    failures["official"] = reason
                    continue
                used_before, _ = self._budget.snapshot()
                try:
                    payload = official_fn()
                except ProviderError as exc:
                    failures["official"] = str(exc)
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    failures["official"] = f"unexpected error: {exc}"
                    continue
                finally:
                    used_after, _ = self._budget.snapshot()
                    if used_after > used_before:
                        quota_spent = True
            else:
                try:
                    payload = site_fn()
                except ProviderError as exc:
                    failures["site"] = str(exc)
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    failures["site"] = f"unexpected error: {exc}"
                    continue
            return rung, payload, failures, quota_spent
        return None, None, failures, quota_spent

    def _ladder_refusal(
        self, symbol: str, platform: str, failures: dict[str, str], *, quota_spent: bool
    ) -> dict[str, Any]:
        """Both rungs failed (or the only available one did) -- a structured
        refusal naming each failure by rung, per the module docstring."""
        detail = "; ".join(f"{rung}: {reason}" for rung, reason in failures.items())
        return self._refusal(
            symbol,
            platform,
            f"adanos on-demand lookup failed on every available rung -- {detail}",
            mode=None,
            quota_spent=quota_spent,
        )

    def _structured_answer_reason(
        self, data: dict[str, Any], symbol: str, platform: str
    ) -> tuple[str, bool] | None:
        """``None`` when ``data`` is real data; otherwise ``(reason,
        unsupported)`` for whichever structured "no" it is (see
        ``_is_structured_vendor_answer``) -- distinguishing "unsupported
        ticker" (the vendor does not track this symbol at all, observed as
        ``{"detail": {"error_code": "unsupported_ticker", ...}}``,
        ``unsupported`` ``True``) from "found: false" (a tracked ticker with
        no data in the requested window, ``unsupported`` ``False``), since
        those are different situations worth reporting differently -- and,
        for ``enrich_candidates``, worth treating differently for memoisation
        purposes -- even though both stop the ladder the same way."""
        detail = data.get("detail")
        if isinstance(detail, dict) and detail.get("error_code") == "unsupported_ticker":
            message = (
                detail.get("message") or f"{symbol} is not a ticker adanos tracks on {platform}"
            )
            return f"adanos: unsupported ticker -- {message}", True
        if data.get("found") is False:
            return (
                f"adanos has no {platform} data for {symbol} in the requested window (found: false)",
                False,
            )
        return None

    def _vendor_answer_refusal(
        self,
        symbol: str,
        platform: str,
        reason: str,
        *,
        mode: str,
        quota_spent: bool,
        unsupported: bool = False,
    ) -> dict[str, Any]:
        """The vendor gave a definitive, structured "no" rather than failing
        -- either ``{"found": false}`` (a ticker it tracks, no data in the
        requested window) or a ``{"detail": {"error_code":
        "unsupported_ticker", ...}}`` body (a ticker it does not track at
        all, ``unsupported=True``) -- see ``_is_structured_vendor_answer``/
        the module docstring. Either way this is a normal result, not an
        exception, and NOT a reason to try the other rung: the vendor has
        already answered."""
        return self._refusal(
            symbol,
            platform,
            reason,
            mode=mode,
            quota_spent=quota_spent,
            unsupported_ticker=unsupported,
        )

    def _site_get(self, url: str, *, platform: str, kind: str) -> Any:
        """One keyless site-proxy on-demand request (stock detail / explain
        / market-sentiment). Returns the decoded JSON body on success, which
        may itself be a structured "no" answer -- ``{"found": false}`` (see
        ``_is_structured_vendor_answer``) -- rather than the requested data;
        that is NOT an error (see the module docstring). Raises a typed
        ``ProviderError`` subclass on anything that should count as this
        rung failing: an HTTP error status with no recognisable structured
        body, a non-JSON body, or a JSON body shaped ``{"error": ...}`` with
        neither a ``found`` nor a ``detail.error_code`` key (endpoint-absent,
        not data).

        A structured vendor answer can arrive on a non-2xx status too --
        observed live 2026-08-03: an unsupported ticker returns
        ``{"detail": {"error_code": "unsupported_ticker", "message": ...}}``
        possibly under HTTP 404 -- so the body is inspected before falling
        back to treating a non-2xx status as an unconditional failure.
        """
        headers = {"User-Agent": self.config.user_agent, "Accept": "application/json"}
        response = self._send_request(url, headers=headers)
        with self._lock:
            self._calls += 1

        if response.status_code == 429:
            self._last_error = f"HTTP 429 for site {kind} ({platform})"
            raise RateLimitError(
                f"adanos site proxy rate limit reached for {kind} ({platform})",
                provider=self.name,
                retry_after_s=60.0,
            )
        if response.status_code in (401, 403):
            self._last_error = f"HTTP {response.status_code} for site {kind} ({platform})"
            raise SourceBlockedError(
                f"adanos site proxy answered HTTP {response.status_code} for {kind} ({platform}) "
                "-- unexpected for a keyless page-proxy endpoint, treated as a block signal",
                provider=self.name,
            )

        try:
            payload = response.json()
            parsed_ok = True
        except Exception:
            payload = None
            parsed_ok = False

        if response.status_code >= 400:
            if parsed_ok and isinstance(payload, dict) and _is_structured_vendor_answer(payload):
                # A structured "no" (see the docstring above) riding on a
                # non-2xx status -- NOT a rung failure.
                with self._lock:
                    self._last_success = utc_now()
                return payload
            # Any other non-2xx: an unconfirmed/absent site route (e.g. the
            # confirmed-absent Polymarket /explain -- see the module
            # docstring) or a shape change, not a per-ticker answer.
            self._last_error = f"HTTP {response.status_code} for site {kind} ({platform})"
            raise ProviderError(
                f"adanos site proxy returned HTTP {response.status_code} for {kind} ({platform})",
                provider=self.name,
                retryable=response.status_code >= 500,
            )

        if not parsed_ok:
            self._last_error = f"non-JSON response for site {kind} ({platform})"
            raise SourceBlockedError(
                f"adanos site proxy returned a non-JSON body for {kind} ({platform}) -- "
                "unexpected for a documented JSON API, treated as a possible block/challenge "
                "response",
                provider=self.name,
            )

        if not isinstance(payload, dict):
            self._last_error = f"unexpected payload shape for site {kind} ({platform})"
            raise ProviderError(
                f"adanos site proxy returned an unexpected payload shape for {kind} ({platform})",
                provider=self.name,
            )
        if _is_structured_vendor_answer(payload):
            with self._lock:
                self._last_success = utc_now()
            return payload
        if "error" in payload:
            # {"error": "Not found"} (200 or a would-be-404 masqueraded as
            # 200) -- the route itself does not exist on this proxy base,
            # not a per-ticker answer (see the module docstring).
            self._last_error = f"endpoint-absent for site {kind} ({platform})"
            raise ProviderError(
                f"adanos site proxy has no {kind} route for {platform} ({payload.get('error')!r})",
                provider=self.name,
            )

        with self._lock:
            self._last_success = utc_now()
        return payload

    def _detail_cache_path(self, symbol: str, session: dt.date) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / "adanos" / "detail" / f"{symbol}-{session.isoformat()}.json"

    def cached_detail(
        self, symbol: str, session: dt.date, *, platform: str | None = None
    ) -> dict[str, Any] | None:
        """A same-session enrichment cache entry for ``symbol``, or ``None``
        when absent, unreadable, or written for a different platform than
        requested. Reading this NEVER spends quota -- it is what lets
        ``mcp_server.get_adanos_detail`` answer "cached from enrichment, no
        quota spent" for a symbol ``Pipeline.scan`` already enriched this
        session (see ``enrich_candidates`` below)."""
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
        self, url: str, *, headers: dict[str, str], platform: str, kind: str, ticker: str = ""
    ) -> Any:
        """One on-demand OFFICIAL call (stock detail / explain / market
        sentiment) -- the official rung of the site-first ladder (see the
        module docstring). The caller must already have confirmed there is a
        key and budget to spend (``_official_unavailable_reason``) -- this
        only sends the request, records it against the monthly budget, and
        translates the response. No retries. ``ticker`` is omitted for the
        platform-level market-sentiment endpoint, which has none."""
        subject = f" for {ticker}" if ticker else ""
        try:
            response = self._send_request(url, headers=headers)
        except ProviderError:
            self._last_error = f"network error for {kind}{subject} ({platform})"
            raise

        with self._lock:
            self._calls += 1
        self._record_official_call(response)

        if response.status_code == 401:
            self._last_error = f"HTTP 401 for {kind}{subject} ({platform})"
            raise AuthenticationError(
                f"adanos rejected the API key (HTTP 401) for {kind}{subject} -- check the "
                f"{self.config.api_key_credential} credential",
                provider=self.name,
            )
        if response.status_code == 403:
            self._last_error = f"HTTP 403 for {kind}{subject} ({platform})"
            raise ProviderError(
                f"adanos refused {kind}{subject}: the requested history window exceeds this "
                "account's tier depth (HTTP 403) -- request a shorter from/to range or upgrade "
                "the tier",
                provider=self.name,
            )
        if response.status_code == 429:
            self._last_error = f"HTTP 429 for {kind}{subject} ({platform})"
            raise RateLimitError(
                f"adanos burst rate limit reached for {kind}{subject}",
                provider=self.name,
                retry_after_s=60.0,
            )
        if response.status_code == 404:
            self._last_error = f"HTTP 404 for {kind}{subject} ({platform})"
            raise ProviderError(
                f"adanos official API has no {kind}{subject} on {platform} (HTTP 404) -- "
                "unsupported or unknown ticker",
                provider=self.name,
            )
        if response.status_code >= 500:
            self._last_error = f"HTTP {response.status_code} for {kind}{subject} ({platform})"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for {kind}{subject}",
                provider=self.name,
                retryable=True,
            )
        if response.status_code >= 400:
            self._last_error = f"HTTP {response.status_code} for {kind}{subject} ({platform})"
            raise ProviderError(
                f"adanos returned HTTP {response.status_code} for {kind}{subject}",
                provider=self.name,
            )

        try:
            payload = response.json()
        except Exception as exc:
            self._last_error = f"non-JSON response for {kind}{subject} ({platform})"
            raise SourceBlockedError(
                f"adanos returned a non-JSON body for {kind}{subject} -- unexpected for a "
                "documented JSON API, treated as a possible block/challenge response",
                provider=self.name,
            ) from exc

        with self._lock:
            self._last_success = utc_now()
        return payload

    # --- official rung builders (URL/header construction only) -------------

    def _official_stock_detail(
        self, symbol: str, platform: str, from_date: str | None, to_date: str | None
    ) -> Any:
        params = []
        if from_date:
            params.append(f"from={from_date}")
        if to_date:
            params.append(f"to={to_date}")
        query = ("?" + "&".join(params)) if params else ""
        base = self.config.official_base_url.rstrip("/")
        url = f"{base}/{_stock_base(platform)}/stock/{symbol}{query}"
        headers = {
            "X-API-Key": self._api_key or "",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        return self._ondemand_request(
            url, headers=headers, platform=platform, kind="stock detail", ticker=symbol
        )

    def _official_explain(self, symbol: str, platform: str) -> Any:
        base = self.config.official_base_url.rstrip("/")
        url = f"{base}/{_stock_base(platform)}/stock/{symbol}/explain"
        headers = {
            "X-API-Key": self._api_key or "",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        return self._ondemand_request(
            url, headers=headers, platform=platform, kind="explain", ticker=symbol
        )

    def _official_market_sentiment(self, platform: str) -> Any:
        base = self.config.official_base_url.rstrip("/")
        url = f"{base}/{_stock_base(platform)}/market-sentiment"
        headers = {
            "X-API-Key": self._api_key or "",
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        return self._ondemand_request(
            url, headers=headers, platform=platform, kind="market sentiment"
        )

    # --- site rung builders (URL construction only) -------------------------

    def _site_stock_detail(
        self, symbol: str, platform: str, from_date: str | None, to_date: str | None
    ) -> Any:
        base = self.config.site_base_url.rstrip("/")
        days = _days_param(from_date, to_date)
        query = f"?days={days}" if days is not None else ""
        url = f"{base}/{_site_base(platform)}/stock/{symbol}{query}"
        return self._site_get(url, platform=platform, kind="stock detail")

    def _site_explain(self, symbol: str, platform: str) -> Any:
        if platform == "polymarket":
            # Confirmed absent by live probe 2026-08-03:
            # proxy-polymarket/stock/{ticker}/explain returns
            # {"error": "Not found"} -- unlike Polymarket's OTHER on-demand
            # routes (stock detail, market-sentiment), explain is not mirrored
            # here at all. Skip the network round-trip entirely rather than
            # probing a route already known not to exist (see the module
            # docstring) -- this still counts as a normal site-rung failure
            # for the ladder, which falls back to official.
            raise ProviderError(
                "adanos site proxy does not mirror explain for polymarket (confirmed absent -- "
                "official API only)",
                provider=self.name,
            )
        base = self.config.site_base_url.rstrip("/")
        url = f"{base}/{_site_base(platform)}/stock/{symbol}/explain"
        return self._site_get(url, platform=platform, kind="explain")

    def _site_market_sentiment(self, platform: str) -> Any:
        base = self.config.site_base_url.rstrip("/")
        url = f"{base}/{_site_base(platform)}/market-sentiment"
        return self._site_get(url, platform=platform, kind="market sentiment")

    # --- envelope builders ---------------------------------------------------

    def _detail_success_envelope(
        self, symbol: str, platform: str, data: dict[str, Any], *, mode: str, quota_spent: bool
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "symbol": symbol,
            "platform": platform,
            "mode": mode,
            "quota_spent": quota_spent,
            "fetched_at": utc_now().isoformat(),
            "buzz_score": _as_float(data.get("buzz_score")),
            "sentiment_score": _as_float(data.get("sentiment_score")),
            "bullish_pct": _as_float(data.get("bullish_pct")),
            "bearish_pct": _as_float(data.get("bearish_pct")),
            "mentions": _as_int(data.get(_COUNT_FIELD.get(platform, "mentions"))),
            # Platform-specific extras (top_tweets/top_authors/daily_trend/
            # sentiment_breakdown/... -- whatever this platform's response
            # shape carries) pass through untouched EXCEPT for known
            # text-snippet fields, which get the same sanitisation as any
            # other untrusted social text (see the module docstring).
            "raw": _sanitize_raw_snippets(data),
            "budget": self.budget_status(),
        }

    def _explain_success_envelope(
        self, symbol: str, platform: str, data: dict[str, Any], *, mode: str, quota_spent: bool
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "symbol": symbol,
            "platform": platform,
            "mode": mode,
            "quota_spent": quota_spent,
            "explanation": data.get("explanation"),
            "cached": data.get("cached"),
            "generated_at": data.get("generated_at"),
            "budget": self.budget_status(),
        }

    def _market_sentiment_success_envelope(
        self, platform: str, data: dict[str, Any], *, mode: str, quota_spent: bool
    ) -> dict[str, Any]:
        drivers = data.get("drivers")
        return {
            "accepted": True,
            "platform": platform,
            "mode": mode,
            "quota_spent": quota_spent,
            "fetched_at": utc_now().isoformat(),
            "buzz_score": _as_float(data.get("buzz_score")),
            "sentiment_score": _as_float(data.get("sentiment_score")),
            "bullish_pct": _as_float(data.get("bullish_pct")),
            "bearish_pct": _as_float(data.get("bearish_pct")),
            # Activity count: "mentions" for x/reddit/news, "trade_count" for
            # polymarket -- same per-platform field as _detail_success_
            # envelope's "mentions", via the shared _COUNT_FIELD mapping.
            "mentions": _as_int(data.get(_COUNT_FIELD.get(platform, "mentions"))),
            "trend": data.get("trend"),
            "active_tickers": _as_int(data.get("active_tickers")),
            #: Top momentum drivers (``{"ticker", "buzz_score",
            #: "sentiment_score"}`` plus "mentions" or "trade_count"
            #: per-platform, per the verified shape) -- vendor-computed
            #: numbers, nothing to sanitise, but run through the same helper
            #: for uniformity/future-proofing in case a future vendor version
            #: adds a text field here.
            "drivers": _sanitize_raw_snippets(drivers) if isinstance(drivers, list) else [],
            "raw": _sanitize_raw_snippets(data),
            "budget": self.budget_status(),
        }

    # --- public on-demand entry points ---------------------------------------

    def fetch_stock_detail(
        self,
        ticker: str,
        platform: str = "x",
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """One ticker's full detail: daily trend, sentiment breakdown, top
        mentions/authors/subreddits/tweets -- whatever the vendor's
        ``GET /stock/{ticker}`` returns for ``platform``, parsed defensively
        (missing keys tolerated, nothing invented) and passed through under
        ``raw`` (text-snippet fields sanitised -- see the module docstring),
        alongside a normalized header block (``buzz_score``/
        ``sentiment_score``/``bullish_pct``/``bearish_pct``/``mentions``).

        **Site-first, official fallback** (module docstring, Hybrid mode):
        tries the keyless site proxy first (free, ``quota_spent: false``),
        falling back to the official API only if the site rung fails and a
        key resolves with budget to spend (``quota_spent: true`` in that
        case). ``config.prefer_official_api`` reverses the rung order.
        **Never raises**: an unsupported platform, a ``{"found": false}``
        answer from whichever rung answered, or every available rung failing
        all come back as a structured ``{"accepted": false, ...}`` refusal
        (see ``_refusal``/``_vendor_answer_refusal``/``_ladder_refusal``).
        """
        symbol = ticker.strip().upper()
        platform = platform.strip().lower()
        if platform not in _OFFICIAL_PATHS:
            return self._refusal(
                symbol,
                platform,
                f"unsupported platform '{platform}' -- expected one of {sorted(_OFFICIAL_PATHS)}",
            )

        mode, payload, failures, quota_spent = self._run_ladder(
            site_fn=lambda: self._site_stock_detail(symbol, platform, from_date, to_date),
            official_fn=lambda: self._official_stock_detail(symbol, platform, from_date, to_date),
        )
        if mode is None:
            return self._ladder_refusal(symbol, platform, failures, quota_spent=quota_spent)
        data = payload if isinstance(payload, dict) else {}
        answer = self._structured_answer_reason(data, symbol, platform)
        if answer is not None:
            reason, unsupported = answer
            return self._vendor_answer_refusal(
                symbol, platform, reason, mode=mode, quota_spent=quota_spent, unsupported=unsupported
            )
        return self._detail_success_envelope(
            symbol, platform, data, mode=mode, quota_spent=quota_spent
        )

    def fetch_explain(self, ticker: str, platform: str = "x") -> dict[str, Any]:
        """The vendor's AI trend explanation (llama-3.1-8b, cached
        server-side for 6h) for one ticker. Same site-first/official-fallback
        ladder as ``fetch_stock_detail`` (module docstring). Note that even
        an official-side CACHE HIT on the vendor's own end still spends this
        installation's quota once the official rung is reached, since the
        vendor still counts the call -- the returned ``cached``/
        ``generated_at`` fields describe whether ITS text was freshly
        generated, which is orthogonal to whether OUR request spent local
        quota (``quota_spent``). **Never raises** -- same refusal contract as
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

        mode, payload, failures, quota_spent = self._run_ladder(
            site_fn=lambda: self._site_explain(symbol, platform),
            official_fn=lambda: self._official_explain(symbol, platform),
        )
        if mode is None:
            return self._ladder_refusal(symbol, platform, failures, quota_spent=quota_spent)
        data = payload if isinstance(payload, dict) else {}
        answer = self._structured_answer_reason(data, symbol, platform)
        if answer is not None:
            reason, unsupported = answer
            return self._vendor_answer_refusal(
                symbol, platform, reason, mode=mode, quota_spent=quota_spent, unsupported=unsupported
            )
        return self._explain_success_envelope(
            symbol, platform, data, mode=mode, quota_spent=quota_spent
        )

    def fetch_market_sentiment(self, platform: str = "x") -> dict[str, Any]:
        """Service-level buzz/sentiment snapshot for ``platform``: overall
        trend, activity, and the top momentum ``drivers`` (ticker-level
        breakdown). NOT a per-ticker lookup -- unlike ``fetch_stock_detail``/
        ``fetch_explain`` the envelope carries no ``symbol`` and there is no
        ``{"found": false}`` case. Same site-first/official-fallback ladder
        and envelope conventions (``mode``, ``quota_spent``, ``budget``) as
        the module docstring's Hybrid mode section describes. **Never
        raises.**
        """
        platform = platform.strip().lower()
        if platform not in _OFFICIAL_PATHS:
            return {
                "accepted": False,
                "platform": platform,
                "reason": (
                    f"unsupported platform '{platform}' -- expected one of "
                    f"{sorted(_OFFICIAL_PATHS)}"
                ),
                "mode": None,
                "quota_spent": False,
                "budget": self.budget_status(),
            }

        mode, payload, failures, quota_spent = self._run_ladder(
            site_fn=lambda: self._site_market_sentiment(platform),
            official_fn=lambda: self._official_market_sentiment(platform),
        )
        if mode is None:
            detail = "; ".join(f"{rung}: {reason}" for rung, reason in failures.items())
            return {
                "accepted": False,
                "platform": platform,
                "reason": f"adanos market-sentiment lookup failed on every available rung -- {detail}",
                "mode": None,
                "quota_spent": quota_spent,
                "budget": self.budget_status(),
            }
        data = payload if isinstance(payload, dict) else {}
        return self._market_sentiment_success_envelope(
            platform, data, mode=mode, quota_spent=quota_spent
        )

    def enrich_candidates(
        self,
        symbols: list[str],
        *,
        session: dt.date,
        on_snapshot: Callable[[AdanosSnapshot], None] | None = None,
    ) -> int:
        """Bounded, best-effort post-scan enrichment: one ``fetch_stock_detail``
        call each (on ``config.detail_platform_default``) for a breadth of
        ``symbols`` (already ordered best-first and de-duplicated by the
        caller -- see ``pipeline.Pipeline._enrich_adanos_candidates``)
        governed by ``config.enrich_scope``:

        * ``"all"`` (default) -- every distinct symbol, capped by
          ``config.enrich_max_symbols_per_scan``. Symbols beyond the cap are
          logged by name (INFO), never silently dropped.
        * ``"top_n"`` -- only the first ``config.enrich_top_candidates``
          symbols (the previous, pre-generalisation behaviour).
        * ``"off"`` -- no-op, same as ``config.enrich_enabled = False``.

        Each attempted symbol's result is cached to
        ``cache_dir/adanos/detail/{symbol}-{session}.json``; a symbol already
        cached for this session is skipped (INFO log, not an error, no
        network call) -- the whole point of the cache is that a re-scan of
        the same session, or a later ``AdanosProvider.cached_detail``/
        ``mcp_server.get_adanos_detail`` call for the same symbol, must not
        re-fetch it. A symbol memoised as vendor-unsupported (see
        ``_UnsupportedTickerStore``) within its 30-trading-day re-probe
        horizon is skipped the same way, also with no network call.

        ``config.enrich_delay_seconds`` is slept BETWEEN successive actual
        network attempts only -- never before the first attempt, and never
        for a cache/memo hit (which makes no network call at all).

        Effective whenever ``config.enrich_enabled`` -- unlike before the
        site-first ladder existed, this no longer requires
        ``adanos_api_key`` to resolve: ``fetch_stock_detail`` now works
        keylessly via the site rung by default, so ordinary enrichment spends
        ZERO official-API quota (see the module docstring's Hybrid mode
        section). A resolved key only matters as a per-symbol fallback when
        that symbol's site lookup fails.

        Each symbol's ``fetch_stock_detail`` result is judged independently:
        there is no "stop early once the budget guard refuses" special case
        (that assumed every call spent the shared official budget, which is
        no longer true in site mode) -- a refusal for one symbol (every rung
        failed, or a genuine vendor "no") is simply skipped, same as any
        other per-symbol failure, and the loop moves on. A definitive
        ``unsupported_ticker`` refusal is additionally memoised so future
        scans skip it without a network call until the re-probe horizon;
        the quieter ``{"found": false}`` "tracked but no data right now"
        refusal is deliberately NOT memoised (it can change hourly).

        ``on_snapshot``, when given, is called once per SUCCESSFUL
        enrichment with an ``AdanosSnapshot`` built from that symbol's
        detail header (buzz/sentiment/bullish/bearish/mentions, plus
        whatever ``trend``/platform-engagement field the detail payload
        carries -- ``trend_history`` is honestly empty when the detail
        response does not include one, never fabricated). This is how
        ``pipeline.Pipeline`` feeds ``db.models.AdanosSnapshotRow`` for a
        symbol outside the top-100 trending feeds, reusing the exact same
        ``(session, platform, symbol)`` storage contract
        ``data.ingest.DataIngestor.ingest_adanos`` writes for trending (see
        ``pipeline.Pipeline._store_adanos_enrichment_snapshot``). Any
        exception the callback raises is caught and logged here -- a
        secondary storage failure must never abort enrichment or a scan.

        **Never raises.** A scan must never fail because enrichment
        degraded; any per-symbol failure (network, vendor error, disk) is
        logged and the loop continues to the next symbol. Returns the number
        of symbols newly cached this call (for logging/tests) -- NOT
        necessarily the number of official-API requests spent, since most
        will be free site-mode hits; each cached entry's own
        ``quota_spent`` field records that individually.
        """
        if self._cache_dir is None:
            log.info("adanos enrichment skipped: no cache_dir configured")
            return 0
        if not self.config.enrich_enabled or self.config.enrich_scope == "off":
            return 0
        if self.config.enrich_scope == "top_n" and self.config.enrich_top_candidates <= 0:
            return 0

        candidates = self._enrichment_candidates(symbols)
        platform = self.config.detail_platform_default
        spent = 0
        attempted = 0
        try:
            for raw_symbol in candidates:
                symbol = raw_symbol.strip().upper()
                if not symbol:
                    continue
                if self._unsupported.is_memoized(symbol, today=session):
                    log.info(
                        "adanos enrichment: %s memoised as unsupported (re-probe horizon not "
                        "yet reached); skipping",
                        symbol,
                    )
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

                if attempted > 0 and self.config.enrich_delay_seconds > 0:
                    time.sleep(self.config.enrich_delay_seconds)
                attempted += 1
                try:
                    result = self.fetch_stock_detail(symbol, platform=platform)
                except Exception as exc:  # pragma: no cover - fetch_stock_detail no longer raises
                    log.info("adanos enrichment: %s failed (%s); skipping", symbol, exc)
                    continue
                if not result.get("accepted", True):
                    log.info("adanos enrichment: %s refused (%s)", symbol, result.get("reason"))
                    if result.get("unsupported_ticker"):
                        self._unsupported.memoize(symbol, today=session)
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
                if on_snapshot is not None:
                    try:
                        on_snapshot(_snapshot_from_detail(symbol, platform, result))
                    except Exception:
                        log.warning(
                            "adanos enrichment: on_snapshot callback failed for %s; the cached "
                            "detail was still written",
                            symbol,
                            exc_info=True,
                        )
                spent += 1
        except Exception:  # pragma: no cover - defensive, see docstring
            log.warning("adanos enrichment raised unexpectedly; scan is unaffected", exc_info=True)
        return spent

    def _enrichment_candidates(self, symbols: list[str]) -> list[str]:
        """``symbols`` narrowed to ``config.enrich_scope``'s breadth -- the
        cap/log-what's-dropped policy for ``"all"`` scope, or the top-N slice
        for ``"top_n"`` scope. Assumes the caller (``enrich_candidates``) has
        already ruled out ``"off"``/disabled."""
        if self.config.enrich_scope == "top_n":
            return symbols[: self.config.enrich_top_candidates]
        cap = self.config.enrich_max_symbols_per_scan
        if len(symbols) > cap:
            dropped = symbols[cap:]
            log.info(
                "adanos enrichment: capped at %d of %d distinct signal symbol(s) this scan; "
                "dropped %s",
                cap,
                len(symbols),
                ", ".join(dropped),
            )
        return symbols[:cap]


def _snapshot_from_detail(symbol: str, platform: str, envelope: dict[str, Any]) -> AdanosSnapshot:
    """Build an ``AdanosSnapshot`` from a successful ``fetch_stock_detail``
    envelope (``envelope["accepted"] is True``) -- what
    ``AdanosProvider.enrich_candidates`` hands to its ``on_snapshot``
    callback so a symbol enriched outside the top-100 trending feeds still
    feeds ``db.models.AdanosSnapshotRow``/``webapi.attention``.

    Reads the SAME normalized header fields ``_detail_success_envelope``
    already computed (``buzz_score``/``sentiment_score``/``bullish_pct``/
    ``bearish_pct``/``mentions``) plus two fields only present under
    ``raw`` (the vendor's own per-ticker detail payload, already sanitised):
    ``company_name`` and ``trend``. ``trend_history`` is a 7-point series
    Adanos's *trending*/*market-sentiment* endpoints carry but the
    per-ticker ``/stock/{ticker}`` detail response does not (see the module
    docstring's verified-shape notes) -- stored honestly as an empty list
    here rather than fabricated from ``daily_trend``, which is a different,
    differently-shaped series.
    """
    raw = envelope.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    observed_at = utc_now()
    fetched_at = envelope.get("fetched_at")
    if isinstance(fetched_at, str):
        with contextlib.suppress(ValueError):
            observed_at = dt.datetime.fromisoformat(fetched_at)
    engagement_field = _ENGAGEMENT_FIELD.get(platform, "total_upvotes")
    return AdanosSnapshot(
        symbol=symbol,
        platform=platform,
        company_name=str(raw.get("company_name") or ""),
        buzz_score=envelope.get("buzz_score") or 0.0,
        mentions=envelope.get("mentions") or 0,
        trend=str(raw.get("trend") or ""),
        sentiment_score=envelope.get("sentiment_score"),
        bullish_pct=envelope.get("bullish_pct"),
        bearish_pct=envelope.get("bearish_pct"),
        engagement=_as_float(raw.get(engagement_field)) or 0.0,
        trend_history=_as_float_list(raw.get("trend_history")),
        observed_at=observed_at,
    )


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


def _parse_feed_rows(
    payload: Any, *, platform: str, observed_at: dt.datetime
) -> list[AdanosSnapshot]:
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
