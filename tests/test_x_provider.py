"""Tests for the X (Twitter) provider: mode selection and the cookie-session
adapter, driven over a mocked transport.

The official API v2 path is exercised end-to-end in
``tests/test_providers.py`` (``test_x_without_bearer_token_disables_cleanly``,
``test_x_status_states_the_paid_tier_requirement``); this file focuses on the
mode-preference logic and the new session mode added for ADR-0008 Decision 1
/ Decision 5, including its fail-closed behaviour.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from claudetrade.config import XConfig
from claudetrade.providers.base import (
    NotConfiguredError,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.providers.social.x_provider import XProvider

NOW = dt.datetime(2024, 6, 5, 18, 0, tzinfo=dt.UTC)


def _timeline_payload(entries: list[dict]) -> dict:
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {"instructions": [{"entries": entries}]}
                }
            }
        }
    }


def _tweet_entry(
    tweet_id: str = "111",
    *,
    created: str = "Wed Jun 05 18:00:00 +0000 2024",
    text: str = "Bullish setup forming",
    screen_name: str = "trader1",
    followers: int = 100,
    favorites: int = 5,
    replies: int = 1,
    retweets: int = 2,
) -> dict:
    return {
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "legacy": {
                            "id_str": tweet_id,
                            "created_at": created,
                            "full_text": text,
                            "favorite_count": favorites,
                            "reply_count": replies,
                            "retweet_count": retweets,
                        },
                        "core": {
                            "user_results": {
                                "result": {
                                    "legacy": {
                                        "screen_name": screen_name,
                                        "followers_count": followers,
                                    }
                                }
                            }
                        },
                    }
                }
            }
        }
    }


def _cursor_entry() -> dict:
    """A pagination-cursor entry, carrying no tweet -- must be skipped, not crash."""
    return {"content": {"cursorType": "Bottom", "value": "cursor-abc"}}


class _XSessionStub:
    def __init__(self, payload: dict | None = None):
        self.payload = payload
        self.override_response: httpx.Response | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override_response is not None:
            return self.override_response
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def session_config() -> XConfig:
    return XConfig(
        enabled=True,
        bearer_credential="x_bearer_token",  # left unresolved -- forces session mode
        session_enabled=True,
        session_symbols=["AAPL"],
        session_rate_limit_per_minute=6000,  # avoid real sleeps between test requests
    )


@pytest.fixture
def session_credentials(monkeypatch):
    monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "test-ct0")


def _install(monkeypatch, stub: _XSessionStub) -> None:
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(stub.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.social.x_provider.httpx.Client", _factory)
    monkeypatch.setattr("claudetrade.providers.social.x_provider.time.sleep", lambda *_: None)


class TestModeSelection:
    """Official API always preferred; session mode only as an explicit opt-in."""

    def test_official_mode_preferred_when_bearer_token_resolves(self, monkeypatch):
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", "tok")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "auth")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "ct0")
        config = XConfig(enabled=True, session_enabled=True)
        provider = XProvider(config)
        assert provider.mode == "official"

    def test_session_mode_used_when_no_bearer_token_and_session_enabled(
        self, session_config, session_credentials
    ):
        provider = XProvider(session_config)
        assert provider.mode == "session"

    def test_no_credentials_at_all_raises_cleanly(self, monkeypatch):
        """No bearer, no cookies: cleanly disabled regardless of the
        (now-default-True) ``enabled``/``session_enabled`` flags -- this is
        exactly what lets ``get_social_providers`` skip X without a crash."""
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_CT0", raising=False)
        config = XConfig(enabled=True)
        with pytest.raises(NotConfiguredError):
            XProvider(config)

    def test_session_enabled_but_cookies_missing_raises(self, monkeypatch):
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_CT0", raising=False)
        config = XConfig(enabled=True, session_enabled=True)
        with pytest.raises(NotConfiguredError):
            XProvider(config)

    def test_auto_enable_defaults_are_true(self):
        """Owner directive (2026-07-31): X mirrors Reddit's self-selecting
        posture -- both ``enabled`` and ``session_enabled`` default to
        ``True`` ("use if credentialed"), not an extra opt-in flag."""
        config = XConfig()
        assert config.enabled is True
        assert config.session_enabled is True

    def test_session_mode_auto_activates_with_no_explicit_session_enabled_flag(
        self, monkeypatch
    ):
        """The whole point of the auto-enable change: an operator who has
        only ever set the two session cookies (never touched
        ``session_enabled``) gets a working session-mode provider, exactly
        like ``RedditProvider``'s cookie-session self-selection."""
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "owner-auth-token")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "owner-ct0")

        config = XConfig(session_symbols=["AAPL"])  # enabled/session_enabled left at their defaults
        provider = XProvider(config)
        assert provider.mode == "session"

    def test_explicit_disable_knob_prevents_session_mode_even_when_credentialed(
        self, monkeypatch
    ):
        """The auto-enable change does not remove the ability to opt out:
        ``session_enabled = false`` must still refuse the ToS-risking
        cookie-session path even when both cookies resolve."""
        monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "owner-auth-token")
        monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "owner-ct0")

        config = XConfig(session_enabled=False)
        with pytest.raises(NotConfiguredError):
            XProvider(config)

    def test_enabled_false_is_still_an_explicit_top_level_disable(self):
        """``enabled = false`` keeps X off the ``get_social_providers`` list
        outright, regardless of any credentials -- verified at the registry
        layer in ``tests/test_providers.py``; this only checks the config
        default/override still exists and is settable."""
        config = XConfig(enabled=False)
        assert config.enabled is False


class TestSessionHappyPath:
    def test_maps_tweet_fields_and_sanitises_text(
        self, session_config, session_credentials, monkeypatch
    ):
        stub = _XSessionStub(_timeline_payload([_tweet_entry(), _cursor_entry()]))
        _install(monkeypatch, stub)

        posts = XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

        assert len(posts) == 1
        post = posts[0]
        assert post.external_id == "111"
        assert post.community == "$AAPL"
        assert post.score == 5
        assert post.num_reposts == 2
        assert post.author_hash and "trader1" not in post.author_hash
        assert post.author_followers == 100.0

    def test_cashtag_gets_dollar_prefix_added(
        self, session_credentials, monkeypatch
    ):
        config = XConfig(
            enabled=True,
            session_enabled=True,
            session_symbols=["MSFT"],  # no leading '$'
            session_rate_limit_per_minute=6000,
        )
        stub = _XSessionStub(_timeline_payload([_tweet_entry()]))
        _install(monkeypatch, stub)

        posts = XProvider(config).fetch_posts(since=NOW - dt.timedelta(days=1))
        assert posts[0].community == "$MSFT"

    def test_cookies_are_sent_and_bearer_is_not_the_official_token(
        self, session_config, session_credentials, monkeypatch
    ):
        stub = _XSessionStub(_timeline_payload([_tweet_entry()]))
        _install(monkeypatch, stub)

        XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

        assert len(stub.requests) == 1
        cookie_header = stub.requests[0].headers.get("cookie", "")
        assert "auth_token=test-auth-token" in cookie_header
        assert "ct0=test-ct0" in cookie_header


class TestSessionFailClosed:
    """ADR-0008 Decision 1: 401/403/challenge/parse-failure all disable the
    source for the cycle -- never a retry, never a workaround."""

    def test_401_fails_closed(self, session_config, session_credentials, monkeypatch):
        stub = _XSessionStub()
        stub.override_response = httpx.Response(401, json={"errors": [{"message": "bad auth"}]})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

    def test_403_fails_closed(self, session_config, session_credentials, monkeypatch):
        stub = _XSessionStub()
        stub.override_response = httpx.Response(403, json={"errors": [{"message": "forbidden"}]})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

    def test_challenge_html_response_fails_closed(
        self, session_config, session_credentials, monkeypatch
    ):
        """A login-wall/challenge interstitial served as HTML with a 200."""
        stub = _XSessionStub()
        stub.override_response = httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>please verify</html>"
        )
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

    def test_429_raises_rate_limit_error(
        self, session_config, session_credentials, monkeypatch
    ):
        stub = _XSessionStub()
        stub.override_response = httpx.Response(429, headers={"Retry-After": "55"}, json={})
        _install(monkeypatch, stub)

        with pytest.raises(RateLimitError) as exc:
            XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))
        assert exc.value.retry_after_s == 55.0

    def test_unrecognised_timeline_shape_fails_closed(
        self, session_config, session_credentials, monkeypatch
    ):
        """A 200 with valid JSON that doesn't match the expected shape at all
        (e.g. x.com changed its internal GraphQL response) must fail closed
        rather than silently returning zero posts or guessing at a new shape."""
        stub = _XSessionStub({"unexpected": "shape"})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(session_config).fetch_posts(since=NOW - dt.timedelta(days=1))

    def test_multiple_symbols_stop_at_first_block(self, session_credentials, monkeypatch):
        """A block on the first symbol must prevent any request for the second."""
        config = XConfig(
            enabled=True,
            session_enabled=True,
            session_symbols=["AAPL", "MSFT"],
            session_rate_limit_per_minute=6000,
        )
        stub = _XSessionStub()
        stub.override_response = httpx.Response(403, json={})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(config).fetch_posts(since=NOW - dt.timedelta(days=1))
        assert len(stub.requests) == 1  # never attempted MSFT
