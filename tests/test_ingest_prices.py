"""``DataIngestor.ingest_prices`` -- the daily-bars fetch/persist path.

Covers the benchmark-bars fix: a real refresh log showed "benchmark SPY
unavailable; regime reported as UNKNOWN for all sessions" because SPY (an
ETF, not a universe member) was never included in the bar-fetch loop.
``ingest_prices`` must always fetch/store bars for
``config.market_data.benchmark_symbol``, even when the caller's ``symbols``
list omits it entirely -- and, since a later refresh log showed the
merged-batch inclusion alone is not enough (one unrelated symbol's batch
failure can still take the benchmark down with it), a dedicated,
independent single-symbol fetch is the actual guarantee.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import PriceBar, Security
from claudetrade.domain import Bar
from claudetrade.providers.base import ProviderError


class _FakeBarsProvider:
    """Returns one bar per requested symbol, whatever was asked for --
    records exactly which symbols each call requested so tests can assert on
    it directly, independent of what ends up persisted."""

    name = "fake_bars_provider"

    def __init__(self):
        self.requested_batches: list[list[str]] = []

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        self.requested_batches.append(list(symbols))
        return {
            symbol: [
                Bar(
                    symbol=symbol,
                    session=start,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1_000_000,
                    adj_close=100.5,
                    source=self.name,
                )
            ]
            for symbol in symbols
        }


def test_ingest_prices_always_includes_the_benchmark(memory_db, make_bar):
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    provider = _FakeBarsProvider()
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    # The caller's universe deliberately excludes the benchmark (an ETF is
    # not a universe member in the packaged seed lists).
    ingestor.ingest_prices(["AAPL", "MSFT"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    requested = {s for batch in provider.requested_batches for s in batch}
    assert "SPY" in requested

    with memory_db.read_session() as session:
        rows = session.execute(select(PriceBar).where(PriceBar.symbol == "SPY")).scalars().all()
    assert rows, "SPY bars must be persisted even though it was not in the requested universe"


def test_ingest_prices_does_not_duplicate_an_already_present_benchmark(memory_db):
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    provider = _FakeBarsProvider()
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    ingestor.ingest_prices(["AAPL", "SPY"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    # Deduped -- SPY appears exactly once per fetch batch, not twice.
    for batch in provider.requested_batches:
        assert batch.count("SPY") == 1


class _FailsWhenBatchedProvider:
    """Raises ``ProviderError`` for any multi-symbol request -- simulating
    one unrelated symbol's failure taking down the whole batch it shares
    with the benchmark -- but succeeds for a genuinely single-symbol call.
    This is exactly the shape the dedicated benchmark fetch is meant to
    survive that the old merged-only inclusion could not."""

    name = "fails_when_batched"

    def __init__(self):
        self.requested_batches: list[list[str]] = []

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        self.requested_batches.append(list(symbols))
        if len(symbols) > 1:
            raise ProviderError(
                "simulated unrelated-symbol batch failure",
                provider=self.name,
                retryable=True,
            )
        return {
            symbol: [
                Bar(
                    symbol=symbol,
                    session=start,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1_000_000,
                    adj_close=100.5,
                    source=self.name,
                )
            ]
            for symbol in symbols
        }


def test_ingest_prices_dedicated_benchmark_fetch_survives_a_batch_wide_failure(memory_db):
    """Benchmark bars must be stored even when every other symbol's fetch
    raises: the merged-batch inclusion alone ties SPY's fate to whatever
    else shares its batch, but the dedicated, independent single-symbol
    call is the actual guarantee."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    provider = _FailsWhenBatchedProvider()
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    ingestor.ingest_prices(["AAPL", "MSFT"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    # The merged batch (AAPL, MSFT, SPY) was attempted and failed outright.
    assert any(len(batch) > 1 for batch in provider.requested_batches)
    # The dedicated, single-symbol call for SPY was made and succeeded.
    assert ["SPY"] in provider.requested_batches

    with memory_db.read_session() as session:
        spy_rows = session.execute(select(PriceBar).where(PriceBar.symbol == "SPY")).scalars().all()
        other_rows = session.execute(
            select(PriceBar).where(PriceBar.symbol == "AAPL")
        ).scalars().all()
    assert spy_rows, "SPY bars must be persisted via the dedicated call despite the batch failure"
    assert other_rows == [], "AAPL never got real bars -- its shared batch failed outright"


def test_ingest_prices_logs_error_when_benchmark_has_no_bars_anywhere(memory_db, caplog):
    """When no configured source has anything for the benchmark, even after
    the dedicated attempt, this is logged as an ERROR (not a warning) --
    regime classification is entirely dependent on it."""

    class _NoBarsProvider:
        name = "no_bars_provider"

        def get_daily_bars(self, symbols, start, end, *, adjusted=True):
            return {symbol: [] for symbol in symbols}

    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    provider = _NoBarsProvider()
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    with caplog.at_level(logging.ERROR, logger="claudetrade.data.ingest"):
        ingestor.ingest_prices(["AAPL"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("UNKNOWN" in r.getMessage() and "SPY" in r.getMessage() for r in error_records)


class _FakeCapProviderWithProgressHook:
    """get_market_caps provider that honours the duck-typed
    ``on_symbol_progress`` contract the way ``TipRanksProvider`` does:
    one hook call per symbol resolved."""

    name = "fake_cap_provider"

    def __init__(self):
        self.on_symbol_progress = None

    def get_market_caps(self, symbols):
        caps = {}
        for i, symbol in enumerate(symbols, start=1):
            caps[symbol] = 5e9
            if self.on_symbol_progress is not None:
                self.on_symbol_progress(i, len(symbols))
        return caps


def test_securities_phase_reports_per_symbol_progress(memory_db):
    """The securities phase must report intermediate progress, not just its
    0% and 100% endpoints -- a live run showed the UI banner stuck at
    ``0/2417 (0%)`` for the entire ~40-minute cap-enrichment pass while the
    provider's console log counted up normally."""
    from claudetrade.domain import SecurityInfo

    config = AppConfig()
    provider = _FakeCapProviderWithProgressHook()
    events: list[tuple[str, int, int]] = []
    ingestor = DataIngestor(
        config,
        memory_db,
        market_provider=provider,
        progress_callback=lambda phase, done, total: events.append((phase, done, total)),
    )
    securities = [SecurityInfo(symbol=f"SYM{i}", name=f"Sym {i} Inc") for i in range(5)]

    ingestor.ingest_securities(securities, IngestReport())

    securities_events = [e for e in events if e[0] == "securities"]
    dones = [done for _, done, _ in securities_events]
    # Intermediate values between the 0% start and 100% end, one per symbol.
    assert dones[0] == 0
    assert dones[-1] == 5
    assert {1, 2, 3, 4} <= set(dones)
    # The hook must be unhooked once the phase is over.
    assert provider.on_symbol_progress is None


# ----------------------------------------------------------------------------
# Delisted cleanup: symbols confirmed unknown by BOTH tipranks and yahoo, in
# the same refresh, with no recently-stored bars.
# ----------------------------------------------------------------------------


class _FakeTipRanksLike:
    """Duck-types the one piece of ``TipRanksProvider`` this feature reads:
    ``.name`` and the per-refresh ``_not_found`` set."""

    name = "tipranks"

    def __init__(self, not_found=None):
        self._not_found = set(not_found or [])

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        return {s: [] for s in symbols}


class _FakeYahooLike:
    """Duck-types the one piece of ``YahooMarketProvider`` this feature
    reads: ``.name`` and the per-refresh ``_not_found`` set."""

    name = "yahoo"

    def __init__(self, not_found=None):
        self._not_found = set(not_found or [])

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        return {s: [] for s in symbols}


class _CascadeLike:
    """Minimal ``FallbackMarketProvider``-shaped wrapper (``.primary`` /
    ``.fallbacks``): tries primary, then each fallback, filling any symbol
    either has real bars for. Close enough to the real cascade for these
    tests without importing ``providers.registry``, matching the duck-typed
    convention ``DataIngestor._market_cap_sources``/``_market_provider_named``
    already rely on."""

    name = "fallback"

    def __init__(self, primary, fallbacks):
        self.primary = primary
        self.fallbacks = fallbacks

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        out: dict[str, list] = {}
        unfilled = set(symbols)
        for provider in [self.primary, *self.fallbacks]:
            if not unfilled:
                break
            result = provider.get_daily_bars(list(unfilled), start, end, adjusted=adjusted)
            for symbol, bars in result.items():
                if bars and symbol not in out:
                    out[symbol] = bars
                    unfilled.discard(symbol)
        for symbol in symbols:
            out.setdefault(symbol, [])
        return out


def _insert_security(db, symbol, *, delisted_date=None):
    with db.session() as session:
        session.add(Security(symbol=symbol, name=symbol, delisted_date=delisted_date))


def _insert_bar(db, symbol, session_date):
    with db.session() as session:
        session.add(
            PriceBar(
                symbol=symbol,
                session=session_date,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                adj_close=1.0,
                volume=0.0,
                source="test",
            )
        )


def test_both_sources_unknown_and_no_recent_bars_deactivates(memory_db):
    """The core deactivation rule: both providers agree the ticker is
    unknown in the same refresh, and there is no recent price history --
    the security is marked inactive and the change is recorded as a visible
    data-quality finding."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    _insert_security(memory_db, "WBA")

    wrapper = _CascadeLike(
        primary=_FakeTipRanksLike(not_found={"WBA"}),
        fallbacks=[_FakeYahooLike(not_found={"WBA"})],
    )
    ingestor = DataIngestor(config, memory_db, market_provider=wrapper)
    report = IngestReport()

    ingestor.ingest_prices(["WBA"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    today = dt.datetime.now(tz=dt.UTC).date()
    with memory_db.read_session() as session:
        row = session.get(Security, "WBA")
    assert row is not None
    assert row.delisted_date == today

    findings = [
        i for i in report.quality.issues
        if i.symbol == "WBA" and i.category == "symbol_deactivated"
    ]
    assert findings, "the deactivation must be recorded as a visible data-quality finding"


def test_only_one_source_unknown_leaves_security_untouched(memory_db):
    """A single provider disagreeing must never be enough -- both must
    agree in the same refresh."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    _insert_security(memory_db, "PARTIAL")

    wrapper = _CascadeLike(
        primary=_FakeTipRanksLike(not_found={"PARTIAL"}),
        fallbacks=[_FakeYahooLike(not_found=set())],  # yahoo has no opinion
    )
    ingestor = DataIngestor(config, memory_db, market_provider=wrapper)
    report = IngestReport()

    ingestor.ingest_prices(["PARTIAL"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    with memory_db.read_session() as session:
        row = session.get(Security, "PARTIAL")
    assert row is not None
    assert row.delisted_date is None
    assert not any(i.category == "symbol_deactivated" for i in report.quality.issues)


def test_recent_bars_symbol_is_not_deactivated_despite_both_sources_unknown(memory_db):
    """A provider hiccup (both sources briefly unknown) must never
    deactivate a name that was trading as recently as last month --
    conservatism is the whole point of the 30-day recency guard."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    _insert_security(memory_db, "RECENT")
    today = dt.datetime.now(tz=dt.UTC).date()
    _insert_bar(memory_db, "RECENT", today - dt.timedelta(days=5))

    wrapper = _CascadeLike(
        primary=_FakeTipRanksLike(not_found={"RECENT"}),
        fallbacks=[_FakeYahooLike(not_found={"RECENT"})],
    )
    ingestor = DataIngestor(config, memory_db, market_provider=wrapper)
    report = IngestReport()

    ingestor.ingest_prices(["RECENT"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report)

    with memory_db.read_session() as session:
        row = session.get(Security, "RECENT")
    assert row is not None
    assert row.delisted_date is None
    assert not any(i.category == "symbol_deactivated" for i in report.quality.issues)


def test_ingest_securities_never_clobbers_an_existing_deactivation(memory_db):
    """A subsequent securities-reference-data pass with no opinion on
    delisting (``SecurityInfo.delisted_date is None``, the common case for
    every provider except an explicit packaged CSV entry) must not silently
    clear a deactivation this application made itself -- that would undo
    the cleanup before the very next refresh's price-fetch phase even ran."""
    from claudetrade.domain import SecurityInfo

    config = AppConfig()
    today = dt.datetime.now(tz=dt.UTC).date()
    _insert_security(memory_db, "WBA", delisted_date=today)

    ingestor = DataIngestor(config, memory_db, market_provider=None)
    report = IngestReport()

    ingestor.ingest_securities([SecurityInfo(symbol="WBA", name="Walgreens")], report)

    with memory_db.read_session() as session:
        row = session.get(Security, "WBA")
    assert row.delisted_date == today


# ----------------------------------------------------------------------------
# Current-session bar merge (TipRanks GetQuotes) -- conservative, append-only
# ----------------------------------------------------------------------------


class _YahooLikeBars:
    """A ``get_daily_bars`` provider that only ever has SOME symbols'
    history -- used to simulate Yahoo's chart endpoint lagging by a session
    (no bar for "today" yet)."""

    name = "yahoo"

    def __init__(self, bars_by_symbol):
        self._bars_by_symbol = bars_by_symbol

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        return {s: list(self._bars_by_symbol.get(s, [])) for s in symbols}


class _TipRanksWithCurrentBars:
    """Duck-types the pieces of ``TipRanksProvider`` this feature reads:
    ``.name == "tipranks"``, a (deliberately empty) ``get_daily_bars``, and
    ``get_current_session_bars``."""

    name = "tipranks"

    def __init__(self, current_bars_by_symbol):
        self._current_bars = current_bars_by_symbol
        self.requested: list[list[str]] = []

    def get_daily_bars(self, symbols, start, end, *, adjusted=True):
        return {s: [] for s in symbols}

    def get_current_session_bars(self, symbols):
        self.requested.append(list(symbols))
        return {s: self._current_bars[s] for s in symbols if s in self._current_bars}


def test_merge_appends_current_session_bar_when_daily_history_lacks_today(memory_db):
    """The core conservative-merge behaviour: Yahoo's chart has nothing for
    today yet, so the TipRanks GetQuotes current-session bar is appended."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    today = dt.datetime.now(tz=dt.UTC).date()
    yesterday = today - dt.timedelta(days=1)

    yahoo_like = _YahooLikeBars({
        "AAPL": [
            Bar(symbol="AAPL", session=yesterday, open=1, high=1, low=1, close=1,
                volume=1, source="yahoo"),
        ],
    })
    current_bar = Bar(
        symbol="AAPL", session=today, open=2.0, high=3.0, low=1.5, close=2.5,
        volume=100.0, source="tipranks_getquotes",
    )
    tipranks_like = _TipRanksWithCurrentBars({"AAPL": current_bar})
    wrapper = _CascadeLike(primary=tipranks_like, fallbacks=[yahoo_like])

    ingestor = DataIngestor(config, memory_db, market_provider=wrapper)
    report = IngestReport()
    collected = ingestor.ingest_prices(["AAPL"], yesterday, today, report)

    sessions = {b.session for b in collected["AAPL"]}
    assert sessions == {yesterday, today}
    appended = next(b for b in collected["AAPL"] if b.session == today)
    assert appended.source == "tipranks_getquotes"
    assert appended.close == pytest.approx(2.5)

    # Persisted too, via the normal bars-persist path.
    with memory_db.read_session() as session:
        rows = session.execute(
            select(PriceBar).where(PriceBar.symbol == "AAPL", PriceBar.session == today)
        ).scalars().all()
    assert rows


def test_merge_never_overwrites_an_existing_today_bar(memory_db):
    """CONSERVATIVE by design: even though the GetQuotes bar might be
    "fresher", an already-present bar for today's session is never replaced
    or duplicated -- this merge only ever fills a session that has NOTHING
    at all yet."""
    config = AppConfig()
    config.market_data.benchmark_symbol = "SPY"
    today = dt.datetime.now(tz=dt.UTC).date()

    yahoo_like = _YahooLikeBars({
        "AAPL": [
            Bar(symbol="AAPL", session=today, open=10.0, high=10.0, low=10.0,
                close=10.0, volume=10.0, source="yahoo"),
        ],
    })
    getquotes_bar = Bar(
        symbol="AAPL", session=today, open=999.0, high=999.0, low=999.0,
        close=999.0, volume=999.0, source="tipranks_getquotes",
    )
    tipranks_like = _TipRanksWithCurrentBars({"AAPL": getquotes_bar})
    wrapper = _CascadeLike(primary=tipranks_like, fallbacks=[yahoo_like])

    ingestor = DataIngestor(config, memory_db, market_provider=wrapper)
    report = IngestReport()
    collected = ingestor.ingest_prices(["AAPL"], today, today, report)

    assert len(collected["AAPL"]) == 1
    assert collected["AAPL"][0].close == pytest.approx(10.0)
    assert collected["AAPL"][0].source == "yahoo"
    # The pre-filter must have skipped AAPL entirely -- it already had a bar
    # for today, so GetQuotes was never even queried for it.
    requested_symbols = {s for batch in tipranks_like.requested for s in batch}
    assert "AAPL" not in requested_symbols


def test_merge_is_noop_without_a_tipranks_provider_in_the_chain(memory_db):
    """No provider named "tipranks" anywhere in the configured chain -- a
    plain fake bars provider -- must never raise; the merge silently does
    nothing and the run proceeds exactly as it did before this feature."""
    config = AppConfig()
    provider = _FakeBarsProvider()  # .name == "fake_bars_provider"
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    collected = ingestor.ingest_prices(
        ["AAPL"], dt.date(2024, 1, 2), dt.date(2024, 1, 2), report
    )
    assert collected["AAPL"]  # bars still fetched normally


def test_merge_current_session_bars_dedupes_by_exact_session_date():
    """Direct unit test of the merge's own safety net: even for a symbol the
    "today" pre-filter let through, an exact session-date match against
    what's already collected is never duplicated or overwritten -- this is
    what makes the merge safe regardless of how precisely the pre-filter's
    notion of "today" lines up with the exchange's own trading day."""
    config = AppConfig()
    ingestor = DataIngestor(config, None, market_provider=None)

    existing_bar = Bar(
        symbol="AAPL", session=dt.date(2024, 1, 5), open=1.0, high=1.0, low=1.0,
        close=1.0, volume=1.0, source="yahoo",
    )
    collected = {"AAPL": [existing_bar]}

    class _Tip:
        name = "tipranks"

        def get_current_session_bars(self, symbols):
            return {
                "AAPL": Bar(
                    symbol="AAPL", session=dt.date(2024, 1, 5), open=99.0, high=99.0,
                    low=99.0, close=99.0, volume=99.0, source="tipranks_getquotes",
                )
            }

    ingestor.market = _Tip()
    ingestor._merge_current_session_bars(["AAPL"], collected)

    assert len(collected["AAPL"]) == 1
    assert collected["AAPL"][0] is existing_bar  # untouched, never replaced


def test_merge_current_session_bars_appends_for_a_brand_new_symbol_key():
    """A symbol with NO entry at all yet in ``collected`` (not even an empty
    list) is still handled -- the merge creates the list rather than
    assuming every symbol already has a (possibly empty) entry."""
    config = AppConfig()
    ingestor = DataIngestor(config, None, market_provider=None)
    collected: dict[str, list[Bar]] = {}

    bar = Bar(
        symbol="NEW", session=dt.date(2024, 1, 5), open=1.0, high=1.0, low=1.0,
        close=1.0, volume=1.0, source="tipranks_getquotes",
    )

    class _Tip:
        name = "tipranks"

        def get_current_session_bars(self, symbols):
            return {"NEW": bar}

    ingestor.market = _Tip()
    ingestor._merge_current_session_bars(["NEW"], collected)

    assert collected["NEW"] == [bar]


def test_persist_bars_reactivates_a_symbol_that_produces_real_bars_again(memory_db):
    """The other half of the reactivation contract: once a refresh actually
    finds real bars for a previously-deactivated symbol, ``delisted_date``
    is cleared automatically."""
    config = AppConfig()
    today = dt.datetime.now(tz=dt.UTC).date()
    _insert_security(memory_db, "WBA", delisted_date=today)

    provider = _FakeBarsProvider()
    ingestor = DataIngestor(config, memory_db, market_provider=provider)
    report = IngestReport()

    ingestor._persist_bars(
        "WBA",
        [
            Bar(
                symbol="WBA",
                session=dt.date(2024, 1, 2),
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.2,
                volume=1000.0,
                adj_close=10.2,
                source="test",
            )
        ],
        report,
    )

    with memory_db.read_session() as session:
        row = session.get(Security, "WBA")
    assert row.delisted_date is None
    assert any(i.category == "symbol_reactivated" and i.symbol == "WBA" for i in report.quality.issues)
