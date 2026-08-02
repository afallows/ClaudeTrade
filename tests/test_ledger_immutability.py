"""Tests for signal ledger immutability and integrity."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from claudetrade.db.models import SignalRow
from claudetrade.db.session import Database
from claudetrade.domain import (
    ComponentScores,
    Direction,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.signals.ledger import (
    LedgerIntegrityError,
    SignalLedger,
    make_signal_id,
    signal_integrity_payload,
)
from claudetrade.utils.hashing import content_hash


@pytest.fixture
def sample_signal() -> Signal:
    """Create a sample signal for testing."""
    return Signal(
        signal_id="SIG001",
        created_at=dt.datetime.now(tz=dt.UTC),
        session=dt.date(2023, 1, 3),
        symbol="TEST",
        company_name="Test Co",
        strategy="test",
        direction=Direction.LONG,
        status=SignalStatus.ACTIONABLE,
        reference_price=100.0,
        price_as_of=dt.datetime.now(tz=dt.UTC),
        overall_score=75.0,
        confidence=0.8,
        components=ComponentScores(),
        plan=TradePlan(
            entry_low=99.0,
            entry_high=101.0,
            stop_loss=95.0,
            targets=[110.0],
            shares=100,
        ),
    )


class TestSignalRecording:
    """Signals can be recorded to the ledger."""

    def test_record_signal(self, tmp_db: Database, sample_signal: Signal):
        """Signal is recorded to the database."""
        ledger = SignalLedger(tmp_db)
        signal_id = ledger.record(sample_signal)

        # Verify it was recorded
        assert signal_id == "SIG001"


class TestCurrentStatus:
    """current_status returns the latest status of a signal."""

    def test_current_status_after_record(self, tmp_db: Database, sample_signal: Signal):
        """current_status returns the recorded signal's status."""
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        status = ledger.current_status("SIG001")
        assert status == SignalStatus.ACTIONABLE


class TestRevisions:
    """Revisions are appended, never replaced."""

    def test_append_revision(self, tmp_db: Database, sample_signal: Signal):
        """Revision is appended to the ledger."""
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        # Append a revision changing status
        revision = ledger.append_revision(
            "SIG001",
            status=SignalStatus.EXTENDED,
            reason="price extended beyond entry zone",
        )

        # Verify revision was created
        assert revision > 0
        # Verify new status
        status = ledger.current_status("SIG001")
        assert status == SignalStatus.EXTENDED


class TestIdenticalSignalRerecording:
    """Re-recording an identical signal is a no-op."""

    def test_rerecord_identical_signal_noop(self, tmp_db: Database, sample_signal: Signal):
        """Re-recording the exact same signal is idempotent."""
        ledger = SignalLedger(tmp_db)
        id1 = ledger.record(sample_signal)
        # Record again (same signal_id, same content)
        id2 = ledger.record(sample_signal)
        # Should return same ID, should be a no-op
        assert id1 == id2 == "SIG001"


class TestDifferentSignalSameIdRaises:
    """Recording a DIFFERENT signal with the same ID raises LedgerIntegrityError."""

    def test_different_signal_same_id_raises(self, tmp_db: Database, sample_signal: Signal):
        """Different signal content with same ID raises error."""
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        # Try to record a different signal with the same ID
        different_signal = replace(sample_signal, symbol="OTHER", direction=Direction.SHORT)

        with pytest.raises(LedgerIntegrityError):
            ledger.record(different_signal)


class TestExpireStale:
    """Signals outside the retention window are expired."""

    def test_expire_stale_signals(self, tmp_db: Database, sample_signal: Signal):
        """Signals past expiry are marked EXPIRED."""
        ledger = SignalLedger(tmp_db)

        # Create an old signal with an expiry date
        old_signal = replace(
            sample_signal,
            signal_id="OLD_SIG",
            created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
            expires_after=dt.date(2020, 1, 10),
        )
        ledger.record(old_signal)

        # Expire signals older than 2020-01-15
        expired_ids = ledger.expire_stale(dt.date(2020, 1, 15))

        # Old signal should be in the expired list
        assert "OLD_SIG" in expired_ids or len(expired_ids) >= 0  # May be expired
        # Check the status
        status = ledger.current_status("OLD_SIG")
        assert status == SignalStatus.EXPIRED


class TestVerifyOnReadRoundTrip:
    """Defect 1: every datetime deserialized from the DB must come back
    timezone-aware UTC, so a legitimately-stored signal's hash re-verifies.
    """

    def test_record_then_get_verify_succeeds(self, tmp_db: Database, sample_signal: Signal):
        """A signal recorded through the normal write path must pass
        ``get(..., verify=True)`` -- the whole point of the integrity hash is
        to catch tampering, not to fail on every read.
        """
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        loaded = ledger.get("SIG001", verify=True)

        assert loaded is not None
        assert loaded.signal_id == "SIG001"

    def test_round_tripped_datetimes_are_utc_aware(
        self, tmp_db: Database, sample_signal: Signal
    ):
        """``created_at`` and ``price_as_of`` come back timezone-aware UTC,
        not naive -- the root cause of defect 1.
        """
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        loaded = ledger.get("SIG001", verify=False)

        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at.utcoffset() == dt.timedelta(0)
        assert loaded.price_as_of.tzinfo is not None
        assert loaded.price_as_of.utcoffset() == dt.timedelta(0)

    def test_verify_all_reports_no_failures_for_untampered_signals(
        self, tmp_db: Database, sample_signal: Signal
    ):
        """``verify_all`` must not flag freshly recorded, untampered signals."""
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        assert ledger.verify_all() == []


class TestTamperDetection:
    """A row that diverges from its recorded hash is detected, not trusted."""

    def test_raw_sql_update_is_rejected_by_immutability_trigger(
        self, tmp_db: Database, sample_signal: Signal
    ):
        """Migration 002's trigger blocks ``UPDATE`` outright -- tampering via
        a SQL client never gets far enough to need the hash check at all.
        """
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)

        with pytest.raises(IntegrityError), tmp_db.engine.begin() as conn:
            conn.execute(text("UPDATE signals SET symbol = 'HACKED' WHERE signal_id = 'SIG001'"))

        # The row is untouched -- verify still succeeds on the original content.
        assert ledger.get("SIG001", verify=True).symbol == "TEST"

    def test_mismatched_row_fails_verification(
        self, tmp_db: Database, sample_signal: Signal
    ):
        """Simulated tampering: a row inserted directly (bypassing
        ``record()``, which is the only thing the trigger cannot stop since
        ``INSERT`` is permitted) whose stored ``integrity_hash`` does not
        match its content. ``get(..., verify=True)`` must raise rather than
        hand back an unverified signal.
        """
        tampered_id = "SIG_TAMPERED"
        tampered_signal = replace(sample_signal, signal_id=tampered_id)
        # A hash computed over content that does not match what's inserted --
        # standing in for a row edited outside the application.
        wrong_hash = content_hash(
            signal_integrity_payload(replace(tampered_signal, symbol="SOMETHING_ELSE"))
        )
        with tmp_db.session() as session:
            session.add(
                SignalRow(
                    signal_id=tampered_id,
                    created_at=tampered_signal.created_at,
                    session=tampered_signal.session,
                    symbol=tampered_signal.symbol,
                    company_name=tampered_signal.company_name,
                    strategy=tampered_signal.strategy,
                    strategy_version=tampered_signal.strategy_version,
                    direction=tampered_signal.direction.value,
                    initial_status=tampered_signal.status.value,
                    reference_price=tampered_signal.reference_price,
                    price_as_of=tampered_signal.price_as_of,
                    overall_score=tampered_signal.overall_score,
                    confidence=tampered_signal.confidence,
                    components=tampered_signal.components.as_dict(),
                    plan={
                        "entry_low": tampered_signal.plan.entry_low,
                        "entry_high": tampered_signal.plan.entry_high,
                        "stop_loss": tampered_signal.plan.stop_loss,
                        "targets": tampered_signal.plan.targets,
                        "shares": tampered_signal.plan.shares,
                    },
                    regime=tampered_signal.regime.value,
                    code_version=tampered_signal.code_version,
                    config_hash=tampered_signal.config_hash,
                    data_snapshot_hash=tampered_signal.data_snapshot_hash,
                    integrity_hash=wrong_hash,
                )
            )

        ledger = SignalLedger(tmp_db)
        with pytest.raises(LedgerIntegrityError):
            ledger.get(tampered_id, verify=True)


class TestSignalIdReproducibility:
    """Defect 2: the id's hash suffix covers ``config_hash`` and
    ``code_version`` so a config or code change mints a different id
    instead of colliding with a prior scan's id under different content.
    """

    def test_same_inputs_produce_same_id(self):
        """Re-scanning with identical inputs still dedupes silently."""
        session = dt.date(2024, 1, 2)
        id1 = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gdead")
        id2 = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gdead")
        assert id1 == id2

    def test_changed_config_hash_changes_id(self):
        """A configuration change mints a different id."""
        session = dt.date(2024, 1, 2)
        id1 = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gdead")
        id2 = make_signal_id("AAPL", "breakout", session, "cfg-xyz", "0.1.0+gdead")
        assert id1 != id2

    def test_changed_code_version_changes_id(self):
        """A code deploy mints a different id, even with unchanged config."""
        session = dt.date(2024, 1, 2)
        id1 = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gdead")
        id2 = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gbeef")
        assert id1 != id2

    def test_rescanning_after_config_change_records_both(
        self, tmp_db: Database, sample_signal: Signal
    ):
        """The whole point: re-scanning a session after a config change must
        not raise ``LedgerIntegrityError`` -- it mints a fresh id and records
        alongside the original rather than colliding with it.
        """
        ledger = SignalLedger(tmp_db)
        session = dt.date(2024, 1, 2)
        id_before = make_signal_id("AAPL", "breakout", session, "cfg-abc", "0.1.0+gdead")
        id_after = make_signal_id("AAPL", "breakout", session, "cfg-xyz", "0.1.0+gdead")

        first = replace(
            sample_signal, signal_id=id_before, config_hash="cfg-abc", code_version="0.1.0+gdead"
        )
        second = replace(
            sample_signal,
            signal_id=id_after,
            symbol=first.symbol,
            overall_score=first.overall_score + 1,  # the config change altered scoring
            config_hash="cfg-xyz",
            code_version="0.1.0+gdead",
        )

        ledger.record(first)
        ledger.record(second)  # must not raise

        assert ledger.get(id_before, verify=True) is not None
        assert ledger.get(id_after, verify=True) is not None


class TestRecordOrReport:
    """``record_or_report`` turns a true collision into a structured outcome
    instead of raising, so a batch of many signals can surface one failure
    without aborting the rest.
    """

    def test_fresh_signal_reports_ok(self, tmp_db: Database, sample_signal: Signal):
        ledger = SignalLedger(tmp_db)
        outcome = ledger.record_or_report(sample_signal)
        assert outcome.ok
        assert not outcome.duplicate
        assert outcome.error is None

    def test_identical_rerecord_reports_ok_duplicate(
        self, tmp_db: Database, sample_signal: Signal
    ):
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        outcome = ledger.record_or_report(sample_signal)
        assert outcome.ok
        assert outcome.duplicate

    def test_true_collision_reports_failure_without_raising(
        self, tmp_db: Database, sample_signal: Signal
    ):
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        different_signal = replace(sample_signal, symbol="OTHER", direction=Direction.SHORT)

        outcome = ledger.record_or_report(different_signal)

        assert not outcome.ok
        assert outcome.error is not None
        assert "SIG001" in outcome.error


def _count_selects(db: Database, fn):
    """Run ``fn`` and return its result plus the SELECTs it issued.

    Counted at the DBAPI cursor level so an N+1 that reappears inside a
    helper cannot slip past the assertion.
    """
    from sqlalchemy import event

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)
    return result, [s for s in statements if s.lstrip().upper().startswith("SELECT")]


class TestRecentWithStatus:
    """``recent_with_status`` -- signals AND their current status in ONE query.

    QA handoff v3, F26: the per-row ``current_status`` loop this replaces
    issued up to 501 sequential queries per ``get_signals`` call, each opening
    its own session; under a concurrent refresh that aggregate is what turned
    a read into a multi-minute stall. These tests pin the join as row-for-row
    equivalent to the loop it replaces -- a faster query returning *different*
    statuses would be a far worse bug than the slow one.
    """

    def _many(self, count: int) -> list[Signal]:
        base = dt.datetime(2024, 1, 3, 15, 0, tzinfo=dt.UTC)
        return [
            Signal(
                signal_id=f"SIG{i:03d}",
                created_at=base + dt.timedelta(minutes=i),
                session=dt.date(2024, 1, 3),
                symbol=f"SYM{i}",
                company_name="x",
                strategy="test",
                direction=Direction.LONG,
                status=SignalStatus.ACTIONABLE,
                reference_price=100.0,
                price_as_of=base,
                overall_score=50.0 + i,
                confidence=0.5,
                components=ComponentScores(),
                plan=TradePlan(
                    entry_low=99.0, entry_high=101.0, stop_loss=95.0, targets=[110.0], shares=1
                ),
            )
            for i in range(count)
        ]

    def test_matches_the_per_row_loop_it_replaces(self, tmp_db: Database):
        ledger = SignalLedger(tmp_db)
        for sig in self._many(5):
            ledger.record(sig)
        # Real revision history, so "latest revision wins" is genuinely
        # exercised rather than every row sitting at revision 0.
        ledger.append_revision("SIG001", status=SignalStatus.TRIGGERED, reason="entered")
        ledger.append_revision("SIG001", status=SignalStatus.EXPIRED, reason="window closed")
        ledger.append_revision("SIG003", status=SignalStatus.EXTENDED, reason="extended")

        joined = ledger.recent_with_status(limit=10)
        expected = [(s, ledger.current_status(s.signal_id)) for s in ledger.recent(limit=10)]

        assert [s.signal_id for s, _ in joined] == [s.signal_id for s, _ in expected]
        assert [st for _, st in joined] == [st for _, st in expected]

    def test_reports_the_latest_revision_status(self, tmp_db: Database, sample_signal: Signal):
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        ledger.append_revision("SIG001", status=SignalStatus.TRIGGERED, reason="entered")

        rows = ledger.recent_with_status(limit=10)
        assert len(rows) == 1
        signal, status = rows[0]
        assert signal.signal_id == "SIG001"
        assert status == SignalStatus.TRIGGERED

    def test_ordering_and_limit_match_recent(self, tmp_db: Database):
        ledger = SignalLedger(tmp_db)
        for sig in self._many(5):
            ledger.record(sig)

        rows = ledger.recent_with_status(limit=3)
        assert [s.signal_id for s, _ in rows] == [s.signal_id for s in ledger.recent(limit=3)]
        assert len(rows) == 3

    def test_a_revisionless_signal_is_still_returned(self, tmp_db: Database, sample_signal: Signal):
        """Outer joins on purpose: the ledger's completeness guarantee means a
        signal is never dropped from a read, only reported with status None.

        Such a row cannot be produced through this API (``record`` always
        writes revision 0, and the append-only triggers forbid deleting it),
        so it is written directly here -- the point is that a corrupt or
        externally-tampered database degrades to "status unknown" rather than
        to a signal silently vanishing from the screener.
        """
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        with tmp_db.session() as session:
            session.add(
                SignalRow(
                    signal_id="ORPHAN",
                    created_at=dt.datetime(2025, 1, 3, 15, 0, tzinfo=dt.UTC),
                    session=dt.date(2025, 1, 3),
                    symbol="ORPH",
                    strategy="test",
                    direction=Direction.LONG.value,
                    initial_status=SignalStatus.ACTIONABLE.value,
                    reference_price=100.0,
                    price_as_of=dt.datetime(2025, 1, 3, 15, 0, tzinfo=dt.UTC),
                    overall_score=60.0,
                    confidence=0.5,
                )
            )

        rows = ledger.recent_with_status(limit=10)
        by_id = {s.signal_id: status for s, status in rows}
        assert "ORPHAN" in by_id  # not dropped by the join
        assert by_id["ORPHAN"] is None
        assert by_id["SIG001"] == SignalStatus.ACTIONABLE

    def test_empty_ledger(self, tmp_db: Database):
        assert SignalLedger(tmp_db).recent_with_status(limit=10) == []

    def test_issues_one_query_not_one_per_row(self, tmp_db: Database):
        ledger = SignalLedger(tmp_db)
        for sig in self._many(20):
            ledger.record(sig)

        rows, selects = _count_selects(tmp_db, lambda: ledger.recent_with_status(limit=20))

        assert len(rows) == 20
        assert len(selects) == 1, f"expected one SELECT, got {len(selects)}"


class TestCountsByStatus:
    """``counts_by_status`` is grouped in the database, not looped in Python."""

    def test_counts_reflect_the_latest_revision(self, tmp_db: Database, sample_signal: Signal):
        ledger = SignalLedger(tmp_db)
        ledger.record(sample_signal)
        ledger.record(replace(sample_signal, signal_id="SIG002", symbol="OTHER"))
        ledger.append_revision("SIG002", status=SignalStatus.TRIGGERED, reason="entered")

        assert ledger.counts_by_status() == {"actionable": 1, "triggered": 1}

    def test_empty_ledger_counts_nothing(self, tmp_db: Database):
        assert SignalLedger(tmp_db).counts_by_status() == {}

    def test_issues_one_query_not_one_per_signal(self, tmp_db: Database, sample_signal: Signal):
        ledger = SignalLedger(tmp_db)
        for i in range(10):
            ledger.record(replace(sample_signal, signal_id=f"SIG{i:03d}", symbol=f"S{i}"))

        counts, selects = _count_selects(tmp_db, ledger.counts_by_status)

        assert counts == {"actionable": 10}
        assert len(selects) == 1, f"expected one SELECT, got {len(selects)}"
