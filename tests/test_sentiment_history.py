"""Per-symbol sentiment/mention history and the rising screen.

The application's premise is that a stock announces itself as rising
attention before it announces itself in price. That needs a series per
symbol and a way to ask what changed -- these tests pin both, plus the
judgement calls that make the ranking useful rather than noise-first.
"""

from __future__ import annotations

import datetime as dt

import pytest

from claudetrade.db.migrations import init_database
from claudetrade.db.models import Security, SymbolSentimentDaily
from claudetrade.db.session import Database
from claudetrade.sentiment.history import (
    coverage_summary,
    rising_symbols,
    symbol_series,
)

#: A Friday, so trading-day arithmetic in the tests is unambiguous.
AS_OF = dt.date(2026, 7, 31)


@pytest.fixture
def db() -> Database:
    database = Database("sqlite:///:memory:")
    init_database(database)
    return database


def _seed(db: Database, rows: list[tuple[str, dt.date, str, int, float]]) -> None:
    """``(symbol, session, source, post_count, raw_sentiment)``."""
    with db.session() as session:
        for symbol in {r[0] for r in rows}:
            session.merge(Security(symbol=symbol, name=f"{symbol} Inc"))
        for symbol, sess, source, count, sentiment in rows:
            session.add(
                SymbolSentimentDaily(
                    symbol=symbol, session=sess, source=source,
                    post_count=count, raw_sentiment=sentiment,
                    bull_bear_ratio=2.0, confidence=0.6, unique_authors=max(1, count // 2),
                )
            )


class TestSymbolSeries:
    def test_gaps_are_filled_with_real_zeros(self, db: Database) -> None:
        """A session nobody posted about is zero mentions, not missing data --
        but the two must stay distinguishable, hence ``observed``."""
        _seed(db, [("NVDA", dt.date(2026, 7, 30), "all", 12, 0.4)])

        window = symbol_series(db, "NVDA", as_of=AS_OF, days=7)
        by_session = {p.session: p for p in window.points}

        assert by_session[dt.date(2026, 7, 30)].mentions == 12
        assert by_session[dt.date(2026, 7, 30)].observed is True
        assert by_session[AS_OF].mentions == 0
        assert by_session[AS_OF].observed is False

    def test_the_series_covers_trading_sessions_only(self, db: Database) -> None:
        """Padding with weekends would invent a two-day mention drought every
        week and make every rate calculation wrong."""
        window = symbol_series(db, "NVDA", as_of=AS_OF, days=10)

        assert all(p.session.weekday() < 5 for p in window.points)

    def test_attention_and_local_mentions_are_reported_separately_and_summed(
        self, db: Database
    ) -> None:
        """Both count 'times this was talked about', over different corpora,
        so they add -- while staying individually visible."""
        _seed(
            db,
            [
                ("NVDA", dt.date(2026, 7, 30), "all", 10, 0.5),
                ("NVDA", dt.date(2026, 7, 30), "apewisdom:4chan", 61, 0.0),
                ("NVDA", dt.date(2026, 7, 30), "apewisdom:all-stocks", 812, 0.0),
            ],
        )

        point = {p.session: p for p in symbol_series(db, "NVDA", as_of=AS_OF, days=7).points}[
            dt.date(2026, 7, 30)
        ]

        assert point.mentions == 10
        assert point.attention_mentions == 873
        assert point.total_mentions == 883
        # Polarity comes only from the source that measures it.
        assert point.sentiment == pytest.approx(0.5)

    def test_per_source_local_rows_do_not_double_count(self, db: Database) -> None:
        """'reddit'/'news' rows are already inside the 'all' aggregate."""
        _seed(
            db,
            [
                ("NVDA", dt.date(2026, 7, 30), "all", 10, 0.5),
                ("NVDA", dt.date(2026, 7, 30), "reddit", 7, 0.5),
                ("NVDA", dt.date(2026, 7, 30), "news", 3, 0.5),
            ],
        )

        point = {p.session: p for p in symbol_series(db, "NVDA", as_of=AS_OF, days=7).points}[
            dt.date(2026, 7, 30)
        ]
        assert point.mentions == 10

    def test_nothing_after_as_of_is_ever_returned(self, db: Database) -> None:
        """A history call inside a backtest must not see its own future."""
        _seed(db, [("NVDA", dt.date(2026, 8, 3), "all", 999, 0.9)])

        window = symbol_series(db, "NVDA", as_of=AS_OF, days=30)

        assert all(p.session <= AS_OF for p in window.points)
        assert window.total_mentions == 0


class TestRisingSymbols:
    def _baseline_then_surge(self, db: Database, symbol: str, *, quiet: int, loud: int):
        """20 quiet sessions, then 3 loud ones ending at AS_OF."""
        from claudetrade.utils.timeutils import trading_day_range

        sessions = trading_day_range(AS_OF - dt.timedelta(days=45), AS_OF)[-23:]
        rows = [(symbol, s, "all", quiet, 0.1) for s in sessions[:-3]]
        rows += [(symbol, s, "all", loud, 0.5) for s in sessions[-3:]]
        _seed(db, rows)

    def test_a_quiet_symbol_waking_up_outranks_a_permanently_loud_one(
        self, db: Database
    ) -> None:
        """The whole point. Absolute-volume ranking returns the same mega-caps
        every day; self-relative ranking surfaces the name that just changed."""
        self._baseline_then_surge(db, "QUIET", quiet=1, loud=40)
        self._baseline_then_surge(db, "LOUD", quiet=500, loud=520)

        ranked = rising_symbols(db, as_of=AS_OF, limit=10)

        assert ranked[0].symbol == "QUIET"
        quiet = next(t for t in ranked if t.symbol == "QUIET")
        assert quiet.mention_change > 10 * next(
            t for t in ranked if t.symbol == "LOUD"
        ).mention_change

    def test_the_mention_floor_keeps_noise_off_the_list(self, db: Database) -> None:
        """1 -> 3 mentions is a 200% rise and means nothing. Unfloored ratio
        ranking puts exactly this at the top, every time."""
        # 1 mention on each of the 3 recent sessions = 3 total, under the floor.
        self._baseline_then_surge(db, "NOISE", quiet=0, loud=1)

        assert rising_symbols(db, as_of=AS_OF, min_recent_mentions=5) == []
        assert [t.symbol for t in rising_symbols(db, as_of=AS_OF, min_recent_mentions=1)] == [
            "NOISE"
        ]

    def test_a_symbol_emerging_from_silence_ranks_but_stays_sortable(
        self, db: Database
    ) -> None:
        """Silence -> chatter is the most interesting case, so it is kept; an
        uncapped infinite ratio would make the ordering meaningless."""
        self._baseline_then_surge(db, "WOKE", quiet=0, loud=50)

        top = rising_symbols(db, as_of=AS_OF)[0]
        assert top.symbol == "WOKE"
        assert top.mention_change == 10.0  # capped, not inf/NaN
        assert top.baseline_rate == 0.0

    def test_sentiment_change_is_reported_but_never_ranked_on(
        self, db: Database
    ) -> None:
        """A mention surge with collapsing tone is a short setup, not a row to
        hide -- it must stay in the list with its negative change visible."""
        from claudetrade.utils.timeutils import trading_day_range

        sessions = trading_day_range(AS_OF - dt.timedelta(days=45), AS_OF)[-23:]
        rows = [("SOUR", s, "all", 2, 0.6) for s in sessions[:-3]]
        rows += [("SOUR", s, "all", 60, -0.7) for s in sessions[-3:]]
        _seed(db, rows)

        top = rising_symbols(db, as_of=AS_OF)[0]
        assert top.symbol == "SOUR"
        assert top.sentiment_change is not None and top.sentiment_change < -1.0
        assert top.mention_change > 0

    def test_attention_only_symbols_rank_without_inventing_polarity(
        self, db: Database
    ) -> None:
        """ApeWisdom carries mentions and no direction. It must be able to
        drive the ranking while reporting sentiment as unmeasured."""
        from claudetrade.utils.timeutils import trading_day_range

        sessions = trading_day_range(AS_OF - dt.timedelta(days=45), AS_OF)[-23:]
        rows = [("ATTN", s, "apewisdom:all-stocks", 1, 0.0) for s in sessions[:-3]]
        rows += [("ATTN", s, "apewisdom:all-stocks", 90, 0.0) for s in sessions[-3:]]
        _seed(db, rows)

        top = rising_symbols(db, as_of=AS_OF)[0]
        assert top.symbol == "ATTN"
        assert top.has_polarity is False
        assert top.recent_sentiment is None
        assert top.sentiment_change is None

    def test_symbols_absent_from_securities_never_rank(self, db: Database) -> None:
        """Same junk-symbol guard get_trending applies."""
        from claudetrade.utils.timeutils import trading_day_range

        sessions = trading_day_range(AS_OF - dt.timedelta(days=45), AS_OF)[-23:]
        with db.session() as session:
            for s in sessions[-3:]:
                session.add(
                    SymbolSentimentDaily(
                        symbol="ZZZZ", session=s, source="all", post_count=500
                    )
                )

        assert rising_symbols(db, as_of=AS_OF) == []

    def test_future_sessions_cannot_influence_the_ranking(self, db: Database) -> None:
        self._baseline_then_surge(db, "AAA", quiet=1, loud=30)
        _seed(db, [("BBB", dt.date(2026, 8, 3), "all", 9999, 0.9)])

        assert "BBB" not in {t.symbol for t in rising_symbols(db, as_of=AS_OF)}


class TestCoverage:
    def test_reports_how_much_history_backs_a_trend(self, db: Database) -> None:
        """A three-session database cannot support a 20-session baseline no
        matter how good the ranking code is -- saying so is what stops a
        warming-up install reading as 'nothing is rising'."""
        _seed(
            db,
            [
                ("NVDA", dt.date(2026, 7, 29), "all", 5, 0.2),
                ("NVDA", dt.date(2026, 7, 30), "all", 6, 0.2),
                ("MU", dt.date(2026, 7, 30), "apewisdom:4chan", 9, 0.0),
            ],
        )

        summary = coverage_summary(db, as_of=AS_OF, days=90)

        assert summary["sessions_with_data"] == 2
        assert summary["symbols_with_history"] == 2
        assert summary["symbols_with_attention_data"] == 1
        assert summary["latest_session"] == "2026-07-30"

    def test_an_empty_database_reports_zeroes_not_an_error(self, db: Database) -> None:
        summary = coverage_summary(db, as_of=AS_OF, days=90)

        assert summary["sessions_with_data"] == 0
        assert summary["earliest_session"] is None
