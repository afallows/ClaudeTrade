"""X (Twitter) provider: official API v2 adapter, plus an opt-in cookie-session
mode (ADR-0008 Decision 1 / Decision 5's "broader sentiment universe
including X via the owner's personal session").

Two independent live paths, tried in this preference order at construction
time:

1. **Official API v2** (``bearer_credential``) -- the recent-search endpoint
   with a bearer token. Requires a paid tier for meaningful search volume.
   Always preferred when configured: ADR-0008 Decision 1 requires the
   official API remain first-choice.
2. **Cookie-session mode** (``config.session_enabled``), only reached when
   the official path has no bearer token configured. The owner's own
   logged-in x.com session (``auth_token`` + ``ct0`` cookies, exported from
   the browser) drives the same internal GraphQL endpoints the logged-in web
   client itself uses, searching cashtags for configured watchlist symbols.

**This automates a logged-in personal X account. Doing so violates X's Terms
of Service and can lead to account suspension of that account.** The owner
accepts this risk for their own account (ADR-0008 Decision 1); this
application never bundles or defaults credentials for this path, never
solves a CAPTCHA/challenge, never rotates a fingerprint or proxy to work
around a block, and is off by default (``x.session_enabled = False``).

Both paths' output goes through the same ``sanitize_social_text()`` /
author-hash / injection-score pipeline every other adapter in this package
uses.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time

import httpx

from claudetrade.config import XConfig
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


# ============================================================================
# X internal web-client endpoint constants -- SESSION MODE ONLY.
#
# Everything in this block targets x.com's *internal*, unversioned GraphQL
# API that the logged-in web client itself calls -- NOT the official,
# contracted API v2 used by the "official" mode above. X changes these
# paths, query IDs and response shapes without notice and without a
# deprecation window. Breakage here is EXPECTED MAINTENANCE, not a defect in
# this adapter's logic: when it breaks, ``_extract_tweet_results``'s
# fail-closed behaviour (any unexpected shape raises ``SourceBlockedError``)
# is what protects the pipeline -- this module must never guess at a new
# shape or "helpfully" work around a change. Refresh these constants only by
# capturing a fresh, real browser network request (devtools -> Network,
# filter "graphql") while logged in as the account owner, then update this
# block alone; nothing else in this file should need to change alongside it.
#
# There is deliberately NO built-in query ID. It is account-independent but
# rotates without notice, so it belongs to the operator's own browser capture
# and lives in ``XConfig.session_query_id``. Shipping a placeholder here (as
# this module previously did) produced a 404 with an empty body and no
# content-type header, which the checks below then reported as a login wall
# -- sending operators off to re-export cookies that were perfectly valid.
# ``_session_search_url`` is the single place the URL is built.
# ============================================================================
_SEARCH_GRAPHQL_URL_TEMPLATE = "https://x.com/i/api/graphql/{query_id}/SearchTimeline"

#: How an operator gets a query ID. Repeated verbatim in every place that can
#: report the missing-ID failure, because the failure is only actionable with
#: the click-path attached.
_QUERY_ID_CAPTURE_HINT = (
    "Set x.session_query_id in config.toml to the value from your own browser "
    "capture: log in to x.com, open devtools -> Network, filter 'graphql', "
    "search any cashtag, and copy the path segment immediately before "
    "'/SearchTimeline'."
)
#: The static, public bearer token the logged-in web client itself sends
#: alongside session cookies (not a secret -- it identifies the web app, the
#: cookies are what authenticate the *user*). Left as a documented constant
#: here rather than a credential because it carries no account-specific
#: authority on its own.
_WEB_CLIENT_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_SEARCH_GRAPHQL_FEATURES = {
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
# ============================================================================


class XProvider:
    """X (Twitter) adapter for social posts: official API v2, or an opt-in
    cookie-session mode when no official credential is configured.

    Fetches recent posts matching configured query terms (official mode) or
    cashtag-searches configured watchlist symbols (session mode). Respects
    rate limits and fails closed on any block signal in session mode.
    """

    name: str = "x"
    source: SocialSource = SocialSource.X

    def __init__(self, config: XConfig):
        """Initialize the X provider, selecting the best available mode.

        Args:
            config: XConfig with bearer token credential and/or session
                cookie credentials.

        Raises:
            NotConfiguredError: if neither the official bearer token nor
                (with ``session_enabled=True``) both session cookies resolve.
        """
        self.config = config

        bearer_secret = get_secret(config.bearer_credential)
        if bearer_secret is not None:
            self.mode = "official"
            self._bearer_token: str | None = bearer_secret.reveal()
            self._auth_token: str | None = None
            self._ct0: str | None = None
            self._rate_limiter = RateLimiter(
                config.rate_limit_per_minute, name="x", max_wait_s=config.request_timeout_s
            )
            log.info("X provider configured (mode=official) with %d query terms",
                      len(config.query_terms))
            return

        if config.session_enabled:
            auth_token_secret = get_secret(config.auth_token_credential)
            ct0_secret = get_secret(config.ct0_credential)
            if auth_token_secret is not None and ct0_secret is not None:
                self.mode = "session"
                self._bearer_token = None
                self._auth_token = auth_token_secret.reveal()
                self._ct0 = ct0_secret.reveal()
                self._rate_limiter = RateLimiter(
                    config.session_rate_limit_per_minute,
                    name="x_session",
                    max_wait_s=config.session_request_timeout_s,
                )
                log.warning(
                    "X provider configured (mode=session): this automates a logged-in "
                    "personal X account, which violates X's Terms of Service and can "
                    "lead to account suspension. Owner-accepted risk (ADR-0008 "
                    "Decision 1). %d watchlist symbols.",
                    len(config.session_symbols),
                )
                return
            raise NotConfiguredError(
                f"X session mode is enabled but credentials are missing: "
                f"{config.auth_token_credential}, {config.ct0_credential}",
                provider="x",
            )

        raise NotConfiguredError(
            f"X bearer token '{config.bearer_credential}' not configured, and "
            "x.session_enabled is False",
            provider="x",
        )

    def status(self) -> ProviderStatus:
        """Report provider status."""
        if self.mode == "official":
            return ProviderStatus(
                name=self.name,
                kind="social",
                available=True,
                configured=True,
                message=f"X API v2 ({len(self.config.query_terms)} queries)",
                supports_point_in_time=False,
                rate_limit_per_minute=self._rate_limiter.calls_per_minute,
                licence_note="Official X API v2; requires paid tier for production volume",
            )
        # Session mode needs three things, and reporting "configured" on the
        # strength of the two credentials alone is what let a missing query ID
        # masquerade as an authentication failure at fetch time. Each missing
        # piece is named here so the Providers panel answers "why is X quiet?"
        # without anyone having to trigger a live probe.
        blockers = []
        if not (self.config.session_query_id or "").strip():
            blockers.append(f"no GraphQL query ID -- {_QUERY_ID_CAPTURE_HINT}")
        if not self.config.session_symbols:
            blockers.append(
                "no watchlist symbols -- set x.session_symbols in config.toml; "
                "session mode only searches cashtags from that list, so an empty "
                "list means every cycle fetches nothing"
            )
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=not blockers,
            configured=not blockers,
            message=(
                f"X cookie-session mode, UNOFFICIAL "
                f"({len(self.config.session_symbols)} watchlist symbols)"
                if not blockers
                else "X cookie-session mode cannot run: " + "; ".join(blockers)
            ),
            supports_point_in_time=False,
            rate_limit_per_minute=self._rate_limiter.calls_per_minute,
            licence_note=(
                "Automates the owner's own logged-in x.com session against internal, "
                "unversioned web-client GraphQL endpoints -- NOT the official API. "
                "This violates X's Terms of Service and risks suspension of that "
                "account; the owner accepts this risk for their own account "
                "(ADR-0008 Decision 1). Off by default; fails closed (disables the "
                "source for the rest of the cycle, no workaround attempted) on any "
                "401/403/challenge/rate-limit signal or unparseable response."
            ),
        )

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,  # noqa: ARG002
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Fetch recent posts for the configured mode.

        Args:
            since: Start timestamp.
            until: End timestamp; defaults to now.
            symbols: Unused; queries/cashtags are pre-configured.
            limit: Maximum posts to return across all queries.

        Returns:
            List of SocialPost, newest first, all sanitised and author-hashed.

        Raises:
            SourceBlockedError: (session mode only) on any 401/403, challenge,
                or unparseable response -- terminates the fetch for the whole
                cycle per ADR-0008 Decision 1's fail-closed constraint.
        """
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)

        if self.mode == "official":
            return self._fetch_official_posts(since=since, until=until, limit=limit)
        return self._fetch_session_posts(since=since, until=until, limit=limit)

    # --- official API v2 mode (unchanged code path) ------------------------

    def _fetch_official_posts(
        self, *, since: dt.datetime, until: dt.datetime, limit: int | None
    ) -> list[SocialPost]:
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
                        "https://api.twitter.com/2/tweets/search/recent",
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
                            "user.fields": "created_at,public_metrics",
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
                        # Score the RAW text: sanitisation rewrites injection
                        # phrases to "[filtered]", so scoring the sanitised
                        # copy would always return ~0 and leave the stored
                        # risk flag permanently blind.
                        injection_risk = injection_risk_score(text)

                        author_id = tweet.get("author_id", "")
                        author_user = users_by_id.get(author_id, {})
                        author_name = author_user.get("username", "")
                        author_hash = (
                            pseudonymise(author_name, salt="x") if author_name else ""
                        )

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
                            author_age_days=None,
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
                            injection_risk=injection_risk,
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

    # --- cookie-session mode (ADR-0008 Decision 1; owner-accepted risk) ----

    def _fetch_session_posts(
        self, *, since: dt.datetime, until: dt.datetime, limit: int | None
    ) -> list[SocialPost]:
        posts: list[SocialPost] = []

        for raw_symbol in self.config.session_symbols:
            cashtag = raw_symbol if raw_symbol.startswith("$") else f"${raw_symbol}"

            try:
                self._rate_limiter.acquire()
            except RateLimitError as exc:
                log.warning("Session rate limit reached for '%s': %s", cashtag, exc)
                continue

            _human_scale_jitter()

            try:
                fetched = self._fetch_session_query(cashtag, since=since, until=until)
            except (RateLimitError, SourceBlockedError):
                # Fail-closed: disables the source for the rest of THIS
                # cycle. Never retried in a loop, never worked around.
                raise
            except Exception as exc:
                log.warning("Failed to fetch X session search for '%s': %s", cashtag, exc)
                continue

            posts.extend(fetched)
            if limit is not None and len(posts) >= limit:
                break

        posts.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            posts = posts[:limit]
        return posts

    def _fetch_session_query(
        self, cashtag: str, *, since: dt.datetime, until: dt.datetime
    ) -> list[SocialPost]:
        """One cashtag search over the logged-in web client's GraphQL endpoint.

        Raises ``SourceBlockedError``/``RateLimitError`` on any signal ADR-0008
        Decision 1 requires failing closed on; never guesses at a shape it
        cannot confidently parse.
        """
        # Checked before the request, not after: without a query ID there is
        # no URL worth calling, and issuing one anyway is how this failure
        # used to come back disguised as an authentication problem.
        query_id = (self.config.session_query_id or "").strip()
        if not query_id:
            raise SourceBlockedError(
                f"X session mode has no GraphQL query ID configured, so no search "
                f"request was attempted for '{cashtag}'. This is NOT a cookie or "
                f"login problem -- your session credentials were never used. "
                f"{_QUERY_ID_CAPTURE_HINT}",
                provider="x",
            )

        headers = {
            "authorization": f"Bearer {_WEB_CLIENT_BEARER}",
            "cookie": f"auth_token={self._auth_token}; ct0={self._ct0}",
            "x-csrf-token": self._ct0 or "",
            "user-agent": self.config.session_user_agent,
            "accept": "application/json",
        }
        params = {
            "variables": json.dumps(
                {
                    "rawQuery": f"{cashtag} lang:en",
                    "count": self.config.session_max_results_per_query,
                    "querySource": "typed_query",
                    "product": "Latest",
                }
            ),
            "features": json.dumps(_SEARCH_GRAPHQL_FEATURES),
        }

        with httpx.Client(timeout=self.config.session_request_timeout_s) as client:
            response = client.get(
                _SEARCH_GRAPHQL_URL_TEMPLATE.format(query_id=query_id),
                headers=headers,
                params=params,
            )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                wait_s = float(retry_after)
            except (ValueError, TypeError):
                wait_s = 60.0
            raise RateLimitError(
                f"X session rate limit for '{cashtag}'",
                provider="x",
                retry_after_s=wait_s,
            )

        if response.status_code in (401, 403):
            raise SourceBlockedError(
                f"X session denied access for '{cashtag}' (HTTP {response.status_code}) "
                "-- fail-closed per ADR-0008 Decision 1: the session cookies are likely "
                "expired, logged out, or challenged. No retry, no fingerprint/proxy "
                "rotation, no CAPTCHA handling; re-export fresh cookies to resume.",
                provider="x",
            )

        # ORDER MATTERS. The status code is checked FIRST because a stale
        # query ID 404s with an empty body and no content-type header at all,
        # and the content-type branch below would then report that as a login
        # wall -- the one diagnosis guaranteed to send an operator off to
        # re-export cookies that were never the problem. Authentication
        # failures have already been separated out above as 401/403, so any
        # remaining 4xx/5xx here is an endpoint problem, not a credential one.
        if response.status_code >= 400:
            raise SourceBlockedError(
                f"X session search returned HTTP {response.status_code} for "
                f"'{cashtag}' -- fail-closed per ADR-0008 Decision 1. Your session "
                "cookies authenticated fine (a bad cookie returns 401/403, handled "
                f"above); the configured GraphQL query ID has most likely gone "
                f"stale, which X does without notice. {_QUERY_ID_CAPTURE_HINT}",
                provider="x",
            )

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise SourceBlockedError(
                f"X session search returned HTTP {response.status_code} with "
                f"unexpected content-type '{content_type}' for '{cashtag}' -- a 2xx "
                "that isn't JSON is the shape of a login wall or challenge page, so "
                "re-exporting fresh auth_token/ct0 cookies from DevTools "
                "(Application -> Cookies -> https://x.com) is the first thing to "
                "try. Fail-closed per ADR-0008 Decision 1.",
                provider="x",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceBlockedError(
                f"X session search response for '{cashtag}' was not valid JSON -- "
                "fail-closed per ADR-0008 Decision 1.",
                provider="x",
            ) from exc

        try:
            tweet_results = list(_extract_tweet_results(payload))
        except Exception as exc:
            # Any unexpected shape -- including a schema change to the
            # internal endpoint -- fails closed rather than guessing.
            raise SourceBlockedError(
                f"X session search response for '{cashtag}' did not match the "
                "expected timeline shape -- fail-closed per ADR-0008 Decision 1. "
                "This is expected maintenance if x.com changed its internal "
                "GraphQL response shape; see this module's constants-block doc "
                f"note. ({type(exc).__name__}: {exc})",
                provider="x",
            ) from exc

        out: list[SocialPost] = []
        for result in tweet_results:
            try:
                post = self._to_post(result, cashtag)
            except Exception as exc:
                log.debug("skipping malformed X session tweet for %s: %s", cashtag, exc)
                continue
            if post.created_at < since or post.created_at > until:
                continue
            out.append(post)
        return out

    def _to_post(self, tweet_result: dict, cashtag: str) -> SocialPost:
        """Map one internal-API tweet result onto a sanitised ``SocialPost``.

        Raises on any missing/malformed required field so the caller's
        per-item ``except Exception`` degrades that single item, never the
        whole response.
        """
        legacy = tweet_result["legacy"]
        external_id = str(legacy["id_str"])
        created_at = _parse_twitter_datetime(legacy["created_at"])
        if created_at is None:
            raise ValueError("unparseable created_at")

        text = legacy.get("full_text") or legacy.get("text") or ""
        sanitised = sanitize_social_text(text)
        injection_risk = injection_risk_score(text)

        user_legacy = (
            tweet_result.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
        )
        screen_name = user_legacy.get("screen_name") or ""
        author_hash = pseudonymise(screen_name, salt="x") if screen_name else ""
        followers = user_legacy.get("followers_count")

        return SocialPost(
            source=SocialSource.X,
            external_id=external_id,
            created_at=created_at,
            text=sanitised,
            community=cashtag,
            score=int(legacy.get("favorite_count") or 0),
            num_comments=int(legacy.get("reply_count") or 0),
            num_reposts=int(legacy.get("retweet_count") or 0),
            num_replies=int(legacy.get("reply_count") or 0),
            author_hash=author_hash,
            author_age_days=None,
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
        )


# --------------------------------------------------------------------------
# Session-mode parsing helpers
# --------------------------------------------------------------------------


def _extract_tweet_results(payload: dict) -> list[dict]:
    """Walk the internal timeline response down to a flat list of tweet
    results (each a dict with a ``legacy`` field).

    Every step is an explicit key/index lookup with no defaulting -- a
    missing key raises ``KeyError``/``TypeError`` immediately, which the
    caller turns into a fail-closed ``SourceBlockedError``. Guessing a
    plausible-looking fallback here is exactly the behaviour ADR-0008
    Decision 1 forbids: better to disable the source than silently return
    zero, or worse, malformed, posts.
    """
    instructions = payload["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
        "instructions"
    ]
    results: list[dict] = []
    for instruction in instructions:
        for entry in instruction.get("entries", []):
            content = entry.get("content", {})
            item_content = content.get("itemContent")
            if not item_content:
                continue  # cursor/module entries carry no tweet
            tweet_results = item_content["tweet_results"]
            result = tweet_results.get("result")
            if not result:
                continue
            # Some shapes wrap the real tweet one level deeper under "tweet"
            # (e.g. when the top-level result is a moderation wrapper).
            result = result.get("tweet", result)
            if "legacy" in result:
                results.append(result)
    return results


def _parse_twitter_datetime(value: str) -> dt.datetime | None:
    """Parse X's legacy ``created_at`` format, e.g. 'Wed Jun 05 18:00:00 +0000 2024'."""
    try:
        parsed = dt.datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(dt.UTC)


def _human_scale_jitter() -> None:
    """Small randomised pause between requests (ADR-0008 Decision 1: rates
    must be "conservative, human-scale ... with jitter"), on top of the
    deterministic per-minute spacing the rate limiter already enforces."""
    time.sleep(random.uniform(0.05, 0.25))
