"""The append-only research-revision ledger.

An MCP client (Claude Desktop) can perform web research on a generated
signal and submit an updated thesis, an updated invalidation list, and small
bounded adjustments to the already-computed component scores. This module is
the single write path for that: ``ResearchLedger.append_research_revision``.

The design goal is the same one ``signals.ledger`` states for the signal
itself, aimed at a different failure mode: a research finding must be able to
*re-rank* a signal without ever being able to *rewrite* it.

* **Append-only, like the signal ledger.** A research revision is a new row,
  never an edit. ``SignalRow`` itself is never touched -- ``overall_score``,
  ``thesis``, ``invalidation`` and ``components`` on the original row stay
  exactly what the engine wrote. Corrections and updates accumulate as
  further revisions; ``research_history`` returns all of them.
* **The trade plan is structurally unreachable.** This module's write path
  has no parameter for entry, stop, target or size, and
  ``SignalResearchRevisionRow`` has no column for any of them. There is no
  guardrail to bypass here -- the field simply does not exist to be filled
  in, which is a stronger guarantee than validating it away would be.
* **Guardrails reused, not reinvented.** Thesis and invalidation text are
  checked with the exact same rewrite guardrail the AI-thesis-polish path
  uses (``signals.thesis.validate_research_text``): no unrecognised decimal
  price level, no directive phrase, plausible length. A model that tries to
  smuggle a different stop into prose is rejected the same way here as it
  would be there.
* **Score influence is capped and read-time only.** Component-score deltas
  are clamped to ``McpConfig.max_component_adjustment`` before they are ever
  stored, and they never touch ``SignalRow.overall_score`` -- they only feed
  ``signals.scoring.adjusted_overall`` at read time, computed fresh from the
  full revision history every time. Deleting or ignoring every revision
  always recovers the original, unadjusted signal.
* **Fully audited.** Every accepted revision fires ``audit_event`` and every
  row carries an ``integrity_hash`` over its content, verified by
  ``verify_all_research`` the same way ``SignalLedger.verify_all`` checks the
  signal ledger. A migration-installed SQLite trigger rejects raw ``UPDATE``/
  ``DELETE`` on the table, so bypassing this API with a SQL client fails too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select

from claudetrade.config import AppConfig
from claudetrade.db.models import SignalResearchRevisionRow, SignalRow
from claudetrade.db.session import Database
from claudetrade.domain import ComponentScores
from claudetrade.logging_setup import audit_event, get_logger
from claudetrade.signals.scoring import adjusted_overall
from claudetrade.signals.thesis import validate_research_text
from claudetrade.utils.hashing import content_hash
from claudetrade.utils.timeutils import utc_now

log = get_logger(__name__)

#: The only component names a research revision may adjust -- everything
#: ``ComponentScores`` knows how to hold, and nothing else. Computed from the
#: dataclass itself rather than hand-copied, so a future thirteenth-plus
#: component is picked up automatically instead of silently staying
#: unrejectable.
VALID_COMPONENT_NAMES = frozenset(ComponentScores().as_dict())

#: Length bounds for one invalidation-condition string. Deliberately much
#: shorter than a thesis paragraph's bounds (``thesis.DEFAULT_MIN_CHARS`` is
#: 60) -- the engine's own invalidation conditions are short, single-clause
#: bullets (e.g. "Close below the 42.00 stop level"), and requiring
#: thesis-length prose here would reject every legitimate one.
INVALIDATION_MIN_CHARS = 3
INVALIDATION_MAX_CHARS = 400


class ResearchGuardrailError(RuntimeError):
    """A research revision was refused by a guardrail, not a system error.

    Distinct from ``claudetrade.signals.ledger.LedgerIntegrityError`` (which
    means corruption or a programming bug): ``.args[0]`` here is a message
    safe to hand straight back to an MCP client as a structured
    ``{"accepted": false, "reason": ...}`` payload. Covers everything from
    "signal not found" through "unknown component" to a rejected thesis
    rewrite -- one exception type so the MCP tool has one place to catch it.
    """


@dataclass(slots=True, frozen=True)
class ResearchRevisionResult:
    """Outcome of a successful :meth:`ResearchLedger.append_research_revision`."""

    signal_id: str
    revision: int
    original_score: float
    effective_score: float
    #: Component name -> the delta actually stored, after clamping.
    applied_adjustments: dict[str, float]
    #: Component name -> {"requested": x, "applied": y}, present only for
    #: components whose submitted delta was clamped.
    clamped: dict[str, dict[str, float]] = field(default_factory=dict)


def research_integrity_payload(
    *,
    signal_id: str,
    revision: int,
    thesis: str | None,
    invalidation: list[str] | None,
    score_adjustments: dict[str, float],
    rationale: str,
    sources: list[str],
) -> dict[str, object]:
    """The fields covered by a research revision's integrity hash.

    Shared by the write path and :meth:`ResearchLedger.verify_all_research`
    so the two can never drift apart -- mirrors
    ``signals.ledger.signal_integrity_payload``'s role for signals.
    """
    return {
        "signal_id": signal_id,
        "revision": revision,
        "thesis": thesis,
        "invalidation": list(invalidation) if invalidation is not None else None,
        "score_adjustments": {k: round(float(v), 6) for k, v in score_adjustments.items()},
        "rationale": rationale,
        "sources": list(sources),
    }


def _row_to_dict(row: SignalResearchRevisionRow) -> dict[str, object]:
    """Flatten one stored revision row -- built while the session is open."""
    return {
        "revision": row.revision,
        "created_at": row.created_at,
        "actor": row.actor,
        "thesis": row.thesis,
        "invalidation": list(row.invalidation) if row.invalidation is not None else None,
        "score_adjustments": dict(row.score_adjustments or {}),
        "rationale": row.rationale,
        "sources": list(row.sources or []),
        "detail": dict(row.detail or {}),
    }


def _allowed_levels(row: SignalRow) -> list[float]:
    """Price levels a research rewrite may echo: the signal's own plan."""
    plan = dict(row.plan or {})
    levels: list[float] = []
    for key in ("entry_low", "entry_high", "stop_loss"):
        value = plan.get(key)
        if value is not None:
            levels.append(float(value))
    levels.extend(float(t) for t in plan.get("targets", []) or [])
    return levels


class ResearchLedger:
    """Append-only store for MCP-submitted research revisions."""

    def __init__(self, db: Database):
        self.db = db

    # --- writing ------------------------------------------------------

    def append_research_revision(
        self,
        signal_id: str,
        *,
        thesis: str | None,
        invalidation: list[str] | None,
        score_adjustments: dict[str, float] | None,
        rationale: str,
        sources: list[str],
        config: AppConfig,
        actor: str = "mcp",
        detail: dict[str, object] | None = None,
    ) -> ResearchRevisionResult:
        """Validate, clamp and append one research revision.

        Raises:
            ResearchGuardrailError: for every rejection -- unknown signal,
                missing rationale/sources, an unknown component name, or a
                thesis/invalidation rewrite that fails
                ``signals.thesis.validate_research_text``. Callers (the MCP
                tool) catch this and return a structured refusal instead of
                raising it at the transport.
        """
        rationale = (rationale or "").strip()
        if not rationale:
            raise ResearchGuardrailError("rationale is required and must be non-empty")
        if not sources or not all(isinstance(s, str) and s.strip() for s in sources):
            raise ResearchGuardrailError("sources must be a non-empty list of non-empty strings")

        with self.db.read_session() as session:
            row = session.get(SignalRow, signal_id)
            if row is None:
                raise ResearchGuardrailError(f"unknown signal {signal_id}")
            original_thesis = row.thesis
            original_score = row.overall_score
            components = dict(row.components or {})
            allowed_levels = _allowed_levels(row)

        if thesis is not None:
            safe, why = validate_research_text(original_thesis, thesis, allowed_levels)
            if not safe:
                raise ResearchGuardrailError(f"thesis rejected: {why}")

        if invalidation is not None:
            if not isinstance(invalidation, list) or not all(
                isinstance(item, str) for item in invalidation
            ):
                raise ResearchGuardrailError("invalidation must be a list of strings")
            for item in invalidation:
                safe, why = validate_research_text(
                    original_thesis,
                    item,
                    allowed_levels,
                    min_chars=INVALIDATION_MIN_CHARS,
                    max_chars=INVALIDATION_MAX_CHARS,
                )
                if not safe:
                    raise ResearchGuardrailError(f"invalidation item rejected: {why}")

        applied: dict[str, float] = {}
        clamped: dict[str, dict[str, float]] = {}
        if score_adjustments:
            unknown = sorted(set(score_adjustments) - VALID_COMPONENT_NAMES)
            if unknown:
                raise ResearchGuardrailError(f"unknown component(s): {unknown}")
            cap = config.mcp.max_component_adjustment
            for name, raw_delta in score_adjustments.items():
                delta = float(raw_delta)
                bounded = max(-cap, min(cap, delta))
                applied[name] = bounded
                if bounded != delta:
                    clamped[name] = {"requested": delta, "applied": bounded}

        with self.db.session() as session:
            if session.get(SignalRow, signal_id) is None:
                raise ResearchGuardrailError(f"unknown signal {signal_id}")
            current = session.execute(
                select(func.max(SignalResearchRevisionRow.revision)).where(
                    SignalResearchRevisionRow.signal_id == signal_id
                )
            ).scalar()
            revision = int(current or 0) + 1
            integrity = content_hash(
                research_integrity_payload(
                    signal_id=signal_id,
                    revision=revision,
                    thesis=thesis,
                    invalidation=invalidation,
                    score_adjustments=applied,
                    rationale=rationale,
                    sources=sources,
                )
            )
            session.add(
                SignalResearchRevisionRow(
                    signal_id=signal_id,
                    revision=revision,
                    created_at=utc_now(),
                    actor=actor,
                    thesis=thesis,
                    invalidation=list(invalidation) if invalidation is not None else None,
                    score_adjustments=applied,
                    rationale=rationale,
                    sources=list(sources),
                    detail=detail or {},
                    integrity_hash=integrity,
                )
            )

        audit_event(
            "signal_research_revised",
            signal_id=signal_id,
            revision=revision,
            actor=actor,
            adjusted_components=sorted(applied),
        )

        effective_score = adjusted_overall(components, original_score, applied, config)
        return ResearchRevisionResult(
            signal_id=signal_id,
            revision=revision,
            original_score=original_score,
            effective_score=effective_score,
            applied_adjustments=applied,
            clamped=clamped,
        )

    # --- reading --------------------------------------------------------

    @staticmethod
    def _latest_research_join():
        """(subquery) pairing each signal id with its newest research revision.

        Same shape as ``signals.ledger.SignalLedger._latest_revision_join``
        and for the same reason (QA handoff v3, F26): a per-signal_id loop
        calling ``research_history`` would be exactly the N+1 that produced
        the original production stall, this time on ``get_signals``'
        effective-score column.
        """
        return (
            select(
                SignalResearchRevisionRow.signal_id.label("signal_id"),
                func.max(SignalResearchRevisionRow.revision).label("max_revision"),
            )
            .group_by(SignalResearchRevisionRow.signal_id)
            .subquery()
        )

    def latest_research_revisions(
        self, signal_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        """Latest research revision per signal id, in ONE query.

        Returns only the signal ids that actually have at least one research
        revision -- a signal id absent from the result has none. Callers
        (``mcp_server.get_signals``) treat that absence as "no research yet",
        never as "research exists but is empty".
        """
        ids = list(signal_ids)
        if not ids:
            return {}
        latest = self._latest_research_join()
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalResearchRevisionRow)
                .join(
                    latest,
                    (latest.c.signal_id == SignalResearchRevisionRow.signal_id)
                    & (latest.c.max_revision == SignalResearchRevisionRow.revision),
                )
                .where(SignalResearchRevisionRow.signal_id.in_(ids))
            ).scalars().all()
            return {row.signal_id: _row_to_dict(row) for row in rows}

    def research_history(self, signal_id: str) -> list[dict[str, object]]:
        """Full research-revision history for one signal, oldest first."""
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalResearchRevisionRow)
                .where(SignalResearchRevisionRow.signal_id == signal_id)
                .order_by(SignalResearchRevisionRow.revision)
            ).scalars().all()
            return [_row_to_dict(r) for r in rows]

    def verify_all_research(self) -> list[tuple[str, int]]:
        """Integrity-check every stored research revision.

        Mirrors ``SignalLedger.verify_all``. Returns the ``(signal_id,
        revision)`` pairs whose stored content no longer matches their
        recorded hash.
        """
        failures: list[tuple[str, int]] = []
        with self.db.read_session() as session:
            rows = session.execute(select(SignalResearchRevisionRow)).scalars().all()
            for row in rows:
                expected = content_hash(
                    research_integrity_payload(
                        signal_id=row.signal_id,
                        revision=row.revision,
                        thesis=row.thesis,
                        invalidation=list(row.invalidation)
                        if row.invalidation is not None
                        else None,
                        score_adjustments=dict(row.score_adjustments or {}),
                        rationale=row.rationale,
                        sources=list(row.sources or []),
                    )
                )
                if expected != row.integrity_hash:
                    failures.append((row.signal_id, row.revision))
        if failures:
            log.error("integrity check failed for %d research revisions", len(failures))
        return failures


__all__ = [
    "INVALIDATION_MAX_CHARS",
    "INVALIDATION_MIN_CHARS",
    "VALID_COMPONENT_NAMES",
    "ResearchGuardrailError",
    "ResearchLedger",
    "ResearchRevisionResult",
    "research_integrity_payload",
]
