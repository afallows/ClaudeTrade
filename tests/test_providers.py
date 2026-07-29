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

import pytest

from claudetrade.config import MarketDataConfig, RedditConfig, XConfig
from claudetrade.domain import SocialSource
from claudetrade.providers.base import NotConfiguredError, ProviderError
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


def test_stooq_status_declares_its_real_limitations():
    """A provider that cannot serve delisted or point-in-time data must say so.

    The backtester relies on these flags to know whether its universe can be
    treated as survivorship-unbiased.
    """
    status = StooqMarketProvider(MarketDataConfig()).status()
    assert status.supports_delisted is False
    assert status.supports_point_in_time is False
    assert status.licence_note, "licensing limits must be stated for a free feed"


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
    assert get_social_providers(config) == []


def test_synthetic_reddit_provider_is_reachable_without_credentials():
    """The offline generator must be selectable so the app runs with no keys."""
    from claudetrade.config import AppConfig
    from claudetrade.providers.registry import get_social_providers

    config = AppConfig()
    config.reddit.enabled = True
    config.reddit.provider = "synthetic"
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
        AIRequest(task="sentiment", payload={"text": "hello"}, schema_name="SentimentClassification")
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
