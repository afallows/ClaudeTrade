"""Reddit OAuth provider: real adapter to official Reddit API.

Uses client-credentials OAuth only (no user login). Requires reddit_client_id
and reddit_client_secret credentials. If credentials are absent, raises
NotConfiguredError so the source is cleanly disabled.

All text is sanitised via sanitize_social_text() and authors are salted
hashes (never stored as usernames).
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

from claudetrade.config import RedditConfig
from claudetrade.domain import SocialPost, SocialSource
from claudetrade.providers.base import (
    NotConfiguredError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
)
from claudetrade.secrets import get_secret
from claudetrade.utils.hashing import pseudonymise, text_hash
from claudetrade.utils.text import injection_risk_score, sanitize_social_text

log = logging.getLogger(__name__)


class RedditProvider:
    """Official Reddit OAuth API adapter for social posts.

    Fetches recent posts from configured subreddits using client-credentials
    flow. Respects rate limits and Reddit's API contract.
    """

    name: str = "reddit"
    source: SocialSource = SocialSource.REDDIT

    def __init__(self, config: RedditConfig):
        """Initialize the Reddit provider.

        Args:
            config: RedditConfig with credentials, subreddits, rate limits.

        Raises:
            NotConfiguredError: if credentials are not available.
        """
        self.config = config

        # Resolve credentials
        client_id_secret = get_secret(config.client_id_credential)
        client_secret_secret = get_secret(config.client_secret_credential)

        if client_id_secret is None or client_secret_secret is None:
            raise NotConfiguredError(
                f"Reddit credentials not found: {config.client_id_credential}, "
                f"{config.client_secret_credential}",
                provider="reddit",
            )

        self._client_id = client_id_secret.reveal()
        self._client_secret = client_secret_secret.reveal()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

        self._rate_limiter = RateLimiter(
            config.rate_limit_per_minute,
            name="reddit",
            max_wait_s=config.request_timeout_s,
        )

        log.info("Reddit provider configured for subreddits: %s", config.subreddits)

    def status(self) -> ProviderStatus:
        """Report provider status."""
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=True,
            configured=True,
            message=f"Reddit OAuth ({len(self.config.subreddits)} subreddits)",
            supports_point_in_time=False,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
            licence_note="Official Reddit API; requires OAuth credentials",
        )

    def _ensure_token(self) -> None:
        """Refresh the OAuth access token if expired."""
        now = time.time()
        if self._access_token is not None and now < self._token_expires_at:
            return

        try:
            self._rate_limiter.acquire()
        except RateLimitError:
            raise

        try:
            with httpx.Client(timeout=self.config.request_timeout_s) as client:
                response = client.post(
                    "https://www.reddit.com/api/v1/access_token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": self.config.user_agent},
                )
                response.raise_for_status()
                payload = response.json()
                self._access_token = payload["access_token"]
                self._token_expires_at = now + payload.get("expires_in", 3600) - 60
        except Exception as exc:
            log.error("Reddit token refresh failed: %s", exc)
            raise RateLimitError(
                f"Reddit token refresh failed: {exc}",
                provider="reddit",
                retry_after_s=60.0,
            ) from exc

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,  # noqa: ARG002
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Fetch recent posts from configured subreddits.

        Args:
            since: Start timestamp (honoured best-effort by Reddit API).
            until: End timestamp (ignored; Reddit API doesn't support point-in-time).
            symbols: Optional symbols to search for (not used; Reddit queries are pre-configured).
            limit: Maximum posts to return.

        Returns:
            List of SocialPost, newest first, all sanitised and author-hashed.
        """
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)

        self._ensure_token()

        posts: list[SocialPost] = []

        for subreddit in self.config.subreddits:
            try:
                self._rate_limiter.acquire()
            except RateLimitError as exc:
                log.warning("Rate limit reached while fetching r/%s: %s", subreddit, exc)
                continue

            try:
                posts.extend(
                    self._fetch_subreddit(
                        subreddit,
                        since=since,
                        remaining=None if limit is None else limit - len(posts),
                    )
                )
                if limit is not None and len(posts) >= limit:
                    break
            except RateLimitError:
                raise
            except Exception as exc:
                log.warning("Failed to fetch r/%s: %s", subreddit, exc)
                continue

        posts.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            posts = posts[:limit]

        return posts

    def _fetch_subreddit(
        self, subreddit: str, *, since: dt.datetime, remaining: int | None
    ) -> list[SocialPost]:
        """Page through ``/r/<sub>/new`` until posts predate ``since``.

        ``/new`` is strictly reverse-chronological, so the first post older
        than ``since`` means every later one is older too and paging can stop.
        Without paging, a listing call returns at most 100 items and a busy
        subreddit would silently drop everything beyond that -- an
        under-fetch that looks like "quiet day" rather than a data gap.
        """
        collected: list[SocialPost] = []
        after: str | None = None
        page_size = max(1, min(100, self.config.posts_per_subreddit))

        for _ in range(self.config.max_pages_per_subreddit):
            if remaining is not None and len(collected) >= remaining:
                break

            params: dict[str, object] = {"limit": page_size}
            if after:
                params["after"] = after

            with httpx.Client(timeout=self.config.request_timeout_s) as client:
                response = client.get(
                    f"https://oauth.reddit.com/r/{subreddit}/new",
                    headers={
                        "Authorization": f"bearer {self._access_token}",
                        "User-Agent": self.config.user_agent,
                    },
                    params=params,
                )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "60")
                    try:
                        wait_s = float(retry_after)
                    except (ValueError, TypeError):
                        wait_s = 60.0
                    raise RateLimitError(
                        f"Reddit rate limit for r/{subreddit}",
                        provider="reddit",
                        retry_after_s=wait_s,
                    )

                response.raise_for_status()
                payload = response.json()

            children = payload.get("data", {}).get("children", [])
            if not children:
                break

            reached_window_start = False
            for item in children:
                data = item.get("data", {})
                post_created_at = dt.datetime.fromtimestamp(
                    data.get("created_utc", 0), tz=dt.UTC
                )
                if post_created_at < since:
                    reached_window_start = True
                    break

                collected.append(self._to_post(data, subreddit, post_created_at))
                if remaining is not None and len(collected) >= remaining:
                    break

            if reached_window_start:
                break
            after = payload.get("data", {}).get("after")
            if not after:
                break
            self._rate_limiter.acquire()

        return collected

    def _to_post(
        self, data: dict, subreddit: str, created_at: dt.datetime
    ) -> SocialPost:
        """Map one Reddit listing child onto a sanitised ``SocialPost``."""
        text = data.get("title", "") + "\n" + data.get("selftext", "")
        sanitised = sanitize_social_text(text)
        # Score the RAW text. Sanitisation has already rewritten any injection
        # phrase to "[filtered]", so scoring the sanitised copy always returns
        # ~0 and the stored risk flag would be permanently blind -- both for
        # data-quality reporting and for the AI classifier's block, which
        # consults this value.
        injection_risk = injection_risk_score(text)

        author = data.get("author", "")
        author_hash = (
            pseudonymise(author, salt="reddit") if author and author != "[deleted]" else ""
        )

        # A crosspost is identified by carrying a parent, NOT by
        # ``is_crosspostable`` -- that flag means "others are permitted to
        # crosspost this", which is true of most ordinary posts and would
        # mislabel nearly the whole corpus as duplicated content.
        parent_list = data.get("crosspost_parent_list") or []
        crosspost_parent = data.get("crosspost_parent") or (
            parent_list[0].get("name") if parent_list else None
        )

        return SocialPost(
            source=SocialSource.REDDIT,
            # Reddit's ``name`` is the fullname ("t3_abc123"), unique across
            # object types; the bare ``id`` can collide between a post and a
            # comment, which would silently deduplicate distinct content.
            external_id=data.get("name") or f"t3_{data.get('id', '')}",
            created_at=created_at,
            text=sanitised,
            community=f"r/{subreddit}",
            score=data.get("score", 0),
            num_comments=data.get("num_comments", 0),
            num_reposts=0,
            num_replies=0,
            author_hash=author_hash,
            author_age_days=None,
            author_karma=None,
            author_followers=None,
            is_comment=False,
            parent_id=None,
            is_removed=data.get("removed_by_category") is not None,
            is_crosspost=crosspost_parent is not None,
            crosspost_parent=crosspost_parent,
            text_hash=text_hash(sanitised),
            duplicate_group=None,
            injection_risk=injection_risk,
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )
