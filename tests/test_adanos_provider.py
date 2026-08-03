"""Adanos attention/sentiment source: pre-aggregated X/Reddit/Polymarket rows.

Mirrors ``tests/test_apewisdom_provider.py``'s structure and the same two
non-negotiables:

* **No fabricated post-level data.** Adanos publishes finished per-ticker
  rows, not posts -- see ``providers.social.adanos``'s module docstring.
* **Never squeezed into a shape that loses information.** Adanos, unlike
  ApeWisdom, carries real polarity; storing it must not discard that.

Additional coverage specific to Adanos: site vs official mode selection, the
official-mode monthly budget guard (persistent, self-correcting, fail-closed
at the reserve floor), and per-feed degraded-source naming.

No live network anywhere: responses are served from the transcribed fixtures
via ``httpx.MockTransport``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import httpx
import pytest

from claudetrade.config import AdanosConfig, AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import AdanosSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import AdanosSnapshot
from claudetrade.providers.base import (
    AuthenticationError,
    ProviderError,
    RateLimitError,
    SourceBlockedError,
)
from claudetrade.providers.social.adanos import (
    AdanosProvider,
    _as_float,
    _as_int,
    _MonthlyBudgetStore,
    _parse_feed_rows,
)

FIXTURES = Path(__file__).parent / "fixtures" / "adanos"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _client(
    routes: dict[str, object], *, status: int = 200, headers: dict | None = None
) -> httpx.Client:
    """A client serving ``{url_substring: payload}``, recording every request
    (URL and headers) so tests can assert on both."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                if isinstance(payload, str):  # non-JSON body
                    return httpx.Response(status, text=payload, headers=headers or {})
                return httpx.Response(status, json=payload, headers=headers or {})
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls  # type: ignore[attr-defined]
    return client


def _config(**overrides) -> AdanosConfig:
    # High rate limit so a 3-feed fetch_snapshots() call doesn't sit through
    # RateLimiter pacing during tests.
    base = {"calls_per_minute": 6000}
    base.update(overrides)
    return AdanosConfig(**base)


class TestParsingXFeed:
    def test_reads_buzz_sentiment_and_trend_history(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("x_trending"), platform="x", observed_at=None)
        }
        nvda = rows["NVDA"]
        assert nvda.platform == "x"
        assert nvda.buzz_score == pytest.approx(87.5)
        assert nvda.trend == "rising"
        assert nvda.mentions == 950
        assert nvda.sentiment_score == pytest.approx(0.42)
        assert nvda.bullish_pct == pytest.approx(68.0)
        assert nvda.bearish_pct == pytest.approx(32.0)
        assert nvda.engagement == pytest.approx(12000.0)
        assert nvda.trend_history == [40.0, 45.0, 50.0, 60.0, 70.0, 80.0, 87.5]

    def test_numeric_strings_parse_identically_to_numbers(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("x_trending"), platform="x", observed_at=None)
        }
        assert rows["MU"].mentions == 210  # arrived as "210"

    def test_null_sentiment_stays_none_not_fabricated_zero(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("x_trending"), platform="x", observed_at=None)
        }
        assert rows["MU"].sentiment_score is None

    def test_crypto_tickers_are_dropped(self) -> None:
        symbols = {
            r.symbol
            for r in _parse_feed_rows(_fixture("x_trending"), platform="x", observed_at=None)
        }
        assert "BTC" not in symbols
        assert "NVDA" in symbols

    def test_junk_rows_are_skipped_not_fatal(self) -> None:
        """Empty ticker and a non-dict entry must cost only themselves."""
        rows = _parse_feed_rows(_fixture("x_trending"), platform="x", observed_at=None)
        assert {r.symbol for r in rows} == {"NVDA", "MU"}


class TestParsingRedditFeed:
    def test_reads_reddit_specific_fields_via_the_shared_mentions_column(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(
                _fixture("reddit_trending"), platform="reddit", observed_at=None
            )
        }
        assert rows["GME"].mentions == 610
        assert rows["GME"].trend == "rising"
        assert rows["GME"].bullish_pct == pytest.approx(70.0)
        assert rows["NVDA"].sentiment_score == pytest.approx(-0.05)


class TestParsingPolymarketFeed:
    def test_reads_trade_count_and_liquidity_instead_of_mentions_and_upvotes(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(
                _fixture("polymarket_trending"), platform="polymarket", observed_at=None
            )
        }
        nvda = rows["NVDA"]
        assert nvda.mentions == 1200  # trade_count
        assert nvda.engagement == pytest.approx(250000.5)  # total_liquidity

    def test_null_bull_bear_split_stays_none(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(
                _fixture("polymarket_trending"), platform="polymarket", observed_at=None
            )
        }
        tsla = rows["TSLA"]
        assert tsla.sentiment_score is None
        assert tsla.bullish_pct is None
        assert tsla.bearish_pct is None


class TestParsingNewsFeed:
    def test_reads_mentions_and_source_count_instead_of_engagement(self) -> None:
        """News rows share ``mentions`` with x/reddit but have no
        engagement metric -- ``source_count`` (distinct outlets) is stored
        through the ``engagement`` field instead, same mechanism as
        polymarket's ``total_liquidity``."""
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("news_trending"), platform="news", observed_at=None)
        }
        spcx = rows["SPCX"]
        assert spcx.platform == "news"
        assert spcx.company_name == "Space Exploration Technologies Corp"
        assert spcx.buzz_score == pytest.approx(63.5)
        assert spcx.trend == "falling"
        assert spcx.mentions == 22
        assert spcx.engagement == pytest.approx(12.0)  # source_count
        assert spcx.sentiment_score == pytest.approx(0.191)
        assert spcx.bullish_pct == pytest.approx(41.0)
        assert spcx.bearish_pct == pytest.approx(5.0)
        assert spcx.trend_history == [66.7, 67.7, 64.1, 65.6, 64.5, 55.7, 63.5]

    def test_numeric_string_mentions_parse_identically_to_numbers(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("news_trending"), platform="news", observed_at=None)
        }
        assert rows["NVDA"].mentions == 38  # arrived as "38"

    def test_null_sentiment_stays_none_not_fabricated_zero(self) -> None:
        rows = {
            r.symbol: r
            for r in _parse_feed_rows(_fixture("news_trending"), platform="news", observed_at=None)
        }
        assert rows["NVDA"].sentiment_score is None

    def test_crypto_tickers_are_dropped(self) -> None:
        symbols = {
            r.symbol
            for r in _parse_feed_rows(_fixture("news_trending"), platform="news", observed_at=None)
        }
        assert "BTC" not in symbols
        assert "SPCX" in symbols

    def test_junk_rows_are_skipped_not_fatal(self) -> None:
        rows = _parse_feed_rows(_fixture("news_trending"), platform="news", observed_at=None)
        assert {r.symbol for r in rows} == {"SPCX", "NVDA"}


class TestParseDrift:
    def test_unexpected_top_level_shape_raises_rather_than_degrading_silently(self) -> None:
        """Adanos is a documented, versioned API -- unlike ApeWisdom's
        tolerant-and-empty posture, a shape the vendor never documented is
        surfaced as a failure, not silently swallowed into no data."""
        with pytest.raises(ProviderError):
            _parse_feed_rows({"unexpected": "shape"}, platform="x", observed_at=None)

    def test_wrapped_results_envelope_is_still_accepted(self) -> None:
        """Tolerated in case a future response wraps the array."""
        rows = _parse_feed_rows({"results": _fixture("x_trending")}, platform="x", observed_at=None)
        assert {r.symbol for r in rows} == {"NVDA", "MU"}


class TestNumericHelpers:
    def test_as_int_rejects_booleans(self) -> None:
        assert _as_int(True) is None
        assert _as_int("1,024") == 1024

    def test_as_float_rejects_booleans_and_passes_through_none(self) -> None:
        assert _as_float(True) is None
        assert _as_float(None) is None
        assert _as_float("0.42") == pytest.approx(0.42)


class TestModeSelection:
    def test_defaults_to_site_mode(self) -> None:
        provider = AdanosProvider(_config())
        assert provider.mode == "site"

    def test_official_mode_requires_both_flag_and_resolvable_key(self, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module

        monkeypatch.setattr(adanos_module, "get_secret", lambda name: None)
        provider = AdanosProvider(_config(prefer_official_api=True))
        assert provider.mode == "site"  # no key resolves -> degrades to site mode

    def test_official_mode_when_key_resolves(self, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        provider = AdanosProvider(_config(prefer_official_api=True))
        assert provider.mode == "official"


class TestFetchSiteMode:
    def test_walks_each_enabled_feed(self) -> None:
        client = _client(
            {
                "proxy-x/trending": _fixture("x_trending"),
                "proxy/trending": _fixture("reddit_trending"),
                "proxy-polymarket/trending": _fixture("polymarket_trending"),
                "proxy-news/trending": _fixture("news_trending"),
            }
        )
        provider = AdanosProvider(_config(), client=client)
        rows = provider.fetch_snapshots()

        assert {r.platform for r in rows} == {"x", "reddit", "polymarket", "news"}
        # NVDA appears in all four feeds and stays four distinct rows --
        # collapsing them would lose which platform is talking.
        assert sum(1 for r in rows if r.symbol == "NVDA") == 4
        assert len(client.calls) == 4  # type: ignore[attr-defined]

    def test_disabled_feed_is_not_requested(self) -> None:
        client = _client({"proxy-x/trending": _fixture("x_trending")})
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        rows = provider.fetch_snapshots()
        assert {r.platform for r in rows} == {"x"}
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_no_api_key_header_sent_in_site_mode(self) -> None:
        client = _client({"proxy-x/trending": _fixture("x_trending")})
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        provider.fetch_snapshots()
        assert "X-API-Key" not in client.calls[0].headers  # type: ignore[attr-defined]

    def test_disabled_makes_no_requests_at_all(self) -> None:
        client = _client({"proxy-x/trending": _fixture("x_trending")})
        provider = AdanosProvider(_config(enabled=False), client=client)
        assert provider.fetch_snapshots() == []
        assert client.calls == []  # type: ignore[attr-defined]

    def test_one_failing_feed_does_not_lose_the_others(self) -> None:
        client = _client({"proxy-x/trending": _fixture("x_trending")})  # reddit/polymarket/news 404
        provider = AdanosProvider(_config(), client=client)
        rows = provider.fetch_snapshots()
        assert {r.platform for r in rows} == {"x"}
        assert set(provider.last_feed_failures) == {
            "adanos_reddit",
            "adanos_polymarket",
            "adanos_news",
        }

    def test_news_feed_alone_can_be_disabled(self) -> None:
        """Mirrors ``test_disabled_feed_is_not_requested`` for the fourth
        feed specifically."""
        client = _client(
            {
                "proxy-x/trending": _fixture("x_trending"),
                "proxy/trending": _fixture("reddit_trending"),
                "proxy-polymarket/trending": _fixture("polymarket_trending"),
            }
        )
        provider = AdanosProvider(_config(feed_news=False), client=client)
        rows = provider.fetch_snapshots()
        assert {r.platform for r in rows} == {"x", "reddit", "polymarket"}
        assert len(client.calls) == 3  # type: ignore[attr-defined]

    def test_news_feed_failure_is_reported_by_name(self) -> None:
        client = _client(
            {
                "proxy-x/trending": _fixture("x_trending"),
                "proxy/trending": _fixture("reddit_trending"),
                "proxy-polymarket/trending": _fixture("polymarket_trending"),
                # proxy-news/trending intentionally unregistered -> 404
            }
        )
        provider = AdanosProvider(_config(), client=client)
        rows = provider.fetch_snapshots()
        assert {r.platform for r in rows} == {"x", "reddit", "polymarket"}
        assert "adanos_news" in provider.last_feed_failures


class TestFetchOfficialMode:
    def _provider(self, client, **overrides) -> AdanosProvider:
        base = {
            "prefer_official_api": True,
            "feed_reddit": False,
            "feed_polymarket": False,
            "feed_news": False,
        }
        base.update(overrides)
        return AdanosProvider(_config(**base), client=client)

    def _make(self, client, monkeypatch, cache_dir=None, **overrides) -> AdanosProvider:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        base = {
            "prefer_official_api": True,
            "feed_reddit": False,
            "feed_polymarket": False,
            "feed_news": False,
        }
        base.update(overrides)
        return AdanosProvider(_config(**base), client=client, cache_dir=cache_dir)

    def test_x_api_key_header_present_in_official_mode(self, monkeypatch) -> None:
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})
        provider = self._make(client, monkeypatch)
        provider.fetch_snapshots()
        assert client.calls[0].headers["x-api-key"] == "sk_live_test"  # type: ignore[attr-defined]

    def test_hits_official_host_not_site_proxy(self, monkeypatch) -> None:
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})
        provider = self._make(client, monkeypatch)
        provider.fetch_snapshots()
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert "api.adanos.org" in url
        assert "proxy" not in url

    def test_news_official_url_uses_the_inferred_news_stocks_path(self, monkeypatch) -> None:
        """Same inferred-path posture as Polymarket's official endpoint (see
        the module docstring) -- ``news/stocks/v1/trending`` under
        ``official_base_url``, no ``proxy-news`` segment."""
        client = _client({"news/stocks/v1/trending": _fixture("news_trending")})
        provider = self._make(client, monkeypatch, feed_x=False, feed_news=True)
        provider.fetch_snapshots()
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert url.startswith("https://api.adanos.org/news/stocks/v1/trending")
        assert "proxy-news" not in url

    def test_news_official_404_degrades_only_the_news_feed(self, monkeypatch) -> None:
        """Mirrors the module docstring's Polymarket posture: an unconfirmed
        official-mode path 404ing degrades cleanly rather than failing the
        whole provider."""
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})  # news 404s
        provider = self._make(client, monkeypatch, feed_x=True, feed_news=True)
        rows = provider.fetch_snapshots()
        assert {r.platform for r in rows} == {"x"}
        assert "adanos_news" in provider.last_feed_failures


class TestErrorTaxonomy:
    def test_429_raises_rate_limit_error(self) -> None:
        client = _client({"proxy-x/trending": {}}, status=429)
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        with pytest.raises(RateLimitError):
            provider._fetch_platform("x", None)

    def test_site_mode_401_is_source_blocked_not_authentication(self) -> None:
        client = _client({"proxy-x/trending": {}}, status=401)
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        with pytest.raises(SourceBlockedError):
            provider._fetch_platform("x", None)

    def test_official_mode_401_is_authentication_error(self, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "bad-key", "keyring")
        )
        client = _client({"x/stocks/v1/trending": {}}, status=403)
        provider = AdanosProvider(
            _config(
                prefer_official_api=True, feed_reddit=False, feed_polymarket=False, feed_news=False
            ),
            client=client,
        )
        with pytest.raises(AuthenticationError):
            provider._fetch_platform("x", None)

    def test_5xx_is_retryable_provider_error(self) -> None:
        client = _client({"proxy-x/trending": {}}, status=503)
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        with pytest.raises(ProviderError) as exc_info:
            provider._fetch_platform("x", None)
        assert exc_info.value.retryable is True

    def test_non_json_body_is_source_blocked(self) -> None:
        client = _client({"proxy-x/trending": "<html>captcha</html>"})
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        with pytest.raises(SourceBlockedError):
            provider._fetch_platform("x", None)

    def test_no_retries_on_failure(self) -> None:
        """One failed request per feed per call -- never a retry loop."""
        client = _client({"proxy-x/trending": {}}, status=500)
        provider = AdanosProvider(
            _config(feed_reddit=False, feed_polymarket=False, feed_news=False), client=client
        )
        rows = provider.fetch_snapshots()
        assert rows == []
        assert len(client.calls) == 1  # type: ignore[attr-defined]


class TestMonthlyBudget:
    def test_used_increments_per_official_call(self, tmp_path, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})
        provider = AdanosProvider(
            _config(
                prefer_official_api=True,
                feed_reddit=False,
                feed_polymarket=False,
                feed_news=False,
                monthly_budget=250,
                monthly_reserve=15,
            ),
            client=client,
            cache_dir=tmp_path,
        )
        provider.fetch_snapshots()
        used, _ = provider._budget.snapshot()
        assert used == 1
        provider.fetch_snapshots()
        used, _ = provider._budget.snapshot()
        assert used == 2

    def test_self_corrects_from_remaining_header(self, tmp_path, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        # The vendor reports only 1 remaining out of a 250 budget, even
        # though this is the first call this process has made -- e.g. a
        # second process, or a prior crashed run, already spent the rest.
        client = _client(
            {"x/stocks/v1/trending": _fixture("x_trending")},
            headers={"X-RateLimit-Remaining-Monthly": "1"},
        )
        provider = AdanosProvider(
            _config(
                prefer_official_api=True,
                feed_reddit=False,
                feed_polymarket=False,
                feed_news=False,
                monthly_budget=250,
                monthly_reserve=15,
            ),
            client=client,
            cache_dir=tmp_path,
        )
        provider.fetch_snapshots()
        used, _ = provider._budget.snapshot()
        assert used == 249  # max(local increment of 1, 250 - 1)

    def test_fails_closed_at_the_reserve_floor_without_sending_a_request(
        self, tmp_path, monkeypatch
    ) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})
        provider = AdanosProvider(
            _config(
                prefer_official_api=True,
                feed_reddit=False,
                feed_polymarket=False,
                feed_news=False,
                monthly_budget=5,
                monthly_reserve=2,
            ),
            client=client,
            cache_dir=tmp_path,
        )
        for _ in range(3):
            provider.fetch_snapshots()  # used -> 3, remaining -> 2 (== reserve)
        assert len(client.calls) == 3  # type: ignore[attr-defined]

        rows = provider.fetch_snapshots()  # remaining <= reserve: must fail closed
        assert rows == []
        assert len(client.calls) == 3  # no new request was sent  # type: ignore[attr-defined]
        assert "adanos_x" in provider.last_feed_failures

    def test_never_falls_back_to_extra_site_proxy_calls_when_exhausted(
        self, tmp_path, monkeypatch
    ) -> None:
        """Budget exhaustion must degrade the feed, not silently switch it to
        site mode and keep serving data at a different cadence."""
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        client = _client(
            {
                "x/stocks/v1/trending": _fixture("x_trending"),
                "proxy-x/trending": _fixture("x_trending"),
            }
        )
        provider = AdanosProvider(
            _config(
                prefer_official_api=True,
                feed_reddit=False,
                feed_polymarket=False,
                feed_news=False,
                monthly_budget=1,
                monthly_reserve=0,
            ),
            client=client,
            cache_dir=tmp_path,
        )
        provider.fetch_snapshots()  # used -> 1, remaining -> 0 (== reserve)
        assert provider.mode == "official"  # mode itself never silently flips

        rows = provider.fetch_snapshots()
        assert rows == []
        # Still exactly one call total: the official request from the first
        # cycle. No site-proxy call was made to compensate.
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_resets_on_a_new_calendar_month(self, tmp_path) -> None:
        store = _MonthlyBudgetStore(tmp_path / "budget.json")
        (tmp_path / "budget.json").write_text(
            json.dumps({"month": "2000-01", "used": 999}), encoding="utf-8"
        )
        used, month = store.snapshot()
        assert used == 0
        assert month != "2000-01"

    def test_persists_across_provider_instances(self, tmp_path, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        client = _client({"x/stocks/v1/trending": _fixture("x_trending")})
        cfg = _config(
            prefer_official_api=True,
            feed_reddit=False,
            feed_polymarket=False,
            feed_news=False,
            monthly_budget=250,
        )
        AdanosProvider(cfg, client=client, cache_dir=tmp_path).fetch_snapshots()

        second = AdanosProvider(cfg, client=client, cache_dir=tmp_path)
        used, _ = second._budget.snapshot()
        assert used == 1


class TestStatus:
    def test_reports_no_point_in_time_support(self) -> None:
        status = AdanosProvider(_config()).status()
        assert status.supports_point_in_time is False
        assert status.configured is True
        assert status.kind == "attention"

    def test_disabled_is_reported_not_hidden(self) -> None:
        status = AdanosProvider(_config(enabled=False)).status()
        assert status.configured is False
        assert status.available is False

    def test_official_mode_message_reports_budget_remaining(self, tmp_path, monkeypatch) -> None:
        import claudetrade.providers.social.adanos as adanos_module
        from claudetrade.secrets import SecretValue

        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, "sk_live_test", "keyring")
        )
        provider = AdanosProvider(
            _config(prefer_official_api=True, monthly_budget=250), cache_dir=tmp_path
        )
        status = provider.status()
        assert "250" in status.message
        assert "budget remaining" in status.message.lower()


class _StubAdanos:
    name = "adanos"

    def __init__(self, rows, *, boom: bool = False, feed_failures: dict | None = None):
        self._rows = rows
        self._boom = boom
        self.last_feed_failures = feed_failures or {}

    def fetch_snapshots(self):
        if self._boom:
            raise RuntimeError("upstream exploded")
        return self._rows


class TestIngest:
    """Storage: Adanos rows land in their own table, distinct from both
    ``symbol_sentiment_daily``'s ``"all"`` aggregate and ApeWisdom's
    attention-only rows."""

    @pytest.fixture
    def db(self) -> Database:
        from claudetrade.db.migrations import init_database

        database = Database("sqlite:///:memory:")
        init_database(database)
        with database.session() as session:
            session.add(Security(symbol="NVDA", name="NVIDIA"))
            session.add(Security(symbol="GME", name="GameStop"))
        return database

    def _ingest(self, db, rows, **kw):
        ingestor = DataIngestor(AppConfig(), db, adanos_providers=[_StubAdanos(rows, **kw)])
        return ingestor.ingest_adanos(dt.date(2026, 8, 1), IngestReport())

    def test_stores_rows_with_real_polarity_intact(self, db: Database) -> None:
        written = self._ingest(
            db,
            [
                AdanosSnapshot(
                    "NVDA",
                    "x",
                    buzz_score=87.5,
                    mentions=950,
                    sentiment_score=0.42,
                    bullish_pct=68.0,
                    bearish_pct=32.0,
                    engagement=12000.0,
                    trend="rising",
                    trend_history=[1.0, 2.0],
                )
            ],
        )
        assert written == 1
        with db.read_session() as session:
            row = session.query(AdanosSnapshotRow).one()
        assert row.symbol == "NVDA"
        assert row.platform == "x"
        assert row.sentiment_score == pytest.approx(0.42)
        assert row.bullish_pct == pytest.approx(68.0)
        assert row.bearish_pct == pytest.approx(32.0)
        assert row.trend == "rising"
        assert row.trend_history == [1.0, 2.0]

    def test_never_writes_symbol_sentiment_daily(self, db: Database) -> None:
        """Adanos storage must not bleed into the table strategies score
        against or ApeWisdom's attention-only rows."""
        from claudetrade.db.models import SymbolSentimentDaily

        self._ingest(db, [AdanosSnapshot("NVDA", "x", mentions=100)])
        with db.read_session() as session:
            assert session.query(SymbolSentimentDaily).count() == 0

    def test_symbols_outside_the_tracked_universe_are_dropped(self, db: Database) -> None:
        written = self._ingest(
            db,
            [
                AdanosSnapshot("NVDA", "x", mentions=100),
                AdanosSnapshot("ZZZZ", "x", mentions=999),
            ],
        )
        assert written == 1
        with db.read_session() as session:
            assert {r.symbol for r in session.query(AdanosSnapshotRow).all()} == {"NVDA"}

    def test_re_ingesting_the_same_session_and_platform_updates_not_duplicates(
        self, db: Database
    ) -> None:
        self._ingest(db, [AdanosSnapshot("NVDA", "x", buzz_score=50.0)])
        self._ingest(db, [AdanosSnapshot("NVDA", "x", buzz_score=90.0)])
        with db.read_session() as session:
            rows = session.query(AdanosSnapshotRow).all()
        assert len(rows) == 1
        assert rows[0].buzz_score == pytest.approx(90.0)

    def test_same_symbol_different_platform_stays_two_rows(self, db: Database) -> None:
        self._ingest(
            db,
            [
                AdanosSnapshot("NVDA", "x", buzz_score=50.0),
                AdanosSnapshot("NVDA", "reddit", buzz_score=20.0),
            ],
        )
        with db.read_session() as session:
            rows = session.query(AdanosSnapshotRow).all()
        assert {r.platform for r in rows} == {"x", "reddit"}

    def test_a_failing_provider_is_recorded_and_does_not_abort_the_refresh(
        self, db: Database
    ) -> None:
        ingestor = DataIngestor(AppConfig(), db, adanos_providers=[_StubAdanos([], boom=True)])
        report = IngestReport()
        assert ingestor.ingest_adanos(dt.date(2026, 8, 1), report) == 0
        assert "adanos" in report.provider_failures

    def test_per_feed_failures_are_reported_by_name(self, db: Database) -> None:
        """A provider that internally caught one feed's failure (see
        ``AdanosProvider.last_feed_failures``) surfaces it under its own
        ``adanos_<platform>`` key, not merged into one opaque entry."""
        ingestor = DataIngestor(
            AppConfig(),
            db,
            adanos_providers=[
                _StubAdanos(
                    [AdanosSnapshot("NVDA", "x", mentions=1)],
                    feed_failures={
                        "adanos_reddit": "HTTP 500",
                        "adanos_polymarket": "HTTP 404",
                    },
                )
            ],
        )
        report = IngestReport()
        ingestor.ingest_adanos(dt.date(2026, 8, 1), report)
        assert report.provider_failures["adanos_reddit"] == "HTTP 500"
        assert report.provider_failures["adanos_polymarket"] == "HTTP 404"

    def test_no_providers_is_a_silent_no_op(self, db: Database) -> None:
        assert DataIngestor(AppConfig(), db).ingest_adanos(dt.date(2026, 8, 1)) == 0


class TestMigration:
    """Migration 010 -- see ``db.migrations._m010_adanos_snapshots``."""

    def test_adanos_snapshots_table_exists_after_migration(self, memory_db: Database) -> None:
        from claudetrade.db.migrations import init_database

        init_database(memory_db)
        with memory_db.session() as session:
            session.add(AdanosSnapshotRow(symbol="NVDA", session=dt.date(2026, 8, 1), platform="x"))

    def test_latest_version_covers_migration_010(self) -> None:
        from claudetrade.db.migrations import LATEST_VERSION

        assert LATEST_VERSION >= 10

    def test_migrating_an_already_current_database_is_idempotent(self, memory_db: Database) -> None:
        from claudetrade.db.migrations import init_database, migrate

        init_database(memory_db)
        assert migrate(memory_db) == []

    def test_fresh_and_upgraded_schema_agree(self, unmigrated_db: Database) -> None:
        """A DB migrated step by step from 0 ends up with the same table a
        brand-new ``create_all`` produces (the fresh-vs-migrated posture
        every migration since 004 documents)."""
        from claudetrade.db.migrations import migrate

        migrate(unmigrated_db, target=9)
        migrate(unmigrated_db, target=10)
        with unmigrated_db.session() as session:
            session.add(
                AdanosSnapshotRow(symbol="GME", session=dt.date(2026, 8, 1), platform="reddit")
            )


class TestCredentialCatalogVisibility:
    """The Adanos credential must appear wherever the shared catalog is
    consumed -- see ``secrets.credential_catalog`` and its docstring on why
    a credential with no field on either screen is a support ticket."""

    def test_in_credential_catalog(self) -> None:
        from claudetrade.secrets import credential_catalog

        config = AppConfig()
        names = {name for name, _, _ in credential_catalog(config)}
        assert config.adanos.api_key_credential in names

    def test_visible_via_the_system_credentials_endpoint(
        self, tmp_app_config: AppConfig, tmp_db: Database
    ) -> None:
        from fastapi.testclient import TestClient

        from claudetrade.pipeline import Pipeline
        from claudetrade.webapi.app import create_app

        client = TestClient(
            create_app(tmp_app_config, pipeline=Pipeline(tmp_app_config, tmp_db)),
            base_url="http://127.0.0.1",
        )
        response = client.get("/api/system/credentials")
        assert response.status_code == 200
        names = {item["name"] for item in response.json()["credentials"]}
        assert tmp_app_config.adanos.api_key_credential in names

    def test_secrets_list_includes_adanos_key(self) -> None:
        """``claudetrade secrets list`` derives from the same catalog."""
        from typer.testing import CliRunner

        from claudetrade.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 0
        assert "adanos_api_key" in result.output


class TestProbeHostList:
    """``claudetrade probe`` must know about the Adanos hosts."""

    def test_live_endpoints_include_both_adanos_hosts(self) -> None:
        from claudetrade.cli import LIVE_ENDPOINTS

        hosts = {host for _source, host, _url, _needs_key in LIVE_ENDPOINTS}
        assert "adanos.org" in hosts
        assert "api.adanos.org" in hosts

    def test_site_proxy_row_is_keyless(self) -> None:
        from claudetrade.cli import LIVE_ENDPOINTS

        row = next(row for row in LIVE_ENDPOINTS if row[1] == "adanos.org")
        assert row[3] is False  # needs_key

    def test_official_row_needs_a_key(self) -> None:
        from claudetrade.cli import LIVE_ENDPOINTS

        row = next(row for row in LIVE_ENDPOINTS if row[1] == "api.adanos.org")
        assert row[3] is True  # needs_key


# ---------------------------------------------------------------------------
# Hybrid mode: on-demand official calls (fetch_stock_detail / fetch_explain)
#
# Independent of site-vs-official trending mode -- see the module docstring's
# "Hybrid mode" section. These are ALWAYS official and ALWAYS budget-guarded
# whenever a key resolves at all, regardless of ``prefer_official_api``.
# ---------------------------------------------------------------------------


def _hybrid_provider(
    client, monkeypatch, *, cache_dir=None, key="sk_live_test", **overrides
) -> AdanosProvider:
    """A provider wired for on-demand calls: ``get_secret`` is monkeypatched
    explicitly (never left to the real credential store -- this machine may
    have a real key configured) to either resolve ``key`` or (``key=None``)
    resolve nothing at all."""
    import claudetrade.providers.social.adanos as adanos_module
    from claudetrade.secrets import SecretValue

    if key is None:
        monkeypatch.setattr(adanos_module, "get_secret", lambda name: None)
    else:
        monkeypatch.setattr(
            adanos_module, "get_secret", lambda name: SecretValue(name, key, "keyring")
        )
    return AdanosProvider(_config(**overrides), client=client, cache_dir=cache_dir)


class TestHybridModeKeyResolution:
    def test_key_resolves_regardless_of_prefer_official_api(self, monkeypatch) -> None:
        """The whole point of hybrid mode: on-demand calls need a key even
        when trending stays in its site-mode default."""
        provider = _hybrid_provider(_client({}), monkeypatch)  # prefer_official_api defaults False
        assert provider.mode == "site"  # trending mode is unaffected
        assert provider.budget_status()["key_resolved"] is True  # on-demand calls ARE available

    def test_no_key_means_on_demand_calls_are_unavailable(self, monkeypatch) -> None:
        provider = _hybrid_provider(_client({}), monkeypatch, key=None)
        assert provider.budget_status()["key_resolved"] is False


class TestFetchStockDetailUrlConstruction:
    def test_per_platform_url_and_auth_header(self, monkeypatch, tmp_path) -> None:
        cases = {
            "x": "x/stocks/v1/stock/NVDA",
            "reddit": "reddit/stocks/v1/stock/NVDA",
            "polymarket": "polymarket/stocks/v1/stock/NVDA",
            "news": "news/stocks/v1/stock/NVDA",
        }
        for platform, expected_path in cases.items():
            client = _client({expected_path: _fixture("x_stock_detail")})
            provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
            result = provider.fetch_stock_detail("nvda", platform=platform)
            assert result["accepted"] is True
            url = str(client.calls[0].url)  # type: ignore[attr-defined]
            assert url.startswith(f"https://api.adanos.org/{expected_path}")
            assert client.calls[0].headers["x-api-key"] == "sk_live_test"  # type: ignore[attr-defined]

    def test_from_to_params_are_passed_through(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.fetch_stock_detail(
            "NVDA", platform="x", from_date="2026-07-01", to_date="2026-08-01"
        )
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert "from=2026-07-01" in url
        assert "to=2026-08-01" in url

    def test_unsupported_platform_is_a_refusal_not_a_request(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA", platform="bogus")
        assert result["accepted"] is False
        assert client.calls == []  # type: ignore[attr-defined]


class TestFetchStockDetailParsing:
    def test_normalized_header_and_raw_passthrough(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")

        assert result["symbol"] == "NVDA"
        assert result["platform"] == "x"
        assert result["buzz_score"] == pytest.approx(87.5)
        assert result["sentiment_score"] == pytest.approx(0.42)
        assert result["bullish_pct"] == pytest.approx(68.0)
        assert result["bearish_pct"] == pytest.approx(32.0)
        assert result["mentions"] == 950
        # Nothing invented -- the vendor's own fields pass through untouched.
        assert result["raw"]["daily_trend"][0]["date"] == "2026-07-27"
        assert result["raw"]["sentiment_breakdown"]["bullish"] == 68.0
        assert "budget" in result

    def test_spends_exactly_one_official_call(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, monthly_budget=250)
        provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 1


class TestFetchStockDetailBudgetGuard:
    def test_no_key_returns_a_structured_refusal_without_sending_a_request(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "no adanos_api_key" in result["reason"]
        assert result["budget"]["key_resolved"] is False
        assert client.calls == []  # type: ignore[attr-defined]

    def test_fails_closed_at_the_reserve_floor_without_sending_a_request(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, monthly_budget=5, monthly_reserve=2
        )
        for _ in range(3):
            provider.fetch_stock_detail("NVDA")  # used -> 3, remaining -> 2 (== reserve)
        assert len(client.calls) == 3  # type: ignore[attr-defined]

        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "resets" in result["reason"]
        assert len(client.calls) == 3  # type: ignore[attr-defined]  # no new request sent

    def test_refusals_never_touch_the_budget_counter(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 0

    def test_self_corrects_from_the_remaining_header(self, monkeypatch, tmp_path) -> None:
        client = _client(
            {"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")},
            headers={"X-RateLimit-Remaining-Monthly": "1"},
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, monthly_budget=250)
        provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 249  # max(local increment of 1, 250 - 1)


class TestFetchStockDetailErrorTaxonomy:
    def test_401_is_authentication_error(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=401)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(AuthenticationError):
            provider.fetch_stock_detail("NVDA")

    def test_403_names_the_history_window(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=403)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(ProviderError) as exc_info:
            provider.fetch_stock_detail("NVDA")
        assert "history window" in str(exc_info.value)

    def test_429_is_rate_limit_error(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=429)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(RateLimitError):
            provider.fetch_stock_detail("NVDA")

    def test_404_names_unsupported_ticker(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=404)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(ProviderError) as exc_info:
            provider.fetch_stock_detail("NVDA")
        assert "unsupported" in str(exc_info.value).lower()

    def test_5xx_is_retryable(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=503)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(ProviderError) as exc_info:
            provider.fetch_stock_detail("NVDA")
        assert exc_info.value.retryable is True

    def test_no_retries_on_failure(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(ProviderError):
            provider.fetch_stock_detail("NVDA")
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_a_failed_call_still_counts_against_the_budget(self, monkeypatch, tmp_path) -> None:
        """The vendor served (and presumably counted) the request even
        though it errored -- the local counter must reflect that."""
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(ProviderError):
            provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 1


class TestFetchExplain:
    def test_url_construction(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("nvda")
        assert result["accepted"] is True
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert url == "https://api.adanos.org/x/stocks/v1/stock/NVDA/explain"

    def test_returns_explanation_cached_and_generated_at(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("NVDA")
        assert "AI chip demand" in result["explanation"]
        assert result["cached"] is False
        assert result["generated_at"] == "2026-08-02T00:00:00+00:00"
        assert "budget" in result

    def test_no_key_is_a_structured_refusal(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_explain("NVDA")
        assert result["accepted"] is False
        assert client.calls == []  # type: ignore[attr-defined]

    def test_budget_refusal_names_the_reset_date(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, monthly_budget=1, monthly_reserve=0
        )
        provider.fetch_explain("NVDA")  # spends the one call -> remaining 0 == reserve
        result = provider.fetch_explain("NVDA")
        assert result["accepted"] is False
        assert "resets" in result["reason"]

    def test_401_is_authentication_error(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": {}}, status=401)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        with pytest.raises(AuthenticationError):
            provider.fetch_explain("NVDA")


class TestBudgetStatus:
    def test_shape_and_content(self, monkeypatch, tmp_path) -> None:
        provider = _hybrid_provider(
            _client({}), monkeypatch, cache_dir=tmp_path, monthly_budget=250, monthly_reserve=15
        )
        status = provider.budget_status()
        assert status["key_resolved"] is True
        assert status["budget"] == 250
        assert status["used"] == 0
        assert status["remaining"] == 250
        assert status["reserve"] == 15
        assert re.match(r"^\d{4}-\d{2}$", status["month"])
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", status["resets_hint"])

    def test_never_spends_the_budget(self, monkeypatch, tmp_path) -> None:
        provider = _hybrid_provider(_client({}), monkeypatch, cache_dir=tmp_path)
        provider.budget_status()
        provider.budget_status()
        used, _ = provider._budget.snapshot()
        assert used == 0

    def test_key_resolved_false_when_no_key(self, monkeypatch, tmp_path) -> None:
        provider = _hybrid_provider(_client({}), monkeypatch, cache_dir=tmp_path, key=None)
        assert provider.budget_status()["key_resolved"] is False


class TestCachedDetail:
    SESSION = dt.date(2026, 8, 3)

    def test_reads_back_a_written_cache_entry(self, tmp_path) -> None:
        provider = AdanosProvider(_config(), cache_dir=tmp_path)
        path = provider._detail_cache_path("NVDA", self.SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"symbol": "NVDA", "platform": "x", "buzz_score": 80.0}), encoding="utf-8"
        )

        cached = provider.cached_detail("nvda", self.SESSION)
        assert cached["buzz_score"] == 80.0

    def test_platform_mismatch_is_a_miss(self, tmp_path) -> None:
        provider = AdanosProvider(_config(), cache_dir=tmp_path)
        path = provider._detail_cache_path("NVDA", self.SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"symbol": "NVDA", "platform": "x"}), encoding="utf-8")

        assert provider.cached_detail("NVDA", self.SESSION, platform="reddit") is None

    def test_missing_entry_is_none(self, tmp_path) -> None:
        provider = AdanosProvider(_config(), cache_dir=tmp_path)
        assert provider.cached_detail("NVDA", self.SESSION) is None

    def test_no_cache_dir_is_none(self) -> None:
        provider = AdanosProvider(_config())
        assert provider.cached_detail("NVDA", self.SESSION) is None


class TestEnrichTopCandidates:
    """``AdanosProvider.enrich_top_candidates`` -- the bounded, budget-guarded
    post-scan enrichment ``pipeline.Pipeline._enrich_adanos_top_candidates``
    calls with the session's top-scoring symbols."""

    SESSION = dt.date(2026, 8, 3)

    def test_one_call_per_symbol_up_to_the_configured_top_n(self, monkeypatch, tmp_path) -> None:
        client = _client(
            {
                "x/stocks/v1/stock/AAA": _fixture("x_stock_detail"),
                "x/stocks/v1/stock/BBB": _fixture("x_stock_detail"),
                "x/stocks/v1/stock/CCC": _fixture("x_stock_detail"),
            }
        )
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=2
        )
        spent = provider.enrich_top_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert spent == 2
        urls = {str(c.url).split("?")[0] for c in client.calls}  # type: ignore[attr-defined]
        assert urls == {
            "https://api.adanos.org/x/stocks/v1/stock/AAA",
            "https://api.adanos.org/x/stocks/v1/stock/BBB",
        }

    def test_writes_a_cache_file_per_enriched_symbol(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=3
        )
        provider.enrich_top_candidates(["AAA"], session=self.SESSION)

        path = provider._detail_cache_path("AAA", self.SESSION)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["symbol"] == "AAA"
        assert data["enriched_at_session"] == self.SESSION.isoformat()

    def test_a_symbol_already_cached_this_session_is_not_spent_again(
        self, monkeypatch, tmp_path
    ) -> None:
        """The mechanism behind "no double spend same session": a re-scan of
        the same trading session must not re-enrich (and re-spend for) a
        symbol it already enriched."""
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=3
        )

        spent_first = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        spent_second = provider.enrich_top_candidates(["AAA"], session=self.SESSION)

        assert spent_first == 1
        assert spent_second == 0
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_disabled_makes_no_calls(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, enrich_enabled=False)
        spent = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_zero_top_candidates_disables_enrichment(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=0
        )
        spent = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_no_key_skips_silently(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        spent = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_no_cache_dir_skips_silently(self, monkeypatch) -> None:
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=None)
        spent = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_one_symbols_failure_does_not_abort_the_rest(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/BBB": _fixture("x_stock_detail")})  # AAA 404s
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=2
        )
        spent = provider.enrich_top_candidates(["AAA", "BBB"], session=self.SESSION)
        assert spent == 1
        assert provider._detail_cache_path("BBB", self.SESSION).exists()
        assert not provider._detail_cache_path("AAA", self.SESSION).exists()

    def test_budget_guard_stops_early_rather_than_skipping_one_by_one(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client(
            {
                "x/stocks/v1/stock/AAA": _fixture("x_stock_detail"),
                "x/stocks/v1/stock/BBB": _fixture("x_stock_detail"),
            }
        )
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_top_candidates=3,
            monthly_budget=1,
            monthly_reserve=0,
        )
        spent = provider.enrich_top_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert spent == 1  # AAA spends the only unit of budget; BBB/CCC never attempted
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_never_raises_even_on_an_unexpected_exception(self, monkeypatch, tmp_path) -> None:
        """Belt and suspenders: even a non-ProviderError raised from inside
        the loop must not escape -- a scan's success can never hinge on this."""
        client = _client({"x/stocks/v1/stock/AAA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_top_candidates=1
        )

        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(provider, "fetch_stock_detail", _boom)
        spent = provider.enrich_top_candidates(["AAA"], session=self.SESSION)
        assert spent == 0


class TestConfigValidators:
    def test_enrich_top_candidates_must_be_within_0_and_10(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(enrich_top_candidates=11)
        with pytest.raises(ValidationError):
            _config(enrich_top_candidates=-1)
        _config(enrich_top_candidates=0)
        _config(enrich_top_candidates=10)

    def test_detail_platform_default_must_be_a_known_platform(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(detail_platform_default="bogus")
        _config(detail_platform_default="reddit")
