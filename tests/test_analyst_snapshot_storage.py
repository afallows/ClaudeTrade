"""Tests for the ``analyst_snapshots`` table: migration 011, same-session
storage replace, the batched ``latest_and_previous_snapshots`` read, and the
pure ``analyst_delta`` diff function (``claudetrade.data.analyst``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import event, inspect

from claudetrade.data.analyst import (
    analyst_delta,
    latest_and_previous_snapshots,
    snapshot_to_row_fields,
)
from claudetrade.db.migrations import LATEST_VERSION, current_version, migrate
from claudetrade.db.models import AnalystSnapshotRow, Security
from claudetrade.db.session import Database
from claudetrade.domain import AnalystRatingAction, AnalystSnapshot


def _snapshot(
    symbol: str,
    session: dt.date,
    *,
    buy: int = 5,
    hold: int = 10,
    sell: int = 1,
    pt_mean: float | None = 100.0,
    rating: int | None = 3,
    actions: list[AnalystRatingAction] | None = None,
) -> AnalystSnapshot:
    return AnalystSnapshot(
        symbol=symbol,
        as_of_session=session,
        consensus_rating=rating,
        buy_count=buy,
        hold_count=hold,
        sell_count=sell,
        consensus_rate=50.0,
        price_target_mean=pt_mean,
        price_target_high=(pt_mean + 20.0) if pt_mean is not None else None,
        price_target_low=(pt_mean - 20.0) if pt_mean is not None else None,
        price_target_currency="USD",
        analyst_count=buy + hold + sell,
        recent_rating_actions=actions or [],
        last_eps_surprise_pct=-5.0,
        next_earnings_estimate_eps=1.23,
        fetched_at=dt.datetime.combine(session, dt.time(20, 0), tzinfo=dt.UTC),
    )


def _store(db: Database, snapshot: AnalystSnapshot) -> None:
    with db.session() as session:
        existing = (
            session.query(AnalystSnapshotRow)
            .filter_by(symbol=snapshot.symbol, session=snapshot.as_of_session)
            .one_or_none()
        )
        row = existing or AnalystSnapshotRow(symbol=snapshot.symbol, session=snapshot.as_of_session)
        for field_name, value in snapshot_to_row_fields(snapshot).items():
            setattr(row, field_name, value)
        if existing is None:
            session.add(row)


class TestAnalystSnapshotsMigration:
    def test_fresh_schema_already_has_the_table(self, memory_db: Database):
        insp = inspect(memory_db.engine)
        assert "analyst_snapshots" in insp.get_table_names()

    def test_current_version_reaches_latest(self, unmigrated_db: Database):
        assert current_version(unmigrated_db) == 0
        migrate(unmigrated_db)
        assert current_version(unmigrated_db) == LATEST_VERSION
        assert current_version(unmigrated_db) >= 11

    def test_migration_is_idempotent_when_rerun(self, unmigrated_db: Database):
        applied_first = migrate(unmigrated_db)
        assert 11 in applied_first
        applied_second = migrate(unmigrated_db)
        assert applied_second == []
        insp = inspect(unmigrated_db.engine)
        assert "analyst_snapshots" in insp.get_table_names()

    def test_migrate_to_target_10_then_up_to_latest_still_reaches_it(self, tmp_path):
        """Applying migrations up to version 10 and then continuing to
        latest reaches version 11 and leaves the table present -- exercises
        ``_m011_analyst_snapshots``'s own ``checkfirst=True`` no-op path,
        since ``_m001_create_schema``'s ``create_all`` already reflects the
        current ORM model (including ``AnalystSnapshotRow``) regardless of
        which migration version create_all itself ran under."""
        db = Database(f"sqlite:///{tmp_path / 'staged.db'}")
        migrate(db, target=10)
        assert current_version(db) == 10

        applied = migrate(db)
        assert 11 in applied
        assert current_version(db) == LATEST_VERSION
        insp = inspect(db.engine)
        assert "analyst_snapshots" in insp.get_table_names()
        db.dispose()


class TestSameSessionReplace:
    def test_re_storing_the_same_session_replaces_not_duplicates(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        session_date = dt.date(2026, 7, 30)
        _store(memory_db, _snapshot("INTC", session_date, buy=5, hold=10, sell=1))
        _store(memory_db, _snapshot("INTC", session_date, buy=7, hold=23, sell=2))

        with memory_db.read_session() as s:
            rows = s.query(AnalystSnapshotRow).filter_by(symbol="INTC").all()
        assert len(rows) == 1
        assert (rows[0].buy_count, rows[0].hold_count, rows[0].sell_count) == (7, 23, 2)

    def test_different_sessions_both_persist(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 20)))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 30)))

        with memory_db.read_session() as s:
            rows = s.query(AnalystSnapshotRow).filter_by(symbol="INTC").all()
        assert len(rows) == 2


class TestBatchedRead:
    def test_returns_latest_and_previous_per_symbol(self, memory_db: Database):
        with memory_db.session() as s:
            s.add(Security(symbol="INTC", name="Intel"))

        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 10), pt_mean=90.0))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 20), pt_mean=100.0))
        _store(memory_db, _snapshot("INTC", dt.date(2026, 7, 30), pt_mean=119.11))

        result = latest_and_previous_snapshots(memory_db, ["INTC"])
        latest, previous = result["INTC"]
        assert latest is not None and latest.as_of_session == dt.date(2026, 7, 30)
        assert previous is not None and previous.as_of_session == dt.date(2026, 7, 20)
        assert latest.price_target_mean == 119.11
        assert previous.price_target_mean == 100.0

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
        or 50 -- never one query per symbol."""
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


class TestAnalystDelta:
    def test_no_previous_session_yields_no_changes(self):
        current = _snapshot("INTC", dt.date(2026, 7, 30))
        delta = analyst_delta(current, None)
        assert delta.has_previous is False
        assert delta.previous_session is None
        assert delta.buy_count_change is None
        assert delta.coverage_change is None
        assert delta.price_target_mean_change is None
        assert delta.new_rating_actions == []

    def test_count_and_coverage_changes(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), buy=5, hold=20, sell=2)
        current = _snapshot("INTC", dt.date(2026, 7, 30), buy=7, hold=23, sell=2)
        delta = analyst_delta(current, previous)
        assert delta.has_previous is True
        assert delta.buy_count_change == 2
        assert delta.hold_count_change == 3
        assert delta.sell_count_change == 0
        assert delta.coverage_change == current.analyst_count - previous.analyst_count

    def test_price_target_mean_change_and_pct(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), pt_mean=100.0)
        current = _snapshot("INTC", dt.date(2026, 7, 30), pt_mean=110.0)
        delta = analyst_delta(current, previous)
        assert delta.price_target_mean_change == 10.0
        assert delta.price_target_mean_change_pct == 10.0

    def test_price_target_change_is_none_when_either_side_missing(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), pt_mean=None)
        current = _snapshot("INTC", dt.date(2026, 7, 30), pt_mean=110.0)
        delta = analyst_delta(current, previous)
        assert delta.price_target_mean_change is None
        assert delta.price_target_mean_change_pct is None

    def test_consensus_rating_change_none_when_either_side_missing(self):
        previous = _snapshot("INTC", dt.date(2026, 7, 20), rating=None)
        current = _snapshot("INTC", dt.date(2026, 7, 30), rating=3)
        delta = analyst_delta(current, previous)
        assert delta.consensus_rating_change is None

    def test_new_rating_actions_are_those_dated_after_previous_session(self):
        previous_session = dt.date(2026, 7, 20)
        old_action = AnalystRatingAction(
            date=dt.date(2026, 7, 15), firm="F1", analyst_name="A1", rating_id=1
        )
        new_action = AnalystRatingAction(
            date=dt.date(2026, 7, 28), firm="F2", analyst_name="A2", rating_id=1
        )
        previous = _snapshot("INTC", previous_session)
        current = _snapshot(
            "INTC", dt.date(2026, 7, 30), actions=[new_action, old_action]
        )
        delta = analyst_delta(current, previous)
        assert delta.new_rating_actions == [new_action]

    def test_new_rating_actions_empty_with_no_previous(self):
        action = AnalystRatingAction(
            date=dt.date(2026, 7, 28), firm="F", analyst_name="A", rating_id=1
        )
        current = _snapshot("INTC", dt.date(2026, 7, 30), actions=[action])
        delta = analyst_delta(current, None)
        assert delta.new_rating_actions == []
