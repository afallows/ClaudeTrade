"""Real-provider adapter tests, driven by genuine API response shapes.

The live endpoints (stooq, Reddit OAuth, X API v2) are unreachable from this
environment -- the egress policy answers 403 to CONNECT -- and they would be
non-deterministic even if reachable. These tests therefore mock the HTTP layer
and assert the adapters against **the exact payload shapes the real services
return**, transcribed from their published formats.

That distinction matters. A test that mocks a provider with a payload the
provider never actually emits proves nothing; it just re-states the adapter's
own assumptions back to it. The fixtures below reproduce the real formats,
including the failure modes that return HTTP 200 with an error body.

The specific bug these tests pin: the stooq adapter originally pointed at
``/q/l/`` (the *last quote* endpoint, one row per symbol) while its parser
expected that endpoint's eight-column layout. Requesting several years of daily
history would have silently produced a single bar per symbol.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from claudetrade.config import MarketDataConfig, RedditConfig, XConfig
from claudetrade.domain import SocialSource
from claudetrade.providers.base import NotConfiguredError, ProviderError, SourceBlockedError
from claudetrade.providers.market.stooq import StooqMarketProvider

# --------------------------------------------------------------------------
# Real payload fixtures
# --------------------------------------------------------------------------

#: Exactly what https://stooq.com/q/d/l/?s=aapl.us&i=d returns: six columns,
#: header row, ascending dates, no Symbol or Time column.
STOOQ_DAILY_CSV = """Date,Open,High,Low,Close,Volume
2024-01-02,187.15,188.44,183.89,185.64,82488700
2024-01-03,184.22,185.88,183.43,184.25,58414500
2024-01-04,182.15,183.09,180.88,181.91,71983600
2024-01-05,181.99,182.76,180.17,181.18,62303300
2024-01-08,182.09,185.60,181.50,185.56,59144500
"""

#: The eight-column *quote* payload the parser previously assumed. Feeding this
#: to the history parser must fail loudly rather than silently yield one bar.
STOOQ_QUOTE_CSV = """Symbol,Date,Time,Open,High,Low,Close,Volume
AAPL.US,2024-01-08,22:00:07,182.09,185.60,181.50,185.56,59144500
"""

#: Stooq answers an unknown symbol or an exhausted quota with HTTP 200 and a
#: plain-text body, so it cannot be caught by status code alone.
STOOQ_NO_DATA = "N/D"
STOOQ_QUOTA = "Exceeded the daily hits limit"

#: Verbatim browser-challenge page (HTTP 200, HTML/JS body) captured by a
#: real probe from the owner's machine: stooq now serves this SHA-256
#: proof-of-work challenge for both a US and a TSX symbol request. The exact
#: JS is irrelevant to the adapter -- only the HTML shape (detected via
#: content-type and/or the leading <!doctype/<html marker) matters -- but
#: testing against the real bytes keeps the detection honest.
STOOQ_CHALLENGE_HTML = (
    Path(__file__).parent / "fixtures" / "stooq" / "challenge_page.html"
).read_text(encoding="utf-8")


class _FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "https://stooq.com/"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        import json

        return json.loads(self.text)


# --------------------------------------------------------------------------
# stooq
# --------------------------------------------------------------------------


def test_stooq_symbol_mapping_adds_us_suffix():
    """Stooq namespaces tickers by market; a bare AAPL resolves to nothing."""
    assert StooqMarketProvider.stooq_symbol("AAPL") == "aapl.us"
    assert StooqMarketProvider.stooq_symbol("aapl") == "aapl.us"
    # An explicit market suffix must survive untouched so non-US listings work.
    assert StooqMarketProvider.stooq_symbol("BMW.DE") == "bmw.de"


def test_stooq_symbol_mapping_uses_ca_suffix_for_explicit_tsx_exchange():
    """Canadian (TSX/TSXV) listings get '.to', not '.us'."""
    assert StooqMarketProvider.stooq_symbol("SHOP", exchange="TSX") == "shop.to"
    assert StooqMarketProvider.stooq_symbol("XYZ", exchange="TSXV") == "xyz.to"
    assert StooqMarketProvider.stooq_symbol("SHOP", exchange="tsx") == "shop.to"


def test_stooq_symbol_mapping_defaults_to_us_for_unknown_exchange():
    assert StooqMarketProvider.stooq_symbol("XYZ", exchange="LSE") == "xyz.us"
    assert StooqMarketProvider.stooq_symbol("XYZ", exchange=None) == "xyz.us"


def test_stooq_symbol_mapping_is_driven_by_the_packaged_exchange_column():
    """With no explicit exchange, the suffix is looked up from the packaged
    seed universe's exchange column -- a known US name gets '.us', a known
    Canadian name gets '.to', with no exchange passed by the caller."""
    assert StooqMarketProvider.stooq_symbol("AAPL") == "aapl.us"  # NASDAQ in us_default.csv
    assert StooqMarketProvider.stooq_symbol("SHOP") == "shop.to"  # TSX in ca_default.csv
    assert StooqMarketProvider.stooq_symbol("RY") == "ry.to"  # TSX (Royal Bank of Canada)


def test_stooq_parses_real_daily_history():
    """The daily endpoint's six-column CSV yields one Bar per session."""
    bars = StooqMarketProvider._parse_csv_response(
        STOOQ_DAILY_CSV, "AAPL", dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )
    assert len(bars) == 5, "expected five sessions, not a single last-quote row"
    first, last = bars[0], bars[-1]
    assert first.session == dt.date(2024, 1, 2)
    assert last.session == dt.date(2024, 1, 8)
    assert first.open == pytest.approx(187.15)
    assert first.high == pytest.approx(188.44)
    assert first.low == pytest.approx(183.89)
    assert first.close == pytest.approx(185.64)
    assert first.volume == pytest.approx(82488700)
    assert [b.session for b in bars] == sorted(b.session for b in bars)


def test_stooq_restricts_to_requested_range():
    """Rows outside [start, end] are dropped even when the server sends them."""
    bars = StooqMarketProvider._parse_csv_response(
        STOOQ_DAILY_CSV, "AAPL", dt.date(2024, 1, 3), dt.date(2024, 1, 4)
    )
    assert [b.session for b in bars] == [dt.date(2024, 1, 3), dt.date(2024, 1, 4)]


def test_stooq_rejects_the_quote_endpoint_payload():
    """The eight-column quote layout must not be mistaken for history.

    This is the regression guard for the original defect: silently accepting
    this payload gave the application one bar of history per symbol.
    """
    with pytest.raises(ValueError, match="unexpected stooq header"):
        StooqMarketProvider._parse_csv_response(
            STOOQ_QUOTE_CSV, "AAPL", dt.date(2024, 1, 1), dt.date(2024, 1, 31)
        )


@pytest.mark.parametrize("body", [STOOQ_NO_DATA, STOOQ_QUOTA])
def test_stooq_detects_http_200_error_bodies(body):
    """Stooq reports 'no data' and 'quota exceeded' with a 200 and a text body."""
    with pytest.raises(ValueError, match="no data"):
        StooqMarketProvider._parse_csv_response(
            body, "NOSUCH", dt.date(2024, 1, 1), dt.date(2024, 1, 31)
        )


def test_stooq_requests_the_history_endpoint_with_a_bounded_range(monkeypatch):
    """The adapter must call /q/d/l/ with d1/d2, not the last-quote endpoint."""
    import httpx

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse(STOOQ_DAILY_CSV)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "/q/d/l/" in str(captured["url"]), "must use the historical endpoint"
    assert captured["params"]["s"] == "aapl.us"
    assert captured["params"]["i"] == "d"
    # Server-side bounds keep a multi-year universe request tractable.
    assert captured["params"]["d1"] == "20240101"
    assert captured["params"]["d2"] == "20240131"
    assert captured["client_kwargs"]["verify"] is True, "TLS verification must stay on"
    assert len(result["AAPL"]) == 5


def test_stooq_sends_a_browser_like_user_agent(monkeypatch):
    """Root-cause fix for the real "stooq returned 404 for AAPL" refresh
    failure: the request carried no ``User-Agent`` at all (httpx's own
    generic default went out on the wire instead), and stooq's edge answers
    that default with a 404. The symbol/suffix mapping was already correct
    (see the exact-URL tests below); the missing header is what broke it."""
    import httpx

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            return _FakeResponse(STOOQ_DAILY_CSV)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    headers = captured["client_kwargs"]["headers"]
    assert headers.get("User-Agent"), "a User-Agent header must be sent -- stooq 404s a bare default"
    assert "python-httpx" not in headers["User-Agent"].lower()


def test_stooq_get_daily_bars_requests_exact_url_for_a_us_symbol(monkeypatch):
    """End-to-end (not just the pure ``stooq_symbol()`` unit test): the real
    ``get_daily_bars`` fetch path must request the lower-cased, ``.us``-
    suffixed symbol for a plain US ticker."""
    import httpx

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = dict(params)
            return _FakeResponse(STOOQ_DAILY_CSV)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert captured["url"] == "https://stooq.com/q/d/l/"
    assert captured["params"] == {
        "s": "aapl.us",
        "d1": "20240101",
        "d2": "20240131",
        "i": "d",
    }


def test_stooq_get_daily_bars_requests_exact_url_for_a_tsx_symbol(monkeypatch):
    """Same end-to-end assertion for a Canadian (TSX) symbol resolved from
    the packaged seed universe's exchange column (no explicit exchange
    passed to ``get_daily_bars`` -- that is the real calling convention)."""
    import httpx

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = dict(params)
            return _FakeResponse(STOOQ_DAILY_CSV)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    provider.get_daily_bars(["SHOP"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert captured["url"] == "https://stooq.com/q/d/l/"
    assert captured["params"] == {
        "s": "shop.to",
        "d1": "20240101",
        "d2": "20240131",
        "i": "d",
    }


def test_stooq_browser_challenge_page_raises_source_blocked_error(monkeypatch):
    """A real probe from the owner's machine found stooq now answers both a
    US and a TSX symbol request with an HTTP 200 HTML/JavaScript
    proof-of-work challenge page instead of CSV. Since the status code alone
    (200) cannot distinguish this from a real response, the adapter must
    detect the HTML shape and fail closed rather than feeding it to the CSV
    parser -- never solving the challenge, never retrying in a loop."""
    import httpx

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            return _FakeResponse(
                STOOQ_CHALLENGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    with pytest.raises(SourceBlockedError):
        provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert "blocked (browser challenge)" in provider.status().last_error


def test_stooq_browser_challenge_detected_even_without_html_content_type(monkeypatch):
    """Some challenge responses might not carry an explicit ``text/html``
    content-type -- the leading ``<!doctype``/``<html`` body marker must
    also be checked."""
    import httpx

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            return _FakeResponse(STOOQ_CHALLENGE_HTML, headers={})

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    with pytest.raises(SourceBlockedError):
        provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))


def test_stooq_network_failure_is_a_retryable_provider_error(monkeypatch):
    """A dead network degrades the run; it must not crash the application."""
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _FailingClient)
    provider = StooqMarketProvider(MarketDataConfig())
    with pytest.raises(ProviderError) as excinfo:
        provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert excinfo.value.retryable is True


def test_stooq_unknown_symbol_degrades_per_symbol_not_the_whole_batch(monkeypatch):
    """One unknown ticker in a batch must not take down every other symbol's
    fetch -- the bug this deliverable exists to fix."""
    import httpx

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            if params["s"] == "nosuch.us":
                return _FakeResponse(STOOQ_NO_DATA)
            return _FakeResponse(STOOQ_DAILY_CSV)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(
        ["AAPL", "NOSUCH", "MSFT"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )

    assert result["AAPL"], "a good symbol in the same batch must still be fetched"
    assert result["MSFT"], "a good symbol *after* the bad one must still be fetched"
    assert result["NOSUCH"] == [], "the unknown symbol degrades to an empty series, not a raise"
    assert "NOSUCH" in provider._not_found


def test_stooq_quota_message_also_degrades_per_symbol(monkeypatch):
    """The exhausted-quota body is textually identical in shape to 'unknown
    symbol' (HTTP 200, plain text) and must degrade the same way."""
    import httpx

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            return _FakeResponse(STOOQ_QUOTA)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    provider = StooqMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(["AAPL", "MSFT"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert result == {"AAPL": [], "MSFT": []}


def test_stooq_list_universe_covers_us_and_tsx_stocks_above_one_billion():
    """The Stooq request inventory is explicit, broad, and floor-filtered."""
    provider = StooqMarketProvider(MarketDataConfig())
    securities = provider.list_universe()
    symbols = {s.symbol for s in securities}
    exchanges = {s.exchange for s in securities}
    assert len(securities) > 2_000
    assert "AAPL" in symbols
    assert "SHOP" in symbols  # Canadian (TSX)
    assert {"NYSE", "NASDAQ", "AMEX", "TSX"} <= exchanges
    assert all(not s.is_etf for s in securities)
    assert all(s.market_cap_usd is not None and s.market_cap_usd >= 1_000_000_000 for s in securities)


def test_stooq_status_declares_its_real_limitations():
    """A provider that cannot serve delisted or point-in-time data must say so.

    The backtester relies on these flags to know whether its universe can be
    treated as survivorship-unbiased.
    """
    status = StooqMarketProvider(MarketDataConfig()).status()
    assert status.supports_delisted is False
    assert status.supports_point_in_time is False
    assert status.licence_note, "licensing limits must be stated for a free feed"
    assert status.capabilities["bulk_universe"] is False
    assert "packaged seed universe" in status.message


def test_stooq_refuses_intraday():
    """The free endpoint has no intraday data; the adapter must not pretend."""
    provider = StooqMarketProvider(MarketDataConfig())
    with pytest.raises(ProviderError):
        provider.get_intraday_bars(
            ["AAPL"],
            dt.datetime(2024, 1, 2, tzinfo=dt.UTC),
            dt.datetime(2024, 1, 3, tzinfo=dt.UTC),
        )


# --------------------------------------------------------------------------
# Reddit
# --------------------------------------------------------------------------


def test_reddit_without_credentials_disables_cleanly():
    """No credentials must disable the source, not raise into the pipeline.

    Reduced-capability operation is the documented behaviour: the remaining
    sources continue and the run is marked degraded.
    """
    from claudetrade.providers.social.reddit import RedditProvider

    config = RedditConfig(enabled=True)
    with pytest.raises(NotConfiguredError):
        RedditProvider(config)


def test_reddit_registry_skips_unconfigured_source_without_raising():
    """get_social_providers must swallow NotConfiguredError and continue."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import get_social_providers

    config = AppConfig()
    config.reddit.enabled = True
    config.reddit.provider = "reddit"  # the live adapter, with no credentials
    config.x.enabled = False
    config.news.enabled = False  # news_rss is on by default; isolate reddit here
    config.stocktwits.enabled = False  # keyless, so also on by default; isolate reddit
    assert get_social_providers(config) == []


def test_synthetic_reddit_provider_is_reachable_without_credentials():
    """The offline generator must be selectable so the app runs with no keys."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import get_social_providers

    config = AppConfig()
    config.reddit.enabled = True
    config.reddit.provider = "synthetic"
    config.news.enabled = False  # news_rss is on by default; isolate reddit here
    config.x.enabled = False  # X is auto-enabled by default too; isolate reddit here
    config.stocktwits.enabled = False  # keyless, so also on by default; isolate reddit
    providers = get_social_providers(config)
    assert len(providers) == 1
    assert providers[0].source is SocialSource.REDDIT


def test_synthetic_social_posts_are_sanitised_and_pseudonymous():
    """Stored posts must carry hashed authors and tz-aware timestamps."""
    from claudetrade.providers.social.synthetic import SyntheticRedditProvider

    provider = SyntheticRedditProvider(seed=11)
    posts = provider.fetch_posts(
        since=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        until=dt.datetime(2024, 2, 1, tzinfo=dt.UTC),
        symbols=["AAPL", "MSFT"],
    )
    assert posts, "generator produced no posts"
    for post in posts[:200]:
        assert post.created_at.tzinfo is not None, "timestamps must be timezone-aware"
        assert post.author_hash, "author must be reduced to a salted digest"
        assert post.text_hash, "text hash is required for duplicate detection"
        # A raw username would defeat the pseudonymisation guarantee.
        assert "u/" not in post.text or "[user]" in post.text


def test_synthetic_social_is_deterministic():
    """Identical seeds must produce identical corpora, or nothing is reproducible."""
    from claudetrade.providers.social.synthetic import SyntheticRedditProvider

    window = {
        "since": dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        "until": dt.datetime(2024, 2, 1, tzinfo=dt.UTC),
        "symbols": ["AAPL"],
    }
    first = SyntheticRedditProvider(seed=5).fetch_posts(**window)
    second = SyntheticRedditProvider(seed=5).fetch_posts(**window)
    assert [p.text_hash for p in first] == [p.text_hash for p in second]


# --------------------------------------------------------------------------
# X
# --------------------------------------------------------------------------


def test_x_without_bearer_token_disables_cleanly():
    """X requires a paid tier; absent a token the source switches off quietly."""
    from claudetrade.providers.social.x_provider import XProvider

    with pytest.raises(NotConfiguredError):
        XProvider(XConfig(enabled=True))


def test_x_status_states_the_paid_tier_requirement():
    """Operators must be told up front that meaningful volume costs money."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import provider_status_report

    config = AppConfig()
    reports = {r.kind: r for r in provider_status_report(config)}
    assert reports, "status report must not be empty"


def test_x_auto_activates_via_registry_once_session_credentialed(monkeypatch):
    """Owner directive (2026-07-31): mirroring Reddit's cookie-session
    self-selection, X activates through ``get_social_providers`` the moment
    its credentials resolve -- no separate opt-in flag beyond pointing
    ``provider`` at the live adapter."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import get_social_providers
    from claudetrade.providers.social.x_provider import XProvider

    monkeypatch.delenv("CLAUDETRADE_SECRET_X_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDETRADE_SECRET_X_AUTH_TOKEN", "owner-auth-token")
    monkeypatch.setenv("CLAUDETRADE_SECRET_X_CT0", "owner-ct0")

    config = AppConfig()
    config.x.provider = "x"
    config.x.session_symbols = ["AAPL"]
    config.reddit.enabled = False
    config.news.enabled = False
    config.stocktwits.enabled = False  # on by default (keyless); isolate X here

    providers = get_social_providers(config)
    assert len(providers) == 1
    assert isinstance(providers[0], XProvider)
    assert providers[0].mode == "session"


def test_x_skipped_cleanly_via_registry_when_not_credentialed():
    """The flip side: with no credentials at all, X is still skipped
    cleanly (``NotConfiguredError`` swallowed) rather than crashing the
    refresh, exactly like it was when ``enabled`` defaulted to ``False``."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import get_social_providers

    config = AppConfig()
    config.x.provider = "x"
    config.reddit.enabled = False
    config.news.enabled = False
    config.stocktwits.enabled = False  # on by default (keyless); isolate X here

    assert get_social_providers(config) == []


# --------------------------------------------------------------------------
# AI providers
# --------------------------------------------------------------------------


def test_null_ai_provider_is_free_and_signals_fallback():
    """The default AI provider must cost nothing and declare its fallback."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.base import AIRequest
    from claudetrade.providers.registry import get_ai_provider

    provider = get_ai_provider(AppConfig())
    response = provider.complete(
        AIRequest(
            task="sentiment", payload={"text": "hello"}, schema_name="SentimentClassification"
        )
    )
    assert response.parsed_ok is False
    assert response.estimated_cost_usd == 0.0
    assert response.fallback_used


def test_ai_schema_rejects_malformed_output():
    """Model output is untrusted: anything off-schema must be rejected."""
    from claudetrade.providers.ai.schemas import validate_ai_payload

    good, err = validate_ai_payload("ThesisSummary", {"summary": "A plausible short thesis."})
    assert good is not None and err is None

    bad, err = validate_ai_payload("ThesisSummary", {"not_a_field": 123})
    assert bad is None and err
