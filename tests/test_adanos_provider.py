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
# Hybrid mode: on-demand calls (fetch_stock_detail / fetch_explain /
# fetch_market_sentiment), site-first with official fallback
#
# Independent of site-vs-official trending mode -- see the module docstring's
# "Hybrid mode" section. Revised 2026-08-03: the site proxy mirrors these
# on-demand routes too, so all three now run a two-rung ladder (site first,
# official fallback -- reversed by ``prefer_official_api``) and NEVER raise;
# every outcome (success, a structured vendor "no", or both rungs failing)
# comes back as a dict. Fixtures ending ``_site`` are the site-proxy shape;
# ``x_stock_detail``/``x_stock_explain`` (no suffix) are the official shape,
# used to exercise the fallback rung by leaving the site route unregistered
# (which the shared ``_client`` helper 404s by default -- a real failure).
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
        provider = _hybrid_provider(_client({}), monkeypatch)  # prefer_official_api defaults False
        assert provider.mode == "site"  # trending mode is unaffected
        assert provider.budget_status()["key_resolved"] is True

    def test_no_key_means_official_fallback_is_unavailable(self, monkeypatch) -> None:
        """The key only matters for the official FALLBACK rung now -- the
        site rung (tried first) never needed one."""
        provider = _hybrid_provider(_client({}), monkeypatch, key=None)
        assert provider.budget_status()["key_resolved"] is False


class TestOnDemandLadderOrdering:
    """The two-rung ladder itself: site first by default, official only as a
    fallback, ``prefer_official_api`` reversing the order -- shared by
    ``fetch_stock_detail``/``fetch_explain``/``fetch_market_sentiment``.
    Exercised here via ``fetch_stock_detail`` as the representative case."""

    def test_site_is_tried_first_by_default(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "site"
        assert result["quota_spent"] is False
        assert len(client.calls) == 1  # type: ignore[attr-defined]
        assert "proxy-x" in str(client.calls[0].url)  # type: ignore[attr-defined]

    def test_official_is_tried_only_as_a_fallback(self, monkeypatch, tmp_path) -> None:
        # Site route is unregistered -> the shared _client 404s it -> the
        # ladder falls back to the (registered) official route.
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "official"
        assert result["quota_spent"] is True
        assert len(client.calls) == 2  # type: ignore[attr-defined]
        assert "proxy-x" in str(client.calls[0].url)  # type: ignore[attr-defined]
        assert "api.adanos.org" in str(client.calls[1].url)  # type: ignore[attr-defined]

    def test_prefer_official_api_reverses_the_order(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, prefer_official_api=True
        )
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "official"
        assert result["quota_spent"] is True
        assert len(client.calls) == 1  # type: ignore[attr-defined]  # site never tried
        assert "api.adanos.org" in str(client.calls[0].url)  # type: ignore[attr-defined]

    def test_prefer_official_api_falls_back_to_site_when_official_fails(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, prefer_official_api=True
        )
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "site"
        # The failed official attempt still reached the vendor and spent
        # quota, even though the site rung ultimately answered.
        assert result["quota_spent"] is True
        used, _ = provider._budget.snapshot()
        assert used == 1


class TestFetchStockDetailUrlConstruction:
    def test_per_platform_site_url_no_api_key(self, monkeypatch, tmp_path) -> None:
        cases = {
            "x": "proxy-x/stock/NVDA",
            "reddit": "proxy/stock/NVDA",
            "polymarket": "proxy-polymarket/stock/NVDA",
            "news": "proxy-news/stock/NVDA",
        }
        for platform, expected_path in cases.items():
            client = _client({expected_path: _fixture("x_stock_detail_site")})
            provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
            result = provider.fetch_stock_detail("nvda", platform=platform)
            assert result["accepted"] is True, (platform, result)
            assert result["mode"] == "site"
            url = str(client.calls[0].url)  # type: ignore[attr-defined]
            assert url.startswith(f"https://adanos.org/api/{expected_path}")
            assert "X-API-Key" not in client.calls[0].headers  # type: ignore[attr-defined]
            assert len(client.calls) == 1  # type: ignore[attr-defined]  # no official fallback needed

    def test_unsupported_platform_is_a_refusal_not_a_request(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA", platform="bogus")
        assert result["accepted"] is False
        assert result["mode"] is None
        assert result["quota_spent"] is False
        assert client.calls == []  # type: ignore[attr-defined]


class TestFetchStockDetailSiteDaysParam:
    """The site rung takes ``days`` (a window size), not ``from``/``to`` like
    the official API -- ``_days_param`` translates one into the other."""

    def test_from_to_translated_to_a_day_count(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.fetch_stock_detail(
            "NVDA", platform="x", from_date="2026-07-26", to_date="2026-08-02"
        )
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert "days=7" in url

    def test_no_dates_omits_the_days_param(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.fetch_stock_detail("NVDA")
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert "days=" not in url


class TestFetchStockDetailOfficialFallbackUrlConstruction:
    def test_per_platform_url_and_auth_header(self, monkeypatch, tmp_path) -> None:
        cases = {
            "x": "x/stocks/v1/stock/NVDA",
            "reddit": "reddit/stocks/v1/stock/NVDA",
            "polymarket": "polymarket/stocks/v1/stock/NVDA",
            "news": "news/stocks/v1/stock/NVDA",
        }
        for platform, expected_path in cases.items():
            # Site route deliberately unregistered -> falls back to official.
            client = _client({expected_path: _fixture("x_stock_detail")})
            provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
            result = provider.fetch_stock_detail("nvda", platform=platform)
            assert result["accepted"] is True
            assert result["mode"] == "official"
            official_call = client.calls[-1]  # type: ignore[attr-defined]
            url = str(official_call.url)
            assert url.startswith(f"https://api.adanos.org/{expected_path}")
            assert official_call.headers["x-api-key"] == "sk_live_test"

    def test_from_to_params_are_passed_through(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.fetch_stock_detail(
            "NVDA", platform="x", from_date="2026-07-01", to_date="2026-08-01"
        )
        url = str(client.calls[-1].url)  # type: ignore[attr-defined]
        assert "from=2026-07-01" in url
        assert "to=2026-08-01" in url


class TestFetchStockDetailParsing:
    def test_normalized_header_and_raw_passthrough_site_mode(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")

        assert result["symbol"] == "NVDA"
        assert result["platform"] == "x"
        assert result["mode"] == "site"
        assert result["quota_spent"] is False
        assert result["buzz_score"] == pytest.approx(87.5)
        assert result["sentiment_score"] == pytest.approx(0.42)
        assert result["bullish_pct"] == pytest.approx(68.0)
        assert result["bearish_pct"] == pytest.approx(32.0)
        assert result["mentions"] == 950
        # Nothing invented -- the vendor's own fields pass through untouched.
        assert result["raw"]["daily_trend"][0]["date"] == "2026-07-27"
        assert result["raw"]["period_days"] == 7
        assert "budget" in result

    def test_polymarket_detail_uses_trade_count_for_the_normalized_mentions_field(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-polymarket/stock/TSLA": _fixture("polymarket_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("TSLA", platform="polymarket")
        assert result["accepted"] is True
        assert result["mentions"] == 1200  # trade_count -- polymarket has no "mentions" field
        assert result["raw"]["unique_traders"] == 400
        assert result["raw"]["total_liquidity"] == pytest.approx(250000.5)

    def test_news_detail_tolerates_a_daily_trend_shorter_than_seven_days(
        self, monkeypatch, tmp_path
    ) -> None:
        """Coordinator-verified live 2026-08-03: a quiet day can be omitted
        from ``daily_trend`` -- must not assume a fixed 7-entry window."""
        client = _client({"proxy-news/stock/NVDA": _fixture("news_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA", platform="news")
        assert result["accepted"] is True
        assert len(result["raw"]["daily_trend"]) == 6
        assert result["raw"]["source_count"] == 12

    def test_text_snippets_in_top_tweets_are_sanitised(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")

        first = result["raw"]["top_tweets"][0]["text_snippet"]
        assert "[url]" in first
        assert "example.com" not in first
        assert "[email]" in first
        assert "trader@example.com" not in first
        # sentiment_score alongside the snippet is untouched -- only the text
        # field under a known snippet key is rewritten.
        assert result["raw"]["top_tweets"][0]["sentiment_score"] == pytest.approx(0.6)

        second = result["raw"]["top_tweets"][1]["text_snippet"]
        assert "[user]" in second
        assert "some_trader" not in second

    def test_spends_no_quota_in_site_mode(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, monthly_budget=250)
        provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 0

    def test_spends_exactly_one_official_call_when_falling_back(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, monthly_budget=250)
        provider.fetch_stock_detail("NVDA")
        used, _ = provider._budget.snapshot()
        assert used == 1


class TestFetchStockDetailStructuredAnswers:
    """The vendor's two distinct "no" shapes (observed live 2026-08-03) --
    neither is an exception, and neither triggers the ladder's fallback,
    since the vendor has already given a definitive answer."""

    def test_found_false_is_a_structured_refusal_not_an_exception(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-x/stock/ZZZZ": _fixture("stock_detail_not_found")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("ZZZZ")
        assert result["accepted"] is False
        assert "found: false" in result["reason"]
        assert result["mode"] == "site"
        assert result["quota_spent"] is False
        assert result["unsupported_ticker"] is False
        assert len(client.calls) == 1  # type: ignore[attr-defined]  # official never tried

    def test_unsupported_ticker_error_code_is_a_distinct_structured_refusal(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client(
            {"proxy-x/stock/ZZZZFAKE": _fixture("stock_detail_unsupported_ticker")}, status=404
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("ZZZZFAKE")
        assert result["accepted"] is False
        assert "unsupported ticker" in result["reason"].lower()
        assert "ZZZZFAKE" in result["reason"]
        assert result["mode"] == "site"
        assert result["unsupported_ticker"] is True
        assert len(client.calls) == 1  # type: ignore[attr-defined]  # official never tried

    def test_endpoint_absent_error_body_is_a_site_failure_not_data(
        self, monkeypatch, tmp_path
    ) -> None:
        """``{"error": "Not found"}`` -- unlike ``{"found": false}`` or the
        unsupported-ticker shape -- means the ROUTE itself does not exist on
        this proxy base, so it must trigger the ladder's fallback rather
        than being parsed as an answer."""
        client = _client(
            {
                "proxy-x/stock/NVDA": _fixture("endpoint_absent"),
                "x/stocks/v1/stock/NVDA": _fixture("x_stock_detail"),
            }
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "official"


class TestFetchStockDetailBudgetGuard:
    def test_no_key_still_succeeds_via_site(self, monkeypatch, tmp_path) -> None:
        """Unlike before the site-first ladder, on-demand detail no longer
        requires a key to succeed at all."""
        client = _client({"proxy-x/stock/NVDA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "site"
        assert result["quota_spent"] is False

    def test_no_key_and_site_failure_is_a_refusal_without_an_official_request(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-x/stock/NVDA": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "no adanos_api_key" in result["reason"]
        assert len(client.calls) == 1  # type: ignore[attr-defined]  # site only, no unmetered fallback

    def test_fails_closed_at_the_reserve_floor_without_sending_an_official_request(
        self, monkeypatch, tmp_path
    ) -> None:
        # Site left unregistered -> every attempt falls back to official,
        # spending budget, until the reserve floor is reached.
        client = _client({"x/stocks/v1/stock/NVDA": _fixture("x_stock_detail")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, monthly_budget=5, monthly_reserve=2
        )
        for _ in range(3):
            provider.fetch_stock_detail("NVDA")  # used -> 3, remaining -> 2 (== reserve)
        assert len(client.calls) == 6  # type: ignore[attr-defined]  # 3x (site attempt + official)

        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "resets" in result["reason"]
        assert result["quota_spent"] is False
        assert len(client.calls) == 7  # type: ignore[attr-defined]  # +1 site attempt; no official sent

    def test_refusals_never_touch_the_budget_counter_when_no_key_resolves(
        self, monkeypatch, tmp_path
    ) -> None:
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
        provider.fetch_stock_detail("NVDA")  # site unregistered -> falls back to official
        used, _ = provider._budget.snapshot()
        assert used == 249  # max(local increment of 1, 250 - 1)


class TestFetchStockDetailErrorTaxonomy:
    """``fetch_stock_detail`` never raises -- a genuine HTTP failure on
    either rung is recorded, not raised, and only surfaces as a refusal if
    EVERY available rung failed (see ``_ladder_refusal``). The site rung is
    left unregistered in most of these so it fails "naturally" via the
    shared ``_client`` helper's 404 default, isolating each case to the
    official rung's behaviour."""

    def test_both_rungs_failing_returns_a_refusal_naming_both(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA": {}, "x/stocks/v1/stock/NVDA": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert result["mode"] is None
        assert "site:" in result["reason"]
        assert "official:" in result["reason"]

    def test_401_on_official_rung_is_recorded_not_raised(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=401)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "official:" in result["reason"]
        assert "401" in result["reason"]

    def test_403_names_the_history_window(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=403)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "history window" in result["reason"]

    def test_429_is_recorded_not_raised(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=429)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "official:" in result["reason"]

    def test_5xx_is_recorded_not_raised(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=503)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert "official:" in result["reason"]

    def test_no_retries_on_failure(self, monkeypatch, tmp_path) -> None:
        client = _client({})  # both rungs fail via the shared 404 default
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.fetch_stock_detail("NVDA")
        # Exactly one attempt per rung -- site then official, no retry loop.
        assert len(client.calls) == 2  # type: ignore[attr-defined]

    def test_a_failed_official_attempt_still_counts_against_the_budget(
        self, monkeypatch, tmp_path
    ) -> None:
        """The vendor served (and presumably counted) the request even
        though it errored -- the local counter must reflect that."""
        client = _client({"x/stocks/v1/stock/NVDA": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_stock_detail("NVDA")
        assert result["accepted"] is False
        assert result["quota_spent"] is True
        used, _ = provider._budget.snapshot()
        assert used == 1


class TestFetchExplain:
    def test_site_url_construction(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA/explain": _fixture("x_stock_explain_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("nvda")
        assert result["accepted"] is True
        assert result["mode"] == "site"
        assert result["quota_spent"] is False
        url = str(client.calls[0].url)  # type: ignore[attr-defined]
        assert url == "https://adanos.org/api/proxy-x/stock/NVDA/explain"

    def test_returns_explanation_cached_and_generated_at(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA/explain": _fixture("x_stock_explain_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("NVDA")
        assert "AI chip demand" in result["explanation"]
        assert result["cached"] is False
        assert result["generated_at"] == "2026-08-02T00:00:00+00:00"
        assert "budget" in result

    def test_falls_back_to_official_when_the_site_rung_fails(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("NVDA")
        assert result["accepted"] is True
        assert result["mode"] == "official"
        assert result["quota_spent"] is True
        url = str(client.calls[-1].url)  # type: ignore[attr-defined]
        assert url == "https://api.adanos.org/x/stocks/v1/stock/NVDA/explain"

    def test_polymarket_explain_skips_the_site_rung_entirely(self, monkeypatch, tmp_path) -> None:
        """Confirmed absent by a live probe 2026-08-03: the site proxy never
        mirrors explain for Polymarket (``{"error": "Not found"}``) -- unlike
        Polymarket's other on-demand routes. ``fetch_explain`` must go
        straight to the official rung rather than wasting a network
        round-trip on a route already known not to exist."""
        client = _client({"polymarket/stocks/v1/stock/TSLA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("TSLA", platform="polymarket")
        assert result["accepted"] is True
        assert result["mode"] == "official"
        assert len(client.calls) == 1  # type: ignore[attr-defined]  # no site round-trip at all
        assert "proxy-polymarket" not in str(client.calls[0].url)  # type: ignore[attr-defined]

    def test_polymarket_explain_with_no_key_names_the_confirmed_absence(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_explain("TSLA", platform="polymarket")
        assert result["accepted"] is False
        assert "confirmed absent" in result["reason"]
        assert client.calls == []  # type: ignore[attr-defined]  # neither rung sent a request

    def test_no_key_and_site_failure_is_a_structured_refusal(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/NVDA/explain": {}}, status=500)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        result = provider.fetch_explain("NVDA")
        assert result["accepted"] is False
        assert client.calls  # type: ignore[attr-defined]  # the free site attempt was still made

    def test_budget_refusal_names_the_reset_date_when_both_rungs_fail(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": _fixture("x_stock_explain")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, monthly_budget=1, monthly_reserve=0
        )
        provider.fetch_explain("NVDA")  # site fails, official spends the one call -> remaining 0
        result = provider.fetch_explain("NVDA")  # site fails again, official now budget-guarded
        assert result["accepted"] is False
        assert "resets" in result["reason"]

    def test_401_on_official_rung_is_recorded_not_raised(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/stock/NVDA/explain": {}}, status=401)
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_explain("NVDA")
        assert result["accepted"] is False
        assert "official:" in result["reason"]


class TestFetchMarketSentiment:
    def test_site_success_for_x(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/market-sentiment": _fixture("market_sentiment_x")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment("x")

        assert result["accepted"] is True
        assert result["mode"] == "site"
        assert result["quota_spent"] is False
        assert "symbol" not in result  # service-level, not per-ticker
        assert result["buzz_score"] == pytest.approx(72.0)
        assert result["sentiment_score"] == pytest.approx(0.18)
        assert result["bullish_pct"] == pytest.approx(55.0)
        assert result["bearish_pct"] == pytest.approx(45.0)
        assert result["mentions"] == 15400
        assert result["active_tickers"] == 340
        assert len(result["drivers"]) == 5
        assert result["drivers"][0]["ticker"] == "NVDA"
        assert "budget" in result

    def test_polymarket_uses_trade_count_for_the_normalized_mentions_field(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client(
            {"proxy-polymarket/market-sentiment": _fixture("market_sentiment_polymarket")}
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment("polymarket")
        assert result["accepted"] is True
        assert result["mentions"] == 5000  # trade_count -- polymarket has no "mentions" field

    def test_default_platform_is_x(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/market-sentiment": _fixture("market_sentiment_x")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment()
        assert result["accepted"] is True
        assert result["platform"] == "x"

    def test_falls_back_to_official_when_the_site_rung_fails(self, monkeypatch, tmp_path) -> None:
        client = _client({"x/stocks/v1/market-sentiment": _fixture("market_sentiment_x")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment("x")
        assert result["accepted"] is True
        assert result["mode"] == "official"
        assert result["quota_spent"] is True
        url = str(client.calls[-1].url)  # type: ignore[attr-defined]
        assert url.startswith("https://api.adanos.org/x/stocks/v1/market-sentiment")

    def test_unsupported_platform_is_a_refusal_not_a_request(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment("bogus")
        assert result["accepted"] is False
        assert client.calls == []  # type: ignore[attr-defined]

    def test_both_rungs_failing_is_a_structured_refusal(self, monkeypatch, tmp_path) -> None:
        client = _client({})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        result = provider.fetch_market_sentiment("x")
        assert result["accepted"] is False
        assert result["mode"] is None
        assert "site:" in result["reason"]
        assert "official:" in result["reason"]
        assert "budget" in result


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


class TestEnrichCandidatesTopNScope:
    """``AdanosProvider.enrich_candidates`` under ``enrich_scope = "top_n"``
    -- the pre-generalisation top-N-only behaviour, still available
    explicitly. ``pipeline.Pipeline._enrich_adanos_candidates`` calls this
    with the session's symbols, best-scoring first; the provider applies
    scope/cap itself (see ``TestEnrichCandidatesAllScope`` for the new
    default)."""

    SESSION = dt.date(2026, 8, 3)

    def test_one_call_per_symbol_up_to_the_configured_top_n(self, monkeypatch, tmp_path) -> None:
        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "proxy-x/stock/BBB": _fixture("x_stock_detail_site"),
                "proxy-x/stock/CCC": _fixture("x_stock_detail_site"),
            }
        )
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=2,
            enrich_delay_seconds=0,
        )
        spent = provider.enrich_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert spent == 2
        urls = {str(c.url).split("?")[0] for c in client.calls}  # type: ignore[attr-defined]
        assert urls == {
            "https://adanos.org/api/proxy-x/stock/AAA",
            "https://adanos.org/api/proxy-x/stock/BBB",
        }
        used, _ = provider._budget.snapshot()
        assert used == 0  # site mode -- ordinary enrichment spends zero official quota

    def test_writes_a_cache_file_per_enriched_symbol(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=3,
        )
        provider.enrich_candidates(["AAA"], session=self.SESSION)

        path = provider._detail_cache_path("AAA", self.SESSION)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["symbol"] == "AAA"
        assert data["mode"] == "site"
        assert data["quota_spent"] is False
        assert data["enriched_at_session"] == self.SESSION.isoformat()

    def test_a_symbol_already_cached_this_session_is_not_spent_again(
        self, monkeypatch, tmp_path
    ) -> None:
        """The mechanism behind "no double spend same session": a re-scan of
        the same trading session must not re-enrich (and re-fetch for) a
        symbol it already enriched."""
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=3,
        )

        spent_first = provider.enrich_candidates(["AAA"], session=self.SESSION)
        spent_second = provider.enrich_candidates(["AAA"], session=self.SESSION)

        assert spent_first == 1
        assert spent_second == 0
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_disabled_makes_no_calls(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, enrich_enabled=False)
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_scope_off_disables_enrichment_regardless_of_enrich_enabled(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, enrich_scope="off")
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_zero_top_candidates_disables_enrichment(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=0,
        )
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_no_key_still_enriches_via_site(self, monkeypatch, tmp_path) -> None:
        """Unlike before the site-first ladder, enrichment no longer requires
        ``adanos_api_key`` to resolve at all -- ``fetch_stock_detail`` works
        keylessly via the free site rung."""
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path, key=None)
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 1
        assert len(client.calls) == 1  # type: ignore[attr-defined]

    def test_no_cache_dir_skips_silently(self, monkeypatch) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=None)
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 0
        assert client.calls == []  # type: ignore[attr-defined]

    def test_one_symbols_failure_does_not_abort_the_rest(self, monkeypatch, tmp_path) -> None:
        # AAA: both rungs fail (neither route registered). BBB: site succeeds.
        client = _client({"proxy-x/stock/BBB": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=2,
            enrich_delay_seconds=0,
        )
        spent = provider.enrich_candidates(["AAA", "BBB"], session=self.SESSION)
        assert spent == 1
        assert provider._detail_cache_path("BBB", self.SESSION).exists()
        assert not provider._detail_cache_path("AAA", self.SESSION).exists()

    def test_budget_exhaustion_only_affects_symbols_whose_site_lookup_fails(
        self, monkeypatch, tmp_path
    ) -> None:
        """No more "stop early once budget-guarded" -- that assumed every
        call spent the shared official budget, which is no longer true in
        site mode. Each symbol is judged independently: AAA's free site hit
        succeeds regardless of the official budget; BBB's site lookup fails
        and falls back to an already-exhausted official rung; CCC fails both
        rungs outright."""
        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "x/stocks/v1/stock/BBB": _fixture("x_stock_detail"),
            }
        )
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=3,
            enrich_delay_seconds=0,
            monthly_budget=1,
            monthly_reserve=1,  # official rung starts already at its reserve floor
        )
        spent = provider.enrich_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert spent == 1
        assert provider._detail_cache_path("AAA", self.SESSION).exists()
        assert not provider._detail_cache_path("BBB", self.SESSION).exists()
        assert not provider._detail_cache_path("CCC", self.SESSION).exists()
        used, _ = provider._budget.snapshot()
        assert used == 0  # official was never actually reached -- pre-emptively refused each time

    def test_never_raises_even_on_an_unexpected_exception(self, monkeypatch, tmp_path) -> None:
        """Belt and suspenders: even a non-ProviderError raised from inside
        the loop must not escape -- a scan's success can never hinge on this."""
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_scope="top_n",
            enrich_top_candidates=1,
        )

        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(provider, "fetch_stock_detail", _boom)
        spent = provider.enrich_candidates(["AAA"], session=self.SESSION)
        assert spent == 0


class TestEnrichCandidatesAllScope:
    """``enrich_scope = "all"`` -- the new default: every distinct signal
    symbol, best-scoring first, capped by ``enrich_max_symbols_per_scan``."""

    SESSION = dt.date(2026, 8, 3)

    def test_default_scope_is_all(self) -> None:
        assert _config().enrich_scope == "all"

    def test_enriches_every_distinct_symbol_by_default(self, monkeypatch, tmp_path) -> None:
        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "proxy-x/stock/BBB": _fixture("x_stock_detail_site"),
                "proxy-x/stock/CCC": _fixture("x_stock_detail_site"),
            }
        )
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_delay_seconds=0
        )
        spent = provider.enrich_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert spent == 3

    def test_caps_at_enrich_max_symbols_per_scan_and_logs_the_dropped_symbols(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        symbols = [f"SYM{i}" for i in range(5)]
        routes = {f"proxy-x/stock/{s}": _fixture("x_stock_detail_site") for s in symbols[:3]}
        client = _client(routes)
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_max_symbols_per_scan=3,
            enrich_delay_seconds=0,
        )
        with caplog.at_level("INFO"):
            spent = provider.enrich_candidates(symbols, session=self.SESSION)
        assert spent == 3
        for kept in symbols[:3]:
            assert provider._detail_cache_path(kept, self.SESSION).exists()
        for dropped in symbols[3:]:
            assert not provider._detail_cache_path(dropped, self.SESSION).exists()
        assert any("SYM3" in r.message and "SYM4" in r.message for r in caplog.records)

    def test_never_touches_enrich_top_candidates_in_all_scope(self, monkeypatch, tmp_path) -> None:
        """``enrich_top_candidates`` is a ``"top_n"``-only knob -- a low
        value must not silently cap ``"all"`` scope."""
        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "proxy-x/stock/BBB": _fixture("x_stock_detail_site"),
            }
        )
        provider = _hybrid_provider(
            client,
            monkeypatch,
            cache_dir=tmp_path,
            enrich_top_candidates=1,  # would cap top_n scope at 1
            enrich_delay_seconds=0,
        )
        spent = provider.enrich_candidates(["AAA", "BBB"], session=self.SESSION)
        assert spent == 2


class TestEnrichCandidatesDelay:
    """``enrich_delay_seconds`` -- paced between successive ACTUAL network
    calls only; never before the first call, and never for a cache/memo
    hit."""

    SESSION = dt.date(2026, 8, 3)

    #: ``RateLimiter.acquire`` (``providers.base``) also calls ``time.sleep``
    #: for its own token-bucket pacing -- and since these tests' patched
    #: ``time.sleep`` records instead of actually blocking, real wall-clock
    #: time never advances, so the rate limiter's own (sub-second, given
    #: ``calls_per_minute=6000`` in ``_config``) waits show up in the
    #: recording too. Filtering to sleeps at or above 1 second isolates
    #: THIS test's own ``enrich_delay_seconds`` pacing from that unrelated
    #: noise without needing to fake ``time.monotonic`` as well.
    _NOISE_FLOOR = 1.0

    def test_sleeps_between_calls_but_not_before_the_first(self, monkeypatch, tmp_path) -> None:
        import claudetrade.providers.social.adanos as adanos_module

        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "proxy-x/stock/BBB": _fixture("x_stock_detail_site"),
                "proxy-x/stock/CCC": _fixture("x_stock_detail_site"),
            }
        )
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_delay_seconds=2.5
        )
        sleeps: list[float] = []
        monkeypatch.setattr(adanos_module.time, "sleep", sleeps.append)

        provider.enrich_candidates(["AAA", "BBB", "CCC"], session=self.SESSION)
        assert [s for s in sleeps if s >= self._NOISE_FLOOR] == [2.5, 2.5]

    def test_zero_delay_never_sleeps(self, monkeypatch, tmp_path) -> None:
        import claudetrade.providers.social.adanos as adanos_module

        client = _client(
            {
                "proxy-x/stock/AAA": _fixture("x_stock_detail_site"),
                "proxy-x/stock/BBB": _fixture("x_stock_detail_site"),
            }
        )
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_delay_seconds=0
        )
        sleeps: list[float] = []
        monkeypatch.setattr(adanos_module.time, "sleep", sleeps.append)

        provider.enrich_candidates(["AAA", "BBB"], session=self.SESSION)
        assert [s for s in sleeps if s >= self._NOISE_FLOOR] == []

    def test_cache_hits_do_not_count_toward_pacing(self, monkeypatch, tmp_path) -> None:
        """AAA is already cached (no network call); BBB is the first REAL
        network attempt this run and must not be delayed."""
        import claudetrade.providers.social.adanos as adanos_module

        client = _client({"proxy-x/stock/BBB": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(
            client, monkeypatch, cache_dir=tmp_path, enrich_delay_seconds=5.0
        )
        path = provider._detail_cache_path("AAA", self.SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"symbol": "AAA", "platform": "x"}), encoding="utf-8")

        sleeps: list[float] = []
        monkeypatch.setattr(adanos_module.time, "sleep", sleeps.append)

        provider.enrich_candidates(["AAA", "BBB"], session=self.SESSION)
        # Only one real network attempt (BBB) -- nothing to pace between.
        assert [s for s in sleeps if s >= self._NOISE_FLOOR] == []


class TestEnrichCandidatesUnsupportedTickerMemo:
    """Vendor-definitive ``unsupported_ticker`` refusals are memoised so a
    scan never re-asks about the same symbol every day; the quieter
    ``{"found": false}`` refusal is deliberately NOT memoised."""

    SESSION = dt.date(2026, 8, 3)

    def test_unsupported_ticker_refusal_is_memoized_and_skips_a_later_call_with_no_request(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client(
            {"proxy-x/stock/ZZZZFAKE": _fixture("stock_detail_unsupported_ticker")}, status=404
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)

        spent_first = provider.enrich_candidates(["ZZZZFAKE"], session=self.SESSION)
        assert spent_first == 0
        calls_after_first = len(client.calls)  # type: ignore[attr-defined]
        assert calls_after_first >= 1

        spent_second = provider.enrich_candidates(["ZZZZFAKE"], session=self.SESSION)
        assert spent_second == 0
        assert len(client.calls) == calls_after_first  # type: ignore[attr-defined]  # no new request

    def test_found_false_quiet_refusal_is_not_memoized(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/ZZZZ": _fixture("stock_detail_not_found")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)

        provider.enrich_candidates(["ZZZZ"], session=self.SESSION)
        first_calls = len(client.calls)  # type: ignore[attr-defined]

        provider.enrich_candidates(["ZZZZ"], session=self.SESSION)
        # Tried again -- NOT memoised, unlike the unsupported-ticker case.
        assert len(client.calls) > first_calls  # type: ignore[attr-defined]

    def test_memo_persists_across_provider_instances(self, monkeypatch, tmp_path) -> None:
        client = _client(
            {"proxy-x/stock/ZZZZFAKE": _fixture("stock_detail_unsupported_ticker")}, status=404
        )
        first = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        first.enrich_candidates(["ZZZZFAKE"], session=self.SESSION)
        calls_after_first = len(client.calls)  # type: ignore[attr-defined]

        second = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        second.enrich_candidates(["ZZZZFAKE"], session=self.SESSION)
        assert len(client.calls) == calls_after_first  # type: ignore[attr-defined]

    def test_memo_expires_after_the_30_trading_day_horizon(self, monkeypatch, tmp_path) -> None:
        from claudetrade.utils.timeutils import next_trading_day

        client = _client(
            {"proxy-x/stock/ZZZZFAKE": _fixture("stock_detail_unsupported_ticker")}, status=404
        )
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        provider.enrich_candidates(["ZZZZFAKE"], session=self.SESSION)
        calls_after_first = len(client.calls)  # type: ignore[attr-defined]

        still_memoized_session = next_trading_day(self.SESSION, skip=29)
        provider.enrich_candidates(["ZZZZFAKE"], session=still_memoized_session)
        assert len(client.calls) == calls_after_first  # type: ignore[attr-defined]  # still memoised

        expired_session = next_trading_day(self.SESSION, skip=31)
        provider.enrich_candidates(["ZZZZFAKE"], session=expired_session)
        assert len(client.calls) > calls_after_first  # type: ignore[attr-defined]  # re-probed


class TestEnrichCandidatesOnSnapshot:
    """The ``on_snapshot`` callback -- how ``pipeline.Pipeline`` feeds
    ``db.models.AdanosSnapshotRow`` from a successful enrichment (see
    ``tests/test_pipeline_adanos_enrichment.py`` for the pipeline-side
    storage wiring this feeds)."""

    SESSION = dt.date(2026, 8, 3)

    def test_called_once_per_successful_enrichment_with_the_normalized_header(
        self, monkeypatch, tmp_path
    ) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        snapshots = []

        spent = provider.enrich_candidates(
            ["AAA"], session=self.SESSION, on_snapshot=snapshots.append
        )

        assert spent == 1
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.symbol == "AAA"
        assert snap.platform == "x"
        assert snap.buzz_score == pytest.approx(87.5)
        assert snap.sentiment_score == pytest.approx(0.42)
        assert snap.bullish_pct == pytest.approx(68.0)
        assert snap.bearish_pct == pytest.approx(32.0)
        assert snap.mentions == 950
        assert snap.trend == "rising"
        assert snap.company_name == "NVIDIA Corp"
        # The per-ticker detail response carries no trend_history field
        # (that's a trending/market-sentiment shape) -- honestly empty.
        assert snap.trend_history == []

    def test_not_called_for_a_refused_symbol(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/ZZZZ": _fixture("stock_detail_not_found")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        snapshots = []

        provider.enrich_candidates(["ZZZZ"], session=self.SESSION, on_snapshot=snapshots.append)

        assert snapshots == []

    def test_callback_exception_does_not_abort_enrichment(self, monkeypatch, tmp_path) -> None:
        client = _client({"proxy-x/stock/AAA": _fixture("x_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)

        def _boom(_snapshot) -> None:
            raise RuntimeError("storage boom")

        spent = provider.enrich_candidates(["AAA"], session=self.SESSION, on_snapshot=_boom)
        assert spent == 1  # the cache write still counted
        assert provider._detail_cache_path("AAA", self.SESSION).exists()

    def test_polymarket_engagement_uses_total_liquidity(self, monkeypatch, tmp_path) -> None:
        from claudetrade.providers.social.adanos import _snapshot_from_detail

        client = _client({"proxy-polymarket/stock/TSLA": _fixture("polymarket_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        envelope = provider.fetch_stock_detail("TSLA", platform="polymarket")
        snap = _snapshot_from_detail("TSLA", "polymarket", envelope)
        assert snap.mentions == 1200  # trade_count
        assert snap.engagement == pytest.approx(250000.5)  # total_liquidity

    def test_news_engagement_uses_source_count(self, monkeypatch, tmp_path) -> None:
        from claudetrade.providers.social.adanos import _snapshot_from_detail

        client = _client({"proxy-news/stock/NVDA": _fixture("news_stock_detail_site")})
        provider = _hybrid_provider(client, monkeypatch, cache_dir=tmp_path)
        envelope = provider.fetch_stock_detail("NVDA", platform="news")
        snap = _snapshot_from_detail("NVDA", "news", envelope)
        assert snap.engagement == pytest.approx(12.0)  # source_count


class TestConfigValidators:
    def test_enrich_top_candidates_must_be_within_0_and_10(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(enrich_top_candidates=11)
        with pytest.raises(ValidationError):
            _config(enrich_top_candidates=-1)
        _config(enrich_top_candidates=0)
        _config(enrich_top_candidates=10)

    def test_enrich_scope_must_be_a_known_value(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(enrich_scope="bogus")
        _config(enrich_scope="all")
        _config(enrich_scope="top_n")
        _config(enrich_scope="off")

    def test_enrich_max_symbols_per_scan_must_be_within_1_and_200(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(enrich_max_symbols_per_scan=0)
        with pytest.raises(ValidationError):
            _config(enrich_max_symbols_per_scan=201)
        _config(enrich_max_symbols_per_scan=1)
        _config(enrich_max_symbols_per_scan=200)

    def test_enrich_delay_seconds_must_be_non_negative(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(enrich_delay_seconds=-0.1)
        _config(enrich_delay_seconds=0.0)
        _config(enrich_delay_seconds=30.0)

    def test_detail_platform_default_must_be_a_known_platform(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _config(detail_platform_default="bogus")
        _config(detail_platform_default="reddit")
