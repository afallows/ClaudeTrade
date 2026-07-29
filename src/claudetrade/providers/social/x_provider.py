"""X (Twitter) v2 API provider: real adapter to official X API.

Uses the X API v2 recent-search endpoint with a bearer token. Requires
meaningful search volume for a paid tier. If credential is absent, raises
NotConfiguredError so the source is cleanly disabled.

All text is sanitised via sanitize_social_text() and authors are salted
hashes (never stored as usernames).
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from claudetrade.config import XConfig
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


class XProvider:
    """Official X (Twitter) API v2 adapter for social posts.

    Fetches recent posts matching configured query terms using the
    recent-search endpoint. Requires a paid API tier for meaningful volume.
    Respects rate limits and X's API contract.
    """

    name: str = "x"
    source: SocialSource = SocialSource.X

    def __init__(self, config: XConfig):
        """Initialize the X provider.

        Args:
            config: XConfig with bearer token credential and query terms.

        Raises:
            NotConfiguredError: if bearer token is not available.
        """
        self.config = config

        # Resolve credentials
        bearer_secret = get_secret(config.bearer_credential)
        if bearer_secret is None:
            raise NotConfiguredError(
                f"X bearer token '{config.bearer_credential}' not configured",
                provider="x",
            )

        self._bearer_token = bearer_secret.reveal()

        self._rate_limiter = RateLimiter(
            config.rate_limit_per_minute,
            name="x",
            max_wait_s=config.request_timeout_s,
        )

        log.info("X provider configured with %d query terms", len(config.query_terms))

    def status(self) -> ProviderStatus:
        """Report provider status."""
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=True,
            configured=True,
            message=f"X API v2 ({len(self.config.query_terms)} queries)",
            supports_point_in_time=False,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
            licence_note="Official X API v2; requires paid tier for production volume",
        )

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,  # noqa: ARG002
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Fetch recent posts matching configured query terms.

        Args:
            since: Start timestamp (honoured by X API with start_time parameter).
            until: End timestamp (honoured by X API with end_time parameter).
            symbols: Optional symbols to search for (not used; queries are pre-configured).
            limit: Maximum posts to return across all queries.

        Returns:
            List of SocialPost, newest first, all sanitised and author-hashed.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if until is not None and until.tzinfo is None:
            raise ValueError("until must be timezone-aware")
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)
        if since > until:
            raise ValueError("since must not be later than until")

        posts: list[SocialPost] = []

        for query in self.config.query_terms:
            try:
                self._rate_limiter.acquire()
            except RateLimitError as exc:
                log.warning("Rate limit reached for query '%s': %s", query, exc)
                continue

            try:
                with httpx.Client(timeout=self.config.request_timeout_s) as client:
                    response = client.get(
                        "https://api.x.com/2/tweets/search/recent",
                        headers={"Authorization": f"Bearer {self._bearer_token}"},
                        params={
                            "query": query,
                            "max_results": self.config.max_results_per_query,
                            "start_time": since.isoformat(),
                            "end_time": until.isoformat(),
                            "tweet.fields": (
                                "created_at,author_id,public_metrics,lang"
                            ),
                            "expansions": "author_id",
                            "user.fields": "created_at,public_metrics,username",
                        },
                    )

                    if response.status_code == 429:
                        # Respect Retry-After header
                        retry_after = response.headers.get("Retry-After", "60")
                        try:
                            wait_s = float(retry_after)
                        except (ValueError, TypeError):
                            wait_s = 60.0
                        raise RateLimitError(
                            f"X rate limit for query '{query}'",
                            provider="x",
                            retry_after_s=wait_s,
                        )

                    if response.status_code == 403:
                        log.error(
                            "X API access denied; likely requires paid tier. Query: %s", query
                        )
                        continue

                    response.raise_for_status()
                    payload = response.json()

                    # Build author map from includes
                    users_by_id: dict[str, dict] = {}
                    for user in payload.get("includes", {}).get("users", []):
                        users_by_id[user["id"]] = user

                    # Extract tweets
                    for tweet in payload.get("data", []):
                        created_at_str = tweet.get("created_at")
                        if created_at_str:
                            created_at = dt.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        else:
                            created_at = dt.datetime.now(tz=dt.UTC)

                        text = tweet.get("text", "")
                        sanitised = sanitize_social_text(text)

                        author_id = tweet.get("author_id", "")
                        author_user = users_by_id.get(author_id, {})
                        author_name = author_user.get("username", "")
                        # Hash the stable API id when the optional username is
                        # absent. This preserves unique-author metrics without
                        # persisting either identifier in plaintext.
                        author_key = author_name or author_id
                        author_hash = pseudonymise(author_key, salt="x") if author_key else ""

                        author_age_days = None
                        account_created = author_user.get("created_at")
                        if account_created:
                            created = dt.datetime.fromisoformat(
                                account_created.replace("Z", "+00:00")
                            )
                            author_age_days = max(0, (created_at - created).days)

                        metrics = tweet.get("public_metrics", {})
                        post = SocialPost(
                            source=SocialSource.X,
                            external_id=tweet.get("id", ""),
                            created_at=created_at,
                            text=sanitised,
                            community="",
                            score=metrics.get("like_count", 0),
                            num_comments=metrics.get("reply_count", 0),
                            num_reposts=metrics.get("retweet_count", 0),
                            num_replies=metrics.get("reply_count", 0),
                            author_hash=author_hash,
                            author_age_days=author_age_days,
                            author_karma=None,
                            author_followers=author_user.get("public_metrics", {}).get(
                                "followers_count"
                            ),
                            is_comment=False,
                            parent_id=tweet.get("in_reply_to_user_id"),
                            is_removed=False,
                            is_crosspost=False,
                            crosspost_parent=None,
                            text_hash=text_hash(sanitised),
                            duplicate_group=None,
                            injection_risk=injection_risk_score(sanitised),
                            fetched_at=dt.datetime.now(tz=dt.UTC),
                        )
                        posts.append(post)

                        if limit is not None and len(posts) >= limit:
                            break

                    if limit is not None and len(posts) >= limit:
                        break

            except RateLimitError:
                raise
            except Exception as exc:
                log.warning("Failed to fetch from X with query '%s': %s", query, exc)
                continue

        posts.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            posts = posts[:limit]

        return posts
