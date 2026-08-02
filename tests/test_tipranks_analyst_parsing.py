"""Tests for ``claudetrade.providers.market.tipranks_analyst``.

Driven against the two real fixtures the project owner captured from their
own machine (see ``tests/test_tipranks_provider.py``'s own module docstring
for the provenance note) -- no network access, no mocking of the parser
itself. Field-level assertions here cross-reference the exact evidence the
module docstring cites for ``ratingId``/``actionId``/``eTypeId`` semantics.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from claudetrade.providers.market.tipranks_analyst import (
    CONSENSUS_OVER_TIME_MAX,
    RECENT_RATING_ACTIONS_MAX,
    parse_analyst_snapshot,
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
    def test_uses_latest_ranked_consensus_for_buy_hold_sell(self):
        """CONFIRMED distinct from the broader ``consensuses[0]`` row on this
        fixture (nH=24 there vs. nH=23 here) -- the ranked subset is used."""
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert (snap.buy_count, snap.hold_count, snap.sell_count) == (7, 23, 2)
        assert snap.analyst_count == 32

    def test_consensus_rating_and_rate_from_selected_consensuses_row(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.consensus_rating == 3
        assert snap.consensus_rate == 33.0

    def test_price_target_fields_from_pt_consensus(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.price_target_mean == 119.11
        assert snap.price_target_high == 200.0
        assert snap.price_target_low == 80.0
        assert snap.price_target_currency == "USD"

    def test_consensus_over_time_parsed_and_ascending(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.consensus_over_time) == 5
        dates = [p.date for p in snap.consensus_over_time]
        assert dates == sorted(dates)

    def test_non_analyst_stocktwits_row_is_excluded(self):
        """The ``notRankedExperts`` row (eTypeId=3, Stocktwits) must never
        appear among the parsed rating actions -- only the two real
        ``experts[]`` analysts (eTypeId=1) do."""
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        names = {a.analyst_name for a in snap.recent_rating_actions}
        assert names == {"Vijay Rakesh", "Vivek Arya"}
        assert "imbossmanjack" not in " ".join(names)

    def test_rating_id_2_maps_to_hold_and_pt_is_109(self):
        """Cross-referenced against the owner's own worked example: Vijay
        Rakesh, ratingId=2, priceTarget=109."""
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        rakesh = next(a for a in snap.recent_rating_actions if a.analyst_name == "Vijay Rakesh")
        assert rakesh.rating_id == 2
        assert rakesh.rating_label == "hold"
        assert rakesh.price_target == 109.0
        assert rakesh.firm == "Mizuho Securities"

    def test_rating_id_1_is_buy_confirmed_by_headline_text(self):
        """Vivek Arya's own row is headlined "Buy Rating Reaffirmed" under
        ratingId=1 -- direct textual confirmation, not inference."""
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        arya = next(a for a in snap.recent_rating_actions if a.analyst_name == "Vivek Arya")
        assert arya.rating_id == 1
        assert arya.rating_label == "buy"

    def test_action_id_5_maps_to_reiterate(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        for action in snap.recent_rating_actions:
            assert action.action_id == 5
            assert action.action_label == "reiterate"

    def test_analyst_stars_and_success_rate_carried_through(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        rakesh = next(a for a in snap.recent_rating_actions if a.analyst_name == "Vijay Rakesh")
        assert rakesh.analyst_stars == 5.0
        assert rakesh.analyst_success_rate == 0.0
        assert rakesh.included_in_consensus is True

    def test_last_eps_surprise_and_next_earnings_estimate(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.last_eps_surprise_pct == -48.0
        assert snap.next_earnings_estimate_eps == 0.38

    def test_fetched_at_and_session_are_stamped_from_arguments(self):
        snap = parse_analyst_snapshot(INTC_OVERVIEW, "INTC", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.symbol == "INTC"
        assert snap.as_of_session == SESSION
        assert snap.fetched_at == FETCHED_AT


class TestTeckBFixture:
    """Canadian-listing edge case: CAD price targets, a confirmed upgrade
    action, and a same-firm price-target raise with no rating change."""

    def test_price_target_currency_is_cad(self):
        snap = parse_analyst_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        assert snap.price_target_currency == "CAD"
        assert snap.price_target_mean == 90.84

    def test_action_id_3_maps_to_upgrade_confirmed_by_headline(self):
        """Brian MacArthur's row is headlined "upgraded to Outperform from
        Market Perform" -- direct textual confirmation of actionId=3."""
        snap = parse_analyst_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        macarthur = next(
            a for a in snap.recent_rating_actions if a.analyst_name == "Brian MacArthur"
        )
        assert macarthur.action_id == 3
        assert macarthur.action_label == "upgrade"
        assert macarthur.rating_id == 1
        assert macarthur.rating_label == "buy"
        assert macarthur.price_target == 93.0
        assert macarthur.old_price_target == 90.0

    def test_price_target_raise_with_unchanged_rating_is_reiterate(self):
        snap = parse_analyst_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        peterson = next(a for a in snap.recent_rating_actions if a.analyst_name == "Bill Peterson")
        assert peterson.action_id == 5
        assert peterson.action_label == "reiterate"
        assert peterson.price_target == 47.0
        assert peterson.old_price_target == 46.0

    def test_rating_actions_sorted_most_recent_first(self):
        snap = parse_analyst_snapshot(TECK_B_OVERVIEW, "TECK-B", SESSION, FETCHED_AT)
        assert snap is not None
        dates = [a.date for a in snap.recent_rating_actions]
        assert dates == sorted(dates, reverse=True)


class TestNoCoverage:
    def test_empty_overview_is_none(self):
        assert parse_analyst_snapshot({}, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_none_overview_is_none(self):
        assert parse_analyst_snapshot(None, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_overview_with_only_a_non_analyst_row_is_none(self):
        """A payload whose only expert-shaped data is a non-analyst
        (eTypeId=3) row, with empty consensus/pt blocks, must not produce a
        snapshot -- there is no real analyst coverage here at all."""
        overview = {
            "consensuses": [],
            "ptConsensus": [],
            "experts": [],
            "notRankedExperts": [
                {
                    "name": "somebody",
                    "firm": "StockTwits",
                    "eTypeId": 3,
                    "ratings": [{"ratingId": 3, "actionId": 8, "date": "2026-07-07T00:00:00"}],
                }
            ],
        }
        assert parse_analyst_snapshot(overview, "ZZZZ", SESSION, FETCHED_AT) is None

    def test_malformed_shapes_do_not_raise(self):
        """Defensive parsing throughout: wrong types at every level degrade
        to 'contributes nothing' rather than raising."""
        overview = {
            "consensuses": "not-a-list",
            "latestRankedConsensus": ["also-wrong"],
            "ptConsensus": {"not": "a-list"},
            "consensusOverTime": [{"date": None, "buy": 1}],
            "experts": [{"eTypeId": 1, "name": "x", "ratings": "not-a-list"}],
            "notRankedExperts": None,
        }
        # No coverage signal survives any of the malformed fields, so this
        # is expected to resolve to None -- the important assertion is that
        # it does not raise.
        assert parse_analyst_snapshot(overview, "ZZZZ", SESSION, FETCHED_AT) is None


class TestBoundedLengths:
    def test_consensus_over_time_is_capped(self):
        rows = [
            {
                "buy": 1,
                "hold": 1,
                "sell": 1,
                "date": (dt.date(2020, 1, 1) + dt.timedelta(days=30 * i)).isoformat(),
                "consensus": 3,
                "priceTarget": 10.0,
            }
            for i in range(CONSENSUS_OVER_TIME_MAX + 10)
        ]
        overview = {"consensusOverTime": rows, "consensuses": [], "ptConsensus": []}
        snap = parse_analyst_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.consensus_over_time) == CONSENSUS_OVER_TIME_MAX
        # The most recent points are kept, not the oldest.
        assert snap.consensus_over_time[-1].date == dt.date(
            2020, 1, 1
        ) + dt.timedelta(days=30 * (CONSENSUS_OVER_TIME_MAX + 9))

    def test_recent_rating_actions_is_capped(self):
        ratings = [
            {
                "ratingId": 1,
                "actionId": 5,
                "date": (dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat(),
                "priceTarget": 10.0,
            }
            for i in range(RECENT_RATING_ACTIONS_MAX + 10)
        ]
        overview = {
            "experts": [{"eTypeId": 1, "name": "Prolific Analyst", "firm": "F", "ratings": ratings}],
            "consensuses": [],
            "ptConsensus": [],
        }
        snap = parse_analyst_snapshot(overview, "MANY", SESSION, FETCHED_AT)
        assert snap is not None
        assert len(snap.recent_rating_actions) == RECENT_RATING_ACTIONS_MAX
