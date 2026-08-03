"""Tests for ``claudetrade.providers.market.tipranks_institutional
.institutional_score`` -- the pure, dedicated scoring function the module
docstring says gets "its own dedicated test file since it's pure and
heavily specified".

Two families of cases:

* Synthetic ``domain.InstitutionalSnapshot`` objects built directly (no
  parsing involved) exercising every documented branch: both axes present,
  each axis alone, no data at all, staleness decay (including the "near
  zero at 2 quarters" requirement for the hedge-fund axis), market-cap
  normalization direction, and output clamping.
* The two real fixtures (via ``parse_institutional_snapshot``), pinned to
  the exact values this module actually computes for them -- a regression
  guard against silent formula drift, cross-checked by hand against the
  module's own constants when this test file was written.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from claudetrade.domain import (
    HedgeFundHoldingQuarter,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)
from claudetrade.providers.market.tipranks_institutional import (
    HEDGE_FUND_STALENESS_FULL_DECAY_DAYS,
    INSIDER_STALENESS_FULL_DECAY_DAYS,
    institutional_score,
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


def _insider_month(year: int, month: int) -> InsiderTransactionMonth:
    return InsiderTransactionMonth(month=month, year=year)


def _quarter(date: dt.date, *, holding: int = 1_000_000, net_change: int = 0) -> HedgeFundHoldingQuarter:
    return HedgeFundHoldingQuarter(date=date, holding_amount=holding, net_shares_change=net_change)


def _snapshot(**overrides) -> InstitutionalSnapshot:
    defaults: dict = {
        "symbol": "TEST",
        "as_of_session": SESSION,
    }
    defaults.update(overrides)
    return InstitutionalSnapshot(**defaults)


class TestNoDataAtAll:
    def test_empty_snapshot_yields_none_score_and_none_subscores(self):
        snap = _snapshot()
        result = institutional_score(snap, SESSION)
        assert result.score is None
        assert result.insider_subscore is None
        assert result.hedge_fund_subscore is None
        assert result.insider_weight_applied == 0.0
        assert result.hedge_fund_weight_applied == 0.0


class TestInsiderAxisOnly:
    def test_insider_flow_and_confidence_blend_no_hedge_fund_data(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=500_000.0,
            insider_confidence_stock_score=0.9,
            market_cap_usd=1_000_000_000.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_subscore is None
        assert result.hedge_fund_weight_applied == 0.0
        assert result.insider_subscore is not None
        # No hedge-fund axis present -> the blended score equals the
        # insider subscore exactly (its weight absorbs the whole blend).
        assert result.score == pytest.approx(result.insider_subscore)
        assert -1.0 <= result.score <= 1.0

    def test_positive_flow_and_high_confidence_yields_positive_axis(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1_000_000.0,
            insider_confidence_stock_score=0.95,
            market_cap_usd=500_000_000.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore > 0
        assert result.score > 0

    def test_negative_flow_and_low_confidence_yields_negative_axis(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=-1_000_000.0,
            insider_confidence_stock_score=0.05,
            market_cap_usd=500_000_000.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore < 0
        assert result.score < 0


class TestHedgeFundAxisOnly:
    def test_hedge_fund_sentiment_and_flow_blend_no_insider_data(self):
        snap = _snapshot(
            hedge_fund_sentiment=0.8,
            hedge_fund_holdings_by_quarter=[
                _quarter(SESSION, holding=1_000_000, net_change=100_000)
            ],
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore is None
        assert result.insider_weight_applied == 0.0
        assert result.hedge_fund_subscore is not None
        assert result.score == pytest.approx(result.hedge_fund_subscore)
        assert -1.0 <= result.score <= 1.0

    def test_bullish_hedge_fund_sentiment_yields_positive_axis(self):
        snap = _snapshot(
            hedge_fund_sentiment=0.9,
            hedge_fund_holdings_by_quarter=[_quarter(SESSION, net_change=50_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_subscore > 0

    def test_bearish_hedge_fund_sentiment_yields_negative_axis(self):
        snap = _snapshot(
            hedge_fund_sentiment=0.1,
            hedge_fund_holdings_by_quarter=[_quarter(SESSION, net_change=-50_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_subscore < 0


class TestBothAxesPresent:
    def test_insider_axis_weighted_above_hedge_fund_when_both_fresh(self):
        """Per the task spec: insider axis weighted ABOVE hedge-fund when
        both are equally fresh (age 0 for both)."""
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1_000_000.0,
            insider_confidence_stock_score=0.9,
            market_cap_usd=500_000_000.0,
            hedge_fund_sentiment=0.9,
            hedge_fund_holdings_by_quarter=[_quarter(SESSION, net_change=50_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_weight_applied > result.hedge_fund_weight_applied
        assert result.score is not None


class TestStalenessDecay:
    def test_insider_axis_full_weight_when_freshly_dated(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(SESSION.year, SESSION.month)],
            insider_net_3m_usd=100_000.0,
            insider_confidence_stock_score=0.7,
            market_cap_usd=1_000_000_000.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_age_days is not None
        assert result.insider_age_days <= 30  # within the same calendar month
        assert result.insider_weight_applied > 0.0

    def test_insider_axis_decays_to_zero_at_full_decay_days(self):
        old_month = SESSION - dt.timedelta(days=int(INSIDER_STALENESS_FULL_DECAY_DAYS) + 5)
        snap = _snapshot(
            insider_monthly=[_insider_month(old_month.year, old_month.month)],
            insider_net_3m_usd=100_000.0,
            insider_confidence_stock_score=0.7,
            market_cap_usd=1_000_000_000.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore is not None  # the value itself still exists
        assert result.insider_weight_applied == 0.0  # but is fully decayed out
        assert result.score is None  # and nothing else is present to blend with

    def test_hedge_fund_axis_full_weight_at_zero_age(self):
        snap = _snapshot(
            hedge_fund_sentiment=0.7,
            hedge_fund_holdings_by_quarter=[_quarter(SESSION, net_change=10_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_age_days == 0
        assert result.hedge_fund_weight_applied > 0.0

    def test_hedge_fund_axis_near_zero_at_two_quarters_old(self):
        """Task spec: hedge-fund axis staleness weight is "near zero at 2
        quarters old" -- pinned here at ``HEDGE_FUND_STALENESS_FULL_DECAY_DAYS``
        (~2 quarters, 182 days)."""
        two_quarters_ago = SESSION - dt.timedelta(days=int(HEDGE_FUND_STALENESS_FULL_DECAY_DAYS))
        snap = _snapshot(
            hedge_fund_sentiment=0.7,
            hedge_fund_holdings_by_quarter=[_quarter(two_quarters_ago, net_change=10_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_weight_applied == pytest.approx(0.0, abs=1e-9)

    def test_hedge_fund_axis_one_quarter_old_still_carries_meaningful_weight(self):
        one_quarter_ago = SESSION - dt.timedelta(days=91)
        snap = _snapshot(
            hedge_fund_sentiment=0.7,
            hedge_fund_holdings_by_quarter=[_quarter(one_quarter_ago, net_change=10_000)],
        )
        result = institutional_score(snap, SESSION)
        assert result.hedge_fund_weight_applied > 0.0
        # Roughly half of full weight at roughly half the decay window.
        assert result.hedge_fund_weight_applied < result.hedge_fund_weight_applied + 1e-9

    def test_no_dated_evidence_at_all_is_zero_weight_even_with_a_subscore(self):
        """A snapshot with an insider confidence score but literally no
        monthly bucket to date it by -- ``_insider_axis_age_days`` returns
        ``None``, and ``_staleness_weight(None, ...)`` is 0.0 (never a
        fabricated freshness)."""
        snap = _snapshot(insider_confidence_stock_score=0.9)
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore is not None
        assert result.insider_age_days is None
        assert result.insider_weight_applied == 0.0
        assert result.score is None


class TestMarketCapNormalization:
    def test_same_dollar_flow_scores_larger_on_a_smaller_market_cap(self):
        """The core "market-cap normalized" requirement: an identical net
        insider $ flow must read as a STRONGER signal against a smaller
        company than against a mega-cap."""
        small_cap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1_000_000.0,
            market_cap_usd=50_000_000.0,
        )
        mega_cap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1_000_000.0,
            market_cap_usd=500_000_000_000.0,
        )
        small_result = institutional_score(small_cap, SESSION)
        mega_result = institutional_score(mega_cap, SESSION)
        assert small_result.insider_subscore > mega_result.insider_subscore > 0

    def test_missing_market_cap_drops_the_flow_component_not_the_whole_axis(self):
        """No market cap to normalize against -> the flow component is
        absent, but the confidence component alone still produces an axis
        value (weight redistribution, never a fabricated 0)."""
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1_000_000.0,
            insider_confidence_stock_score=0.8,
            market_cap_usd=None,
        )
        result = institutional_score(snap, SESSION)
        assert result.insider_subscore is not None


class TestClamping:
    def test_score_never_exceeds_plus_or_minus_one(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=1e15,
            insider_confidence_stock_score=1.0,
            market_cap_usd=1.0,
            hedge_fund_sentiment=1.0,
            hedge_fund_holdings_by_quarter=[
                _quarter(SESSION, holding=1, net_change=10_000_000)
            ],
        )
        result = institutional_score(snap, SESSION)
        assert result.score is not None
        assert -1.0 <= result.score <= 1.0
        assert -1.0 <= result.insider_subscore <= 1.0
        assert -1.0 <= result.hedge_fund_subscore <= 1.0

    def test_extreme_negative_flow_still_clamped(self):
        snap = _snapshot(
            insider_monthly=[_insider_month(2026, 7)],
            insider_net_3m_usd=-1e15,
            insider_confidence_stock_score=0.0,
            market_cap_usd=1.0,
        )
        result = institutional_score(snap, SESSION)
        assert result.score == pytest.approx(-1.0)


class TestFixtureRegression:
    """Pinned end-to-end values (parser + scorer together) for the two
    committed fixtures -- a regression guard against silent formula drift.
    Computed by hand from the module's own constants when this test was
    written; see ``providers.market.tipranks_institutional``'s docstrings
    for the underlying rationale of every weight/constant used below."""

    def test_intc_fixture_score(self):
        snap = parse_institutional_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        result = institutional_score(snap, SESSION)
        assert result.score == pytest.approx(-0.2076757441205672, abs=1e-6)
        assert result.insider_subscore == pytest.approx(-0.3908446873602923, abs=1e-6)
        assert result.hedge_fund_subscore == pytest.approx(0.4802253982686228, abs=1e-6)
        assert result.insider_age_days == 29
        assert result.hedge_fund_age_days == 121

    def test_teck_b_fixture_score(self):
        snap = parse_institutional_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        result = institutional_score(snap, SESSION)
        assert result.score == pytest.approx(-0.6108866681174698, abs=1e-6)
        assert result.insider_subscore == pytest.approx(-0.6255329870737475, abs=1e-6)
        assert result.hedge_fund_subscore == pytest.approx(-0.5558816035927828, abs=1e-6)
        assert result.insider_age_days == 29
        assert result.hedge_fund_age_days == 121
