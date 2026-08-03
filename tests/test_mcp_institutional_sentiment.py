"""Tests for the ``get_institutional_sentiment`` MCP tool
(``claudetrade.mcp_server``).

Mirrors ``tests/test_mcp_analyst_sentiment.py`` exactly, one tool over: a
dedicated module kept separate from ``tests/test_mcp_server.py`` to minimise
the diff against that large shared module. ``EXPECTED_TOOL_NAMES`` there
gained ``"get_institutional_sentiment"`` -- its own registration-completeness
assertion would otherwise fail the moment this tool exists -- but no test
body in that file changes.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' package is not installed")

from claudetrade import mcp_server
from claudetrade.config import AppConfig
from claudetrade.data.institutional import snapshot_to_row_fields
from claudetrade.db.models import InstitutionalSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import (
    HedgeFundHolderMove,
    HedgeFundHoldingQuarter,
    InsiderTransaction,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)
from claudetrade.pipeline import Pipeline
from claudetrade.providers.market.tipranks_institutional import institutional_score


@pytest.fixture
def pipeline(tmp_app_config: AppConfig, tmp_db: Database) -> Pipeline:
    return Pipeline(tmp_app_config, tmp_db)


def _store_snapshot(db: Database, snapshot: InstitutionalSnapshot) -> None:
    score_result = institutional_score(snapshot, snapshot.as_of_session)
    with db.session() as session:
        if session.get(Security, snapshot.symbol) is None:
            session.add(Security(symbol=snapshot.symbol, name=snapshot.symbol))
        row = InstitutionalSnapshotRow(symbol=snapshot.symbol, session=snapshot.as_of_session)
        for field_name, value in snapshot_to_row_fields(snapshot, score_result).items():
            setattr(row, field_name, value)
        session.add(row)


def _snapshot(symbol: str, session: dt.date, **overrides) -> InstitutionalSnapshot:
    defaults = {
        "symbol": symbol,
        "as_of_session": session,
        "insider_monthly": [InsiderTransactionMonth(month=session.month, year=session.year)],
        "insider_net_3m_usd": 500_000.0,
        "insider_net_3m_usd_vendor": 480_000.0,
        "insider_confidence_stock_score": 0.7,
        "num_of_insiders": 10,
        "hedge_fund_sentiment": 0.6,
        "hedge_fund_holdings_by_quarter": [
            HedgeFundHoldingQuarter(date=session, holding_amount=1_000_000)
        ],
        "market_cap_usd": 1_000_000_000.0,
        "fetched_at": dt.datetime.combine(session, dt.time(20, 0), tzinfo=dt.UTC),
    }
    defaults.update(overrides)
    return InstitutionalSnapshot(**defaults)


def test_unknown_symbol_is_an_honest_empty_result_not_an_error(pipeline: Pipeline) -> None:
    result = mcp_server.get_institutional_sentiment(pipeline, "nope")
    assert result["symbol"] == "NOPE"
    assert result["available"] is False
    assert result["snapshot"] is None
    assert result["delta"] is None
    assert "No stored institutional-sentiment snapshot" in result["note"]


def test_happy_path_with_no_previous_session(pipeline: Pipeline, tmp_db: Database) -> None:
    session_date = dt.date(2026, 7, 30)
    txn = InsiderTransaction(
        name="David Zinsner",
        is_officer=True,
        officer_title="EVP, CFO",
        estimated_shares_value=249985.0,
        r_date=dt.date(2026, 1, 27),
        link="http://sec.gov/example",
    )
    _store_snapshot(
        tmp_db, _snapshot("INTC", session_date, recent_insider_transactions=[txn])
    )

    result = mcp_server.get_institutional_sentiment(pipeline, "intc")

    assert result["symbol"] == "INTC"
    assert result["available"] is True
    assert result["note"] is None
    snap = result["snapshot"]
    assert snap["as_of_session"] == session_date.isoformat()
    assert snap["insider_net_3m_usd"] == 500_000.0
    assert snap["insider_net_3m_usd_vendor"] == 480_000.0
    assert snap["num_of_insiders"] == 10
    assert len(snap["recent_insider_transactions"]) == 1
    assert snap["recent_insider_transactions"][0]["name"] == "David Zinsner"
    assert snap["score"] is not None
    assert snap["insider_subscore"] is not None

    delta = result["delta"]
    assert delta["has_previous"] is False
    assert delta["previous_session"] is None
    assert delta["net_flow_change"] is None
    assert delta["score_change"] is None
    assert delta["new_holder_moves"] == []
    assert delta["new_insider_transactions"] == []


def test_happy_path_with_previous_session_reports_deltas(
    pipeline: Pipeline, tmp_db: Database
) -> None:
    with tmp_db.session() as session:
        session.add(Security(symbol="INTC", name="Intel"))
    _store_snapshot(
        tmp_db,
        _snapshot(
            "INTC", dt.date(2026, 7, 20), insider_net_3m_usd=100_000.0, hedge_fund_sentiment=0.4
        ),
    )
    new_move = HedgeFundHolderMove(
        manager_name="Stanley Druckenmiller",
        institution_name="Duquesne Family Office LLC",
        effective_date=dt.date(2026, 7, 28),
        change_amount=411400.0,
        stars=2.26,
    )
    _store_snapshot(
        tmp_db,
        _snapshot(
            "INTC",
            dt.date(2026, 7, 30),
            insider_net_3m_usd=300_000.0,
            hedge_fund_sentiment=0.6,
            notable_holder_moves=[new_move],
        ),
    )

    result = mcp_server.get_institutional_sentiment(pipeline, "INTC")

    delta = result["delta"]
    assert delta["has_previous"] is True
    assert delta["previous_session"] == "2026-07-20"
    assert delta["net_flow_change"] == pytest.approx(200_000.0)
    assert delta["hedge_fund_sentiment_change"] == pytest.approx(0.2)
    assert len(delta["new_holder_moves"]) == 1
    assert delta["new_holder_moves"][0]["manager_name"] == "Stanley Druckenmiller"
    # Both sessions had usable data, so a real score delta is reported (not
    # merely present-vs-absent -- see get_institutional_sentiment's own
    # docstring for why this is a recomputation, not a stored-row read).
    assert delta["score_change"] is not None


def test_symbol_is_normalised_to_uppercase(pipeline: Pipeline, tmp_db: Database) -> None:
    _store_snapshot(tmp_db, _snapshot("INTC", dt.date(2026, 7, 30)))
    result = mcp_server.get_institutional_sentiment(pipeline, "  intc  ")
    assert result["symbol"] == "INTC"
    assert result["available"] is True


def test_tool_is_registered_read_only_and_bounded(
    pipeline: Pipeline, tmp_app_config: AppConfig
) -> None:
    """The FastMCP wiring itself: built, present, described as read-only
    (no "WRITE" marker), and wrapped in the F26 watchdog like every other
    tool -- matching ``test_mcp_server.py``'s own server-construction
    assertions."""
    server = mcp_server.build_server(pipeline, tmp_app_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}

    assert "get_institutional_sentiment" in tools
    tool = tools["get_institutional_sentiment"]
    assert tool.description.strip()
    assert "read-only" in tool.description.lower()
    assert "WRITE" not in tool.description
    assert tool.is_async
    assert set(tool.parameters.get("properties", {})) == {"symbol"}
