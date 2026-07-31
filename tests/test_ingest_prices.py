"""``DataIngestor.ingest_prices`` -- the daily-bars fetch/persist path.

Covers the benchmark-bars fix: a real refresh log showed "benchmark SPY
unavailable; regime reported as UNKNOWN for all sessions" because SPY (an
ETF, not a universe member) was never included in the bar-fetch loop.
``ingest_prices`` must always fetch/store bars for
``config.market_data.benchmark_symbol``, even when the caller's ``symbols``
list omits it entirely.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import PriceBar
from claudetrade.domain import Bar


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
