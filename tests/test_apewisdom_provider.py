"""ApeWisdom attention source: Reddit and 4chan aggregate mention counts.

Two things these tests exist to hold in place, beyond ordinary parsing:

* **No fabricated post-level data.** ApeWisdom publishes tallies, not posts.
  The provider must never manufacture authors, text or timestamps to squeeze
  itself into the post-shaped ``SocialProvider`` protocol -- that is what the
  removed synthetic providers did, and ``unique_authors``/``bot_risk``/
  ``manipulation_risk`` are all computed from post-level identity, so
  invented posts would feed the manipulation model fiction rather than
  merely adding noise.
* **Attention never becomes polarity.** A mention count says people are
  talking, never what they are saying (``sentiment.aggregation``: counting
  mentions toward bullishness "is exactly the mistake this module exists to
  avoid"). Stored attention rows must stay out of the ``"all"`` aggregate
  strategies score against.

No live network anywhere: responses are served from the transcribed fixtures
via ``httpx.MockTransport``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from claudetrade.config import ApeWisdomConfig, AppConfig
from claudetrade.data.ingest import DataIngestor, IngestReport
from claudetrade.db.models import Security, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.domain import SymbolAttention
from claudetrade.providers.social.apewisdom import ApeWisdomProvider, _as_int, _parse_results

FIXTURES = Path(__file__).parent / "fixtures" / "apewisdom"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _client(routes: dict[str, object], *, status: int = 200) -> httpx.Client:
    """A client serving ``{url_substring: payload}`` and 404 for anything else."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                if isinstance(payload, str):  # non-JSON body
                    return httpx.Response(status, text=payload)
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls  # type: ignore[attr-defined]
    return client


def _config(**overrides) -> ApeWisdomConfig:
    base = {"filters": ["all-stocks"], "max_pages_per_filter": 1, "min_mentions": 5}
    base.update(overrides)
    return ApeWisdomConfig(**base)


class TestParsing:
    def test_reads_tickers_counts_and_the_sources_own_baseline(self) -> None:
        rows = _parse_results(
            _fixture("all-stocks_page1"), community="all-stocks", observed_at=None
        )
        by_symbol = {r.symbol: r for r in rows}

        assert by_symbol["NVDA"].mentions == 812
        assert by_symbol["NVDA"].upvotes == 4210
        assert by_symbol["NVDA"].mentions_prev == 500
        assert by_symbol["NVDA"].community == "all-stocks"

    def test_numeric_strings_parse_identically_to_numbers(self) -> None:
        """The live API has been seen serving counts both ways; pinning one
        shape would make the provider brittle against its own vendor."""
        rows = {
            r.symbol: r
            for r in _parse_results(
                _fixture("all-stocks_page1"), community="all-stocks", observed_at=None
            )
        }
        assert rows["MU"].mentions == 430  # arrived as "430"
        assert rows["MU"].upvotes == 1980
        assert rows["MU"].mentions_prev == 610

    def test_crypto_tickers_are_dropped(self) -> None:
        """ApeWisdom emits crypto under stock filters when a post names both;
        this application screens US equities."""
        symbols = {
            r.symbol
            for r in _parse_results(
                _fixture("all-stocks_page1"), community="all-stocks", observed_at=None
            )
        }
        assert "BTC" not in symbols
        assert "NVDA" in symbols

    def test_malformed_rows_are_skipped_not_fatal(self) -> None:
        """A bad row must cost that row, not the whole community's data."""
        rows = _parse_results(_fixture("4chan_page1"), community="4chan", observed_at=None)
        assert {r.symbol for r in rows} == {"MU", "NVDA"}

    @pytest.mark.parametrize(
        "payload", [None, {}, {"results": "nope"}, {"results": [1, 2]}, []]
    )
    def test_unusable_payloads_yield_nothing_rather_than_raising(self, payload) -> None:
        assert _parse_results(payload, community="x", observed_at=None) == []

    def test_as_int_rejects_booleans(self) -> None:
        """``bool`` is an ``int`` subclass -- a JSON ``true`` is not a count."""
        assert _as_int(True) is None
        assert _as_int("1,024") == 1024
        assert _as_int("banana") is None


class TestAcceleration:
    def test_change_is_measured_against_the_sources_own_baseline(self) -> None:
        assert SymbolAttention("A", "c", mentions=150, mentions_prev=100).mention_acceleration == (
            pytest.approx(0.5)
        )
        assert SymbolAttention("A", "c", mentions=50, mentions_prev=100).mention_acceleration == (
            pytest.approx(-0.5)
        )

    def test_a_first_sighting_has_unknown_not_infinite_acceleration(self) -> None:
        """No baseline means no measurement. Reporting a huge number for a
        symbol's first appearance would manufacture a spike out of absence."""
        assert SymbolAttention("A", "c", mentions=900, mentions_prev=None).mention_acceleration == 0.0
        assert SymbolAttention("A", "c", mentions=900, mentions_prev=0).mention_acceleration == 0.0

    def test_clipped_to_the_same_band_as_the_post_rate_version(self) -> None:
        """Shares ``sentiment.aggregation``'s +/-10 clip so the two
        acceleration measures are on one comparable scale."""
        assert SymbolAttention("A", "c", mentions=10_000, mentions_prev=1).mention_acceleration == (
            10.0
        )


class TestFetch:
    def test_walks_each_configured_filter(self) -> None:
        client = _client(
            {
                "filter/all-stocks/page/1": _fixture("all-stocks_page1"),
                "filter/4chan/page/1": _fixture("4chan_page1"),
            }
        )
        provider = ApeWisdomProvider(
            _config(filters=["all-stocks", "4chan"]), client=client
        )
        rows = provider.fetch_attention()

        assert {r.community for r in rows} == {"all-stocks", "4chan"}
        # MU appears in both communities and stays two distinct observations:
        # collapsing them would lose which population is talking.
        assert sum(1 for r in rows if r.symbol == "MU") == 2

    def test_min_mentions_drops_noise(self) -> None:
        client = _client({"filter/all-stocks/page/1": _fixture("all-stocks_page1")})
        provider = ApeWisdomProvider(_config(min_mentions=100), client=client)
        symbols = {r.symbol for r in provider.fetch_attention()}
        assert "AAPL" not in symbols  # 3 mentions
        assert "NVDA" in symbols

    def test_stops_at_the_declared_page_count(self) -> None:
        """The 4chan fixture declares pages=1; requesting page 2 would be a
        wasted call against a rate-limited free API."""
        client = _client({"filter/4chan/page/1": _fixture("4chan_page1")})
        provider = ApeWisdomProvider(
            _config(filters=["4chan"], max_pages_per_filter=5), client=client
        )
        provider.fetch_attention()
        assert all("page/1" in url for url in client.calls)  # type: ignore[attr-defined]

    def test_one_failing_filter_does_not_lose_the_others(self) -> None:
        client = _client({"filter/4chan/page/1": _fixture("4chan_page1")})  # all-stocks 404s
        provider = ApeWisdomProvider(
            _config(filters=["all-stocks", "4chan"]), client=client
        )
        rows = provider.fetch_attention()
        assert rows and {r.community for r in rows} == {"4chan"}

    def test_rate_limit_and_non_json_degrade_to_empty_never_raise(self) -> None:
        limited = ApeWisdomProvider(
            _config(), client=_client({"filter": {"x": 1}}, status=429)
        )
        assert limited.fetch_attention() == []

        garbage = ApeWisdomProvider(_config(), client=_client({"filter": "<html>nope</html>"}))
        assert garbage.fetch_attention() == []

    def test_disabled_makes_no_requests_at_all(self) -> None:
        client = _client({"filter": _fixture("4chan_page1")})
        provider = ApeWisdomProvider(_config(enabled=False), client=client)
        assert provider.fetch_attention() == []
        assert client.calls == []  # type: ignore[attr-defined]


class TestStatus:
    def test_reports_no_point_in_time_support(self) -> None:
        """The API serves a rolling current 24h window with no history
        endpoint, so attention can never be backfilled for a past session.
        Claiming otherwise would invite look-ahead in a backtest."""
        status = ApeWisdomProvider(_config()).status()
        assert status.supports_point_in_time is False
        assert status.configured is True
        assert status.kind == "attention"

    def test_disabled_is_reported_not_hidden(self) -> None:
        status = ApeWisdomProvider(_config(enabled=False)).status()
        assert status.configured is False
        assert status.available is False


class _StubAttention:
    name = "apewisdom"

    def __init__(self, rows, *, boom: bool = False):
        self._rows = rows
        self._boom = boom

    def fetch_attention(self):
        if self._boom:
            raise RuntimeError("upstream exploded")
        return self._rows


class TestIngest:
    """Storage: attention rows must be visible without touching the polarity
    aggregate strategies score against."""

    @pytest.fixture
    def db(self) -> Database:
        from claudetrade.db.migrations import init_database

        database = Database("sqlite:///:memory:")
        init_database(database)
        with database.session() as session:
            session.add(Security(symbol="NVDA", name="NVIDIA"))
            session.add(Security(symbol="MU", name="Micron"))
        return database

    def _ingest(self, db, rows, **kw):
        ingestor = DataIngestor(AppConfig(), db, attention_providers=[_StubAttention(rows)], **kw)
        return ingestor.ingest_attention(dt.date(2026, 7, 31), IngestReport())

    def test_stores_attention_under_its_own_source_label(self, db: Database) -> None:
        written = self._ingest(
            db, [SymbolAttention("NVDA", "4chan", mentions=61, upvotes=140, mentions_prev=50)]
        )
        assert written == 1

        with db.read_session() as session:
            row = session.query(SymbolSentimentDaily).one()
        assert row.source == "apewisdom:4chan"
        assert row.post_count == 61
        assert row.total_engagement == pytest.approx(140.0)
        assert row.mention_acceleration == pytest.approx(0.22, abs=0.01)

    def test_never_writes_the_combined_aggregate_strategies_score_against(
        self, db: Database
    ) -> None:
        """The load-bearing guarantee, restated for what it actually protects.

        It used to be described as "this source cannot move a signal's score
        no matter what it reports". That was true only because attention data
        was excluded from every axis -- including the one it measures better
        than anything else in the system (QA #5). It now feeds ATTENTION
        scoring, ranked against its own history, so the guarantee this test
        protects is narrower and more precise: never the combined ``"all"``
        row, and therefore never any POLARITY component, ``manipulation_risk``
        or ``data_confidence``, all of which read that row alone. See
        ``tests/test_scoring_sentiment_axes.py`` for the scoring-side half of
        the same separation.
        """
        self._ingest(db, [SymbolAttention("NVDA", "all-stocks", mentions=800)])

        with db.read_session() as session:
            sources = {r.source for r in session.query(SymbolSentimentDaily).all()}
        assert "all" not in sources

    def test_carries_no_polarity_and_claims_no_authors(self, db: Database) -> None:
        """An aggregate tally does not know who spoke or which way they
        leaned. Both must read as absent, not as measured-and-neutral."""
        self._ingest(db, [SymbolAttention("NVDA", "4chan", mentions=61, upvotes=140)])

        with db.read_session() as session:
            row = session.query(SymbolSentimentDaily).one()
        assert row.raw_sentiment == 0.0
        assert row.bull_bear_ratio == 1.0  # the column default, never measured
        assert row.unique_authors == 0
        assert row.confidence == 0.0
        assert row.labels["attention_only"] == 1.0

        # And the domain model agrees it is not a usable polarity sample:
        # ``is_sufficient`` gates on unique authors, which an aggregate tally
        # genuinely cannot report.
        from claudetrade.domain import SymbolSentiment

        assert not SymbolSentiment(
            symbol=row.symbol,
            session=row.session,
            source=row.source,
            post_count=row.post_count,
            unique_authors=row.unique_authors,
        ).is_sufficient

    def test_symbols_outside_the_tracked_universe_are_dropped(self, db: Database) -> None:
        written = self._ingest(
            db,
            [
                SymbolAttention("NVDA", "4chan", mentions=61),
                SymbolAttention("ZZZZ", "4chan", mentions=999),
            ],
        )
        assert written == 1
        with db.read_session() as session:
            assert {r.symbol for r in session.query(SymbolSentimentDaily).all()} == {"NVDA"}

    def test_re_ingesting_the_same_session_updates_rather_than_duplicates(
        self, db: Database
    ) -> None:
        self._ingest(db, [SymbolAttention("NVDA", "4chan", mentions=61)])
        self._ingest(db, [SymbolAttention("NVDA", "4chan", mentions=90)])

        with db.read_session() as session:
            rows = session.query(SymbolSentimentDaily).all()
        assert len(rows) == 1 and rows[0].post_count == 90

    def test_a_failing_provider_is_recorded_and_does_not_abort_the_refresh(
        self, db: Database
    ) -> None:
        ingestor = DataIngestor(
            AppConfig(), db, attention_providers=[_StubAttention([], boom=True)]
        )
        report = IngestReport()
        assert ingestor.ingest_attention(dt.date(2026, 7, 31), report) == 0
        assert "apewisdom" in report.provider_failures

    def test_no_providers_is_a_silent_no_op(self, db: Database) -> None:
        assert DataIngestor(AppConfig(), db).ingest_attention(dt.date(2026, 7, 31)) == 0
