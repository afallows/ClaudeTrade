# Troubleshooting

Common issues and solutions.

## Installation and Setup

### "Python 3.11 not found"

**Error**: `python -m venv` or similar fails because Python 3.11+ is not installed.

**Solution**:
1. Download Python 3.11+ from https://www.python.org/
2. On Windows: Run the installer; check "Add Python to PATH"
3. On macOS: Install via Homebrew: `brew install python@3.11`
4. On Linux: Use your package manager: `apt install python3.11` (Debian/Ubuntu) or `yum install python3.11` (RHEL)
5. Verify: `python3 --version` should show 3.11 or higher

### "pip install fails with permission denied"

**Error**: Installing packages in the venv fails.

**Solution**:
1. Ensure venv is activated: `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\activate` (Windows)
2. Verify: Prompt should show `(.venv)` prefix
3. Retry: `pip install -e .`

### "ModuleNotFoundError: No module named 'claudetrade'"

**Error**: Python cannot find the package.

**Solution**:
1. Ensure venv is activated
2. Ensure you are in the ClaudeTrade root directory
3. Reinstall: `pip install -e .`

---

## Configuration

### "config.toml not found"

**Error**: Application fails to load configuration.

**Solution**:
1. Copy the example: `cp config.example.toml ~/.claudetrade/config.toml`
2. Or set the path: `export CLAUDETRADE_CONFIG=/path/to/config.toml`
3. Verify: `echo $CLAUDETRADE_CONFIG` (Linux/macOS) or `echo %CLAUDETRADE_CONFIG%` (Windows)

### "Invalid configuration: unknown section"

**Error**: TOML parse error or unknown config key.

**Solution**:
1. Validate TOML syntax: Use an online TOML validator (https://www.toml-lint.com/)
2. Check spelling: Section names are case-sensitive (`[database]`, not `[Database]`)
3. Check the example: Compare your config.toml to config.example.toml for structure
4. Check Python version: Ensure you are using Python 3.11+ (older versions have stricter TOML parsing)

### "CLAUDETRADE_CONFIG points to a non-existent file"

**Error**: Environment variable set to wrong path.

**Solution**:
```bash
# Check the variable
echo $CLAUDETRADE_CONFIG  # Linux/macOS
echo %CLAUDETRADE_CONFIG%  # Windows

# Fix it
export CLAUDETRADE_CONFIG=~/.claudetrade/config.toml
# or
set CLAUDETRADE_CONFIG=C:\Users\YourName\.claudetrade\config.toml
```

---

## Credentials

### "credential not found"

**Error**:
```
credential 'anthropic_api_key' not found. Set the environment variable 
CLAUDETRADE_SECRET_ANTHROPIC_API_KEY, or store it with: claudetrade secrets set anthropic_api_key
```

**Solution**:
1. **Set environment variable**:
   ```bash
   export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
   ```
   or
   ```bash
   set CLAUDETRADE_SECRET_ANTHROPIC_API_KEY=sk-ant-...  # Windows
   ```

2. **Or use OS credential store**:
   ```bash
   claudetrade secrets set anthropic_api_key
   # Prompts for the value; stores in Keychain/Credential Manager
   ```

3. **Verify it worked**:
   ```bash
   # Should not error if credential exists
   python -c "from claudetrade.secrets import get_secret; print(get_secret('anthropic_api_key'))"
   ```

### "credential is required but not configured"

**Error**: A feature (e.g., Anthropic AI) is enabled but the credential is missing.

**Solution**:
1. Disable the feature:
   ```toml
   [ai]
   provider = "null"  # Use rule-based sentiment instead
   ```
   or
2. Provide the credential (see above)

---

## Data and Providers

### "stale data" warning

**Error**: Market data is older than configured threshold.

**Solution**:
1. Refresh data manually:
   ```bash
   python -m claudetrade.pipeline refresh
   ```
2. Or increase the staleness threshold:
   ```toml
   [market_data]
   stale_after_hours = 48.0  # Increase from 30 hours
   ```

### "provider failed: rate limit exceeded"

**Error**: Too many API calls to a provider.

**Solution**:
1. **Stooq**: Wait 1 minute; the rate limit resets hourly
2. **Reddit/X**: Check your API tier; increase `rate_limit_per_minute` if you have higher tier
3. **LLM (OpenAI/Anthropic)**: Reduce post samples or disable AI:
   ```toml
   [ai]
   provider = "null"
   ```

### "provider failed: connection timeout"

**Error**: API call took longer than configured timeout.

**Solution**:
1. Check internet connection: `ping google.com`
2. Increase timeout:
   ```toml
   [market_data]
   request_timeout_s = 30.0  # Increase from 20
   ```
3. Check if the provider is down: Visit https://stooq.com or provider's status page

---

## Database

### "database is locked" (SQLite)

**Error**: `sqlite3.OperationalError: database is locked`

**Solution**:
1. Close other connections: If the database is open in another Python process or tool, close it
2. Increase busy timeout:
   ```toml
   [database]
   busy_timeout_ms = 20000  # Increase from 10000
   ```
3. Use SQLite WAL mode (should be default):
   ```toml
   [database]
   sqlite_wal = true
   ```
4. If issue persists, restart the application

### "table signals already exists" (migration)

**Error**: Migration fails because the table exists.

**Solution**:
1. This should not happen (migrations are idempotent)
2. Check the `schema_version` table:
   ```bash
   python -c "
   from claudetrade.db.session import Database
   from claudetrade.config import AppConfig
   db = Database(AppConfig.load())
   with db.session() as s:
       print(s.query(SchemaVersion).all())
   "
   ```
3. If migrations are incomplete, delete and re-init:
   ```bash
   rm ~/.claudetrade/claudetrade.db
   python -m claudetrade.db.migrations  # Re-run migrations
   ```

---

## Backtesting

### "backtest very slow" or "freezes"

**Error**: Walk-forward backtest over 10+ years is taking hours.

**Solution**:
1. Reduce date range:
   ```python
   engine.backtest_symbols(
       symbols=symbols,
       start_date=date(2023, 1, 1),  # Start more recently
       end_date=date(2024, 1, 1),
   )
   ```
2. Reduce universe size:
   ```toml
   [universe]
   max_symbols = 100  # Test on fewer names
   ```
3. Disable expensive features:
   ```toml
   [ai]
   provider = "null"  # Disable LLM
   [sentiment]
   use_ai_classifier = false
   ```

### "no signals generated" or "all signals rejected"

**Error**: Backtest runs but produces no trades.

**Solution**:
1. Check the filters:
   ```toml
   [filters]
   min_price = 5.0
   min_market_cap_usd = 500_000_000
   ```
   These may be too restrictive for your universe.

2. Lower thresholds:
   ```toml
   [filters]
   min_market_cap_usd = 100_000_000  # Reduce from 500M
   ```

3. Enable synthetic data to test:
   ```toml
   [market_data]
   provider = "synthetic"
   [reddit]
   provider = "synthetic"
   ```
   Synthetic data is guaranteed to produce signals.

### "win/loss ratio is degenerate"

**Error**: Backtest produces high win rate but negative expectancy, flagged as suspicious.

**Solution**:
1. This is intentional: the system detects "many small wins, huge losses"
2. Review the full metrics (profit factor, average win/loss, Sharpe ratio)
3. Increase time stops:
   ```python
   # In strategy code
   time_stop_days = 15  # Force-close after this many days
   ```

---

## UI and Visualization (When Implemented)

### "streamlit: port already in use"

**Error**: Port 8501 is already in use.

**Solution**:
```bash
# Change port in config
[ui]
port = 8502

# Or kill the existing process
lsof -i :8501  # Find PID
kill -9 <PID>   # Kill it (macOS/Linux)
```

---

## General Debugging

### "Enable debug logging"

**Solution**:
```toml
[logging]
level = "DEBUG"
console = true
```

Then check the logs:
```bash
tail -f ~/.claudetrade/logs/claudetrade.log
```

### "Check audit log"

**Solution**:
```bash
python -c "
from claudetrade.db.session import Database
from claudetrade.config import AppConfig
from claudetrade.db.models import AuditLog

db = Database(AppConfig.load())
with db.session() as s:
    for row in s.query(AuditLog).order_by(-AuditLog.created_at).limit(10):
        print(row.action, row.detail, row.created_at)
"
```

### "Run tests to verify installation"

**Solution**:
```bash
pytest tests/ -v
# or skip slow tests
pytest tests/ -m "not slow" -v
```

---

## Getting Help

1. **Check this troubleshooting guide** for your error
2. **Read the logs**: `~/.claudetrade/logs/claudetrade.log`
3. **Verify the config**: Compare to `config.example.toml`
4. **Isolate the issue**: Disable optional features (Reddit, X, AI) to narrow down the problem
5. **Open an issue**: If stuck, file a GitHub issue with:
   - Error message and full traceback
   - Steps to reproduce
   - Your config (sanitized of secrets)
   - Relevant logs

---

## Common Gotchas

1. **Config environment variable vs file**: Env vars override TOML; check both
2. **Credentials are never logged**: You will not see the actual API key in logs (by design)
3. **Synthetic data is not real**: Backtests on synthetic data prove nothing about real markets
4. **Win/loss ratio is easy to game**: The system includes validation warnings to catch this
5. **Earnings dates can leak**: If fetched seconds before market close, the date is effectively known
6. **Social sentiment is mutable**: Upvote counts change; historical sentiment is approximate
