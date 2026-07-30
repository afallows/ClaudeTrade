"""The immutable signal ledger.

The reported win/loss ratio is only meaningful if the denominator is complete.
The single easiest way to make a strategy look good is to quietly drop the
signals that failed -- so this module makes that structurally difficult:

* A signal is written **once**. There is no update path in this API.
* Status changes are **appended** as revisions. The current state of a signal is
  its latest revision; the original is always still there.
* Each row carries an ``integrity_hash`` over its immutable fields, verified on
  read. A row edited outside the application is detected, not trusted.
* Migration 002 installs database triggers rejecting ``UPDATE`` and ``DELETE``
  on ``signals``, ``signal_revisions`` and the audit log, so bypassing this API
  with a SQL client fails too.

None of this stops a determined operator with filesystem access from deleting
the database. It does mean that partial tampering is detectable and that no
ordinary code path -- including a future bug -- can silently rewrite history.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from sqlalchemy import func, select

from claudetrade.db.models import SignalRevisionRow, SignalRow
from claudetrade.db.session import Database
from claudetrade.domain import (
    ComponentScores,
    Direction,
    MarketRegime,
    Signal,
    SignalStatus,
    TradePlan,
)
from claudetrade.logging_setup import audit_event, get_logger
from claudetrade.utils.hashing import content_hash, short_hash
from claudetrade.utils.timeutils import ensure_utc, utc_now

log = get_logger(__name__)


class LedgerIntegrityError(RuntimeError):
    """A stored signal failed its integrity check, or a rule was violated."""


@dataclass(slots=True, frozen=True)
class RecordOutcome:
    """Structured result of :meth:`SignalLedger.record_or_report`.

    Attributes:
        signal_id: The id that was attempted.
        ok: ``True`` when the signal is durably stored under this id --
            either freshly written or an idempotent re-recording of the
            exact same content.
        duplicate: ``True`` when ``ok`` and the row already existed with
            matching content (nothing was written this call).
        error: The message from the ``LedgerIntegrityError`` when ``ok`` is
            ``False``; ``None`` otherwise. A genuine collision -- the same
            id arriving with *different* content -- now only happens from
            data corruption or a hash regression, since ``make_signal_id``
            folds ``config_hash`` and ``code_version`` into the id, so a
            routine config or code change already mints a different id
            rather than reaching this path.
    """

    signal_id: str
    ok: bool
    duplicate: bool = False
    error: str | None = None


def signal_integrity_payload(signal: Signal) -> dict[str, object]:
    """The fields covered by the integrity hash.

    Deliberately the *decision* fields: symbol, direction, levels, scores and
    the reproducibility triple. Presentation text is excluded so that fixing a
    typo in a thesis does not look like tampering with a trade.
    """
    return {
        "signal_id": signal.signal_id,
        "created_at": signal.created_at.isoformat(),
        "session": signal.session.isoformat(),
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "strategy_version": signal.strategy_version,
        "direction": signal.direction.value,
        "reference_price": round(signal.reference_price, 6),
        "overall_score": round(signal.overall_score, 4),
        "confidence": round(signal.confidence, 6),
        "entry_low": round(signal.plan.entry_low, 6),
        "entry_high": round(signal.plan.entry_high, 6),
        "stop_loss": round(signal.plan.stop_loss, 6),
        "targets": [round(t, 6) for t in signal.plan.targets],
        "shares": signal.plan.shares,
        "code_version": signal.code_version,
        "config_hash": signal.config_hash,
        "data_snapshot_hash": signal.data_snapshot_hash,
    }


def make_signal_id(
    symbol: str, strategy: str, session: dt.date, config_hash: str, code_version: str = ""
) -> str:
    """Deterministic identifier.

    Derived from the decision inputs so that regenerating the same scan
    produces the same id -- which is what makes a duplicate insert detectable
    rather than creating a second copy of one decision.

    ``config_hash`` and ``code_version`` are both part of the hash suffix.
    Without them, re-scanning a session after a configuration change or a
    code deploy reuses the *same* id for what is now different content: a
    different score, a different plan, a different thesis. ``record()``
    correctly refuses that as a same-id-different-content collision -- the
    id claimed to name one immutable decision but two different decisions
    showed up under it. Folding both reproducibility fields into the id
    means a changed config or a changed build mints a genuinely different id
    (a distinct research artifact), while an identical re-scan -- same
    config, same code -- still reduces to the same id and dedupes silently,
    as before.

    ``code_version`` defaults to ``""`` only so the signature stays easy to
    call positionally in tests that don't care about it; production code
    (``SignalEngine``) always passes the real ``CODE_VERSION``. This does
    not migrate ids already stored: a row's ``signal_id`` is whatever was
    minted when it was written, and lookups by id never recompute it.
    """
    return f"{session.isoformat()}-{symbol}-{strategy}-{short_hash([symbol, strategy, session.isoformat(), config_hash, code_version], 8)}"


class SignalLedger:
    """Append-only store for signals and their status history."""

    def __init__(self, db: Database):
        self.db = db

    # --- writing ----------------------------------------------------------

    def record(self, signal: Signal) -> str:
        """Persist a new signal and its initial revision.

        Returns:
            The signal id.

        Raises:
            LedgerIntegrityError: if a signal with this id already exists. The
                caller must not "update" it -- append a revision instead.
        """
        integrity = content_hash(signal_integrity_payload(signal))
        with self.db.session() as session:
            existing = session.get(SignalRow, signal.signal_id)
            if existing is not None:
                if existing.integrity_hash == integrity:
                    log.debug("signal %s already recorded; ignoring duplicate", signal.signal_id)
                    return signal.signal_id
                raise LedgerIntegrityError(
                    f"signal {signal.signal_id} already exists with different contents. "
                    "Signals are immutable; append a revision rather than rewriting one."
                )

            row = SignalRow(
                signal_id=signal.signal_id,
                created_at=ensure_utc(signal.created_at),
                session=signal.session,
                symbol=signal.symbol,
                company_name=signal.company_name,
                strategy=signal.strategy,
                strategy_version=signal.strategy_version,
                direction=signal.direction.value,
                initial_status=signal.status.value,
                reference_price=signal.reference_price,
                price_as_of=ensure_utc(signal.price_as_of),
                overall_score=signal.overall_score,
                confidence=signal.confidence,
                components=signal.components.as_dict(),
                plan=asdict(signal.plan),
                regime=signal.regime.value,
                next_earnings_date=signal.next_earnings_date,
                days_to_earnings=signal.days_to_earnings,
                earnings_confirmed=signal.earnings_confirmed,
                thesis=signal.thesis,
                invalidation=list(signal.invalidation),
                exit_conditions=list(signal.exit_conditions),
                evidence=list(signal.evidence),
                risks=list(signal.risks),
                data_freshness_hours=signal.data_freshness_hours,
                data_warnings=list(signal.data_warnings),
                expires_after=signal.expires_after,
                code_version=signal.code_version,
                config_hash=signal.config_hash,
                data_snapshot_hash=signal.data_snapshot_hash,
                ai_metadata=dict(signal.ai_metadata),
                extras=dict(signal.extras),
                integrity_hash=integrity,
            )
            session.add(row)
            session.add(
                SignalRevisionRow(
                    signal_id=signal.signal_id,
                    revision=0,
                    status=signal.status.value,
                    reason="signal created",
                    observed_price=signal.reference_price,
                    created_at=ensure_utc(signal.created_at),
                    actor="signal_engine",
                    detail={"score": signal.overall_score, "confidence": signal.confidence},
                )
            )
        audit_event(
            "signal_recorded",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction.value,
        )
        return signal.signal_id

    def record_or_report(self, signal: Signal) -> RecordOutcome:
        """Like :meth:`record`, but reports a collision instead of raising.

        Intended for a caller recording many signals from one scan (see
        ``pipeline.scan``), where one signal's true id collision should not
        abort every other signal's recording. ``record()`` remains the
        stricter, raising primitive that this wraps -- callers that want the
        "fail the whole batch" behaviour should keep calling it directly.
        """
        try:
            with self.db.read_session() as session:
                pre_existing = session.get(SignalRow, signal.signal_id) is not None
        except Exception:  # pragma: no cover - defensive; read_session shouldn't raise here
            pre_existing = False
        try:
            self.record(signal)
        except LedgerIntegrityError as exc:
            log.error("signal %s could not be recorded: %s", signal.signal_id, exc)
            return RecordOutcome(signal.signal_id, ok=False, error=str(exc))
        return RecordOutcome(signal.signal_id, ok=True, duplicate=pre_existing)

    def append_revision(
        self,
        signal_id: str,
        *,
        status: SignalStatus,
        reason: str,
        observed_price: float | None = None,
        actor: str = "system",
        detail: dict[str, object] | None = None,
    ) -> int:
        """Append a status change. Returns the new revision number.

        Raises:
            LedgerIntegrityError: when the signal does not exist.
        """
        with self.db.session() as session:
            if session.get(SignalRow, signal_id) is None:
                raise LedgerIntegrityError(f"cannot revise unknown signal {signal_id}")
            current = session.execute(
                select(func.max(SignalRevisionRow.revision)).where(
                    SignalRevisionRow.signal_id == signal_id
                )
            ).scalar()
            revision = int(current or 0) + 1
            session.add(
                SignalRevisionRow(
                    signal_id=signal_id,
                    revision=revision,
                    status=status.value,
                    reason=reason,
                    observed_price=observed_price,
                    created_at=utc_now(),
                    actor=actor,
                    detail=detail or {},
                )
            )
        audit_event(
            "signal_revised",
            signal_id=signal_id,
            revision=revision,
            status=status.value,
            reason=reason,
        )
        return revision

    def expire_stale(self, as_of: dt.date, *, actor: str = "scheduler") -> list[str]:
        """Expire signals past their ``expires_after`` date.

        This is what stops a missed entry from being 'triggered' weeks later at
        a conveniently favourable price.
        """
        expired: list[str] = []
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalRow).where(
                    SignalRow.expires_after.is_not(None), SignalRow.expires_after < as_of
                )
            ).scalars().all()
            candidates = [(r.signal_id, r.expires_after) for r in rows]

        for signal_id, expires_after in candidates:
            if self.current_status(signal_id) in {
                SignalStatus.EXPIRED,
                SignalStatus.TRIGGERED,
                SignalStatus.REJECTED,
            }:
                continue
            self.append_revision(
                signal_id,
                status=SignalStatus.EXPIRED,
                reason=f"entry window closed on {expires_after}",
                actor=actor,
            )
            expired.append(signal_id)
        if expired:
            log.info("expired %d stale signals as of %s", len(expired), as_of)
        return expired

    # --- reading ----------------------------------------------------------

    def current_status(self, signal_id: str) -> SignalStatus | None:
        """The status from the most recent revision."""
        with self.db.read_session() as session:
            row = session.execute(
                select(SignalRevisionRow.status)
                .where(SignalRevisionRow.signal_id == signal_id)
                .order_by(SignalRevisionRow.revision.desc())
                .limit(1)
            ).scalar()
        return SignalStatus(row) if row else None

    def get(self, signal_id: str, *, verify: bool = True) -> Signal | None:
        """Load one signal, optionally verifying its integrity hash."""
        with self.db.read_session() as session:
            row = session.get(SignalRow, signal_id)
            if row is None:
                return None
            signal = _row_to_signal(row)
        if verify and not self.verify(signal, row.integrity_hash):
            raise LedgerIntegrityError(
                f"signal {signal_id} failed its integrity check: the stored row does not match "
                "its recorded hash, which means it was modified outside the application"
            )
        return signal

    def verify(self, signal: Signal, expected_hash: str) -> bool:
        return content_hash(signal_integrity_payload(signal)) == expected_hash

    def verify_all(self) -> list[str]:
        """Integrity-check every stored signal. Returns the ids that failed."""
        failures: list[str] = []
        with self.db.read_session() as session:
            rows = session.execute(select(SignalRow)).scalars().all()
            for row in rows:
                signal = _row_to_signal(row)
                if content_hash(signal_integrity_payload(signal)) != row.integrity_hash:
                    failures.append(row.signal_id)
        if failures:
            log.error("integrity check failed for %d signals", len(failures))
        return failures

    def for_session(self, session_date: dt.date) -> list[Signal]:
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalRow)
                .where(SignalRow.session == session_date)
                .order_by(SignalRow.overall_score.desc())
            ).scalars().all()
            return [_row_to_signal(r) for r in rows]

    def for_symbol(self, symbol: str, *, limit: int = 100) -> list[Signal]:
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalRow)
                .where(SignalRow.symbol == symbol)
                .order_by(SignalRow.created_at.desc())
                .limit(limit)
            ).scalars().all()
            return [_row_to_signal(r) for r in rows]

    def recent(self, *, limit: int = 200) -> list[Signal]:
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
            ).scalars().all()
            return [_row_to_signal(r) for r in rows]

    def history(self, signal_id: str) -> list[dict[str, object]]:
        """Full revision history, oldest first."""
        with self.db.read_session() as session:
            rows = session.execute(
                select(SignalRevisionRow)
                .where(SignalRevisionRow.signal_id == signal_id)
                .order_by(SignalRevisionRow.revision)
            ).scalars().all()
            return [
                {
                    "revision": r.revision,
                    "status": r.status,
                    "reason": r.reason,
                    "observed_price": r.observed_price,
                    "created_at": r.created_at,
                    "actor": r.actor,
                    "detail": r.detail,
                }
                for r in rows
            ]

    def counts_by_status(self) -> dict[str, int]:
        """Latest-revision status counts, for the dashboard."""
        out: dict[str, int] = {}
        with self.db.read_session() as session:
            ids = session.execute(select(SignalRow.signal_id)).scalars().all()
        for signal_id in ids:
            status = self.current_status(signal_id)
            key = status.value if status else "unknown"
            out[key] = out.get(key, 0) + 1
        return out


def _row_to_utc(value: dt.datetime) -> dt.datetime:
    """Coerce a datetime read back from the database to timezone-aware UTC.

    Every datetime is written through ``ensure_utc()`` (see ``record``), so
    the value stored is always a tz-aware UTC instant. SQLite, however, has
    no native timezone type: it stores the ``isoformat()`` text SQLAlchemy
    hands it and -- depending on driver version -- can hand back a *naive*
    ``datetime`` on read. Comparing that naive value against the tz-aware
    original that the integrity hash was computed over then never matches,
    so ``get(..., verify=True)`` raised for every legitimately stored signal
    regardless of tampering. Since the value is always UTC by construction,
    a naive read-back is missing tzinfo, not a different instant -- so it is
    reattached rather than converted. A backend that *does* preserve tzinfo
    (e.g. Postgres) is handled by normalising to UTC instead of overwriting
    whatever offset it reports.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _row_to_signal(row: SignalRow) -> Signal:
    """Rehydrate a domain ``Signal`` from its stored row."""
    plan_data = dict(row.plan or {})
    plan = TradePlan(
        entry_low=plan_data.get("entry_low", 0.0),
        entry_high=plan_data.get("entry_high", 0.0),
        stop_loss=plan_data.get("stop_loss", 0.0),
        targets=list(plan_data.get("targets", [])),
        target_fractions=list(plan_data.get("target_fractions", [])),
        trailing_stop_atr=plan_data.get("trailing_stop_atr"),
        time_stop_days=plan_data.get("time_stop_days", 10),
        expected_holding_days=plan_data.get("expected_holding_days", 10),
        shares=plan_data.get("shares", 0),
        notional_usd=plan_data.get("notional_usd", 0.0),
        risk_per_share=plan_data.get("risk_per_share", 0.0),
        reward_per_share=plan_data.get("reward_per_share", 0.0),
        dollar_risk=plan_data.get("dollar_risk", 0.0),
    )
    components = ComponentScores(**dict(row.components or {}))
    return Signal(
        signal_id=row.signal_id,
        created_at=_row_to_utc(row.created_at),
        session=row.session,
        symbol=row.symbol,
        company_name=row.company_name,
        strategy=row.strategy,
        direction=Direction(row.direction),
        status=SignalStatus(row.initial_status),
        reference_price=row.reference_price,
        price_as_of=_row_to_utc(row.price_as_of),
        overall_score=row.overall_score,
        confidence=row.confidence,
        components=components,
        plan=plan,
        regime=MarketRegime(row.regime) if row.regime else MarketRegime.UNKNOWN,
        next_earnings_date=row.next_earnings_date,
        days_to_earnings=row.days_to_earnings,
        earnings_confirmed=row.earnings_confirmed,
        thesis=row.thesis,
        invalidation=list(row.invalidation or []),
        exit_conditions=list(row.exit_conditions or []),
        evidence=list(row.evidence or []),
        risks=list(row.risks or []),
        data_freshness_hours=row.data_freshness_hours,
        data_warnings=list(row.data_warnings or []),
        expires_after=row.expires_after,
        code_version=row.code_version,
        config_hash=row.config_hash,
        strategy_version=row.strategy_version,
        data_snapshot_hash=row.data_snapshot_hash,
        ai_metadata=dict(row.ai_metadata or {}),
        extras=dict(row.extras or {}),
    )
