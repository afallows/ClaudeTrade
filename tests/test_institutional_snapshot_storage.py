"""Tests for the ``institutional_snapshots`` table: migration 012,
same-session storage replace, the batched ``latest_and_previous_snapshots``
read, and the pure ``institutional_delta`` diff function
(``claudetrade.data.institutional``).

Mirrors ``tests/test_analyst_snapshot_storage.py`` exactly, one table over.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, inspect

from claudetrade.data.institutional import (
    institutional_delta,
    latest_and_previous_snapshots,
    snapshot_to_row_fields,
)
from claudetrade.db.migrations import LATEST_VERSION, current_version, migrate
from claudetrade.db.models import InstitutionalSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import (
    HedgeFundHolderMove,
    HedgeFundHoldingQuarter,
    InsiderTransaction,
    InsiderTransactionMonth,
    InstitutionalSnapshot,
)
from claudetrade.providers.market.tipranks_institutional import institutional_score


def _snapshot(
    symbol: str,
    session: dt.date,
    *,
    net_flow: float | None = 500_000.0,
    confidence: float | None = 0.7,
    hf_sentiment: float | None = 0.6,
    holder_moves: list[HedgeFundHolderMove] | None = None,
    insider_transactions: list[InsiderTransaction] | None = None,
) -> InstitutionalSnapshot:
    return InstitutionalSnapshot(
        symbol=symbol,
        as_of_session=session,
        insider_monthly=[InsiderTransactionMonth(month=session.month, year=session.year)],
        insider_net_3m_usd=net_flow,
        insider_net_3m_usd_vendor=net_flow,
        insider_confidence_stock_score=confidence,
        num_of_insiders=10,
        recent_insider_transactions=insider_transactions or [],
        hedge_fund_sentiment=hf_sentiment,
        hedge_fund_holdings_by_quarter=[HedgeFundHoldingQuarter(date=session, holding_amount=1_000_000)],
        notable_holder_moves=holder_moves or [],
        market_cap_usd=1_000_000_000.0,
        fetched_at=dt.datetime.combine(session, dt.time(20, 0), tzinfo=dt.UTC),
    )


def _store(db: Database, snapshot: InstitutionalSnapshot) -> None:
    score_result = institutional_score(snapshot, snapshot.as_of_session)
    with db.session() as session:
        existing = (
            session.query(InstitutionalSnapshotRow)
            .filter_by(symbol=snapshot.symbol, session=snapshot.as_of_session)
            .one_or_none()
        )
        row = existing or InstitutionalSnapshotRow(
            symbol=snapshot.symbol, session=snapshot.as_of_session
        )
        for field_name, value in snapshot_to_row_fields(snapshot, score_result).items():
            setattr(row, field_name, value)
        if existing is None:
            session.add(row)


class TestInstitutionalSnapshotsMigration:
    def test_fresh_schema_already_has_the_table(self, memory_db: Database):
        insp = inspect(memory_db.engine)
        assert "institutional_snapshots" in insp.get_table_names()

    def test_current_version_reaches_latest(self, unmigrated_db: Database):
        assert current_version(unmigrated_db) == 0
        migrate(unmigrated_db)
        assert current_version(unmigrated_db) == LATEST_VERSION
        assert current_version(unmigrated_db) >= 12

    def test_migration_is_idempotent_when_rerun(self, unmigrated_db: Database):
        applied_first = migrate(unmigrated_db)
        assert 12 in applied_first
        applied_second = migrate(unmigrated_db)
        assert applied_second == []
        insp = inspect(unmigrated_db.engine)
        assert "institutional_snapshots" in insp.get_table_names()

    def test_migrate_to_target_11_then_up_to_latest_still_reaches_it(self, tmp_path):
        db = Database(f"sqlite:///{tmp_path / 'staged.db'}")
        migrate(db, target=11)
        assert current_version(db) == 11

        applied = migrate(db)
        assert 12 in applied
        assert current_version(db) == LATEST_VERSION
        insp = inspect(db.engine)
        assert "institutional_snapshots" in insp.get_table_names()
        db.dispose()


class TestSameSessionReplace:
    def test_re_storing_the_same_session_replaces_not_duplicates(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        session_date = dt.date(2026, 7, 30)
        _store(memory_db, _snapshot("INTC", session_date, net_flow=100_000.0))
        _store(memory_db, _snapshot("INTC", session_date, net_flow=-200_000.0))

        with memory_db.read_session() as s:
            rows = s.query(InstitutionalSnapshotRow).filter_by(symbol="INTC").all()
        assert len(rows) == 1
        assert rows[0].insider_net_3m_usd == -200_000.0

    def test_different_sessions_both_persist(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 20)))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 30)))

        with memory_db.read_session() as s:
            rows = s.query(InstitutionalSnapshotRow).filter_by(symbol="INTC").all()
        assert len(rows) == 2

    def test_score_columns_are_stored_alongside_raw_fields(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))
        session_date = dt.date(2026, 7, 30)
        _store(memory_db, _snapshot("INTC", session_date, net_flow=500_000.0, confidence=0.9))

        with memory_db.read_session() as s:
            row = s.query(InstitutionalSnapshotRow).filter_by(symbol="INTC").one()
        assert row.score is not None
        assert row.insider_subscore is not None
        assert row.insider_weight_applied > 0.0


class TestBatchedRead:
    def test_returns_latest_and_previous_per_symbol(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 10), net_flow=10.0))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 20), net_flow=20.0))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 30), net_flow=30.0))

        result = latest_and_previous_snapshots(memory_db, ["INTC"])
        latest, previous = result["INTC"]
        assert latest is not None and latest.as_of_session == dt.date(2026, 7, 30)
        assert previous is not None and previous.as_of_session == dt.date(2026, 7, 20)
        assert latest.insider_net_3m_usd == 30.0
        assert previous.insider_net_3m_usd == 20.0

    def test_symbol_with_no_snapshot_is_none_none(self, memory_db: Database):
        result = latest_and_previous_snapshots(memory_db, ["NOPE"])
        assert result["NOPE"] == (None, None)

    def test_symbol_with_exactly_one_session_has_no_previous(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 30)))

        latest, previous = latest_and_previous_snapshots(memory_db, ["INTC"])["INTC"]
        assert latest is not None
        assert previous is None

    def test_query_count_is_constant_regardless_of_symbol_count(self, memory_db: Database):
        """F26 discipline: two SELECTs total, whether asked about 1 symbol
        or 25."""
        symbols = [f"SYM{i}" for i in range(25)]
        with memory_db.session() as s:
            for sym in symbols:
                s.add(Security(symbol=sym, name=sym))
        for sym in symbols:
            _store(memory_db, _snapshot(sym, dt.date(2026, 7, 20)))
            _store(memory_db, _snapshot(sym, dt.date(2026, 7, 30)))

        statements: list[str] = []

        def _record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(memory_db.engine, "before_cursor_execute", _record)
        try:
            result = latest_and_previous_snapshots(memory_db, symbols)
        finally:
            event.remove(memory_db.engine, "before_cursor_execute", _record)

        selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
        assert len(selects) == 2, f"expected exactly 2 SELECTs, got {len(selects)}"
        assert len(result) == 25
        assert all(latest is not None and previous is not None for latest, previous in result.values())


class TestInstitutionalDelta:
    def test_no_previous_session_yields_no_changes(self):
        current = _snapshot("INTC", dt.date(2026, 7, 30))
        delta = institutional_delta(current, None)
        assert delta.has_previous is False
        assert delta.previous_session is None
        assert delta.net_flow_change is None
        assert delta.hedge_fund_sentiment_change is None
        assert delta.new_holder_moves == []
        assert delta.new_insider_transactions == []

    def test_net_flow_and_sentiment_changes(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), net_flow=100_000.0, hf_sentiment=0.4)
        current = _snapshot("INTC", dt.date(2026, 7, 30), net_flow=300_000.0, hf_sentiment=0.6)
        delta = institutional_delta(current, previous)
        assert delta.has_previous is True
        assert delta.net_flow_change == 200_000.0
        assert delta.hedge_fund_sentiment_change == pytest.approx(0.2)

    def test_changes_are_none_when_either_side_missing(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), net_flow=None, hf_sentiment=None)
        current = _snapshot("INTC", dt.date(2026, 7, 30), net_flow=300_000.0, hf_sentiment=0.6)
        delta = institutional_delta(current, previous)
        assert delta.net_flow_change is None
        assert delta.hedge_fund_sentiment_change is None

    def test_new_holder_moves_are_those_dated_after_previous_session(self):
        previous_session = dt.date(2026, 7, 20)
        old_move = HedgeFundHolderMove(
            manager_name="Old Manager",
            institution_name="Old Fund",
            effective_date=dt.date(2026, 7, 10),
            change_amount=100.0,
        )
        new_move = HedgeFundHolderMove(
            manager_name="New Manager",
            institution_name="New Fund",
            effective_date=dt.date(2026, 7, 28),
            change_amount=200.0,
        )
        previous = _snapshot("INTC", previous_session)
        current = _snapshot("INTC", dt.date(2026, 7, 30), holder_moves=[new_move, old_move])
        delta = institutional_delta(current, previous)
        assert delta.new_holder_moves == [new_move]

    def test_new_insider_transactions_are_those_dated_after_previous_session(self):
        previous_session = dt.date(2026, 7, 20)
        old_txn = InsiderTransaction(name="Old Insider", r_date=dt.date(2026, 7, 10))
        new_txn = InsiderTransaction(name="New Insider", r_date=dt.date(2026, 7, 28))
        previous = _snapshot("INTC", previous_session)
        current = _snapshot(
            "INTC", dt.date(2026, 7, 30), insider_transactions=[new_txn, old_txn]
        )
        delta = institutional_delta(current, previous)
        assert delta.new_insider_transactions == [new_txn]

    def test_new_holder_moves_and_transactions_empty_with_no_previous(self):
        move = HedgeFundHolderMove(
            manager_name="M", institution_name="I", effective_date=dt.date(2026, 7, 28)
        )
        txn = InsiderTransaction(name="X", r_date=dt.date(2026, 7, 28))
        current = _snapshot(
            "INTC", dt.date(2026, 7, 30), holder_moves=[move], insider_transactions=[txn]
        )
        delta = institutional_delta(current, None)
        assert delta.new_holder_moves == []
        assert delta.new_insider_transactions == []
