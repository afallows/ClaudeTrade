"""Stocktwits provider: official public symbol-stream API adapter.

Reads ``api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json`` -- Stocktwits'
own documented, **keyless** endpoint for basic reads (ADR-0008 Decision 1's
"official APIs first-choice" applies here in the fullest sense: this is not
scraping at all, no authentication is bypassed, and no ToS boundary is
tested). The only engineering discipline this source needs is respecting the
vendor's published unauthenticated budget (200 requests/hour): this adapter
budgets conservatively below that ceiling and caps how many symbols one
refresh cycle will fetch (see ``StocktwitsConfig``).

Each message may carry a self-declared ``entities.sentiment.basic`` tag
("Bullish"/"Bearish") -- some authors tag their own posts this way. This is
stored on the mapped ``SocialPost`` as ``sentiment_prior``, a PRIOR HINT, not
a substitute for classification: a self-declared label is evidence someone
asserted it, not ground truth about the post's actual sentiment, so the
ensemble classifier still runs on ``text`` unconditionally for every post,
Stocktwits included.

Every post goes through the same ``sanitize_social_text()`` / author-hash /
injection-score pipeline every other adapter in this package uses.

**Fail-closed (ADR-0008 Decision 1)**: any 401/403, non-JSON response, or
response missing the expected ``messages`` field raises
``SourceBlockedError``, which propagates out of ``fetch_posts`` and
terminates the fetch for the whole cycle -- no retry loop, no fingerprint or
proxy rotation. A 404 for a single symbol (no stream / unknown ticker) is
treated as ordinary "nothing to fetch for this symbol" and only skips that
symbol, matching this codebase's existing precedent for per-symbol gaps
(e.g. ``providers.market.stooq``'s unknown-symbol handling). A 429 raises
``RateLimitError`` (a quantity signal, not a block) and also ends the cycle,
since the vendor's own hourly ceiling has been reached.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time

import httpx

from claudetrade.config import StocktwitsConfig
from claudetrade.domain import SocialPost, SocialSource
from claudetrade.providers.base import (
    ProviderStatus,
    RateLimiter,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.utils.hashing import pseudonymise, text_hash
from claudetrade.utils.text import injection_risk_score, sanitize_social_text

log = logging.getLogger(__name__)

_VALID_SENTIMENT_LABELS = frozenset({"bullish", "bearish"})


class StocktwitsProvider:
    """Stocktwits public symbol-stream adapter for social posts.

    Fetches the most recent messages for a bounded, prioritised set of
    symbols per cycle. No pagination beyond the single stream call per
    symbol is attempted -- keeping request volume small and predictable is
    what makes the conservative unauthenticated rate budget workable across
    an entire universe refresh.
    """

    name: str = "stocktwits"
    source: SocialSource = SocialSource.STOCKTWITS

    def __init__(self, config: StocktwitsConfig):
        """Initialize the Stocktwits provider.

        Args:
            config: ``StocktwitsConfig`` with the watchlist, symbol cap and
                rate limit. No credentials are required -- this endpoint is
                keyless by the vendor's own design.
        """
        self.config = config
        self._rate_limiter = RateLimiter(
            config.rate_limit_per_minute,
            name="stocktwits",
            max_wait_s=config.request_timeout_s,
        )
        log.info(
            "Stocktwits provider configured (%d watchlist symbols, cap %d/cycle, %d/min)",
            len(config.watchlist_symbols),
            config.max_symbols_per_cycle,
            config.rate_limit_per_minute,
        )

    def status(self) -> ProviderStatus:
        """Report provider status."""
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=True,
            configured=True,
            message=(
                f"Stocktwits public stream ({len(self.config.watchlist_symbols)} watchlist "
                f"symbols, cap {self.config.max_symbols_per_cycle}/cycle)"
            ),
            supports_point_in_time=False,
            rate_limit_per_minute=self._rate_limiter.calls_per_minute,
            licence_note=(
                "Stocktwits public symbol-stream API "
                "(api.stocktwits.com/api/2/streams/symbol), documented and keyless for "
                "basic reads -- no scraping, no authentication bypassed. Unauthenticated "
                f"vendor budget is 200 requests/hour; this adapter is configured for "
                f"{self.config.rate_limit_per_minute}/min to keep a working margin. A "
                "message's self-declared sentiment tag is stored as "
                "SocialPost.sentiment_prior, a hint only -- the ensemble classifier still "
                "scores every post's text."
            ),
        )

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Fetch recent messages for a bounded, prioritised set of symbols.

        Args:
            since: Start timestamp; messages older than this are dropped.
            until: End timestamp; defaults to now.
            symbols: Priority order for this cycle (e.g. names with recent
                signals). Falls back to ``config.watchlist_symbols`` when not
                supplied. Either way the list is capped at
                ``config.max_symbols_per_cycle`` -- the budget guard that
                keeps this source within Stocktwits' unauthenticated rate
                ceiling regardless of universe size.
            limit: Maximum posts to return across all symbols.

        Returns:
            List of SocialPost, newest first, all sanitised and author-hashed.

        Raises:
            SourceBlockedError: on any block/challenge/unexpected response --
                terminates the fetch for the whole cycle (ADR-0008 Decision 1).
            RateLimitError: if the vendor's own rate limit is hit (HTTP 429).
        """
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)

        candidates = symbols if symbols else self.config.watchlist_symbols
        selected = _prioritised_symbols(candidates, self.config.max_symbols_per_cycle)

        posts: list[SocialPost] = []
        for symbol in selected:
            try:
                self._rate_limiter.acquire()
            except RateLimitError as exc:
                log.warning("Rate limit reached before fetching %s: %s", symbol, exc)
                continue

            _human_scale_jitter()

            try:
                posts.extend(self._fetch_symbol(symbol, since=since, until=until))
            except (RateLimitError, SourceBlockedError):
                # Fail-closed signals must propagate and end the cycle for
                # this source entirely, never degrade to "skip this symbol".
                raise
            except Exception as exc:
                log.warning("Failed to fetch stocktwits stream for %s: %s", symbol, exc)
                continue

            if limit is not None and len(posts) >= limit:
                break

        posts.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            posts = posts[:limit]
        return posts

    def _fetch_symbol(
        self, symbol: str, *, since: dt.datetime, until: dt.datetime
    ) -> list[SocialPost]:
        """Fetch and map one symbol's stream. Raises on any fail-closed signal."""
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        with httpx.Client(timeout=self.config.request_timeout_s) as client:
            response = client.get(url, headers={"User-Agent": self.config.user_agent})

        if response.status_code == 404:
            # An unknown ticker or a symbol with no stream at all -- ordinary,
            # expected "nothing here", not a block. Degrades this symbol only.
            log.debug("stocktwits: no stream for %s (HTTP 404)", symbol)
            return []

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                wait_s = float(retry_after)
            except (ValueError, TypeError):
                wait_s = 60.0
            raise RateLimitError(
                f"Stocktwits rate limit reached for {symbol}",
                provider="stocktwits",
                retry_after_s=wait_s,
            )

        if response.status_code in (401, 403):
            raise SourceBlockedError(
                f"Stocktwits denied access for {symbol} (HTTP {response.status_code}) -- "
                "fail-closed per ADR-0008 Decision 1: no retry, no fingerprint/proxy "
                "rotation, no CAPTCHA handling.",
                provider="stocktwits",
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise SourceBlockedError(
                f"Stocktwits returned unexpected content-type '{content_type}' for "
                f"{symbol} (possible block/challenge page) -- fail-closed per "
                "ADR-0008 Decision 1.",
                provider="stocktwits",
            )

        if response.status_code >= 400:
            raise SourceBlockedError(
                f"Stocktwits returned HTTP {response.status_code} for {symbol} -- "
                "fail-closed per ADR-0008 Decision 1.",
                provider="stocktwits",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"Stocktwits response for {symbol} was not valid JSON -- fail-closed "
                "per ADR-0008 Decision 1.",
                provider="stocktwits",
            ) from exc

        if not isinstance(payload, dict) or "messages" not in payload:
            raise SourceBlockedError(
                f"Stocktwits response for {symbol} had no 'messages' field -- "
                "unexpected shape, fail-closed per ADR-0008 Decision 1.",
                provider="stocktwits",
            )

        out: list[SocialPost] = []
        for message in payload.get("messages") or []:
            try:
                post = self._to_post(message, symbol)
            except Exception as exc:
                log.debug("skipping malformed stocktwits message for %s: %s", symbol, exc)
                continue
            if post.created_at < since or post.created_at > until:
                continue
            out.append(post)
        return out

    def _to_post(self, message: dict, symbol: str) -> SocialPost:
        """Map one Stocktwits message onto a sanitised ``SocialPost``."""
        external_id = str(message.get("id") or "")
        if not external_id:
            raise ValueError("message has no id")

        created_at = _parse_stocktwits_datetime(message.get("created_at"))
        if created_at is None:
            raise ValueError("message has no parseable created_at")

        body = message.get("body", "") or ""
        sanitised = sanitize_social_text(body)
        # Score the RAW text -- sanitisation has already rewritten any
        # injection phrase to "[filtered]" (same rule as every other adapter
        # in this package).
        injection_risk = injection_risk_score(body)

        user = message.get("user") or {}
        username = user.get("username") or ""
        author_hash = pseudonymise(username, salt="stocktwits") if username else ""
        followers = user.get("followers")
        author_age_days = _author_age_days(user.get("join_date"))

        likes = (message.get("likes") or {}).get("total") or 0
        replies = (message.get("conversation") or {}).get("replies") or 0

        sentiment_prior = _normalise_sentiment(
            ((message.get("entities") or {}).get("sentiment") or {}).get("basic")
        )

        return SocialPost(
            source=SocialSource.STOCKTWITS,
            external_id=external_id,
            created_at=created_at,
            text=sanitised,
            community=f"${symbol}",
            score=int(likes),
            num_comments=int(replies),
            num_reposts=0,
            num_replies=int(replies),
            author_hash=author_hash,
            author_age_days=author_age_days,
            author_karma=None,
            author_followers=float(followers) if followers is not None else None,
            is_comment=False,
            parent_id=None,
            is_removed=False,
            is_crosspost=False,
            crosspost_parent=None,
            text_hash=text_hash(sanitised),
            duplicate_group=None,
            injection_risk=injection_risk,
            fetched_at=dt.datetime.now(tz=dt.UTC),
            sentiment_prior=sentiment_prior,
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _prioritised_symbols(candidates: list[str], cap: int) -> list[str]:
    """Normalise, de-duplicate (keeping first occurrence), and cap the list.

    Order is preserved from ``candidates`` -- the caller is responsible for
    passing recent-signal/watchlist symbols first, which is what makes the
    cap a "cover what matters this cycle" budget rather than an arbitrary
    truncation.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in candidates:
        symbol = _normalise_symbol(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered[: max(0, cap)]


def _normalise_symbol(raw: str) -> str:
    return raw.strip().lstrip("$").upper()


def _normalise_sentiment(value: object) -> str | None:
    """Map Stocktwits' "Bullish"/"Bearish" tag onto the domain's lowercase form."""
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    return label if label in _VALID_SENTIMENT_LABELS else None


def _parse_stocktwits_datetime(value: object) -> dt.datetime | None:
    """Parse Stocktwits' ISO-8601 ``created_at``. ``None`` on any failure."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _author_age_days(join_date: object) -> float | None:
    """Days since a user's Stocktwits ``join_date`` (a plain ``YYYY-MM-DD``)."""
    if not isinstance(join_date, str) or not join_date:
        return None
    try:
        parsed = dt.date.fromisoformat(join_date[:10])
    except ValueError:
        return None
    return float((dt.datetime.now(tz=dt.UTC).date() - parsed).days)


def _human_scale_jitter() -> None:
    """Small randomised pause between requests (ADR-0008 Decision 1: rates
    must be "conservative, human-scale ... with jitter"), on top of the
    deterministic per-minute spacing the rate limiter already enforces."""
    time.sleep(random.uniform(0.02, 0.15))
