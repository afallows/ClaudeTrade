"""Tests for the ``get_analyst_sentiment`` MCP tool (``claudetrade.mcp_server``).

A dedicated module rather than folding these cases into
``tests/test_mcp_server.py``: same fixtures and conventions
(``pytest.importorskip("mcp")``, a local ``pipeline`` fixture over
``tmp_app_config``/``tmp_db``), kept in its own file to minimise the diff
against that large shared module. ``EXPECTED_TOOL_NAMES`` there still had to
gain ``"get_analyst_sentiment"`` -- its own registration-completeness
assertion would otherwise fail the moment this tool exists -- but no test
body in that file changes.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' package is not installed")

from claudetrade import mcp_server
from claudetrade.config import AppConfig
from claudetrade.data.analyst import snapshot_to_row_fields
from claudetrade.db.models import AnalystSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import AnalystRatingAction, AnalystSnapshot
from claudetrade.pipeline import Pipeline


@pytest.fixture
def pipeline(tmp_app_config: AppConfig, tmp_db: Database) -> Pipeline:
    return Pipeline(tmp_app_config, tmp_db)


def _store_snapshot(db: Database, snapshot: AnalystSnapshot) -> None:
    with db.session() as session:
        if session.get(Security, snapshot.symbol) is None:
            session.add(Security(symbol=snapshot.symbol, name=snapshot.symbol))
        row = AnalystSnapshotRow(symbol=snapshot.symbol, session=snapshot.as_of_session)
        for field_name, value in snapshot_to_row_fields(snapshot).items():
            setattr(row, field_name, value)
        session.add(row)


def _snapshot(symbol: str, session: dt.date, **overrides) -> AnalystSnapshot:
    defaults = {
        "symbol": symbol,
        "as_of_session": session,
        "consensus_rating": 3,
        "buy_count": 7,
        "hold_count": 23,
        "sell_count": 2,
        "consensus_rate": 33.0,
        "price_target_mean": 119.11,
        "price_target_high": 200.0,
        "price_target_low": 80.0,
        "price_target_currency": "USD",
        "analyst_count": 32,
        "last_eps_surprise_pct": -48.0,
        "next_earnings_estimate_eps": 0.38,
        "fetched_at": dt.datetime.combine(session, dt.time(20, 0), tzinfo=dt.UTC),
    }
    defaults.update(overrides)
    return AnalystSnapshot(**defaults)


def test_unknown_symbol_is_an_honest_empty_result_not_an_error(pipeline: Pipeline) -> None:
    result = mcp_server.get_analyst_sentiment(pipeline, "nope")
    assert result["symbol"] == "NOPE"
    assert result["available"] is False
    assert result["snapshot"] is None
    assert result["delta"] is None
    assert "No stored analyst-sentiment snapshot" in result["note"]


def test_happy_path_with_no_previous_session(pipeline: Pipeline, tmp_db: Database) -> None:
    session_date = dt.date(2026, 7, 30)
    action = AnalystRatingAction(
        date=dt.date(2026, 7, 28),
        firm="Mizuho Securities",
        analyst_name="Vijay Rakesh",
        rating_id=2,
        rating_label="hold",
        action_id=5,
        action_label="reiterate",
        price_target=109.0,
        analyst_stars=5.0,
        analyst_success_rate=0.0,
        included_in_consensus=True,
    )
    _store_snapshot(tmp_db, _snapshot("INTC", session_date, recent_rating_actions=[action]))

    result = mcp_server.get_analyst_sentiment(pipeline, "intc")

    assert result["symbol"] == "INTC"
    assert result["available"] is True
    assert result["note"] is None
    snap = result["snapshot"]
    assert snap["as_of_session"] == session_date.isoformat()
    assert (snap["buy_count"], snap["hold_count"], snap["sell_count"]) == (7, 23, 2)
    assert snap["analyst_count"] == 32
    assert snap["price_target_mean"] == 119.11
    assert snap["price_target_currency"] == "USD"
    assert len(snap["recent_rating_actions"]) == 1
    assert snap["recent_rating_actions"][0]["analyst_name"] == "Vijay Rakesh"
    assert snap["recent_rating_actions"][0]["rating_label"] == "hold"
    assert snap["recent_rating_actions"][0]["action_label"] == "reiterate"

    delta = result["delta"]
    assert delta["has_previous"] is False
    assert delta["previous_session"] is None
    assert delta["buy_count_change"] is None
    assert delta["new_rating_actions"] == []


def test_happy_path_with_previous_session_reports_deltas(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    with tmp_db.session() as session:
        session.add(Security(symbol="INTC", name="Intel"))
    _store_snapshot(
        tmp_db,
        _snapshot(
            "INTC", dt.date(2026, 7, 20), buy_count=5, hold_count=20, sell_count=2,
            analyst_count=27, price_target_mean=100.0,
        ),
    )
    new_action = AnalystRatingAction(
        date=dt.date(2026, 7, 28), firm="Mizuho", analyst_name="Vijay Rakesh", rating_id=2,
    )
    _store_snapshot(
        tmp_db,
        _snapshot(
            "INTC", dt.date(2026, 7, 30), buy_count=7, hold_count=23, sell_count=2,
            analyst_count=32, price_target_mean=119.11, recent_rating_actions=[new_action],
        ),
    )

    result = mcp_server.get_analyst_sentiment(pipeline, "INTC")

    delta = result["delta"]
    assert delta["has_previous"] is True
    assert delta["previous_session"] == "2026-07-20"
    assert delta["buy_count_change"] == 2
    assert delta["hold_count_change"] == 3
    assert delta["coverage_change"] == 5
    assert delta["price_target_mean_change"] == pytest.approx(19.11)
    assert len(delta["new_rating_actions"]) == 1
    assert delta["new_rating_actions"][0]["analyst_name"] == "Vijay Rakesh"


def test_symbol_is_normalised_to_uppercase(pipeline: Pipeline, tmp_db: Database) -> None:
    _store_snapshot(tmp_db, _snapshot("INTC", dt.date(2026, 7, 30)))
    result = mcp_server.get_analyst_sentiment(pipeline, "  intc  ")
    assert result["symbol"] == "INTC"
    assert result["available"] is True


def test_tool_is_registered_read_only_and_bounded(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """The FastMCP wiring itself: built, present, described as read-only
    (no "WRITE" marker), and wrapped in the F26 watchdog like every other
    tool -- matching ``test_mcp_server.py``'s own server-construction
    assertions (``server._tool_manager.list_tools()``, ``tool.is_async``)."""
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    assert "get_analyst_sentiment" in tools
    tool = tools["get_analyst_sentiment"]
    assert tool.description.strip()
    assert "read-only" in tool.description.lower()
    assert "WRITE" not in tool.description
    assert tool.is_async
    assert set(tool.parameters.get("properties", {})) == {"symbol"}
