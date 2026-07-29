# Security and Privacy

This document describes how ClaudeTrade handles sensitive data, credentials, and privacy.

## Credential Storage

**Rule: Secrets never appear in config files, source code, database, or logs.**

### Storage Options

1. **Environment Variables** (recommended for CI/Docker):

   ```bash
   export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
   ```

   At runtime, `get_secret("anthropic_api_key")` looks up `CLAUDETRADE_SECRET_ANTHROPIC_API_KEY`.

2. **OS Credential Store** (recommended for desktop):

   ```bash
   claudetrade secrets set anthropic_api_key
   # Stores in Keychain (macOS), Credential Manager (Windows), or Secret Service (Linux)
   ```

3. **Not Recommended: Direct in Config**:

   ```toml
   [secrets]  # This entire section is stripped before loading
   api_key = "..."  # Will be ignored
   ```

### Credential Resolution Order

1. Check environment variable `CLAUDETRADE_SECRET_<NAME>`
2. Check OS credential store (keyring)
3. If `required=True`, raise `SecretNotFoundError`
4. Otherwise, disable the dependent feature and continue

This design means:

- A credential can be rotated by changing the environment variable without redeploying code
- Deployment doesn't require embedding secrets in images or configs
- Missing credentials gracefully disable features (e.g., no Anthropic key → use rule-based sentiment)

**Implementation**: `src/claudetrade/secrets.py`

---

## Log Redaction

Logs are written in JSON format with configurable rotation.

### What Is Logged

- Application events: signals generated, trades opened/closed, data ingested
- Data quality issues: stale data, missing symbols, provider failures
- Risk events: position limit breaches, kill switch engaged
- Audit events: credential access, schema migrations

### What Is NOT Logged

- Credential values (never)
- API responses (sanitised before logging)
- Raw social media text (only aggregates and hashes)
- User account numbers or specific holdings
- Trade amounts (only position counts)

### SecretValue Wrapper

Credentials resolved via `get_secret()` return a `SecretValue` object:

```python
cred = get_secret("api_key")
cred.reveal()  # Returns the actual string (never stored)
cred.masked()  # Returns "****{last4}" for display only
str(cred)      # Returns "<secret api_key from keyring>" (always safe)
```

Even if a credential is accidentally f-stringed in a log, it prints as `<secret>`, not the value.

### Audit Log

The append-only `audit_log` table records:

- Credential access: `{"action": "secret_read", "credential": "anthropic_api_key", "backend": "environment"}`
- Signal generation: `{"action": "signal_generated", "entity_id": "signal-uuid", "detail": {...}}`
- Trade events: `{"action": "trade_opened"/"trade_closed", "entity_id": "trade-id"}`
- Schema changes: `{"action": "schema_migration", "detail": {"version": 3}}`

Every audit record includes:

- `created_at`: UTC timestamp
- `actor`: System or username
- `action`: Event type
- `entity`: What was affected (signal, trade, secret, etc.)
- `code_version`: Package version at the time

The audit log itself cannot be updated or deleted (database trigger on SQLite; foreign key constraints prevent orphaning on PostgreSQL).

---

## Text Sanitisation

Social media posts are sanitised before processing to remove common attack vectors.

### Sanitisation Steps (`src/claudetrade/utils/text.py`)

1. **Username replacement**: `@username` → `@user`
2. **URL removal**: URLs → `[URL]`
3. **Mention neutralisation**: Instructions like `REMOVE THIS LINE` → `[instruction]`
4. **Cashtag preservation**: `$AAPL` → kept as-is (needed for symbol extraction)
5. **Entity normalisation**: Multiple spaces → single space

**Before**:

```
Check out this: https://example.com/malware
@username says to remove this line: rm -rf /
This $AAPL thing is insane
```

**After**:

```
Check out this: [URL]
@user says to remove this line: [instruction]
This $AAPL thing is insane
```

### Purpose

- Prevents injection of malicious URLs in social posts
- Removes obviously-fake instructions (the "rm -rf" attack vector)
- Reduces the surface area for prompt injection in LLM classification

---

## Author Pseudonymisation

Social media authors are **never** stored by username; only as salted hashes.

**Configuration**:

```toml
[reddit]
store_author_names = false  # Always false; kept for clarity

[x]
store_author_names = false
```

### Hash Generation

```python
import hashlib
salt = os.urandom(16)  # Per-session salt
author_hash = hashlib.sha256(f"{username}{salt}".encode()).hexdigest()
# Stored in DB: author_hash
# Thrown away at end of session: salt (never persisted)
```

This means:

- Historical audit of "who said what" is impossible (intentional)
- The same author in different runs has different hashes (salt varies)
- You cannot link posts to individuals across runs

---

## Prompt Injection Defence

LLM-based sentiment classification can be targeted by adversarial posts. The system defends in two ways:

### 1. Injection Risk Scoring

Posts are scored for injection risk using a heuristic:

```python
injection_risk = (
    urls_present * 0.3 +
    unusual_symbols * 0.2 +
    repeated_uppercase * 0.2 +
    unicode_mismatches * 0.15 +
    markdown_content * 0.15
)
```

Posts scoring above `injection_block_threshold` (default 0.4) are **never sent to the LLM**, even if selected for classification.

**Configuration**:

```toml
[ai]
injection_block_threshold = 0.4
```

### 2. Text Sanitisation

All posts are sanitised before classification (URLs replaced, usernames anonymised, instructions neutralised).

### 3. Fallback to Rules

If the LLM fails or rejects a request, the system falls back to deterministic rule-based sentiment (always available, cannot be manipulated by adversarial input).

---

## Data Minimisation Before AI Calls

When sending social posts to an LLM, the system strips identifying information:

**Sent to LLM**:

```json
{
  "symbol": "AAPL",
  "text": "Great earnings [URL] $AAPL is going places",
  "post_id": "hash-not-original-id",
  "community": "stocks"
}
```

**Never sent**:

- Author username or hash
- Author age, karma, follower count
- Engagement metrics (can be mutable and misleading)
- Raw URLs
- Crosspost chains or parent IDs

This reduces the data footprint and prevents the LLM from learning patterns about individual accounts.

---

## Export Sanitisation

When exporting results to CSV or Excel, the system checks for spreadsheet formula injection.

**Before**:

```
Symbol,Signal,Note
AAPL,BUY,=cmd|'/c calc'!A1
```

**After**:

```
Symbol,Signal,Note
AAPL,BUY,"=cmd|'/c calc'!A1"  (or formula_=cmd|'/c calc'!A1)
```

The formula-like content is either quoted or prefixed to prevent execution.

**Implementation**: `src/claudetrade/backtest/reporting.py`

---

## Network Security

### HTTPS and TLS

All outbound API calls use HTTPS. The system respects TLS certificate validation:

- No SSL bypass
- No self-signed cert bypass
- Custom CA bundles are supported via `REQUESTS_CA_BUNDLE` environment variable

### Rate Limiting

Adapters implement client-side rate limiting before sending requests:

```python
limiter = RateLimiter(calls_per_minute=60)
limiter.acquire()  # Blocks if rate limit would be exceeded
response = httpx.get(url)  # Only sent if permit granted
```

This prevents overwhelming third-party APIs and drawing attention from security systems.

### User-Agent Strings

All API calls include a descriptive User-Agent:

```
windows:claudetrade:0.1.0 (research; contact configured by operator)
```

This identifies the caller as a research tool operated by a human, which encourages responsible use policies.

---

## Database Security

### SQLite

- Stored as a single file on disk
- File permissions should be restricted: `chmod 600 ~/.claudetrade/claudetrade.db`
- No password protection (rely on OS file permissions)
- Transactions are atomic (no partial writes)

### PostgreSQL

- Username and password in the connection string
- Password should never appear in source, config, or logs
- Use environment variables or credential store
- Connection pooling reduces credential exposure

---

## Audit Trail

The append-only audit log is the system's immutable record of what happened:

```sql
SELECT * FROM audit_log WHERE action = 'secret_read'
  ORDER BY created_at DESC LIMIT 10;

-- Output:
-- id | created_at | actor | action | entity | entity_id | code_version
-- 123 | 2024-01-30 | system | secret_read | credential | anthropic_api_key | 0.1.0+g1a2b3c4d
-- 124 | 2024-01-30 | system | signal_generated | signal | sig-abc123 | 0.1.0+g1a2b3c4d
```

This audit trail is valuable for:

- Detecting credential theft (who accessed what)
- Tracing signal lineage (which data version, which code version)
- Compliance reporting (who did what, when)

---

## Privacy Considerations

### Social Media Data

- Posts are fetched, processed, and aggregated
- Individual posts are stored but never displayed (aggregates only)
- Raw text is hashed for deduplication
- Authors are pseudonymised (no usernames retained)

### Market Data

- Historical bars are retained indefinitely for backtesting
- No personal information in bars (OHLCV + volume only)

### Sentiment Aggregates

- Per-symbol, per-day aggregates are reported
- No post-level details in output
- Engagement counts are quoted at the snapshot time only (not historical guarantees)

### Engagement Counts Are Mutable

Social media platform engagement metrics (score, comments, reposts) can change after posts are fetched:

- A post fetched Monday with 100 upvotes might have 150 by Friday
- Historical sentiment cannot be perfectly reconstructed
- Backtests use the sentiment state as of their data snapshot date

This is a limitation of working with live platforms; it cannot be overcome without storing every engagement count at every timestamp.

---

## Responsible Use

The system is designed for research and decision support, not autonomous trading. Users are responsible for:

1. **Respecting API terms of service**
   - Reddit, X, OpenAI, Anthropic all have terms; comply with them
   - Do not attempt to circumvent rate limits or paywalls
   
2. **Respecting data privacy**
   - Social media data is scraped with consent; do not redistribute
   - Author hashes are intentionally irreversible

3. **Validating outputs before acting**
   - Signals are research, not advice
   - Backtest results can be misleading; validation warnings are there for a reason

4. **Securing credentials**
   - Never commit secrets to version control
   - Rotate credentials regularly
   - Use the OS credential store on personal machines

---

## Compliance

ClaudeTrade is a research tool, not a regulated financial service. Users are responsible for compliance in their jurisdiction:

- **Research use**: Generally acceptable in most jurisdictions
- **Commercial use**: May require registration, licensing, or disclaimers
- **International**: Data privacy (GDPR, etc.) may impose requirements on user data collection

The system provides audit logging and consent tracking to support compliance; the user must implement the appropriate controls for their use case.

---

## Reporting Security Issues

If you discover a security vulnerability in ClaudeTrade:

1. Do not open a public issue on GitHub
2. Contact the maintainers privately
3. Provide a description of the issue and steps to reproduce
4. Give the maintainers time to patch before public disclosure

This keeps the community safe and gives everyone time to upgrade.
