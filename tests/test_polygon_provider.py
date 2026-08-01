"""Tests for the Polygon.io grouped-daily provider (QA handoff v3 F23).

Driven over ``httpx.MockTransport`` (same pattern as
``tests/test_tipranks_provider.py``) using shape-faithful fixtures under
``tests/fixtures/polygon/`` -- this sandbox has no egress, so only the socket
is faked; URL construction, per-date iteration, symbol filtering/notation,
the on-disk per-date cache, key resolution, and the error taxonomy are all
real adapter code paths. The registry-level tests at the bottom prove the two
cascade contracts the F23 fix leans on: an UNCONFIGURED polygon primary
degrades cleanly to the fallbacks, and a configured one leaves per-symbol
gaps (TSX names, delisted tickers) for the fallbacks to fill.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from claudetrade.config import AppConfig, PolygonConfig
from claudetrade.domain import Bar
from claudetrade.providers.base import (
    AuthenticationError,
    NotConfiguredError,
    ProviderError,
    RateLimitError,
)
from claudetrade.providers.market.polygon import (
    PolygonProvider,
    polygon_ticker,
)
from claudetrade.providers.registry import FallbackMarketProvider, get_market_provider

FIXTURES = Path(__file__).parent / "fixtures" / "polygon"
GROUPED_0729 = json.loads((FIXTURES / "grouped_2026-07-29.json").read_text(encoding="utf-8"))
GROUPED_0730 = json.loads((FIXTURES / "grouped_2026-07-30.json").read_text(encoding="utf-8"))

#: Wed/Thu of the fixture week (2026-08-01 is a Saturday).
D_0729 = dt.date(2026, 7, 29)
D_0730 = dt.date(2026, 7, 30)


class _PolygonStub:
    """Serves grouped-daily responses keyed by the date in the URL path."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.payload_by_date: dict[str, dict] = {}
        self.status_override: int | None = None
        self.headers_override: dict[str, str] | None = None
        self.body_override: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(
                self.status_override, json={}, headers=self.headers_override or {}
            )
        if self.body_override is not None:
            return httpx.Response(200, content=self.body_override)
        date = request.url.path.rsplit("/", 1)[-1]
        payload = self.payload_by_date.get(date)
        if payload is None:
            # An OK envelope with nothing in it -- how Polygon answers a date
            # it has no data for.
            return httpx.Response(
                200, json={"status": "OK", "resultsCount": 0, "results": []}
            )
        return httpx.Response(200, json=payload)


def _install(monkeypatch, stub: _PolygonStub) -> None:
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(stub.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.polygon.httpx.Client", _factory)


@pytest.fixture
def stub() -> _PolygonStub:
    s = _PolygonStub()
    s.payload_by_date["2026-07-29"] = GROUPED_0729
    s.payload_by_date["2026-07-30"] = GROUPED_0730
    return s


@pytest.fixture
def no_ambient_key(monkeypatch):
    """Guarantee no polygon key leaks in from the surrounding environment."""
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDETRADE_SECRET_POLYGON_API_KEY", raising=False)


def _provider(tmp_path: Path, *, config: PolygonConfig | None = None) -> PolygonProvider:
    return PolygonProvider(config or PolygonConfig(), cache_dir=tmp_path / "cache")


def _keyed_provider(tmp_path: Path, monkeypatch, **config_kwargs) -> PolygonProvider:
    """A configured provider whose limiter never actually sleeps.

    ``RateLimiter`` really does block, so leaving the free-tier default of
    5/min in place would make a two-date test wait 12 real seconds. The
    limiter itself is exercised by ``providers.base``'s own tests; here the
    default is overridden so this file stays fast. Tests that assert on the
    default rate use ``_provider`` (no override) instead.
    """
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    config_kwargs.setdefault("rate_limit_per_minute", 6000)
    return _provider(tmp_path, config=PolygonConfig(**config_kwargs))


# --------------------------------------------------------------------------
# symbol notation
# --------------------------------------------------------------------------


def test_polygon_ticker_us_class_share_gets_dot_notation():
    """Same narrow rule as tipranks: only a single trailing letter after a
    dash is a share class; nothing else is ever rewritten."""
    assert polygon_ticker("BRK-B") == "BRK.B"
    assert polygon_ticker("bf-b") == "BF.B"
    assert polygon_ticker("AAPL") == "AAPL"
    assert polygon_ticker("LILAP") == "LILAP"


# --------------------------------------------------------------------------
# enabled-by-key semantics
# --------------------------------------------------------------------------


def test_unconfigured_status_says_how_to_configure(tmp_path, no_ambient_key):
    provider = _provider(tmp_path)
    status = provider.status()
    assert status.configured is False
    assert status.available is False
    # The message must be actionable: both the plain env var and the config
    # slot are named, per the F23 spec.
    assert "POLYGON_API_KEY" in status.message
    assert "api_key" in status.message


def test_unconfigured_get_daily_bars_raises_not_configured(tmp_path, no_ambient_key):
    provider = _provider(tmp_path)
    with pytest.raises(NotConfiguredError):
        provider.get_daily_bars(["AAPL"], D_0729, D_0730)


def test_key_resolves_from_plain_env_var(tmp_path, monkeypatch, no_ambient_key):
    monkeypatch.setenv("POLYGON_API_KEY", "env-key")
    provider = _provider(tmp_path)
    assert provider.status().configured is True


def test_key_resolves_from_claudetrade_secret_env(tmp_path, monkeypatch, no_ambient_key):
    monkeypatch.setenv("CLAUDETRADE_SECRET_POLYGON_API_KEY", "secret-key")
    provider = _provider(tmp_path)
    assert provider.status().configured is True


def test_key_resolves_from_config_field(tmp_path, no_ambient_key):
    provider = _provider(tmp_path, config=PolygonConfig(api_key="config-key"))
    assert provider.status().configured is True


def test_config_api_key_is_redacted_from_public_dict():
    """A key set directly in config must never reach the loggable/persistable
    view (or the config hash, which must not vary with a credential)."""
    config = AppConfig()
    config.polygon.api_key = "very-secret"
    hash_with_key = config.config_hash
    assert config.public_dict()["polygon"]["api_key"] == "***"
    config.polygon.api_key = "different-secret"
    assert config.config_hash == hash_with_key


# --------------------------------------------------------------------------
# grouped-daily bars
# --------------------------------------------------------------------------


def test_get_daily_bars_one_call_per_trading_date(tmp_path, monkeypatch, no_ambient_key, stub):
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    # The window spans the weekend + Saturday 2026-08-01: only the two real
    # trading dates may be requested (2026-07-31 has no fixture and would
    # come back empty -- include only Wed..Thu to keep the call count exact).
    result = provider.get_daily_bars(["AAPL", "INTC", "BRK-B", "GONE"], D_0729, D_0730)

    requested_dates = [r.url.path.rsplit("/", 1)[-1] for r in stub.requests]
    assert requested_dates == ["2026-07-29", "2026-07-30"]
    # The key travels as a query parameter on every request.
    assert all("apiKey=test-key" in str(r.url) for r in stub.requests)
    assert all("adjusted=true" in str(r.url) for r in stub.requests)

    # Two bars per covered symbol, sorted by session.
    assert [b.session for b in result["AAPL"]] == [D_0729, D_0730]
    assert result["AAPL"][0].close == pytest.approx(231.15)
    assert result["AAPL"][1].close == pytest.approx(233.02)
    assert result["AAPL"][0].volume == pytest.approx(70790813)
    assert result["AAPL"][0].source == "polygon"
    # adjusted=true is a split-adjusted series with no separate dividend-
    # adjusted close -- adj_close honestly stays None (effective_adj_close
    # falls back to close).
    assert result["AAPL"][0].adj_close is None

    # Dot-notation mapping: Polygon's BRK.B row lands under our BRK-B key.
    assert [b.session for b in result["BRK-B"]] == [D_0729, D_0730]
    assert result["BRK-B"][0].close == pytest.approx(477.83)

    # A requested symbol Polygon has nothing for -> empty list (the cascade
    # fill contract), and an unrequested response row (ZZZC) is filtered out.
    assert result["GONE"] == []
    assert "ZZZC" not in result

    # The malformed row (null OHLC) was skipped without failing the date.
    assert "BROKEN" not in result


def test_weekend_dates_cost_zero_calls(tmp_path, monkeypatch, no_ambient_key, stub):
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    # Sat 2026-08-01 .. Sun 2026-08-02: no trading days, no HTTP.
    result = provider.get_daily_bars(["AAPL"], dt.date(2026, 8, 1), dt.date(2026, 8, 2))
    assert stub.requests == []
    assert result == {"AAPL": []}


# --------------------------------------------------------------------------
# per-date on-disk cache
# --------------------------------------------------------------------------


def test_cache_hit_costs_zero_http(tmp_path, monkeypatch, no_ambient_key, stub):
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    first = provider.get_daily_bars(["AAPL"], D_0729, D_0730)
    assert len(stub.requests) == 2
    assert (tmp_path / "cache" / "polygon" / "2026-07-29.json").exists()

    # Second call -- different symbols, same dates -- is served entirely from
    # disk: this is what makes ingest's chunked calls cheap (chunk 1 fetches
    # each date once, chunks 2..N are free) and backfill re-runs idempotent.
    second = provider.get_daily_bars(["INTC", "SPY"], D_0729, D_0730)
    assert len(stub.requests) == 2
    assert [b.close for b in second["INTC"]] == [pytest.approx(33.62), pytest.approx(34.05)]
    assert first["AAPL"][0].close == pytest.approx(231.15)

    # A fresh provider instance (a new process/run) also hits the same cache.
    reborn = _keyed_provider(tmp_path, monkeypatch)
    _install(monkeypatch, stub)
    reborn.get_daily_bars(["SPY"], D_0729, D_0729)
    assert len(stub.requests) == 2


def test_empty_response_is_never_cached(tmp_path, monkeypatch, no_ambient_key):
    """An empty date (EOD not yet published, or an unmodelled ad-hoc closure)
    must stay re-checkable -- a permanently-cached empty answer is the one
    wrong outcome."""
    stub = _PolygonStub()  # no payloads at all -> every date answers empty
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    assert provider.get_daily_bars(["AAPL"], D_0729, D_0729) == {"AAPL": []}
    assert not (tmp_path / "cache" / "polygon" / "2026-07-29.json").exists()
    provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert len(stub.requests) == 2  # re-fetched, not served from cache


def test_current_session_not_cached_until_settled(tmp_path, monkeypatch, no_ambient_key, stub):
    """An intraday grouped row is a partial-day aggregate; the current
    session's response only becomes the permanent cached answer once the
    session has closed and settled."""
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    # Freeze "now" to 15:00 ET on the 2026-07-29 session (19:00 UTC).
    frozen_open = dt.datetime(2026, 7, 29, 19, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        "claudetrade.providers.market.polygon.utc_now", lambda: frozen_open
    )
    monkeypatch.setattr(
        "claudetrade.providers.market.polygon.current_trading_session", lambda: D_0729
    )
    provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert not (tmp_path / "cache" / "polygon" / "2026-07-29.json").exists()
    assert len(stub.requests) == 1

    # After the close + settle buffer (16:00 ET + 1h -> 21:00 UTC) the same
    # date caches normally.
    frozen_settled = dt.datetime(2026, 7, 29, 21, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(
        "claudetrade.providers.market.polygon.utc_now", lambda: frozen_settled
    )
    provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert (tmp_path / "cache" / "polygon" / "2026-07-29.json").exists()
    assert len(stub.requests) == 2


def test_corrupt_cache_file_is_a_miss_not_an_error(tmp_path, monkeypatch, no_ambient_key, stub):
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)
    cache_file = tmp_path / "cache" / "polygon" / "2026-07-29.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{truncated", encoding="utf-8")

    result = provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert len(stub.requests) == 1  # re-fetched
    assert result["AAPL"][0].close == pytest.approx(231.15)


def test_bypass_cache_refetches_and_rewrites(tmp_path, monkeypatch, no_ambient_key, stub):
    """``--force`` backfills bypass the cache so restatements are picked up."""
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)
    provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert len(stub.requests) == 1

    bars = provider.grouped_daily_bars(["AAPL"], D_0729, bypass_cache=True)
    assert len(stub.requests) == 2
    assert bars["AAPL"].close == pytest.approx(231.15)


# --------------------------------------------------------------------------
# error taxonomy
# --------------------------------------------------------------------------


def test_http_429_raises_rate_limit_error_with_retry_after(
    tmp_path, monkeypatch, no_ambient_key
):
    stub = _PolygonStub()
    stub.status_override = 429
    stub.headers_override = {"Retry-After": "23"}
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    with pytest.raises(RateLimitError) as excinfo:
        provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert excinfo.value.retry_after_s == pytest.approx(23.0)


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_failures_raise_authentication_error(
    tmp_path, monkeypatch, no_ambient_key, status
):
    stub = _PolygonStub()
    stub.status_override = status
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    with pytest.raises(AuthenticationError):
        provider.get_daily_bars(["AAPL"], D_0729, D_0729)


def test_http_5xx_raises_retryable_provider_error(tmp_path, monkeypatch, no_ambient_key):
    stub = _PolygonStub()
    stub.status_override = 503
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    with pytest.raises(ProviderError) as excinfo:
        provider.get_daily_bars(["AAPL"], D_0729, D_0729)
    assert excinfo.value.retryable is True


def test_error_status_payload_raises_provider_error(tmp_path, monkeypatch, no_ambient_key):
    stub = _PolygonStub()
    stub.payload_by_date["2026-07-29"] = {
        "status": "ERROR",
        "error": "Unknown API Key",
        "request_id": "x",
    }
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    with pytest.raises(ProviderError, match="Unknown API Key"):
        provider.get_daily_bars(["AAPL"], D_0729, D_0729)


def test_non_json_body_raises_provider_error(tmp_path, monkeypatch, no_ambient_key):
    stub = _PolygonStub()
    stub.body_override = "<html>maintenance</html>"
    _install(monkeypatch, stub)
    provider = _keyed_provider(tmp_path, monkeypatch)

    with pytest.raises(ProviderError):
        provider.get_daily_bars(["AAPL"], D_0729, D_0729)


# --------------------------------------------------------------------------
# provider metadata / reference-data conventions
# --------------------------------------------------------------------------


def test_bulk_daily_flag_and_not_bars_last_resort(tmp_path, no_ambient_key):
    provider = _provider(tmp_path)
    assert provider.bulk_daily is True
    assert getattr(provider, "bars_last_resort", False) is False
    assert provider.status().rate_limit_per_minute == 5  # free-tier default


def test_reference_data_is_deliberately_minimal(tmp_path, no_ambient_key):
    """Bars source only: nameless security stubs (so the cascade fills real
    reference data from tipranks), no market caps, empty corporate actions,
    intraday unsupported -- but a NON-empty packaged universe, because
    ``FallbackMarketProvider.list_universe`` is primary-only and an empty
    answer would silently empty every polygon-primary refresh."""
    provider = _provider(tmp_path)

    info = provider.get_security_info(["AAPL"])
    assert info["AAPL"].symbol == "AAPL"
    assert info["AAPL"].name == ""  # unfilled -> the cascade keeps looking

    assert provider.get_market_caps(["AAPL"]) == {}
    assert provider.get_corporate_actions(["AAPL"], D_0729, D_0730) == {"AAPL": []}
    assert len(provider.list_universe()) > 0
    with pytest.raises(ProviderError):
        provider.get_intraday_bars(
            ["AAPL"],
            dt.datetime(2026, 7, 29, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 30, tzinfo=dt.UTC),
        )


# --------------------------------------------------------------------------
# cascade behaviour (registry-level)
# --------------------------------------------------------------------------


class _FakeBarsFallback:
    """Minimal fallback that serves one bar per requested symbol."""

    name = "fake_fallback"

    def __init__(self):
        self.requested: list[list[str]] = []

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        self.requested.append(sorted(symbols))
        return {
            s: [
                Bar(
                    symbol=s, session=start, open=1.0, high=1.0, low=1.0,
                    close=1.0, volume=1.0, source=self.name,
                )
            ]
            for s in symbols
        }


def test_unconfigured_polygon_primary_degrades_to_fallbacks(
    tmp_path, monkeypatch, no_ambient_key
):
    """The registry contract F23 depends on: polygon with no key raises
    ``NotConfiguredError`` from the cascade's first attempt, and every
    symbol is filled by the next provider -- no crash, no empty result."""
    primary = _provider(tmp_path)
    fallback = _FakeBarsFallback()
    cascade = FallbackMarketProvider(primary, [fallback])

    result = cascade.get_daily_bars(["AAPL", "MSFT"], D_0729, D_0729)
    assert fallback.requested == [["AAPL", "MSFT"]]
    assert result["AAPL"][0].source == "fake_fallback"
    assert result["MSFT"][0].source == "fake_fallback"


def test_configured_polygon_gaps_are_filled_per_symbol(
    tmp_path, monkeypatch, no_ambient_key, stub
):
    """A symbol absent from the grouped response (a TSX listing, a delisted
    name) cascades to the fallbacks WITHOUT disturbing the symbols polygon
    did serve -- per-symbol fill, not all-or-nothing."""
    _install(monkeypatch, stub)
    primary = _keyed_provider(tmp_path, monkeypatch)
    fallback = _FakeBarsFallback()
    cascade = FallbackMarketProvider(primary, [fallback])

    result = cascade.get_daily_bars(["AAPL", "TECK-B"], D_0729, D_0729)
    assert result["AAPL"][0].source == "polygon"
    assert result["TECK-B"][0].source == "fake_fallback"
    # Only the unfilled symbol reached the fallback.
    assert fallback.requested == [["TECK-B"]]


def test_recommended_owner_config_end_to_end(tmp_path, monkeypatch, no_ambient_key, stub):
    """The exact recommended configuration -- provider = "polygon",
    fallbacks = ["tipranks", "yahoo", "csv"] -- wired through the real
    registry: bars come from the polygon fixture; reference data cascades
    past polygon's nameless stubs to a (stubbed) tipranks dataForTicker."""
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDETRADE_HOME", str(tmp_path))

    config = AppConfig()
    config.paths.app_dir = tmp_path
    config.market_data.provider = "polygon"
    config.market_data.fallbacks = ["tipranks", "yahoo", "csv"]
    # Keep the real (blocking) limiters out of the test's wall clock; the
    # cascade wiring under test here is unrelated to pacing.
    config.polygon.rate_limit_per_minute = 6000
    config.tipranks.rate_limit_per_minute = 6000

    _install(monkeypatch, stub)

    # TipRanks answers the reference-data cascade from the committed INTC
    # fixture (same stubbing approach as tests/test_tipranks_provider.py).
    tipranks_payload = json.loads(
        (Path(__file__).parent / "fixtures" / "tipranks" / "dataForTicker_INTC.json")
        .read_text(encoding="utf-8")
    )

    def _tipranks_handler(request: httpx.Request) -> httpx.Response:
        if httpx.QueryParams(request.url.query).get("ticker") == "INTC":
            return httpx.Response(200, json=tipranks_payload)
        return httpx.Response(404, json={})

    real_client = httpx.Client

    def _tipranks_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_tipranks_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "claudetrade.providers.market.tipranks.httpx.Client", _tipranks_factory
    )

    provider = get_market_provider(config)
    assert isinstance(provider, FallbackMarketProvider)
    assert provider.primary.name == "polygon"
    assert [f.name for f in provider.fallbacks][:2] == ["tipranks", "yahoo"]

    # Bars: from polygon, one grouped call per date, nothing per-symbol.
    bars = provider.get_daily_bars(["INTC"], D_0729, D_0730)
    assert [b.source for b in bars["INTC"]] == ["polygon", "polygon"]
    assert [b.session for b in bars["INTC"]] == [D_0729, D_0730]

    # Reference data: polygon's nameless stub is treated as unfilled and the
    # cascade sources the real record from tipranks.
    info = provider.get_security_info(["INTC"])
    assert info["INTC"].name  # a real company name, from the tipranks fixture
    assert "intel" in info["INTC"].name.lower()

    # Universe: primary-only, must be non-empty for the refresh to work.
    assert len(provider.list_universe()) > 0
