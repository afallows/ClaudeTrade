"""Reddit provider: real adapter to the official Reddit API, with an owner
cookie-session mode and a last-resort unauthenticated fallback (ADR-0008
Decision 1).

Four modes, tried in this preference order at construction time:

1. **Password grant** (``grant_type=password``) -- a script app's client
   id/secret *plus* the owner's own Reddit username/password. An official
   OAuth flow against the same endpoints as (3); preferred whenever all four
   credentials resolve, per the owner's explicit request ("use my reddit
   credentials ... retain the fallback to the standard API").
2. **Cookie-session mode** (``config.session_cookie_credential``) -- the
   owner's own logged-in ``reddit_session`` browser cookie, pasted from
   devtools. Reads the same ``www.reddit.com/r/<sub>/new.json`` endpoint as
   the public-JSON fallback below, but authenticated as the owner rather than
   anonymously, with a browser-style User-Agent (see the
   ``_COOKIE_SESSION_USER_AGENT`` constant below). This is the owner's own
   session, for personal use only (ADR-0008 Decision 1: "own credentials
   only"). Used when the password-grant credentials are not both configured
   but the cookie resolves; preferred over the client-credentials grant.
3. **Client-credentials grant** -- a script app's client id/secret alone.
   Reddit's app-only OAuth flow; no user login. Used when neither the
   password grant nor the cookie session are available but the client
   id/secret are.
4. **Public JSON fallback** -- reads ``www.reddit.com/r/<sub>/new.json``
   completely unauthenticated. Only used when *none* of the above resolve
   AND ``config.public_json_fallback`` is explicitly set. This path is
   ToS-gray for automated use (see the module/class docstrings) and is
   capped at a conservative, human-scale rate.

If none of the four resolves, ``NotConfiguredError`` is raised so the source
is cleanly disabled -- the pipeline continues without it.

Every mode's output goes through the same ``sanitize_social_text()`` /
author-hash / injection-score pipeline; only the transport and
authentication differ. Cookie-session mode reuses the exact same listing
fetch/parse code path as public-JSON mode (pagination, external_id,
crosspost, and injection-scoring behaviour) -- only the URL headers differ.

**Fail-closed (ADR-0008 Decision 1)**: in cookie-session and public-JSON
modes, any block, challenge, CAPTCHA, or unexpected response (HTTP 401/403, a
non-JSON body, or any other non-2xx status) raises ``SourceBlockedError``,
which is *not* caught by this module -- it propagates out of ``fetch_posts``
and terminates the whole fetch for this cycle. There is no retry loop, no
fingerprint/proxy rotation, and no CAPTCHA handling anywhere in this file.
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
    SourceBlockedError,
)
from claudetrade.secrets import get_secret
from claudetrade.utils.hashing import pseudonymise, text_hash
from claudetrade.utils.text import injection_risk_score, sanitize_social_text

log = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

#: Set of modes that hit the public www.reddit.com/r/<sub>/new.json listing
#: endpoint directly (as opposed to oauth.reddit.com with a bearer token).
#: Both share the exact same fetch/parse/fail-closed code path; only the
#: request headers differ (see ``_fetch_subreddit``).
_JSON_LISTING_MODES = frozenset({"public_json", "cookie_session"})

#: Realistic, current-ish desktop Chrome UA -- deliberately used ONLY for
#: cookie-session mode. The probe evidence motivating this adapter (see
#: docs/api-providers.md) is that Reddit's public JSON endpoint 403s any
#: non-browser client regardless of User-Agent, but returns 200 to a real,
#: logged-in browser tab -- i.e. it gates on the session cookie, not the UA
#: string. Sending a browser-style UA alongside the owner's own session
#: cookie mirrors what their actual browser sends for this one mode; the
#: descriptive ``config.user_agent`` (identifying this as an automated
#: research client) remains in force for the OAuth modes, where an
#: identifying UA is the honest and appropriate choice.
_COOKIE_SESSION_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class RedditProvider:
    """Reddit adapter for social posts: OAuth (password or client-credentials
    grant), an owner cookie-session mode, or an opt-in unauthenticated
    public-JSON fallback.

    Fetches recent posts from configured subreddits, paging until the
    requested time window is covered. Respects rate limits and Reddit's API
    contract for the OAuth modes; fails closed immediately in cookie-session
    or public-JSON mode on any sign of a block (see the module docstring).
    """

    name: str = "reddit"
    source: SocialSource = SocialSource.REDDIT

    def __init__(self, config: RedditConfig):
        """Initialize the Reddit provider, selecting the best available mode.

        Args:
            config: RedditConfig with credentials, subreddits, rate limits.

        Raises:
            NotConfiguredError: if no OAuth credentials resolve and
                ``config.public_json_fallback`` is not set.
        """
        self.config = config

        client_id_secret = get_secret(config.client_id_credential)
        client_secret_secret = get_secret(config.client_secret_credential)
        username_secret = get_secret(config.username_credential)
        password_secret = get_secret(config.password_credential)
        session_cookie_secret = get_secret(config.session_cookie_credential)
        token_v2_secret = get_secret(config.token_v2_credential)

        has_client_creds = client_id_secret is not None and client_secret_secret is not None
        has_user_creds = username_secret is not None and password_secret is not None

        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._session_cookie: str | None = None
        self._token_v2: str | None = None
        #: Whether the second cookie (see ``RedditConfig.token_v2_credential``)
        #: resolved -- exposed for ``status()`` and the credentials-test
        #: endpoint so the owner can tell which cookie combination is in use.
        self.has_token_v2 = token_v2_secret is not None

        if has_client_creds and has_user_creds:
            self.mode = "password"
        elif session_cookie_secret is not None:
            self.mode = "cookie_session"
            log.warning(
                "Reddit provider configured (mode=cookie_session, cookies=%s): this reads "
                "the public listing endpoint authenticated with the owner's own session "
                "cookie(s) (personal use only, ADR-0008 Decision 1) -- not the official "
                "OAuth API.",
                "reddit_session+token_v2" if token_v2_secret is not None else "reddit_session only",
            )
        elif has_client_creds:
            self.mode = "client_credentials"
        elif config.public_json_fallback:
            self.mode = "public_json"
            log.warning(
                "Reddit OAuth credentials not configured; falling back to the "
                "unauthenticated public JSON listing endpoint. This path is "
                "ToS-gray for automated use (ADR-0008 Decision 1) -- configure "
                "%s/%s (or %s/%s, or %s for cookie-session mode) to prefer an "
                "authenticated path.",
                config.client_id_credential,
                config.client_secret_credential,
                config.username_credential,
                config.password_credential,
                config.session_cookie_credential,
            )
        else:
            raise NotConfiguredError(
                f"Reddit credentials not found: {config.client_id_credential}, "
                f"{config.client_secret_credential} (or {config.username_credential}, "
                f"{config.password_credential} for the password grant, or "
                f"{config.session_cookie_credential} for cookie-session mode). Set "
                "reddit.public_json_fallback = true to allow the unauthenticated "
                "fallback instead.",
                provider="reddit",
            )

        if self.mode in ("password", "client_credentials"):
            self._client_id = client_id_secret.reveal()  # type: ignore[union-attr]
            self._client_secret = client_secret_secret.reveal()  # type: ignore[union-attr]
            if self.mode == "password":
                self._username = username_secret.reveal()  # type: ignore[union-attr]
                self._password = password_secret.reveal()  # type: ignore[union-attr]
        elif self.mode == "cookie_session":
            self._session_cookie = session_cookie_secret.reveal()  # type: ignore[union-attr]
            if token_v2_secret is not None:
                self._token_v2 = token_v2_secret.reveal()

        self._access_token: str | None = None
        self._token_expires_at = 0.0

        if self.mode == "public_json":
            rate_limit = config.public_json_rate_limit_per_minute
        elif self.mode == "cookie_session":
            rate_limit = config.session_rate_limit_per_minute
        else:
            rate_limit = config.rate_limit_per_minute
        self._rate_limiter = RateLimiter(
            rate_limit,
            name="reddit",
            max_wait_s=config.request_timeout_s,
        )

        log.info(
            "Reddit provider configured (mode=%s) for subreddits: %s",
            self.mode,
            config.subreddits,
        )

    def status(self) -> ProviderStatus:
        """Report provider status, honest about which mode is in effect."""
        if self.mode == "password":
            message = f"Reddit OAuth, password grant ({len(self.config.subreddits)} subreddits)"
            licence_note = (
                "Official Reddit API, password grant (owner's own account credentials, "
                "ADR-0008 Decision 1)."
            )
        elif self.mode == "cookie_session":
            cookies = "reddit_session+token_v2" if self.has_token_v2 else "reddit_session only"
            message = (
                f"Reddit cookie-session, owner's own session ({cookies}, "
                f"{len(self.config.subreddits)} subreddits)"
            )
            licence_note = (
                "Reads www.reddit.com/r/<sub>/new.json authenticated with the "
                f"owner's own session cookie(s) ({cookies}) and a browser-style "
                "User-Agent (ADR-0008 Decision 1: personal use, own credentials "
                "only). Not the official OAuth API -- shares the public-JSON "
                "path's fail-closed behaviour exactly: no retry, no "
                "fingerprint/proxy rotation, no CAPTCHA handling."
            )
        elif self.mode == "client_credentials":
            message = (
                f"Reddit OAuth, client-credentials grant ({len(self.config.subreddits)} "
                "subreddits)"
            )
            licence_note = "Official Reddit API, app-only client-credentials grant."
        else:
            message = (
                f"Reddit public JSON fallback, UNAUTHENTICATED "
                f"({len(self.config.subreddits)} subreddits)"
            )
            licence_note = (
                "Unauthenticated read of www.reddit.com/r/<sub>/new.json. This is "
                "ToS-gray for automated/scheduled use, not a sanctioned API "
                "integration -- an opt-in last resort only (ADR-0008 Decision 1), "
                "rate-capped and fail-closed on any block/challenge signal."
            )
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=True,
            configured=True,
            message=message,
            supports_point_in_time=False,
            rate_limit_per_minute=self._rate_limiter.calls_per_minute,
            licence_note=licence_note,
        )

    def _ensure_token(self) -> None:
        """Refresh the OAuth access token if expired (OAuth modes only)."""
        now = time.time()
        if self._access_token is not None and now < self._token_expires_at:
            return

        self._rate_limiter.acquire()

        grant_data: dict[str, str] = (
            {
                "grant_type": "password",
                "username": self._username or "",
                "password": self._password or "",
            }
            if self.mode == "password"
            else {"grant_type": "client_credentials"}
        )

        try:
            with httpx.Client(timeout=self.config.request_timeout_s) as client:
                response = client.post(
                    _TOKEN_URL,
                    auth=(self._client_id, self._client_secret),
                    data=grant_data,
                    headers={"User-Agent": self.config.user_agent},
                )
                response.raise_for_status()
                payload = response.json()
                self._access_token = payload["access_token"]
                self._token_expires_at = now + payload.get("expires_in", 3600) - 60
        except Exception as exc:
            log.error("Reddit token refresh failed (mode=%s): %s", self.mode, exc)
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

        Raises:
            SourceBlockedError: (cookie-session or public-JSON mode only) on
                any block, challenge, or unexpected response -- this
                terminates the fetch for the whole cycle rather than
                degrading per-subreddit, per ADR-0008 Decision 1's
                fail-closed constraint.
        """
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)

        if self.mode not in _JSON_LISTING_MODES:
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
            except (RateLimitError, SourceBlockedError):
                # Both are fail-closed signals that must propagate: a vendor
                # rate limit or block/challenge terminates the fetch for the
                # whole cycle, never just this one subreddit.
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
        """Page through ``/r/<sub>/new`` (or its public-JSON twin) until posts
        predate ``since``.

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

            if self.mode == "cookie_session":
                url = f"https://www.reddit.com/r/{subreddit}/new.json"
                headers = {
                    "User-Agent": _COOKIE_SESSION_USER_AGENT,
                    "Cookie": self._cookie_header(),
                }
            elif self.mode == "public_json":
                url = f"https://www.reddit.com/r/{subreddit}/new.json"
                headers = {"User-Agent": self.config.user_agent}
            else:
                url = f"https://oauth.reddit.com/r/{subreddit}/new"
                headers = {
                    "Authorization": f"bearer {self._access_token}",
                    "User-Agent": self.config.user_agent,
                }

            with httpx.Client(timeout=self.config.request_timeout_s) as client:
                response = client.get(url, headers=headers, params=params)

            if self.mode in _JSON_LISTING_MODES:
                self._fail_closed_if_blocked(response, subreddit)
                payload = response.json()
            else:
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

    def _cookie_header(self) -> str:
        """Cookie header for cookie-session mode.

        Reddit's own web frontend sends both ``reddit_session`` and
        ``token_v2`` (both HttpOnly -- see ``RedditConfig.token_v2_credential``).
        When ``token_v2`` was not configured, behaviour is unchanged from
        before this was added: ``reddit_session`` alone.
        """
        if self._token_v2:
            return f"reddit_session={self._session_cookie}; token_v2={self._token_v2}"
        return f"reddit_session={self._session_cookie}"

    def _fail_closed_if_blocked(self, response: httpx.Response, subreddit: str) -> None:
        """Public-JSON and cookie-session modes only: raise on any
        block/challenge/unexpected response.

        ADR-0008 Decision 1 is explicit that this terminates the fetch for
        the cycle rather than being retried, evaded, or worked around. A 429
        raises ``RateLimitError`` (a quantity signal, same shape as the OAuth
        path); everything else that isn't a clean 2xx JSON response raises
        ``SourceBlockedError``, which is not caught anywhere in this module.
        In cookie-session mode a 401/403 typically means the pasted session
        cookie has expired or been logged out -- re-export a fresh one from
        the browser to resume.
        """
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                wait_s = float(retry_after)
            except (ValueError, TypeError):
                wait_s = 60.0
            raise RateLimitError(
                f"Reddit public-JSON rate limit for r/{subreddit}",
                provider="reddit",
                retry_after_s=wait_s,
            )

        if response.status_code in (401, 403):
            raise SourceBlockedError(
                f"Reddit public JSON endpoint denied access for r/{subreddit} "
                f"(HTTP {response.status_code}) -- fail-closed per ADR-0008 "
                "Decision 1: no retry, no fingerprint/proxy rotation, no "
                "CAPTCHA handling.",
                provider="reddit",
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            # A block/challenge page is typically served as HTML with a 200,
            # not a clean error status -- this is the "unexpected response"
            # case ADR-0008 Decision 1 also requires failing closed on.
            raise SourceBlockedError(
                f"Reddit public JSON endpoint returned unexpected content-type "
                f"'{content_type}' for r/{subreddit} (possible block/challenge "
                "page) -- fail-closed per ADR-0008 Decision 1.",
                provider="reddit",
            )

        if response.status_code >= 400:
            raise SourceBlockedError(
                f"Reddit public JSON endpoint returned HTTP {response.status_code} "
                f"for r/{subreddit} -- fail-closed per ADR-0008 Decision 1.",
                provider="reddit",
            )

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
