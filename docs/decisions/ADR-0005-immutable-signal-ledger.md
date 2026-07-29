# ADR-0005: Immutable Signal Ledger with Append-Only Enforcement

**Date**: 2024-01-25  
**Status**: Accepted  
**Participants**: ClaudeTrade Contributors

## Decision

Signals are **immutable once written**. Status changes are recorded as appended revisions, never as in-place updates. This is enforced at the database layer.

```
signals table (INSERT only; no UPDATE/DELETE)
  ↓
signal_revisions table (append-only; status history)
```

## Alternatives Considered

1. **Signals are mutable**: Allow UPDATE on signal status in-place.
   - **Rejected**: Makes it possible to hide failed signals by rewriting their status; breaks audit trail.

2. **Signals expire silently**: No record of status change.
   - **Rejected**: No audit trail; cannot investigate why a signal disappeared.

3. **Soft deletes**: Flag signals as deleted but retain rows.
   - **Rejected**: Incomplete; does not address intentional status changes (e.g., rejection).

## Reason Selected

1. **Prevents tampering**: No way to rewrite history; all changes are appended.
2. **Audit trail**: Every status change is timestamped and reasoned.
3. **Debugging**: Can trace why a signal was rejected, extended, or expired.
4. **Backtesting integrity**: Backtest results cannot be "improved" by retroactively changing signal status.

## Implementation

### SQLite Trigger

```sql
CREATE TRIGGER signals_no_update AFTER UPDATE ON signals
BEGIN
  SELECT RAISE(FAIL, 'signals table is append-only');
END;

CREATE TRIGGER signals_no_delete AFTER DELETE ON signals
BEGIN
  SELECT RAISE(FAIL, 'signals table is append-only');
END;
```

### PostgreSQL Foreign Key Constraint

PostgreSQL does not require triggers; a careful schema design prevents orphaning:

- `signal_revisions` has FK to `signals`
- Deleting a signal would violate the FK; deletion is rejected

### Current Status Query

The current status of a signal is its latest revision:

```python
latest = (session.query(SignalRevision)
    .filter(SignalRevision.signal_id == signal_id)
    .order_by(SignalRevision.created_at.desc())
    .first())
current_status = latest.new_status if latest else initial_status
```

### Revision Entry

```python
revision = SignalRevision(
    signal_id=signal.signal_id,
    created_at=utc_now(),
    previous_status="actionable",
    new_status="expired",
    reason="signal expiry window (5 days) has elapsed",
    manual=False,
)
session.add(revision)
session.commit()
```

## Risks

1. **Debugging difficulty**: If a signal is in the wrong state, you cannot just UPDATE it.
   - **Mitigation**: Append a new revision with a human note; provides audit trail.

2. **Space overhead**: Keeping full revision history uses more disk.
   - **Mitigation**: Compress old revisions periodically; for typical usage, negligible.

3. **Query complexity**: Queries for "current status" are slightly slower (need a join).
   - **Mitigation**: Cache current status in the signals row as a denormalization; triggers on revisions keep it in sync.

## Reversal / Migration Plan

If immutability proves problematic:

1. **Duplicate into a new table**: Migrate signals to a new table that allows updates.
2. **Archive old signals**: Keep immutable ledger for historical records; new signals in updatable table.
3. **Gradual transition**: Add a flag `immutable=True/False` to control behaviour per signal.

However, this defeats the purpose; immutability is not a performance optimization, it is an integrity guarantee.

## Related ADRs

- ADR-0001: Layered Architecture (signals are the output of the signal engine layer)
- ADR-0006: Pessimistic Execution Modelling (trades are also immutable)
