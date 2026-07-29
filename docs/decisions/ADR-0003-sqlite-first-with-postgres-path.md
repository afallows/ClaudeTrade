# ADR-0003: SQLite First, PostgreSQL Path Open

**Date**: 2024-01-20  
**Status**: Accepted  
**Participants**: ClaudeTrade Contributors

## Decision

Use **SQLite as the default and recommended database**, but ensure the ORM and schema are **portable to PostgreSQL** with no application logic changes.

```
Default: sqlite:////path/to/claudetrade.db
On-demand: postgresql://user:pass@host/claudetrade
```

## Alternatives Considered

1. **PostgreSQL only**: Requires external infrastructure (server, backups, credentials).
   - **Rejected**: Raises the bar for casual users and testing.

2. **SQLite only, PostgreSQL never**: Simpler; avoids portability overhead.
   - **Rejected**: Enterprise deployments may need PostgreSQL for multi-user access, backups, or regulatory requirements.

3. **Both equally supported**: Maintain test suites for both.
   - **Rejected**: Maintenance burden; SQLite is sufficient for 99% of use cases.

## Reason Selected

1. **Low barrier to entry**: SQLite requires no setup; database is a single file.
2. **Sufficient for single-user research**: Concurrent reads and writes are rare in backtesting workflows.
3. **Portability option**: Open the door to PostgreSQL without rewriting code.
4. **ORM abstraction**: SQLAlchemy handles dialect differences; schema is database-agnostic.

## Implementation

All code uses:

- **SQLAlchemy ORM** only; no raw SQL
- **Portable column types**: Integer, Float, String, JSON, Date, DateTime
- **No SQLite-specific features** (except WAL pragma for performance)
- **No direct SQL strings** in Python (only in migrations, which are tested)

### Database URL Configuration

```toml
[database]
# SQLite (default)
url = ""
filename = "claudetrade.db"

# PostgreSQL (explicit)
url = "postgresql://user:pass@host:5432/claudetrade"
```

The effective URL is computed as:

```python
if config.database.url:
    db_url = config.database.url
else:
    db_path = paths.resolve("data_dir") / config.database.filename
    db_url = f"sqlite+pysqlite:///{db_path}"
```

## Risks

1. **SQLite limitations** (single-writer, no true ACID on network filesystems):
   - **Mitigation**: Document limits; PostgreSQL is the solution for high concurrency.

2. **Portability illusion**: Portability is only verified if both databases are tested.
   - **Mitigation**: Test suite includes both; CI runs against SQLite; PostgreSQL is tested manually before releases.

3. **Migration burden**: Schema changes must support both databases.
   - **Mitigation**: Custom migration runner is database-agnostic; migrations are written in Python, not SQL.

## Reversal / Migration Plan

If SQLite becomes a bottleneck:

1. **User can migrate**: Export data, change URL to PostgreSQL, re-import.
2. **Automated migration tool**: Write a `claudetrade migrate-database` command.
3. **Multi-backend support**: Add documentation for other databases (MySQL, etc.) if the portability layer holds up.

## Implementation Notes

### SQLite Pragmas

```python
# In database session initialization:
if "sqlite" in engine_url:
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
        cursor.execute("PRAGMA busy_timeout=10000")  # 10s wait for locks
        cursor.close()
```

These pragmas improve concurrent read access without changing application code.

### Schema Versioning

Migrations are idempotent and database-agnostic:

```python
# migrations.py (excerpt)
def apply_migration_001(session: Session) -> None:
    """Create signals table."""
    # Uses ORM; SQLAlchemy handles dialect differences
    Base.metadata.create_all(bind=session.bind)
```

### Testing

```bash
# SQLite (default, fast)
pytest tests/

# PostgreSQL (manual, requires running postgres)
pytest tests/ -m postgres
```

## Related ADRs

- ADR-0004: Custom Migration Runner (how migrations are applied and versioned)
