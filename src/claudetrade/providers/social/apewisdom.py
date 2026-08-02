"""ApeWisdom aggregate mention counts for Reddit and 4chan.

ApeWisdom (``apewisdom.io/api``) publishes a free, keyless JSON API that
counts how often each ticker is mentioned across a community over the last
24 hours, alongside the same count 24 hours earlier. This module reads it.

**Why this is not a ``SocialProvider``.** Every other source in
``providers.social`` returns individual :class:`~claudetrade.domain.SocialPost`
objects, because that is what those APIs serve. ApeWisdom serves the
finished aggregate: a ticker, a mention count, an upvote total. There is no
post text, no author, no timestamp. Implementing ``fetch_posts`` here would
mean inventing all three, and the invented values would not sit inertly --
``unique_authors``, ``duplicate_ratio``, ``bot_risk`` and ``manipulation_risk``
are all derived from post-level identity and text, so fabricated posts would
feed the manipulation model confident-looking fiction. That is the same
failure the synthetic providers were removed for. This provider therefore
returns :class:`~claudetrade.domain.SymbolAttention` observations through
``fetch_attention``, and the ingest path stores them under their own
``source`` labels.

**Why it is worth having anyway.** Two things it does that the post-level
sources cannot:

* It observes whole communities continuously -- all of r/wallstreetbets,
  r/stocks, r/investing, r/options for ``all-stocks``, and 4chan's /biz/
  board -- where this application's own Reddit and X fetches are narrow,
  rate-limited windows into the same populations.
* Its rows are already tickers. Nothing here passes through the common-word
  entity resolution that kept minting ``AS``/``YOU``/``DAY`` mentions out of
  ordinary English prose (QA handoff v3, F25), so this source structurally
  cannot produce that class of junk.

**What it cannot tell you** is direction. A mention count says people are
talking, not what they are saying, and ``sentiment.aggregation`` is explicit
that counting mentions toward bullishness "is exactly the mistake this
module exists to avoid". Nothing in this module produces a polarity, and the
rows it feeds never reach the combined ``"all"`` aggregate that strategies
score against.

The response schema below is transcribed from ApeWisdom's published API
documentation rather than captured live (this package is built and tested
with egress blocked), so :func:`_parse_results` is deliberately tolerant:
counts arrive as either JSON numbers or numeric strings depending on the
endpoint version, unknown keys are ignored, and a row missing anything
essential is skipped rather than allowed to abort the fetch.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from claudetrade.config import ApeWisdomConfig
from claudetrade.domain import SymbolAttention
from claudetrade.providers.base import (
    ProviderError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
)
from claudetrade.utils.timeutils import utc_now

log = logging.getLogger(__name__)

#: Ticker strings ApeWisdom emits that are not equities this app screens.
#: Crypto tickers appear even under stock filters when a post mentions both.
_NON_EQUITY = frozenset({"BTC", "ETH", "DOGE", "SHIB", "XRP", "SOL", "ADA", "USDT"})


class ApeWisdomProvider:
    """Reads per-community ticker mention tallies from ApeWisdom."""

    name = "apewisdom"
    kind = "attention"

    def __init__(self, config: ApeWisdomConfig, *, client: httpx.Client | None = None):
        self.config = config
        #: Injected in tests via ``httpx.MockTransport``; ``None`` means this
        #: provider opens (and closes) its own client per fetch, matching
        #: ``news_rss``'s posture of holding no long-lived connection.
        self._client = client
        self._limiter = RateLimiter(
            config.rate_limit_per_minute, name="apewisdom", max_wait_s=30.0
        )

    def status(self) -> ProviderStatus:
        """Report provider status.

        ``configured`` tracks ``enabled`` and a non-empty filter list only --
        there is no credential to check, which is much of this source's
        appeal. ``supports_point_in_time`` is ``False`` and that matters:
        the API reports a rolling *current* 24h window with no history
        endpoint, so a backtest cannot reconstruct what it said on a past
        date. Attention rows are usable going forward, never backfillable.
        """
        configured = self.config.enabled and bool(self.config.filters)
        return ProviderStatus(
            name=self.name,
            kind="attention",
            available=configured,
            configured=configured,
            message=(
                f"ApeWisdom mention counts ({', '.join(self.config.filters)})"
                if configured
                else "disabled or no filters configured"
            ),
            supports_point_in_time=False,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
            licence_note=(
                "Free public JSON API, no key and no authentication; aggregate "
                "mention/upvote counts only -- no post text, no user data, and "
                "no personal information is retrieved or stored. Carries "
                "attention volume only, never sentiment direction."
            ),
        )

    def fetch_attention(self) -> list[SymbolAttention]:
        """Every configured community's current mention tally, best-effort.

        One filter failing (network, rate limit, schema drift) is logged and
        skipped rather than aborting the rest: partial attention data is
        strictly better than none, and this is a supplementary source that
        must never take a refresh down. Returns an empty list -- never
        raises -- when disabled or when every filter fails.
        """
        if not self.config.enabled or not self.config.filters:
            return []

        observed_at = utc_now()
        out: list[SymbolAttention] = []
        for community in self.config.filters:
            try:
                out.extend(self._fetch_filter(community, observed_at))
            except ProviderError as exc:
                log.warning("apewisdom filter %s failed: %s", community, exc)
            except Exception:  # pragma: no cover - defensive
                log.warning("apewisdom filter %s raised unexpectedly", community, exc_info=True)
        if out:
            log.info(
                "apewisdom: %d attention rows across %d community filter(s)",
                len(out),
                len(self.config.filters),
            )
        return out

    def _fetch_filter(self, community: str, observed_at: Any) -> list[SymbolAttention]:
        """Walk the paginated endpoint for one community filter."""
        rows: list[SymbolAttention] = []
        for page in range(1, max(1, self.config.max_pages_per_filter) + 1):
            payload = self._get_json(f"{self.config.base_url}/filter/{community}/page/{page}")
            parsed = _parse_results(payload, community=community, observed_at=observed_at)
            rows.extend(r for r in parsed if r.mentions >= self.config.min_mentions)
            # Stop early rather than requesting pages the API says don't
            # exist -- and stop once a page falls entirely below the mention
            # floor, since results are rank-ordered and every later page is
            # quieter still.
            if not parsed or page >= _total_pages(payload):
                break
            if all(r.mentions < self.config.min_mentions for r in parsed):
                break
        return rows

    def _get_json(self, url: str) -> Any:
        self._limiter.acquire()
        try:
            if self._client is not None:
                response = self._client.get(url, timeout=self.config.request_timeout_s)
            else:
                with httpx.Client(
                    timeout=self.config.request_timeout_s,
                    follow_redirects=True,
                    headers={"User-Agent": self.config.user_agent, "Accept": "application/json"},
                ) as client:
                    response = client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(f"apewisdom request failed: {exc}", provider=self.name) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "apewisdom rate limit reached", provider=self.name, retry_after_s=60.0
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"apewisdom returned HTTP {response.status_code}",
                provider=self.name,
                retryable=response.status_code >= 500,
            )
        try:
            return response.json()
        except Exception as exc:
            raise ProviderError(
                "apewisdom returned a non-JSON body", provider=self.name
            ) from exc


def _total_pages(payload: Any) -> int:
    """Page count the response declares; 1 when it declares none."""
    if isinstance(payload, dict):
        return _as_int(payload.get("pages")) or 1
    return 1


def _parse_results(payload: Any, *, community: str, observed_at: Any) -> list[SymbolAttention]:
    """Convert one API page into attention observations.

    Skips rather than raises on anything malformed: this is a supplementary
    source and a single bad row must not cost the whole community's data.
    """
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    out: list[SymbolAttention] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("ticker") or "").strip().upper()
        # ApeWisdom emits crypto tickers even under stock filters when a post
        # mentions both; this application screens US equities only. Symbols
        # unknown to the universe are dropped later by the securities join,
        # but filtering the obvious ones here keeps the stored rows honest.
        if not symbol or symbol in _NON_EQUITY:
            continue
        mentions = _as_int(entry.get("mentions"))
        if mentions is None:
            continue
        out.append(
            SymbolAttention(
                symbol=symbol,
                community=community,
                mentions=mentions,
                upvotes=_as_int(entry.get("upvotes")) or 0,
                mentions_prev=_as_int(entry.get("mentions_24h_ago")),
                rank=_as_int(entry.get("rank")),
                rank_prev=_as_int(entry.get("rank_24h_ago")),
                name=str(entry.get("name") or "").strip(),
                observed_at=observed_at,
            )
        )
    return out


def _as_int(value: Any) -> int | None:
    """Coerce an API field to ``int``; ``None`` when it isn't a number.

    ApeWisdom returns counts as JSON numbers in some responses and numeric
    strings in others, so this accepts both rather than pinning one shape.
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


__all__ = ["ApeWisdomProvider"]
