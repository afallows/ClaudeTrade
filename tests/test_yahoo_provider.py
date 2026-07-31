"""Tests for the Yahoo Finance bars-fallback market-data provider.

Driven over a mocked transport (``httpx.MockTransport``, same pattern as
``tests/test_reddit_provider.py``) with response shapes transcribed from
Yahoo Finance's real, undocumented ``v8/finance/chart`` endpoint. Only the
socket is fake; everything the provider does with the response -- URL
construction, symbol mapping, JSON parsing, rate limiting, error handling --
is real.

This is the ONLY Yahoo endpoint this adapter calls. The batched
``v7/finance/quote`` endpoint previously tested here has been removed
outright: a real production refresh found it now requires cookie+crumb
authentication and returns HTTP 401 unconditionally, so
``YahooMarketProvider`` no longer attempts it and ``get_market_caps`` simply
inherits the ``MarketDataProvider`` protocol's "not supported" default (see
``providers.market.tipranks.TipRanksProvider`` for the real market-cap
source now).

This environment cannot reach ``query1.finance.yahoo.com`` (egress policy
answers 403), so these mocked-transport tests are the only way to exercise
the real adapter code paths here; see docs/api-providers.md for the account
of what was and was not reachable while building this feature.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from claudetrade.config import MarketDataConfig
from claudetrade.providers.base import ProviderError
from claudetrade.providers.market.yahoo import YahooMarketProvider

# --------------------------------------------------------------------------
# Real payload shapes
# --------------------------------------------------------------------------


def _chart_payload(symbol: str, timestamps: list[int], closes: list[float]) -> dict:
    """Shaped exactly like a real ``v8/finance/chart/{symbol}`` response."""
    n = len(timestamps)
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "currency": "USD", "exchangeName": "NMS"},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [c - 1.0 for c in closes],
                                "high": [c + 1.5 for c in closes],
                                "low": [c - 1.5 for c in closes],
                                "close": closes,
                                "volume": [1_000_000 + i for i in range(n)],
                            }
                        ],
                        "adjclose": [{"adjclose": [c - 0.05 for c in closes]}],
                    },
                }
            ],
            "error": None,
        }
    }


_CHART_ERROR_PAYLOAD = {
    "chart": {
        "result": None,
        "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"},
    }
}


class _YahooStub:
    """Serves chart responses, recording every request made."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.rate_limited_next = False
        self.chart_by_symbol: dict[str, dict] = {}
        #: symbol -> HTTP status Yahoo answers with for that symbol's chart
        #: request, e.g. a real HTTP 404/410 for a delisted ticker -- as
        #: opposed to ``_CHART_ERROR_PAYLOAD``'s HTTP-200-with-JSON-error
        #: shape, which is a different failure mode this adapter already
        #: handled before this change.
        self.status_override: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.rate_limited_next:
            return httpx.Response(429, json={})

        if "/v8/finance/chart/" in request.url.path:
            symbol = request.url.path.rsplit("/", 1)[-1]
            status = self.status_override.get(symbol)
            if status is not None:
                # A real HTTP-level error -- an empty/irrelevant body, since
                # a genuine 404 from this endpoint carries no chart JSON.
                return httpx.Response(status, json={})
            payload = self.chart_by_symbol.get(symbol, _CHART_ERROR_PAYLOAD)
            return httpx.Response(200, json=payload)

        return httpx.Response(404, json={})  # pragma: no cover - unexpected path


def _install(monkeypatch, stub: _YahooStub) -> None:
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(stub.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.yahoo.httpx.Client", _factory)


# --------------------------------------------------------------------------
# symbol mapping
# --------------------------------------------------------------------------


def test_yahoo_symbol_mapping_us_is_bare():
    assert YahooMarketProvider.yahoo_symbol("AAPL") == "AAPL"
    assert YahooMarketProvider.yahoo_symbol("aapl") == "AAPL"


def test_yahoo_symbol_mapping_ca_gets_to_suffix():
    assert YahooMarketProvider.yahoo_symbol("SHOP", exchange="TSX") == "SHOP.TO"
    assert YahooMarketProvider.yahoo_symbol("XYZ", exchange="TSXV") == "XYZ.TO"


def test_yahoo_symbol_mapping_ca_share_class_confirmed_by_real_probe():
    """CONFIRMED by a real probe from the owner's machine: Yahoo's chart
    endpoint accepts the Canadian share-class ticker as ``TECK-B.TO`` --
    this codebase's own hyphenated convention with the ``.TO`` suffix
    appended, NOT a dotted rewrite the way TipRanks' ``TSE:`` notation uses."""
    assert YahooMarketProvider.yahoo_symbol("TECK-B", exchange="TSX") == "TECK-B.TO"


def test_yahoo_symbol_mapping_defaults_from_packaged_universe():
    """With no explicit exchange, falls back to the packaged seed's exchange
    column -- same convention as stooq's symbol mapping."""
    assert YahooMarketProvider.yahoo_symbol("AAPL") == "AAPL"  # NASDAQ
    assert YahooMarketProvider.yahoo_symbol("SHOP") == "SHOP.TO"  # TSX


def test_yahoo_symbol_mapping_passes_through_explicit_suffix():
    assert YahooMarketProvider.yahoo_symbol("BMW.DE") == "BMW.DE"


def test_yahoo_symbol_mapping_share_class_hyphen_untouched():
    """Share classes already use this codebase's hyphen convention (BRK-B),
    which happens to be Yahoo's own convention too -- no remapping needed,
    but the dot-detection must not be tripped by the hyphen."""
    assert YahooMarketProvider.yahoo_symbol("BRK-B") == "BRK-B"


# --------------------------------------------------------------------------
# get_daily_bars
# --------------------------------------------------------------------------


def test_get_daily_bars_parses_real_chart_shape(monkeypatch):
    stub = _YahooStub()
    ts = [
        int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp()),
        int(dt.datetime(2024, 1, 3, 21, 0, tzinfo=dt.UTC).timestamp()),
    ]
    stub.chart_by_symbol["AAPL"] = _chart_payload("AAPL", ts, [185.64, 184.25])
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    bars = result["AAPL"]
    assert len(bars) == 2
    assert bars[0].session == dt.date(2024, 1, 2)
    assert bars[0].close == pytest.approx(185.64)
    assert bars[0].adj_close == pytest.approx(185.59)
    assert bars[0].source == "yahoo"
    assert [b.session for b in bars] == sorted(b.session for b in bars)


def test_get_daily_bars_requests_the_chart_endpoint_with_bounded_range(monkeypatch):
    stub = _YahooStub()
    ts = [int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp())]
    stub.chart_by_symbol["AAPL"] = _chart_payload("AAPL", ts, [185.64])
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    req = stub.requests[0]
    assert "/v8/finance/chart/AAPL" in str(req.url)
    assert "period1=" in str(req.url)
    assert "period2=" in str(req.url)
    assert "interval=1d" in str(req.url)


def test_get_daily_bars_restricts_to_requested_range(monkeypatch):
    stub = _YahooStub()
    ts = [
        int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp()),
        int(dt.datetime(2024, 2, 2, 21, 0, tzinfo=dt.UTC).timestamp()),
    ]
    stub.chart_by_symbol["AAPL"] = _chart_payload("AAPL", ts, [185.64, 190.0])
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert [b.session for b in result["AAPL"]] == [dt.date(2024, 1, 2)]


def test_get_daily_bars_unknown_symbol_degrades_per_symbol(monkeypatch):
    """One delisted/unknown ticker must not take down the rest of the batch --
    same contract as the stooq adapter's per-symbol degrade."""
    stub = _YahooStub()
    ts = [int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp())]
    stub.chart_by_symbol["AAPL"] = _chart_payload("AAPL", ts, [185.64])
    # "NOSUCH" is deliberately absent from chart_by_symbol -> error payload.
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(
        ["AAPL", "NOSUCH"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )
    assert result["AAPL"], "a good symbol in the same batch must still be fetched"
    assert result["NOSUCH"] == [], "unknown symbol degrades to an empty series, not a raise"
    assert "NOSUCH" in provider._not_found


def test_get_daily_bars_http_404_degrades_per_symbol_not_the_batch(monkeypatch):
    """The root-cause bug this change fixes: a real HTTP 404 (delisted
    symbol) from the chart endpoint used to become a generic, batch-aborting
    ``ProviderError`` -- ``get_daily_bars``'s per-symbol loop would then
    re-raise it (see ``_fetch_one``'s ``except ProviderError: raise``
    branch), cancelling every other symbol in the same batch via
    ``parallel_map`` and cascading the whole batch to the fallback provider.
    A batch of three where the MIDDLE symbol 404s must still return bars for
    the other two, and must not raise at all."""
    stub = _YahooStub()
    ts = [int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp())]
    stub.chart_by_symbol["AAA"] = _chart_payload("AAA", ts, [10.0])
    stub.chart_by_symbol["CCC"] = _chart_payload("CCC", ts, [30.0])
    stub.status_override["BBB"] = 404  # a real HTTP 404, not the JSON-error shape
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(
        ["AAA", "BBB", "CCC"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )

    assert result["AAA"], "a good symbol before the 404'd one must still be fetched"
    assert result["CCC"], "a good symbol after the 404'd one must still be fetched"
    assert result["BBB"] == [], "the 404'd symbol degrades to an empty series, not a raise"
    assert "BBB" in provider._not_found


def test_get_daily_bars_http_410_degrades_per_symbol_too(monkeypatch):
    """410 Gone gets the same per-symbol treatment as 404 -- both mean "this
    ticker is not being served any more", mapped in the same code path."""
    stub = _YahooStub()
    ts = [int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp())]
    stub.chart_by_symbol["AAPL"] = _chart_payload("AAPL", ts, [185.64])
    stub.status_override["GONE"] = 410
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(
        ["AAPL", "GONE"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )

    assert result["AAPL"]
    assert result["GONE"] == []
    assert "GONE" in provider._not_found


def test_get_daily_bars_429_still_raises_rate_limit(monkeypatch):
    """A 429 must NOT be swallowed the way a 404 now is -- it is a quantity
    signal (come back later), never treated as "unknown ticker", and still
    aborts the batch as a retryable ``ProviderError``."""
    stub = _YahooStub()
    stub.rate_limited_next = True
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    with pytest.raises(ProviderError) as excinfo:
        provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert excinfo.value.retryable is True
    assert "rate limited" in str(excinfo.value).lower()
    assert "AAPL" not in provider._not_found


def test_get_daily_bars_network_failure_is_a_retryable_provider_error(monkeypatch):
    def _failing_factory(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("claudetrade.providers.market.yahoo.httpx.Client", _failing_factory)
    provider = YahooMarketProvider(MarketDataConfig())
    with pytest.raises(ProviderError) as excinfo:
        provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert excinfo.value.retryable is True


def test_get_daily_bars_null_padded_rows_are_skipped(monkeypatch):
    """Yahoo pads halts/holidays with nulls rather than omitting the row; a
    null OHLC must not become a fabricated zero-price bar."""
    stub = _YahooStub()
    ts = [
        int(dt.datetime(2024, 1, 2, 21, 0, tzinfo=dt.UTC).timestamp()),
        int(dt.datetime(2024, 1, 3, 21, 0, tzinfo=dt.UTC).timestamp()),
    ]
    payload = _chart_payload("AAPL", ts, [185.64, 184.25])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    stub.chart_by_symbol["AAPL"] = payload
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_daily_bars(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert len(result["AAPL"]) == 1


def test_get_intraday_bars_not_implemented():
    provider = YahooMarketProvider(MarketDataConfig())
    with pytest.raises(ProviderError):
        provider.get_intraday_bars(
            ["AAPL"],
            dt.datetime(2024, 1, 2, tzinfo=dt.UTC),
            dt.datetime(2024, 1, 3, tzinfo=dt.UTC),
        )


# --------------------------------------------------------------------------
# get_market_caps -- REMOVED capability (quote API now requires cookie+crumb)
# --------------------------------------------------------------------------


def test_get_market_caps_is_not_supported():
    """The v7/finance/quote batched endpoint this used to call now requires
    cookie+crumb auth in production (HTTP 401 unconditionally) and has been
    removed from this adapter outright -- ``get_market_caps`` is no longer
    overridden at all, so it inherits the ``MarketDataProvider`` protocol's
    "not supported" default, same as synthetic/csv. Real market caps now come
    from ``providers.market.tipranks.TipRanksProvider``."""
    provider = YahooMarketProvider(MarketDataConfig())
    assert provider.get_market_caps(["AAPL", "MSFT"]) == {}


def test_get_market_caps_never_calls_the_network(monkeypatch):
    """Not overridden at all -- there must be no HTTP call of any kind."""
    stub = _YahooStub()
    _install(monkeypatch, stub)
    provider = YahooMarketProvider(MarketDataConfig())
    provider.get_market_caps(["AAPL"])
    assert stub.requests == []


# --------------------------------------------------------------------------
# get_security_info -- packaged-seed-only degrade (no quote endpoint left)
# --------------------------------------------------------------------------


def test_get_security_info_serves_packaged_seed_only():
    """With the quote endpoint gone, this can only ever return what the
    packaged seed universe already knows -- same honest degrade as
    ``StooqMarketProvider.get_security_info``."""
    provider = YahooMarketProvider(MarketDataConfig())
    info = provider.get_security_info(["AAPL", "NOTREAL999"])
    assert info["AAPL"].name  # packaged seed has a name for AAPL
    assert info["NOTREAL999"].symbol == "NOTREAL999"
    assert info["NOTREAL999"].name == ""


def test_get_security_info_never_calls_the_network(monkeypatch):
    stub = _YahooStub()
    _install(monkeypatch, stub)
    provider = YahooMarketProvider(MarketDataConfig())
    provider.get_security_info(["AAPL"])
    assert stub.requests == []


# --------------------------------------------------------------------------
# status / list_universe / corporate actions
# --------------------------------------------------------------------------


def test_status_declares_no_market_cap_capability():
    status = YahooMarketProvider(MarketDataConfig()).status()
    assert status.capabilities["market_caps"] is False
    assert status.capabilities["intraday"] is False
    assert status.licence_note, "undocumented-endpoint caveat must be stated"
    assert "fallback" in status.message.lower() or "fallback" in status.licence_note.lower()


def test_list_universe_returns_packaged_seed():
    """No bulk reference-data endpoint in this free tier either."""
    provider = YahooMarketProvider(MarketDataConfig())
    securities = provider.list_universe()
    symbols = {s.symbol for s in securities}
    assert len(securities) > 500
    assert "AAPL" in symbols
    assert "SHOP" in symbols


def test_get_corporate_actions_returns_honest_empty_result():
    provider = YahooMarketProvider(MarketDataConfig())
    result = provider.get_corporate_actions(["AAPL"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert result == {"AAPL": []}


# --------------------------------------------------------------------------
# default protocol behaviour on the OTHER providers
# --------------------------------------------------------------------------


def test_other_market_providers_default_to_unsupported_market_caps():
    """Every provider except tipranks inherits the Protocol's "not supported"
    default (empty mapping) for ``get_market_caps`` -- including yahoo now
    that its quote-API-backed override has been removed outright."""
    from claudetrade.providers.market.csv_provider import CSVMarketProvider
    from claudetrade.providers.market.stooq import StooqMarketProvider
    from claudetrade.providers.market.synthetic import SyntheticMarketProvider

    assert SyntheticMarketProvider().get_market_caps(["AAPL"]) == {}
    assert CSVMarketProvider().get_market_caps(["AAPL"]) == {}
    assert StooqMarketProvider().get_market_caps(["AAPL"]) == {}
    assert YahooMarketProvider().get_market_caps(["AAPL"]) == {}
