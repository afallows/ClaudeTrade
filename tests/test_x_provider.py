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
        # Session mode refuses to issue a request without one (it is the
        # operator's own browser capture, never shipped); these tests supply a
        # stand-in so they exercise the request path rather than the guard.
        session_query_id="TESTQUERYID",
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
            session_query_id="TESTQUERYID",
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
            session_query_id="TESTQUERYID",
            session_symbols=["AAPL", "MSFT"],
            session_rate_limit_per_minute=6000,
        )
        stub = _XSessionStub()
        stub.override_response = httpx.Response(403, json={})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError):
            XProvider(config).fetch_posts(since=NOW - dt.timedelta(days=1))
        assert len(stub.requests) == 1  # never attempted MSFT


class TestSessionQueryIdDiagnosis:
    """A missing or stale GraphQL query ID must never be reported as a cookie
    problem.

    The shipped placeholder query ID produced a 404 with an empty body and no
    content-type header; because the content-type check ran *before* the
    status-code check, that surfaced as "possible login wall or challenge page
    -- re-export fresh cookies". The cookies were valid the whole time. These
    tests pin the ordering and the wording, because the wording *is* the bug:
    an error that names the wrong cause costs more than one that says nothing.
    """

    def _config(self, **overrides) -> XConfig:
        base = {
            "enabled": True,
            "bearer_credential": "x_bearer_token",
            "session_enabled": True,
            "session_symbols": ["AAPL"],
            "session_rate_limit_per_minute": 6000,
        }
        base.update(overrides)
        return XConfig(**base)

    def test_no_query_id_means_no_request_is_even_attempted(
        self, session_credentials, monkeypatch
    ):
        """Not one doomed HTTP call: with no endpoint to call, issuing the
        request is what let this failure wear an authentication costume."""
        stub = _XSessionStub(_timeline_payload([_tweet_entry()]))
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError) as excinfo:
            XProvider(self._config(session_query_id="")).fetch_posts(
                since=NOW - dt.timedelta(days=1)
            )

        assert stub.requests == []
        assert "query ID" in str(excinfo.value)
        assert "NOT a cookie or login problem" in str(excinfo.value)

    def test_missing_query_id_message_says_how_to_get_one(
        self, session_credentials, monkeypatch
    ):
        """Naming the cause is half a diagnosis; the click-path is the rest."""
        _install(monkeypatch, _XSessionStub())
        with pytest.raises(SourceBlockedError) as excinfo:
            XProvider(self._config(session_query_id="  ")).fetch_posts(
                since=NOW - dt.timedelta(days=1)
            )
        message = str(excinfo.value)
        assert "session_query_id" in message
        assert "devtools" in message.lower()
        assert "SearchTimeline" in message

    def test_404_with_no_content_type_blames_the_endpoint_not_the_cookies(
        self, session_credentials, monkeypatch
    ):
        """A bodiless 404 must blame neither the cookies nor the query ID.

        This assertion used to require the words "gone stale", on the theory
        that this shape meant a rotated query ID. Measured against the live
        endpoint on 2026-08-02 that theory was wrong: a CURRENT query ID (one
        matching a fresh browser capture byte-for-byte) and a garbage one
        return identical bodiless 404s, a real Chrome TLS fingerprint fares no
        better, and the same cookies still get normal JSON from other X
        endpoints. x.com simply refuses non-browser callers here. Blaming the
        query ID sent an operator re-capturing a value that was already
        correct, which is the same class of wrong-diagnosis bug this test
        class exists to prevent -- so the message must now say so.
        """
        stub = _XSessionStub()
        stub.override_response = httpx.Response(404, content=b"")
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError) as excinfo:
            XProvider(self._config(session_query_id="STALE")).fetch_posts(
                since=NOW - dt.timedelta(days=1)
            )

        message = str(excinfo.value)
        assert "query ID" in message
        assert "NOT the cause" in message
        assert "non-browser" in message
        # The two misdiagnoses this shape has now produced, in order.
        assert "login wall" not in message.lower()
        assert "challenge page" not in message.lower()
        assert "gone stale" not in message.lower()

    def test_a_2xx_non_json_body_still_points_at_the_cookies(
        self, session_credentials, monkeypatch
    ):
        """The content-type check keeps its original job -- a *successful*
        response carrying HTML really is the login-wall shape. Narrowing it to
        2xx must not delete it."""
        stub = _XSessionStub()
        stub.override_response = httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>login</html>"
        )
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError) as excinfo:
            XProvider(self._config(session_query_id="OK")).fetch_posts(
                since=NOW - dt.timedelta(days=1)
            )

        message = str(excinfo.value)
        assert "login wall" in message.lower()
        assert "auth_token" in message

    @pytest.mark.parametrize("code", [401, 403])
    def test_real_auth_failures_still_blame_the_cookies(
        self, session_credentials, monkeypatch, code
    ):
        """The reordering must not swing the pendulum the other way: 401/403
        genuinely *are* credential failures and are handled before both
        checks."""
        stub = _XSessionStub()
        stub.override_response = httpx.Response(code, json={})
        _install(monkeypatch, stub)

        with pytest.raises(SourceBlockedError) as excinfo:
            XProvider(self._config(session_query_id="OK")).fetch_posts(
                since=NOW - dt.timedelta(days=1)
            )

        message = str(excinfo.value)
        assert "cookies" in message.lower()
        assert "query ID" not in message

    def test_status_reports_the_missing_query_id_without_a_live_probe(
        self, session_credentials
    ):
        """The Providers panel should answer "why is X quiet?" on sight."""
        status = XProvider(self._config(session_query_id="")).status()
        assert status.configured is False
        assert "query ID" in status.message

    def test_status_reports_an_empty_watchlist_too(self, session_credentials):
        """Cookies plus a query ID plus no symbols fetches nothing forever,
        and used to report as a healthy, fully-configured provider."""
        status = XProvider(self._config(session_query_id="OK", session_symbols=[])).status()
        assert status.configured is False
        assert "session_symbols" in status.message

    def test_fully_configured_session_mode_reports_healthy(self, session_credentials):
        status = XProvider(self._config(session_query_id="OK")).status()
        assert status.configured is True
        assert "UNOFFICIAL" in status.message
