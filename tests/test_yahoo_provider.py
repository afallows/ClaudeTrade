"""Tests for the Yahoo Finance fallback market-data provider.

Driven over a mocked transport (``httpx.MockTransport``, same pattern as
``tests/test_reddit_provider.py``) with response shapes transcribed from
Yahoo Finance's real, undocumented ``v8/finance/chart`` and
``v7/finance/quote`` endpoints. Only the socket is fake; everything the
provider does with the response -- URL construction, symbol mapping, JSON
parsing, rate limiting, error handling -- is real.

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


def _quote_payload(rows: list[dict]) -> dict:
    return {"quoteResponse": {"result": rows, "error": None}}


class _YahooStub:
    """Serves chart/quote responses, recording every request made."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.rate_limited_next = False
        self.chart_by_symbol: dict[str, dict] = {}
        self.quote_rows: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.rate_limited_next:
            return httpx.Response(429, json={})

        if "/v8/finance/chart/" in request.url.path:
            symbol = request.url.path.rsplit("/", 1)[-1]
            payload = self.chart_by_symbol.get(symbol, _CHART_ERROR_PAYLOAD)
            return httpx.Response(200, json=payload)

        if request.url.path == "/v7/finance/quote":
            return httpx.Response(200, json=_quote_payload(self.quote_rows))

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
# get_market_caps -- the capability this provider exists to add
# --------------------------------------------------------------------------


def test_get_market_caps_parses_real_quote_shape(monkeypatch):
    stub = _YahooStub()
    stub.quote_rows = [
        {"symbol": "AAPL", "marketCap": 2_800_000_000_000, "shortName": "Apple Inc."},
        {"symbol": "INTC", "marketCap": 413_002_720_000, "shortName": "Intel Corporation"},
    ]
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    caps = provider.get_market_caps(["AAPL", "INTC"])

    assert caps["AAPL"] == pytest.approx(2_800_000_000_000)
    assert caps["INTC"] == pytest.approx(413_002_720_000)


def test_get_market_caps_requests_the_batched_quote_endpoint(monkeypatch):
    stub = _YahooStub()
    stub.quote_rows = [{"symbol": "AAPL", "marketCap": 2_800_000_000_000}]
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    provider.get_market_caps(["AAPL", "MSFT"])

    req = stub.requests[0]
    assert "/v7/finance/quote" in str(req.url)
    assert "symbols=AAPL%2CMSFT" in str(req.url) or "symbols=AAPL,MSFT" in str(req.url)


def test_get_market_caps_omits_symbols_yahoo_has_no_figure_for(monkeypatch):
    """A symbol Yahoo's response has no marketCap for must be ABSENT from the
    mapping, never filled in with a guess, zero, or a stale value -- this is
    the specific behaviour ADR-0008 Decision 3's data-quality rule depends on
    to distinguish 'unresolved' from 'resolved to zero'."""
    stub = _YahooStub()
    stub.quote_rows = [
        {"symbol": "AAPL", "marketCap": 2_800_000_000_000},
        {"symbol": "NOSUCH", "marketCap": None},
        {"symbol": "ZEROCAP", "marketCap": 0},
    ]
    _install(monkeypatch, stub)

    provider = YahooMarketProvider(MarketDataConfig())
    caps = provider.get_market_caps(["AAPL", "NOSUCH", "ZEROCAP", "NEVERRETURNED"])

    assert caps == {"AAPL": pytest.approx(2_800_000_000_000)}
    assert "NOSUCH" not in caps
    assert "ZEROCAP" not in caps
    assert "NEVERRETURNED" not in caps


def test_get_market_caps_batches_respect_max_symbols_per_request(monkeypatch):
    stub = _YahooStub()
    stub.quote_rows = [{"symbol": "A", "marketCap": 1_500_000_000}]
    _install(monkeypatch, stub)

    config = MarketDataConfig(max_symbols_per_request=2)
    provider = YahooMarketProvider(config)
    provider.get_market_caps(["A", "B", "C", "D", "E"])

    quote_requests = [r for r in stub.requests if "/v7/finance/quote" in r.url.path]
    assert len(quote_requests) == 3  # 5 symbols at batch size 2 -> 3 requests


def test_get_market_caps_batch_failure_does_not_abort_other_batches(monkeypatch):
    """A network failure fetching one batch degrades those symbols to
    'unresolved' rather than raising and losing every other batch's caps."""
    stub = _YahooStub()
    stub.quote_rows = [{"symbol": "C", "marketCap": 3_000_000_000}]

    calls = {"n": 0}
    real_handler = stub.handler
    real_client = httpx.Client

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        if "/v7/finance/quote" in request.url.path:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection refused")
        return real_handler(request)

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(flaky_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.yahoo.httpx.Client", _factory)

    config = MarketDataConfig(max_symbols_per_request=1)
    provider = YahooMarketProvider(config)
    caps = provider.get_market_caps(["A", "C"])

    assert "A" not in caps  # first batch failed
    assert caps["C"] == pytest.approx(3_000_000_000)  # second batch still succeeded


# --------------------------------------------------------------------------
# status / list_universe / corporate actions
# --------------------------------------------------------------------------


def test_status_declares_market_cap_capability():
    status = YahooMarketProvider(MarketDataConfig()).status()
    assert status.capabilities["market_caps"] is True
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
    """The protocol addition must be a no-op for every pre-existing provider:
    synthetic/csv/stooq inherit the Protocol's default (empty mapping) without
    any code change to those modules."""
    from claudetrade.providers.market.csv_provider import CSVMarketProvider
    from claudetrade.providers.market.stooq import StooqMarketProvider
    from claudetrade.providers.market.synthetic import SyntheticMarketProvider

    assert SyntheticMarketProvider().get_market_caps(["AAPL"]) == {}
    assert CSVMarketProvider().get_market_caps(["AAPL"]) == {}
    assert StooqMarketProvider().get_market_caps(["AAPL"]) == {}
