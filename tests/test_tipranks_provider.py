"""Tests for the TipRanks widget-API provider.

Driven over ``httpx.MockTransport`` (same pattern as
``tests/test_reddit_provider.py`` / ``tests/test_yahoo_provider.py``) using
the two real fixtures the project owner captured from their own machine:

* ``tests/fixtures/tipranks/dataForTicker_INTC.json`` -- a US (NASDAQ) listing.
* ``tests/fixtures/tipranks/dataForTicker_TECK_B.json`` -- a Canadian (TSX)
  listing (``ticker=TSE:TECK.B``), with several Canadian-specific schema
  quirks (see that file's own ``_fixture_note`` and the assertions below).

This sandbox cannot reach ``widgets.tipranks.com`` or
``marketsv3.tipranks.com`` (egress is fully blocked); only the socket is
faked here -- URL construction, symbol mapping, JSON parsing, caching, rate
limiting and fail-closed error handling are all real adapter code paths.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from claudetrade.config import TipRanksConfig
from claudetrade.domain import EarningsSession
from claudetrade.providers.base import ProviderError, RateLimitError, SourceBlockedError
from claudetrade.providers.market.tipranks import (
    TipRanksProvider,
    _parse_getquotes_response,
    tipranks_ticker,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tipranks"
INTC_PAYLOAD = json.loads((FIXTURES / "dataForTicker_INTC.json").read_text(encoding="utf-8"))
TECK_B_PAYLOAD = json.loads((FIXTURES / "dataForTicker_TECK_B.json").read_text(encoding="utf-8"))
MHD_HISTORICALPRICES = json.loads(
    (FIXTURES / "historicalprices_MHD.json").read_text(encoding="utf-8")
)


class _TipRanksStub:
    """Serves ``dataForTicker``/``historicalprices`` responses keyed by the
    ``ticker`` query param and the request path."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.payload_by_ticker: dict[str, dict] = {}
        #: Tickers that should get a 200 + null overview instead of the
        #: default (confirmed-by-probe) 404 for "not in payload_by_ticker".
        self.null_overview_tickers: set[str] = set()
        #: ``historicalprices`` fallback responses, keyed by ticker -- a
        #: ticker absent here gets a 404 from that endpoint too.
        self.historicalprices_by_ticker: dict[str, list] = {}
        self.status_override: int | None = None
        self.content_type_override: str | None = None
        self.body_override: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={})
        if self.body_override is not None:
            headers = {"content-type": self.content_type_override or "text/html"}
            return httpx.Response(200, headers=headers, content=self.body_override)

        ticker = httpx.QueryParams(request.url.query).get("ticker", "")

        if request.url.path.endswith("/historicalprices"):
            rows = self.historicalprices_by_ticker.get(ticker)
            if rows is not None:
                return httpx.Response(200, json=rows)
            return httpx.Response(404, json={})

        payload = self.payload_by_ticker.get(ticker)
        if payload is not None:
            return httpx.Response(200, json=payload)
        if ticker in self.null_overview_tickers:
            # A rarer, but still real per the fail-closed rules, shape: 200
            # with an empty/null overview.
            return httpx.Response(200, json={"overview": None})
        # CONFIRMED by a real probe: an unknown/garbage ticker gets a clean
        # HTTP 404, not a 200 with a null overview.
        return httpx.Response(404, json={})


def _install(monkeypatch, stub: _TipRanksStub) -> None:
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(stub.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.tipranks.httpx.Client", _factory)


@pytest.fixture
def stub() -> _TipRanksStub:
    s = _TipRanksStub()
    s.payload_by_ticker["INTC"] = INTC_PAYLOAD
    s.payload_by_ticker["TSE:TECK.B"] = TECK_B_PAYLOAD
    return s


def _provider(tmp_path: Path, *, config: TipRanksConfig | None = None) -> TipRanksProvider:
    return TipRanksProvider(config or TipRanksConfig(), cache_dir=tmp_path / "cache")


# --------------------------------------------------------------------------
# symbol mapping
# --------------------------------------------------------------------------


def test_tipranks_ticker_us_is_bare():
    assert tipranks_ticker("AAPL") == "AAPL"
    assert tipranks_ticker("intc", exchange="NASDAQ") == "INTC"


def test_tipranks_ticker_ca_gets_tse_prefix_and_dotted_share_class():
    """Confirmed against the TECK.B fixture: our hyphenated share-class
    convention (``TECK-B``) becomes TipRanks' ``TSE:TECK.B``."""
    assert tipranks_ticker("TECK-B", exchange="TSX") == "TSE:TECK.B"
    assert tipranks_ticker("SHOP", exchange="TSX") == "TSE:SHOP"
    assert tipranks_ticker("XYZ", exchange="TSXV") == "TSE:XYZ"


def test_tipranks_ticker_us_class_share_gets_dot_notation():
    """CONFIRMED by the owner's live refresh log: TipRanks 404s on 'BRK-B'/
    'BF-B' under this codebase's own dash notation and needs the dot form.
    Yahoo, by contrast, wants the dash form for the very same symbols (see
    ``YahooMarketProvider.yahoo_symbol``) -- this mapping is local to this
    module only."""
    assert tipranks_ticker("BRK-B", exchange="NYSE") == "BRK.B"
    assert tipranks_ticker("BF-B", exchange="NYSE") == "BF.B"
    assert tipranks_ticker("brk-b", exchange="NYSE") == "BRK.B"


def test_tipranks_ticker_plain_and_multiletter_symbols_are_unaffected():
    """A symbol with no dash, or one that merely happens to contain a dash
    that is NOT a single-letter class suffix, must never be rewritten."""
    assert tipranks_ticker("AAPL", exchange="NASDAQ") == "AAPL"
    assert tipranks_ticker("LILAP", exchange="NASDAQ") == "LILAP"


def test_tipranks_ticker_tsx_dot_conversion_is_unchanged_by_the_us_rule():
    """The TSX ``TSE:`` + dotted-share-class mapping is untouched -- it takes
    a different code path from the US single-letter-suffix rule above."""
    assert tipranks_ticker("TECK-B", exchange="TSX") == "TSE:TECK.B"


def test_tipranks_ticker_resolves_exchange_from_packaged_universe():
    """No explicit exchange passed -- the real ``get_daily_bars``/etc calling
    convention -- falls back to the packaged seed's exchange column, same
    convention as stooq/yahoo."""
    assert tipranks_ticker("AAPL") == "AAPL"  # NASDAQ in us_default.csv
    assert tipranks_ticker("TECK-B") == "TSE:TECK.B"  # TSX in ca_default.csv


# --------------------------------------------------------------------------
# earnings -- the headline capability
# --------------------------------------------------------------------------


def test_get_upcoming_earnings_maps_next_earnings_report(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    result = provider.get_upcoming_earnings(["INTC"], through=dt.date(2026, 7, 30))
    events = result["INTC"]
    assert len(events) == 1
    event = events[0]
    assert event.symbol == "INTC"
    assert event.report_date == dt.date(2026, 10, 22)
    assert event.confirmed is False
    assert event.session is EarningsSession.AFTER_CLOSE  # timeOfDay=4
    assert event.eps_estimate == pytest.approx(0.38)
    assert event.eps_actual is None
    assert event.source == "tipranks"
    assert event.as_of is not None


def test_get_historical_earnings_maps_last_reported_eps(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    result = provider.get_historical_earnings(
        ["INTC"], dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )
    events = result["INTC"]
    assert len(events) == 1
    event = events[0]
    assert event.report_date == dt.date(2026, 7, 23)
    assert event.confirmed is True
    assert event.session is EarningsSession.BEFORE_OPEN  # timeOfDay=1
    assert event.eps_estimate == pytest.approx(0.22)
    assert event.eps_actual == pytest.approx(0.42)
    assert event.surprise_pct == pytest.approx(-48.0)  # surprise=-0.48 -> percent
    assert event.revenue_actual == pytest.approx(16128000000.0)
    assert event.revenue_estimate == pytest.approx(14434847000.0)
    assert event.as_of is not None


def test_historical_earnings_outside_window_is_excluded(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    result = provider.get_historical_earnings(
        ["INTC"], dt.date(2020, 1, 1), dt.date(2020, 12, 31)
    )
    assert result["INTC"] == []


def test_earnings_keyed_by_requested_symbol_not_inner_ticker_field(monkeypatch, tmp_path, stub):
    """CONFIRMED schema fact from the TECK.B fixture: the earnings blocks'
    inner ``ticker`` field is "TECK" (the US cross-listing), not the
    requested "TSE:TECK.B" -- never used for identity. Every event returned
    for our canonical "TECK-B" symbol must be keyed by that symbol."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    upcoming = provider.get_upcoming_earnings(["TECK-B"], through=dt.date(2026, 7, 30))
    historical = provider.get_historical_earnings(
        ["TECK-B"], dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )

    assert "TECK-B" in upcoming
    assert upcoming["TECK-B"][0].symbol == "TECK-B"
    assert "TECK-B" in historical
    assert historical["TECK-B"][0].symbol == "TECK-B"
    # The raw payload's inner ticker really is the US-listing form, proving
    # this isn't a vacuous assertion.
    holding = TECK_B_PAYLOAD["overview"]["portfolioHoldingData"]
    assert holding["lastReportedEps"]["ticker"] == "TECK"
    assert holding["nextEarningsReport"]["ticker"] == "TECK"


def test_timeofday_2_is_accepted_without_failing_the_parse(monkeypatch, tmp_path, stub):
    """CONFIRMED schema fact: TECK.B's lastReportedEps.timeOfDay is 2 (a
    value not seen in the INTC fixture, where only 1 and 4 appear). An
    unrecognised/newly-observed code must never raise."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    events = provider.get_historical_earnings(
        ["TECK-B"], dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )["TECK-B"]
    assert len(events) == 1
    assert events[0].session is EarningsSession.DURING  # provisional mapping for 2


def test_unrecognised_timeofday_value_maps_to_unknown_without_raising(monkeypatch, tmp_path, stub):
    payload = json.loads(json.dumps(INTC_PAYLOAD))  # deep copy
    payload["overview"]["portfolioHoldingData"]["lastReportedEps"]["timeOfDay"] = 99
    stub.payload_by_ticker["INTC"] = payload
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    events = provider.get_historical_earnings(
        ["INTC"], dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )["INTC"]
    assert events[0].session is EarningsSession.UNKNOWN


# --------------------------------------------------------------------------
# market caps
# --------------------------------------------------------------------------


def test_get_market_caps_prefers_market_cap_usd(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["INTC"])
    assert caps["INTC"] == pytest.approx(435297215393.0)


def test_get_market_caps_for_tsx_listing_clears_the_billion_dollar_floor(monkeypatch, tmp_path, stub):
    """CONFIRMED schema fact: TECK.B's marketCap == marketCapUSD ==
    28,913,081,465 (both already USD for this listing). Currency-agnostic
    rule: no gating on stockCurrencyTypeID/currencyTypeID -- compare directly
    against the $1B floor."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["TECK-B"])
    assert caps["TECK-B"] == pytest.approx(28_913_081_465.0)
    assert caps["TECK-B"] >= 1_000_000_000.0


def test_get_market_caps_ignores_nested_currency_mismatched_blocks(monkeypatch, tmp_path, stub):
    """The TECK.B fixture's ``portfolioHoldingData.nextDividendDate.marketCap``
    is a DIFFERENT, CAD-only figure (40,620,412,377) from the top-level cap --
    it must never be used as a cap source."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["TECK-B"])
    assert caps["TECK-B"] != pytest.approx(40_620_412_377.0)


def test_get_market_caps_omits_unknown_ticker(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["INTC", "NOSUCH"])
    assert "NOSUCH" not in caps
    assert "NOSUCH" in provider._not_found


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------


def test_get_security_info_maps_us_listing(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    info = provider.get_security_info(["INTC"])["INTC"]
    assert info.name == "Intel"
    assert info.exchange == "NASDAQ"
    assert info.sector == "Technology"
    assert info.industry == "Semiconductors"
    assert info.market_cap_usd == pytest.approx(435297215393.0)


def test_get_security_info_maps_tsx_listing_case_insensitively(monkeypatch, tmp_path, stub):
    """CONFIRMED schema fact: TECK.B's ``overview.market`` is lower-case
    ``"tsx"`` (vs INTC's upper-case ``"NASDAQ"``) -- the mapping must not be
    case-sensitive."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    info = provider.get_security_info(["TECK-B"])["TECK-B"]
    assert info.exchange == "TSX"
    assert info.sector == "Basic Materials"
    assert info.industry == "Industrial Materials"


def test_get_security_info_survives_next_dividend_date_and_related_listings(monkeypatch, tmp_path, stub):
    """Blocks unique to the Canadian fixture -- ``nextDividendDate``,
    ``primaryStock``, ``relatedListings`` (cross-listing linkage to NYSE
    "TECK" and Frankfurt "DE:TEKB") -- must never crash reference-data
    parsing; this adapter simply never reads them."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    info = provider.get_security_info(["TECK-B"])["TECK-B"]
    assert info.name == "Teck Resources"


def test_get_security_info_unknown_ticker_falls_back_to_packaged_seed(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    info = provider.get_security_info(["NOSUCH"])["NOSUCH"]
    assert info.symbol == "NOSUCH"


# --------------------------------------------------------------------------
# daily bars -- close-only, last resort
# --------------------------------------------------------------------------


def test_bars_last_resort_flag_is_set():
    assert TipRanksProvider.bars_last_resort is True


def test_get_daily_bars_are_close_only(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    bars = provider.get_daily_bars(["INTC"], dt.date(2025, 7, 1), dt.date(2026, 8, 1))["INTC"]
    assert bars, "fixture prices must produce bars"
    for bar in bars:
        assert bar.open == bar.high == bar.low == bar.close
        assert bar.volume == 0.0
        assert bar.source == "tipranks"
    assert [b.session for b in bars] == sorted(b.session for b in bars)


def test_get_daily_bars_restricts_to_requested_range(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    bars = provider.get_daily_bars(["INTC"], dt.date(2026, 7, 1), dt.date(2026, 7, 31))["INTC"]
    assert all(dt.date(2026, 7, 1) <= b.session <= dt.date(2026, 7, 31) for b in bars)


def test_get_daily_bars_records_a_data_quality_warning(monkeypatch, tmp_path, stub, caplog):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with caplog.at_level("WARNING"):
        provider.get_daily_bars(["INTC"], dt.date(2025, 7, 1), dt.date(2026, 8, 1))

    warnings = provider.drain_quality_warnings()
    assert len(warnings) == 1
    assert warnings[0].symbol == "INTC"
    assert warnings[0].category == "close_only_bars"
    assert "close-only bars from tipranks" in warnings[0].message
    assert any("close-only bars from tipranks" in r.message for r in caplog.records)
    # Draining clears the queue.
    assert provider.drain_quality_warnings() == []


def test_get_daily_bars_unknown_symbol_degrades_per_symbol(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    result = provider.get_daily_bars(
        ["INTC", "NOSUCH"], dt.date(2025, 7, 1), dt.date(2026, 8, 1)
    )
    assert result["INTC"]
    assert result["NOSUCH"] == []
    assert "NOSUCH" in provider._not_found


def test_get_intraday_bars_not_implemented(tmp_path):
    provider = _provider(tmp_path)
    with pytest.raises(ProviderError):
        provider.get_intraday_bars(
            ["INTC"], dt.datetime(2026, 1, 2, tzinfo=dt.UTC), dt.datetime(2026, 1, 3, tzinfo=dt.UTC)
        )


# --------------------------------------------------------------------------
# fail-closed behaviour (ADR-0008 Decision 1)
# --------------------------------------------------------------------------


def test_401_raises_source_blocked_error(monkeypatch, tmp_path, stub):
    stub.status_override = 401
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(SourceBlockedError):
        provider.get_market_caps(["INTC"])


def test_403_raises_source_blocked_error(monkeypatch, tmp_path, stub):
    stub.status_override = 403
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(SourceBlockedError):
        provider.get_security_info(["INTC"])


def test_429_raises_rate_limit_error(monkeypatch, tmp_path, stub):
    stub.status_override = 429
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(RateLimitError):
        provider.get_upcoming_earnings(["INTC"])


def test_5xx_raises_retryable_provider_error_not_source_blocked(monkeypatch, tmp_path, stub):
    """A server-side outage (5xx) is a retryable ``ProviderError``, distinct
    from a genuine block -- it must NOT raise ``SourceBlockedError`` (which
    would disable the source for the rest of the cycle over a transient
    outage)."""
    stub.status_override = 503
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_market_caps(["INTC"])
    assert not isinstance(excinfo.value, SourceBlockedError)
    assert excinfo.value.retryable is True


def test_html_block_page_raises_source_blocked_error(monkeypatch, tmp_path, stub):
    """A non-JSON content-type (e.g. an HTML block/challenge page served with
    a 200) is caught before JSON parsing is even attempted."""
    stub.body_override = "<html>not json</html>"
    stub.content_type_override = "text/html"
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(SourceBlockedError):
        provider.get_market_caps(["INTC"])


def test_malformed_json_body_raises_source_blocked_error(monkeypatch, tmp_path, stub):
    """A ``json``-labelled content-type whose body does not actually parse
    must also fail closed, via the ``response.json()`` decode failure."""
    stub.body_override = "{not actually valid json"
    stub.content_type_override = "application/json"
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(SourceBlockedError):
        provider.get_market_caps(["INTC"])


def test_missing_overview_key_raises_source_blocked_error(monkeypatch, tmp_path, stub):
    """A response missing the ``overview`` key entirely is an unexpected
    shape -> block. Distinct from an ``overview`` key present but null/empty
    (an ordinary unknown-ticker outcome, see below)."""
    stub.body_override = json.dumps({"somethingElse": True})
    stub.content_type_override = "application/json"
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    with pytest.raises(SourceBlockedError):
        provider.get_market_caps(["INTC"])


def test_null_overview_degrades_only_that_symbol(monkeypatch, tmp_path, stub):
    """An unknown ticker -- ``overview`` key present but null on an
    otherwise-2xx response -- degrades only that symbol; it must NOT raise."""
    stub.null_overview_tickers.add("NEVERHEARDOFIT")
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["NEVERHEARDOFIT"])
    assert caps == {}
    assert "NEVERHEARDOFIT" in provider._not_found


def test_404_degrades_only_that_symbol_not_treated_as_outage(monkeypatch, tmp_path, stub):
    """CONFIRMED by a real probe: a garbage ticker gets a clean HTTP 404, not
    an error page or a null-overview 200. This must degrade only that one
    symbol -- never raise, never spend a retry, never disable the source."""
    _install(monkeypatch, stub)  # "NOSUCH" isn't in payload_by_ticker -> 404
    provider = _provider(tmp_path)
    caps = provider.get_market_caps(["INTC", "NOSUCH"])
    assert caps["INTC"] == pytest.approx(435297215393.0)
    assert "NOSUCH" not in caps
    assert "NOSUCH" in provider._not_found


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------


def test_second_call_within_ttl_is_served_from_cache(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    provider.get_market_caps(["INTC"])
    calls_after_first = len(stub.requests)
    provider.get_market_caps(["INTC"])
    assert len(stub.requests) == calls_after_first, "second call must be served from cache"


def test_cache_persists_across_provider_instances(monkeypatch, tmp_path, stub):
    """The cache is a file under ``cache_dir``, not an in-memory instance
    attribute -- a fresh provider instance (e.g. the next scheduled refresh
    process) must still see the cached response."""
    _install(monkeypatch, stub)
    _provider(tmp_path).get_market_caps(["INTC"])
    calls_after_first = len(stub.requests)

    second = _provider(tmp_path)
    second.get_market_caps(["INTC"])
    assert len(stub.requests) == calls_after_first


def test_cache_expires_after_configured_trading_days(monkeypatch, tmp_path, stub):
    """Backdating the cached ``fetched_date`` past the TTL must force a
    fresh fetch on the next call."""
    _install(monkeypatch, stub)
    config = TipRanksConfig(cache_ttl_trading_days=1)
    provider = _provider(tmp_path, config=config)
    provider.get_market_caps(["INTC"])
    calls_after_first = len(stub.requests)

    cache_file = tmp_path / "cache" / "tipranks" / "INTC.json"
    assert cache_file.exists()
    record = json.loads(cache_file.read_text(encoding="utf-8"))
    record["fetched_date"] = "2020-01-01"  # long enough ago to have elapsed >= 1 trading day
    cache_file.write_text(json.dumps(record), encoding="utf-8")

    provider.get_market_caps(["INTC"])
    assert len(stub.requests) > calls_after_first, "expired cache entry must be refetched"


def test_unknown_ticker_result_is_also_cached(monkeypatch, tmp_path, stub):
    """A cached "unknown ticker" (empty overview) must not be re-fetched
    every call either -- that would defeat the whole point of the cache for
    a universe that always contains a few unresolvable names."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    provider.get_market_caps(["NEVERHEARDOFIT"])
    calls_after_first = len(stub.requests)
    provider.get_market_caps(["NEVERHEARDOFIT"])
    assert len(stub.requests) == calls_after_first


# --------------------------------------------------------------------------
# historicalprices fallback -- "prices_only" state (item 4)
# --------------------------------------------------------------------------


def test_dataforticker_success_never_calls_historicalprices(monkeypatch, tmp_path, stub):
    """A dataForTicker SUCCESS must never trigger the historicalprices probe
    at all -- it is a fallback for the unknown-ticker case only."""
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)
    provider.get_market_caps(["INTC"])
    assert all("historicalprices" not in str(r.url) for r in stub.requests)


def test_404_falls_back_to_historicalprices_and_caches_prices_only(monkeypatch, tmp_path, stub):
    """dataForTicker 404s for MHD (a closed-end fund with no analyst
    coverage), but historicalprices has real rows -- the symbol EXISTS and
    is cached as the distinct 'prices_only' state, not plain 'unknown'."""
    stub.historicalprices_by_ticker["MHD"] = MHD_HISTORICALPRICES
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    caps = provider.get_market_caps(["MHD"])
    assert "MHD" not in caps  # no analyst overview -> no cap, same as unknown
    assert "MHD" in provider._not_found

    resolution = provider._resolve("MHD")
    assert resolution.state == "prices_only"

    cache_file = tmp_path / "cache" / "tipranks" / "MHD.json"
    assert cache_file.exists()
    record = json.loads(cache_file.read_text(encoding="utf-8"))
    assert record["state"] == "prices_only"
    assert record["historical_prices"]


def test_404_with_no_historicalprices_data_either_caches_unknown(monkeypatch, tmp_path, stub):
    """A genuinely delisted/renamed symbol 404s on both endpoints -- cached
    as plain 'unknown', the normal outcome for the vast majority of unknown
    tickers (ANSS, JNPR, FLT, SQ, K, WBA, HES, DFS, PARA, ... per the
    owner's log)."""
    _install(monkeypatch, stub)  # "DELISTED" registered nowhere -> 404 both endpoints
    provider = _provider(tmp_path)

    resolution = provider._resolve("DELISTED")
    assert resolution.state == "unknown"

    cache_file = tmp_path / "cache" / "tipranks" / "DELISTED.json"
    record = json.loads(cache_file.read_text(encoding="utf-8"))
    assert record["state"] == "unknown"


def test_prices_only_cache_uses_the_unknown_ticker_ttl(monkeypatch, tmp_path, stub):
    """A cached 'prices_only' result must not be re-probed on every refresh
    either -- it shares the long ``unknown_ticker_ttl_days`` TTL, not the
    short ``cache_ttl_trading_days`` one."""
    stub.historicalprices_by_ticker["MHD"] = MHD_HISTORICALPRICES
    _install(monkeypatch, stub)
    config = TipRanksConfig(cache_ttl_trading_days=1, unknown_ticker_ttl_days=30)
    provider = _provider(tmp_path, config=config)

    provider.get_market_caps(["MHD"])
    calls_after_first = len(stub.requests)
    provider.get_market_caps(["MHD"])
    assert len(stub.requests) == calls_after_first, "prices_only must be served from cache"


def test_get_daily_bars_prefers_historicalprices_over_close_only_for_prices_only(
    monkeypatch, tmp_path, stub
):
    """A prices_only symbol's bars come from historicalprices (real OHLCV),
    never from close-only synthesis (there is no ``overview.prices`` to
    synthesise from at all for this state)."""
    stub.historicalprices_by_ticker["MHD"] = MHD_HISTORICALPRICES
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    bars = provider.get_daily_bars(["MHD"], dt.date(2023, 1, 1), dt.date(2026, 12, 31))["MHD"]
    assert bars, "fixture rows within range must produce bars"
    # Real OHLCV, not the close-only degrade: open/high/low are not all equal
    # to close for at least one row, and volume is nonzero.
    assert any(b.volume > 0 for b in bars)
    assert any(b.open != b.close or b.high != b.close or b.low != b.close for b in bars)


def test_historicalprices_drops_zero_volume_holiday_padding_rows(monkeypatch, tmp_path, stub):
    """Jan-1 rows in the fixture carry volume 0 -- holiday padding, not real
    trading sessions -- and must be dropped."""
    rows = TipRanksProvider._parse_historicalprices_rows(MHD_HISTORICALPRICES)
    assert dt.date(2024, 1, 1) not in {r["session"] for r in rows}
    assert dt.date(2025, 1, 1) not in {r["session"] for r in rows}
    assert dt.date(2026, 1, 1) not in {r["session"] for r in rows}
    # A real (nonzero-volume) row from the same fixture survives.
    assert dt.date(2023, 11, 6) in {r["session"] for r in rows}


def test_historicalprices_maps_price_to_adj_close_and_close_to_close(monkeypatch, tmp_path, stub):
    rows = {
        r["session"]: r for r in TipRanksProvider._parse_historicalprices_rows(MHD_HISTORICALPRICES)
    }
    row = rows[dt.date(2023, 11, 6)]
    assert row["close"] == pytest.approx(10.51)
    assert row["adj_close"] == pytest.approx(8.97)  # "price" in the raw fixture


def test_historicalprices_cadence_guard_flags_downsampled_series(monkeypatch, tmp_path, stub):
    """The fixture's real cadence is biweekly (~14 calendar days), well
    above the 4-day median-gap threshold -- must be flagged, not served
    silently as ordinary daily bars."""
    stub.historicalprices_by_ticker["MHD"] = MHD_HISTORICALPRICES
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    bars = provider.get_daily_bars(["MHD"], dt.date(2023, 1, 1), dt.date(2026, 12, 31))["MHD"]
    assert bars  # rows are still returned, un-interpolated
    warnings = provider.drain_quality_warnings()
    sparse = [w for w in warnings if w.category == "sparse_bars"]
    assert len(sparse) == 1
    assert sparse[0].symbol == "MHD"
    assert "downsampled" in sparse[0].message


def test_is_sparse_false_for_fewer_than_two_rows():
    assert TipRanksProvider._is_sparse([]) is False
    assert TipRanksProvider._is_sparse([{"session": dt.date(2024, 1, 1)}]) is False


def test_is_sparse_false_for_dense_daily_rows():
    rows = [{"session": dt.date(2024, 1, 1) + dt.timedelta(days=i)} for i in range(10)]
    assert TipRanksProvider._is_sparse(rows) is False


# --------------------------------------------------------------------------
# parallel fetch + progress logging (items 2 and 6)
# --------------------------------------------------------------------------


def test_resolve_map_logs_progress_at_the_configured_cadence(monkeypatch, tmp_path, stub, caplog):
    """"market data: N/TOTAL symbols (X fetched, Y cached, Z unknown)" every
    _PROGRESS_LOG_EVERY_N symbols, so a long refresh never looks hung."""
    monkeypatch.setattr("claudetrade.providers.market.tipranks._PROGRESS_LOG_EVERY_N", 2)
    for i in range(5):
        stub.payload_by_ticker[f"SYM{i}"] = INTC_PAYLOAD
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    with caplog.at_level("INFO"):
        provider.get_market_caps([f"SYM{i}" for i in range(5)])

    progress_lines = [r.message for r in caplog.records if r.message.startswith("market data:")]
    assert any("2/5 symbols" in line for line in progress_lines)
    assert any("5/5 symbols" in line for line in progress_lines)  # final line always fires
    assert any("fetched" in line and "cached" in line and "unknown" in line for line in progress_lines)


def test_resolve_map_uses_worker_threads_up_to_max_workers(monkeypatch, tmp_path, stub):
    """A higher max_workers must not change the resolved results -- only how
    the per-symbol fetch loop is scheduled."""
    for i in range(6):
        stub.payload_by_ticker[f"SYM{i}"] = INTC_PAYLOAD
    _install(monkeypatch, stub)
    config = TipRanksConfig(rate_limit_per_minute=6000)  # avoid rate-limiter stalls in the test
    provider = TipRanksProvider(config, cache_dir=tmp_path / "cache", max_workers=4)

    caps = provider.get_market_caps([f"SYM{i}" for i in range(6)])
    assert len(caps) == 6
    assert all(v == pytest.approx(435297215393.0) for v in caps.values())


def test_get_daily_bars_of_a_mixed_batch_resolves_each_symbol_independently(
    monkeypatch, tmp_path, stub
):
    """found + prices_only + unknown symbols in the same parallel batch each
    resolve to their own correct outcome."""
    stub.historicalprices_by_ticker["MHD"] = MHD_HISTORICALPRICES
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)

    result = provider.get_daily_bars(
        ["INTC", "MHD", "NOSUCH"], dt.date(2023, 1, 1), dt.date(2026, 12, 31)
    )
    assert result["INTC"]  # found -> close-only
    assert all(b.volume == 0.0 for b in result["INTC"])
    assert result["MHD"]  # prices_only -> real OHLCV
    assert any(b.volume > 0 for b in result["MHD"])
    assert result["NOSUCH"] == []  # unknown -> empty


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_status_declares_capabilities(tmp_path):
    status = _provider(tmp_path).status()
    assert status.capabilities["earnings"] is True
    assert status.capabilities["market_caps"] is True
    assert status.capabilities["bars_last_resort"] is True
    assert status.capabilities["getquotes_batch_enabled"] is False
    assert status.licence_note, "ToS posture must be stated"
    assert "fail" in status.licence_note.lower()


# --------------------------------------------------------------------------
# GetQuotes -- optional, UNVERIFIED batching path
# --------------------------------------------------------------------------


def test_getquotes_disabled_by_default_never_calls_marketsv3(monkeypatch, tmp_path, stub):
    _install(monkeypatch, stub)
    provider = _provider(tmp_path)  # use_getquotes_batch defaults to False
    provider.get_market_caps(["TECK-B", "CCL-B"])
    assert all("marketsv3" not in str(r.url) for r in stub.requests)


def test_parse_getquotes_response_is_defensive_to_unexpected_shapes():
    """No real GetQuotes response body was ever obtained (see the module
    docstring) -- the parser must degrade to an empty result for any shape
    that doesn't match its best-guess structure, never raise."""
    assert _parse_getquotes_response(None, ["TSE:TECK.B"]) == {}
    assert _parse_getquotes_response({}, ["TSE:TECK.B"]) == {}
    assert _parse_getquotes_response({"quotes": "not a list"}, ["TSE:TECK.B"]) == {}
    assert _parse_getquotes_response({"quotes": [{"ticker": None}]}, ["TSE:TECK.B"]) == {}
    assert _parse_getquotes_response({"quotes": [123, "bad"]}, ["TSE:TECK.B"]) == {}


def test_parse_getquotes_response_prefers_market_cap_usd():
    payload = {
        "quotes": [
            {"ticker": "TSE:TECK.B", "marketCapUSD": 28_913_081_465.0},
        ]
    }
    caps = _parse_getquotes_response(payload, ["TSE:TECK.B"])
    assert caps == {"TSE:TECK.B": pytest.approx(28_913_081_465.0)}


def test_parse_getquotes_response_converts_local_currency_via_exchange_rate():
    """CONFIRMED currency trap from the real probe: TSE:TECK.B's GetQuotes
    ``marketCap`` (40,620,412,377) is CAD, not USD; multiplying by its
    ``exchangeRate`` (~0.712) recovers the USD figure ``dataForTicker``
    reports directly (28,913,081,465) -- the raw local-currency figure must
    never be returned as-is."""
    payload = {
        "quotes": [
            {"ticker": "TSE:TECK.B", "marketCap": 40_620_412_377.0, "exchangeRate": 0.712},
        ]
    }
    caps = _parse_getquotes_response(payload, ["TSE:TECK.B"])
    assert caps["TSE:TECK.B"] == pytest.approx(28_921_733_612.424, rel=1e-3)


def test_parse_getquotes_response_omits_local_currency_cap_with_no_exchange_rate():
    """Without an ``exchangeRate`` to convert with, a non-USD ``marketCap``
    must never be treated as if it were already USD."""
    payload = {"quotes": [{"ticker": "TSE:CCL.B", "marketCap": 9_000_000_000.0}]}
    caps = _parse_getquotes_response(payload, ["TSE:CCL.B"])
    assert "TSE:CCL.B" not in caps


def test_parse_getquotes_response_us_entry_with_exchange_rate_one():
    """A US entry's ``exchangeRate`` is 1 per the real probe -- local-currency
    conversion is a no-op for it."""
    payload = {"quotes": [{"ticker": "AAPL", "marketCap": 2_800_000_000_000.0, "exchangeRate": 1}]}
    caps = _parse_getquotes_response(payload, ["AAPL"])
    assert caps["AAPL"] == pytest.approx(2_800_000_000_000.0)


def test_getquotes_batch_optimisation_used_when_enabled(monkeypatch, tmp_path):
    """When explicitly opted in, the batch call is attempted first and its
    resolved symbols skip the per-symbol dataForTicker fetch entirely."""
    getquotes_calls: list[httpx.Request] = []
    dataforticker_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "GetQuotes" in request.url.path:
            getquotes_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "quotes": [
                        {"ticker": "TSE:TECK.B", "marketCapUSD": 28_913_081_465.0},
                        {"ticker": "TSE:CCL.B", "marketCapUSD": 9_000_000_000.0},
                    ]
                },
            )
        dataforticker_calls.append(request)
        return httpx.Response(200, json={"overview": None})

    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.tipranks.httpx.Client", _factory)

    config = TipRanksConfig(use_getquotes_batch=True)
    provider = _provider(tmp_path, config=config)
    caps = provider.get_market_caps(["TECK-B", "CCL-B"])

    assert getquotes_calls, "GetQuotes must have been attempted"
    assert caps["TECK-B"] == pytest.approx(28_913_081_465.0)
    assert caps["CCL-B"] == pytest.approx(9_000_000_000.0)
    # Both resolved by the batch call -- no per-symbol dataForTicker fallback needed.
    assert not dataforticker_calls


def test_getquotes_failure_falls_back_to_dataforticker(monkeypatch, tmp_path, stub):
    """A GetQuotes failure (bad shape, network error, anything) must never
    take down market-cap enrichment -- it falls straight back to the
    per-symbol dataForTicker path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "GetQuotes" in request.url.path:
            raise httpx.ConnectError("connection refused")
        return stub.handler(request)

    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("claudetrade.providers.market.tipranks.httpx.Client", _factory)

    config = TipRanksConfig(use_getquotes_batch=True)
    provider = _provider(tmp_path, config=config)
    caps = provider.get_market_caps(["TECK-B", "SHOP"])  # >1 CA symbol triggers the batch attempt
    assert caps["TECK-B"] == pytest.approx(28_913_081_465.0)
