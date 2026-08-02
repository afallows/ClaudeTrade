"""Tests for the MCP research-revision ledger (``claudetrade.signals.research``).

Mirrors the shape of ``tests/test_ledger_immutability.py``: fixtures build a
signal through the normal ledger, then each test class exercises one
guarantee -- append-only writing, guardrail rejection, clamping, tamper
detection, and the read-time ``adjusted_overall`` scoring function.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from claudetrade.config import AppConfig
from claudetrade.db.models import SignalResearchRevisionRow
from claudetrade.db.session import Database
from claudetrade.signals.ledger import SignalLedger
from claudetrade.signals.research import (
    ResearchGuardrailError,
    ResearchLedger,
    research_integrity_payload,
)
from claudetrade.signals.scoring import adjusted_overall
from claudetrade.utils.hashing import content_hash


def _count_selects(db: Database, fn):
    """Run ``fn`` and return its result plus the SELECTs it issued.

    Same helper as ``tests/test_ledger_immutability.py``'s -- duplicated
    locally rather than imported so this module has no cross-test-module
    dependency.
    """
    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _record)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", _record)
    return result, [s for s in statements if s.lstrip().upper().startswith("SELECT")]


@pytest.fixture
def recorded_signal(tmp_db: Database, make_signal):
    """A signal recorded to the ledger, with a known plan and empty thesis."""
    sig = make_signal(symbol="AAPL", overall_score=70.0)
    SignalLedger(tmp_db).record(sig)
    return sig


class TestAppendResearchRevision:
    def test_first_revision_is_number_one(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        result = ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments=None,
            rationale="Checked the latest 10-Q, guidance looks conservative.",
            sources=["https://example.com/10q"],
            config=tmp_app_config,
        )
        assert result.revision == 1
        assert result.signal_id == recorded_signal.signal_id
        assert result.original_score == recorded_signal.overall_score

    def test_revision_numbers_are_monotonic_and_append_only(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        for i in range(3):
            result = ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=None,
                invalidation=None,
                score_adjustments=None,
                rationale=f"Update {i}: found a fresh source.",
                sources=[f"https://example.com/{i}"],
                config=tmp_app_config,
            )
            assert result.revision == i + 1

        history = ledger.research_history(recorded_signal.signal_id)
        assert [h["revision"] for h in history] == [1, 2, 3]

    def test_unknown_signal_is_rejected(self, tmp_db: Database, tmp_app_config: AppConfig):
        ledger = ResearchLedger(tmp_db)
        with pytest.raises(ResearchGuardrailError, match="unknown signal"):
            ledger.append_research_revision(
                "NOPE",
                thesis=None,
                invalidation=None,
                score_adjustments=None,
                rationale="doesn't matter",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_missing_rationale_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        with pytest.raises(ResearchGuardrailError, match="rationale"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=None,
                invalidation=None,
                score_adjustments=None,
                rationale="   ",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_missing_sources_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        with pytest.raises(ResearchGuardrailError, match="sources"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=None,
                invalidation=None,
                score_adjustments=None,
                rationale="Solid rationale here.",
                sources=[],
                config=tmp_app_config,
            )

    def test_unknown_component_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        with pytest.raises(ResearchGuardrailError, match="unknown component"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=None,
                invalidation=None,
                score_adjustments={"not_a_real_component": 5.0},
                rationale="Solid rationale here.",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_adjustment_is_clamped_to_the_configured_cap(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        tmp_app_config.mcp.max_component_adjustment = 10.0
        ledger = ResearchLedger(tmp_db)
        result = ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 50.0, "catalyst_quality": -50.0},
            rationale="Strong new catalyst found, but weak technicals.",
            sources=["https://example.com/catalyst"],
            config=tmp_app_config,
        )
        assert result.applied_adjustments["technical_setup"] == 10.0
        assert result.applied_adjustments["catalyst_quality"] == -10.0
        assert result.clamped["technical_setup"] == {"requested": 50.0, "applied": 10.0}
        assert result.clamped["catalyst_quality"] == {"requested": -50.0, "applied": -10.0}

    def test_adjustment_within_the_cap_is_not_reported_as_clamped(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        result = ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 5.0},
            rationale="Minor technical confirmation.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        assert result.applied_adjustments["technical_setup"] == 5.0
        assert result.clamped == {}

    def test_thesis_rewrite_with_a_novel_price_level_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        rewrite = (
            "Fresh due diligence confirms the setup; expect a move toward "
            "999.99 given the newly announced catalyst and strong volume."
        )
        with pytest.raises(ResearchGuardrailError, match="unrecognised price level"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=rewrite,
                invalidation=None,
                score_adjustments=None,
                rationale="Checked recent filings.",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_thesis_rewrite_may_echo_the_signals_own_plan_levels(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        plan = recorded_signal.plan
        rewrite = (
            f"Fresh due diligence supports the entry near {plan.entry_low:.2f}, "
            f"with the stop unchanged at {plan.stop_loss:.2f} and target "
            f"{plan.targets[0]:.2f} intact given the newly confirmed catalyst."
        )
        result = ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=rewrite,
            invalidation=None,
            score_adjustments=None,
            rationale="Checked recent filings and confirmed the plan still holds.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        assert result.revision == 1

    def test_directive_phrase_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        rewrite = (
            "New research suggests you should widen the stop given the "
            "improved fundamentals and continued institutional accumulation."
        )
        with pytest.raises(ResearchGuardrailError, match="directive"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=rewrite,
                invalidation=None,
                score_adjustments=None,
                rationale="Checked recent filings.",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_invalidation_item_with_a_novel_price_level_is_rejected(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        with pytest.raises(ResearchGuardrailError, match="unrecognised price level"):
            ledger.append_research_revision(
                recorded_signal.signal_id,
                thesis=None,
                invalidation=["Close below 12.34 invalidates the setup"],
                score_adjustments=None,
                rationale="Checked recent filings.",
                sources=["https://example.com"],
                config=tmp_app_config,
            )

    def test_invalidation_item_may_echo_the_signals_own_plan_levels(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        stop = recorded_signal.plan.stop_loss
        result = ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=[f"Close below {stop:.2f} invalidates the setup"],
            score_adjustments=None,
            rationale="Reconfirmed the stop still holds under the new thesis.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        assert result.revision == 1

    def test_the_trade_plan_cannot_be_submitted_at_all(self):
        """No parameter of ``append_research_revision`` accepts a plan/price/
        size/direction field -- structurally, not just by validation."""
        import inspect

        sig = inspect.signature(ResearchLedger.append_research_revision)
        forbidden = {
            "entry",
            "entry_low",
            "entry_high",
            "stop",
            "stop_loss",
            "targets",
            "shares",
            "size",
            "direction",
            "plan",
        }
        assert forbidden.isdisjoint(sig.parameters)


class TestResearchHistoryAndLatest:
    def test_research_history_is_oldest_first(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 1.0},
            rationale="First pass.",
            sources=["https://example.com/1"],
            config=tmp_app_config,
        )
        ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 2.0},
            rationale="Second pass.",
            sources=["https://example.com/2"],
            config=tmp_app_config,
        )
        history = ledger.research_history(recorded_signal.signal_id)
        assert [h["rationale"] for h in history] == ["First pass.", "Second pass."]

    def test_history_for_a_signal_with_no_revisions_is_empty(self, tmp_db: Database, recorded_signal):
        assert ResearchLedger(tmp_db).research_history(recorded_signal.signal_id) == []

    def test_latest_research_revisions_returns_only_the_newest_per_signal(
        self, tmp_db: Database, tmp_app_config: AppConfig, make_signal
    ):
        ledger = ResearchLedger(tmp_db)
        sig_ledger = SignalLedger(tmp_db)
        one = make_signal(symbol="AAA")
        two = make_signal(symbol="BBB")
        sig_ledger.record(one)
        sig_ledger.record(two)

        ledger.append_research_revision(
            one.signal_id, thesis=None, invalidation=None,
            score_adjustments={"technical_setup": 1.0}, rationale="r1",
            sources=["https://example.com/1"], config=tmp_app_config,
        )
        ledger.append_research_revision(
            one.signal_id, thesis=None, invalidation=None,
            score_adjustments={"technical_setup": 3.0}, rationale="r2",
            sources=["https://example.com/2"], config=tmp_app_config,
        )

        latest = ledger.latest_research_revisions([one.signal_id, two.signal_id])
        assert set(latest) == {one.signal_id}
        assert latest[one.signal_id]["revision"] == 2
        assert latest[one.signal_id]["score_adjustments"] == {"technical_setup": 3.0}

    def test_latest_research_revisions_empty_input(self, tmp_db: Database):
        assert ResearchLedger(tmp_db).latest_research_revisions([]) == {}

    def test_latest_research_revisions_issues_one_query_not_one_per_signal(
        self, tmp_db: Database, tmp_app_config: AppConfig, make_signal
    ):
        ledger = ResearchLedger(tmp_db)
        sig_ledger = SignalLedger(tmp_db)
        signals = [make_signal(symbol=f"SYM{i}") for i in range(10)]
        for sig in signals:
            sig_ledger.record(sig)
        for sig in signals[:5]:
            ledger.append_research_revision(
                sig.signal_id, thesis=None, invalidation=None,
                score_adjustments={"technical_setup": 1.0}, rationale="r",
                sources=["https://example.com"], config=tmp_app_config,
            )

        ids = [s.signal_id for s in signals]
        result, selects = _count_selects(tmp_db, lambda: ledger.latest_research_revisions(ids))

        assert len(result) == 5
        assert len(selects) == 1, f"expected one SELECT, got {len(selects)}"


class TestResearchImmutability:
    def test_raw_sql_update_is_rejected_by_trigger(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments=None,
            rationale="Original rationale.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        with pytest.raises(IntegrityError), tmp_db.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE signal_research_revisions SET rationale = 'HACKED' "
                    f"WHERE signal_id = '{recorded_signal.signal_id}'"
                )
            )
        history = ledger.research_history(recorded_signal.signal_id)
        assert history[0]["rationale"] == "Original rationale."

    def test_raw_sql_delete_is_rejected_by_trigger(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments=None,
            rationale="Original rationale.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        with pytest.raises(IntegrityError), tmp_db.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM signal_research_revisions "
                    f"WHERE signal_id = '{recorded_signal.signal_id}'"
                )
            )
        assert len(ledger.research_history(recorded_signal.signal_id)) == 1


class TestResearchIntegrityVerification:
    def test_verify_all_research_reports_no_failures_when_untampered(
        self, tmp_db: Database, tmp_app_config: AppConfig, recorded_signal
    ):
        ledger = ResearchLedger(tmp_db)
        ledger.append_research_revision(
            recorded_signal.signal_id,
            thesis=None,
            invalidation=None,
            score_adjustments={"technical_setup": 4.0},
            rationale="Fine.",
            sources=["https://example.com"],
            config=tmp_app_config,
        )
        assert ledger.verify_all_research() == []

    def test_verify_all_research_catches_a_tampered_row(
        self, tmp_db: Database, recorded_signal
    ):
        # Inserted directly, bypassing append_research_revision (the only
        # thing the trigger cannot stop, since INSERT is permitted) with a
        # hash computed over different content -- standing in for a row
        # edited outside the application.
        wrong_hash = content_hash(
            research_integrity_payload(
                signal_id=recorded_signal.signal_id,
                revision=1,
                thesis=None,
                invalidation=None,
                score_adjustments={},
                rationale="SOMETHING ELSE ENTIRELY",
                sources=["https://example.com"],
            )
        )
        with tmp_db.session() as session:
            session.add(
                SignalResearchRevisionRow(
                    signal_id=recorded_signal.signal_id,
                    revision=1,
                    created_at=dt.datetime.now(tz=dt.UTC),
                    actor="mcp",
                    thesis=None,
                    invalidation=None,
                    score_adjustments={},
                    rationale="the real stored text",
                    sources=["https://example.com"],
                    detail={},
                    integrity_hash=wrong_hash,
                )
            )

        failures = ResearchLedger(tmp_db).verify_all_research()
        assert failures == [(recorded_signal.signal_id, 1)]


class TestAdjustedOverall:
    """``signals.scoring.adjusted_overall`` -- pure, deterministic, no I/O."""

    def test_no_adjustments_leaves_the_score_unchanged(self, tmp_app_config: AppConfig):
        components = {"technical_setup": 60.0, "catalyst_quality": 40.0}
        assert adjusted_overall(components, 70.0, {}, tmp_app_config) == 70.0

    def test_positive_delta_moves_the_score_up(self, tmp_app_config: AppConfig):
        components = {"technical_setup": 60.0}
        result = adjusted_overall(components, 70.0, {"technical_setup": 10.0}, tmp_app_config)
        assert result > 70.0

    def test_negative_delta_moves_the_score_down(self, tmp_app_config: AppConfig):
        components = {"technical_setup": 60.0}
        result = adjusted_overall(components, 70.0, {"technical_setup": -10.0}, tmp_app_config)
        assert result < 70.0

    def test_result_is_clamped_at_100(self, tmp_app_config: AppConfig):
        components = {"technical_setup": 99.0}
        result = adjusted_overall(components, 99.0, {"technical_setup": 1000.0}, tmp_app_config)
        assert result == 100.0

    def test_result_is_clamped_at_0(self, tmp_app_config: AppConfig):
        components = {"technical_setup": 1.0}
        result = adjusted_overall(components, 1.0, {"technical_setup": -1000.0}, tmp_app_config)
        assert result == 0.0

    def test_unknown_component_in_adjustments_is_ignored_not_raised(
        self, tmp_app_config: AppConfig
    ):
        """Consistent with the ledger's behaviour: an unknown name is
        rejected upstream (``ResearchLedger.append_research_revision``
        raises), so this pure function -- which never sees a rejected
        revision in the real path -- treats an unrecognised key as a no-op
        instead of raising a second, looser policy of its own."""
        components = {"technical_setup": 60.0}
        result = adjusted_overall(
            components, 70.0, {"not_a_real_component": 999.0}, tmp_app_config
        )
        assert result == 70.0

    def test_components_missing_from_the_stored_dict_carry_no_weight(
        self, tmp_app_config: AppConfig
    ):
        """Only components present in the stored dict may move the score --
        the renormalisation is over what's actually there, not the full
        13-component schema."""
        components = {"technical_setup": 60.0}
        with_extra = adjusted_overall(
            components, 70.0, {"technical_setup": 10.0, "catalyst_quality": 10.0}, tmp_app_config
        )
        technical_only = adjusted_overall(
            components, 70.0, {"technical_setup": 10.0}, tmp_app_config
        )
        assert with_extra == technical_only
