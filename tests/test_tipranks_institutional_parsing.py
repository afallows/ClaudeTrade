"""Tests for ``claudetrade.providers.market.tipranks_institutional``'s
parsing half (``parse_institutional_snapshot``).

Driven against the two real fixtures the project owner captured from their
own machine (see ``tests/test_tipranks_analyst_parsing.py``'s own module
docstring for the provenance note) -- no network access, no mocking of the
parser itself. See ``tests/test_institutional_score.py`` for the dedicated
scoring-function tests.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from claudetrade.providers.market.tipranks_institutional import (
    HEDGE_FUND_HOLDINGS_MAX,
    INSIDER_MONTHLY_MAX,
    NOTABLE_HOLDER_MOVES_MAX,
    RECENT_INSIDER_TRANSACTIONS_MAX,
    parse_institutional_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tipranks"
INTC_OVERVIEW = json.loads((FIXTURES / "dataForTicker_INTC.json").read_text(encoding="utf-8"))[
    "overview"
]
TECK_B_OVERVIEW = json.loads(
    (FIXTURES / "dataForTicker_TECK_B.json").read_text(encoding="utf-8")
)["overview"]

SESSION = dt.date(2026, 7, 30)
FETCHED_AT = dt.datetime(2026, 7, 30, 20, 0, tzinfo=dt.UTC)


class TestIntcFixture:
    def test_insider_monthly_parsed_ascending(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.insider_monthly) == 3
        months = [(m.year, m.month) for m in snap.insider_monthly]
        assert months == sorted(months)
        assert months == [(2026, 5), (2026, 6), (2026, 7)]

    def test_insider_net_3m_usd_prefers_informative_over_raw(self):
        """June's row has informative_sell_amount=21024.0 (used) even
        though transSellAmount is also 21024.0 there; May's row has
        informative_*=0.0 on BOTH sides despite a buy-heavy RAW tally
        (transBuyAmount=149116.0) -- the informative figure (0.0) is used
        because it is not ``None``, confirming the "prefer informative,
        fall back to raw only when informative is None" rule, not "prefer
        informative unless it looks wrong"."""
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        # July: informative buy/sell both 0.0 -> contributes 0.
        # June: informative buy 0.0, informative sell 21024.0 -> -21024.0.
        # May: informative buy/sell both 0.0 (raw is buy-heavy but ignored
        # since informative is present, not None) -> contributes 0.
        assert snap.insider_net_3m_usd == -21024.0

    def test_insider_net_3m_usd_vendor_is_kept_separately(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.insider_net_3m_usd_vendor == -2486508.48
        assert snap.insider_net_3m_usd != snap.insider_net_3m_usd_vendor

    def test_insider_confidence_signal_fields(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.insider_confidence_stock_score == 0.29
        assert snap.insider_confidence_sector_score == 0.38
        assert snap.insider_confidence_raw_score == 3

    def test_num_of_insiders_and_market_cap(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.num_of_insiders == 28
        assert snap.market_cap_usd == 435297215393.0

    def test_recent_insider_transactions_carries_role_and_link(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.recent_insider_transactions) == 1
        txn = snap.recent_insider_transactions[0]
        assert txn.name == "David Zinsner"
        assert txn.is_officer is True
        assert txn.is_director is False
        assert txn.officer_title == "EVP, CFO"
        assert txn.operation_description == "Buy"
        assert txn.number_of_shares == 5882
        assert txn.estimated_shares_value == 249985.0
        assert txn.r_date == dt.date(2026, 1, 27)
        assert txn.link.startswith("http://sec.gov/")

    def test_hedge_fund_sentiment_and_trend(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.hedge_fund_sentiment == 0.83
        assert snap.hedge_fund_trend_action == 1
        assert snap.hedge_fund_trend_value == 6265577.0

    def test_hedge_fund_holdings_by_quarter_ascending(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.hedge_fund_holdings_by_quarter) == 2
        dates = [h.date for h in snap.hedge_fund_holdings_by_quarter]
        assert dates == sorted(dates)
        latest = snap.hedge_fund_holdings_by_quarter[-1]
        assert latest.date == dt.date(2026, 3, 31)
        assert latest.holding_amount == 89268707
        assert latest.net_shares_change == 6265577
        assert latest.is_complete is True

    def test_notable_holder_moves_ranked_by_change_amount(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.notable_holder_moves) == 1
        move = snap.notable_holder_moves[0]
        assert move.manager_name == "Stanley Druckenmiller"
        assert move.institution_name == "Duquesne Family Office LLC"
        assert move.change_amount == 411400.0
        assert move.stars == 2.26
        assert move.is_active is True

    def test_fetched_at_and_session_are_stamped_from_arguments(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.symbol == "INTC"
        assert snap.as_of_session == SESSION
        assert snap.fetched_at == FETCHED_AT


class TestTeckBFixture:
    """CAD-currency insider amounts on individual ``insiders[]`` rows, and a
    much longer (9-quarter) hedge-fund holdings history than INTC's."""

    def test_recent_insider_transaction_currency_and_role_flags(self):
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.recent_insider_transactions) == 1
        txn = snap.recent_insider_transactions[0]
        assert txn.name == "Norman B Keevil"
        assert txn.is_director is True
        assert txn.is_officer is False
        assert txn.officer_title == "Chairman Emeritus"
        assert txn.operation_description == "Grant/Award/Other Disposal"
        assert txn.estimated_shares_value == 181808.0

    def test_insider_confidence_signal_below_midpoint(self):
        """Lower-than-0.5 ``stockScore`` alongside a negative vendor 3-month
        sum, on this fixture too -- see the parser module's own docstring
        for why this corroborates (but does not vendor-confirm) the 0..1
        scale direction."""
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.insider_confidence_stock_score == 0.08
        assert snap.insider_net_3m_usd_vendor == -7190081.54

    def test_insider_net_3m_usd_sell_heavy_all_three_months(self):
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.insider_net_3m_usd == -111699.0
        assert snap.insider_net_3m_usd < 0

    def test_hedge_fund_holdings_history_longer_than_intc(self):
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.hedge_fund_holdings_by_quarter) == 9
        latest = snap.hedge_fund_holdings_by_quarter[-1]
        assert latest.date == dt.date(2026, 3, 31)
        assert latest.net_shares_change == -1709597

    def test_notable_holder_moves_capped_and_ranked(self):
        """11 rows on this fixture -- exercises the real
        ``NOTABLE_HOLDER_MOVES_MAX`` cap, ranked by ``|change_amount|``
        descending."""
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.notable_holder_moves) == NOTABLE_HOLDER_MOVES_MAX
        top = snap.notable_holder_moves[0]
        assert top.manager_name == "David Costen Haley"
        assert top.change_amount == 1585092.0
        second = snap.notable_holder_moves[1]
        assert second.manager_name == "Chris Davis"
        assert second.change_amount == -1434946.0

    def test_hedge_fund_sentiment_and_trend_bearish(self):
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.hedge_fund_sentiment == 0.15
        assert snap.hedge_fund_trend_action == 3
        assert snap.hedge_fund_trend_value == -1709597.0


class TestNoCoverage:
    def test_empty_overview_is_none(self):
        assert parse_institutional_snapshot({}, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_none_overview_is_none(self):
        assert parse_institutional_snapshot(None, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_overview_with_no_institutional_content_is_none(self):
        overview = {
            "corporateInsiderTransactions": [],
            "insiders": [],
            "insidrConfidenceSignal": {},
            "hedgeFundData": {},
            "numOfInsiders": None,
            "marketCapUSD": 1_000_000.0,
        }
        assert parse_institutional_snapshot(overview, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_malformed_shapes_do_not_raise(self):
        """Defensive parsing throughout: wrong types at every level degrade
        to 'contributes nothing' rather than raising."""
        overview = {
            "corporateInsiderTransactions": "not-a-list",
            "insiders": {"not": "a-list"},
            "insidrConfidenceSignal": ["also-wrong"],
            "hedgeFundData": "not-a-dict",
            "numOfInsiders": "not-an-int",
            "marketCapUSD": None,
        }
        assert parse_institutional_snapshot(overview, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_market_cap_alone_without_any_other_field_is_not_content(self):
        """``marketCapUSD`` is normalization-only data, never by itself a
        signal that this symbol has institutional coverage."""
        overview = {"marketCapUSD": 1_000_000_000.0}
        assert parse_institutional_snapshot(overview, "ZZZZ", SESSION, FETCHED_AT) is None


class TestBoundedLengths:
    def test_insider_monthly_is_capped(self):
        rows = [
            {
                "month": (i % 12) + 1,
                "year": 2020 + i // 12,
                "insidersBuyCount": 1,
                "insidersSellCount": 1,
                "transBuyCount": 1,
                "transSellCount": 1,
                "transBuyAmount": 100.0,
                "transSellAmount": 50.0,
                "informativeBuyCount": 1,
                "informativeSellCount": 1,
                "informativeBuyAmount": 100.0,
                "informativeSellAmount": 50.0,
            }
            for i in range(INSIDER_MONTHLY_MAX + 10)
        ]
        overview = {"corporateInsiderTransactions": rows}
        snap = parse_institutional_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.insider_monthly) == INSIDER_MONTHLY_MAX

    def test_recent_insider_transactions_is_capped_and_ranked_by_value(self):
        rows = [
            {"name": f"Person {i}", "estimatedSharesValue": float(i), "rDate": "2026-01-01"}
            for i in range(RECENT_INSIDER_TRANSACTIONS_MAX + 10)
        ]
        overview = {"insiders": rows}
        snap = parse_institutional_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.recent_insider_transactions) == RECENT_INSIDER_TRANSACTIONS_MAX
        values = [t.estimated_shares_value for t in snap.recent_insider_transactions]
        assert values == sorted(values, reverse=True)

    def test_hedge_fund_holdings_by_quarter_is_capped(self):
        rows = [
            {
                "date": (dt.date(2020, 1, 1) + dt.timedelta(days=91 * i)).isoformat(),
                "holdingAmount": 1000,
                "netSharesChange": 10,
            }
            for i in range(HEDGE_FUND_HOLDINGS_MAX + 10)
        ]
        overview = {"hedgeFundData": {"holdingsByTime": rows}}
        snap = parse_institutional_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.hedge_fund_holdings_by_quarter) == HEDGE_FUND_HOLDINGS_MAX

    def test_notable_holder_moves_is_capped(self):
        rows = [
            {"managerName": f"M{i}", "institutionName": f"I{i}", "changeAmount": float(i)}
            for i in range(NOTABLE_HOLDER_MOVES_MAX + 10)
        ]
        overview = {"hedgeFundData": {"institutionalHoldings": rows}}
        snap = parse_institutional_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.notable_holder_moves) == NOTABLE_HOLDER_MOVES_MAX
