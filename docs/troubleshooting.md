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
1. Ensure venv is activated: `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\activate` (Windows Command Prompt) or `.venv\Scripts\Activate.ps1` (Windows PowerShell)
2. Verify: Prompt should show `(.venv)` prefix
3. Retry: `pip install -r requirements.txt` and `pip install -e .`

### "ModuleNotFoundError: No module named 'claudetrade'" / "'claudetrade' is not recognized"

**Error**: Python cannot find the package, or the `claudetrade` command doesn't exist.

**Cause**: `pip install -r requirements.txt` installs only the third-party dependencies
(pandas, streamlit, etc.) — it does **not** install the ClaudeTrade package itself or
register the `claudetrade` console command. A separate install step is required.

**Solution**:
1. Ensure venv is activated
2. Ensure you are in the ClaudeTrade root directory (the one containing `pyproject.toml`)
3. Install the package: `pip install -e .` (editable install; `pip install .` also works
   for a non-development trial)
4. Verify: `claudetrade version` should print a version string, not an error

---

## Configuration

### "config.toml not found"

**Error**: Application fails to load configuration.

**Solution**:
1. A missing config file is not actually an error — `claudetrade` runs on built-in,
   offline-safe defaults if no `config.toml` exists. This entry only applies if you
   explicitly point `--config` or `CLAUDETRADE_CONFIG` at a file that isn't there.
2. Copy the example to the app directory:
   - macOS/Linux: `mkdir -p ~/.claudetrade && cp config.example.toml ~/.claudetrade/config.toml`
   - Windows (PowerShell): `New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\ClaudeTrade" | Out-Null; Copy-Item config.example.toml "$env:LOCALAPPDATA\ClaudeTrade\config.toml"`
3. Or set the path explicitly: `export CLAUDETRADE_CONFIG=/path/to/config.toml` (or `set CLAUDETRADE_CONFIG=...` on Windows)
4. Verify: `echo $CLAUDETRADE_CONFIG` (Linux/macOS) or `echo %CLAUDETRADE_CONFIG%` (Windows)
5. **If you copied `config.example.toml`**: delete or comment out the `app_dir = "~/.claudetrade"`
   line under `[paths]` before saving it to Windows. That value is not `~`-expanded when it
   comes from the config file, so on Windows it would be treated as a *relative* path (a
   literal `~` folder under wherever you run the command from) instead of your real home
   directory. Leaving `app_dir` out of the file lets it fall back to the correct per-OS
   default (`%LOCALAPPDATA%\ClaudeTrade` on Windows, `~/.claudetrade` on macOS/Linux).

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
   claudetrade refresh
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

### "Refusing to start: a refresh started by the &lt;cli|webapi|mcp&gt; entry point is already running"

**Cause**: Not an error. Only one data refresh may run at a time across the
whole installation — the CLI, the web UI and the MCP server all write the same
SQLite file, so a second concurrent refresh would race the first one's writes.
The lock lives in the database (`refresh_runs` table), so it holds across
processes, and the message names which entry point started the run, when, and
how far along it is.

**Solution**:
1. Wait for the running refresh to finish. Watch it from anywhere:
   - MCP: the `get_refresh_status` tool
   - Web UI/API: `GET /api/system/refresh/status`

   Both report the run whichever entry point started it; `entry_point` names
   the owner.
2. If the process that held the lock died (machine slept, terminal closed,
   task killed), its run stops heartbeating and is automatically taken over
   after about two minutes — retry then. The abandoned run is recorded as
   `failed` with `stale lock taken over`, so it stays visible rather than
   silently disappearing.
3. There is no manual unlock command by design; nothing needs one, because a
   dead lock always expires on its own.

### MCP tool returns `"timed_out": true`

**Error**: An MCP tool call returns
`{"error": "get_signals did not complete within 30s", "timed_out": true, "hint": "a data refresh may be holding the database; retry shortly"}`.

**Cause**: The call exceeded its deadline — almost always because a data
refresh is writing heavily to the same database. The MCP server answers with
this payload instead of hanging: a bounded error is recoverable, an unbounded
wait wedges the client. Other tool calls keep working while one is stuck.

**Solution**:
1. Check whether a refresh is running (`get_refresh_status`) and retry once it
   finishes — this is the expected path.
2. `run_scan` has its own, much larger deadline (a full-universe scan is
   legitimately slow). If it reports `timed_out`, the scan is still running in
   the background and its signals will land in the ledger; check `get_signals`
   again shortly rather than re-running it.
3. Raise the deadlines only if your installation is genuinely slow (a very
   large universe on slow storage):
   ```toml
   [mcp]
   tool_timeout_seconds = 60      # default 30, for ordinary reads
   scan_timeout_seconds = 600     # default 300, for run_scan only
   ```

### "table signals already exists" (migration)

**Error**: Migration fails because the table exists.

**Solution**:
1. This should not happen (migrations are idempotent)
2. Check the `schema_version` table:
   ```bash
   python -c "
   from claudetrade.db.session import get_database
   from claudetrade.db.models import SchemaVersion
   from claudetrade.config import AppConfig
   db = get_database(AppConfig.load())
   with db.read_session() as s:
       print(s.query(SchemaVersion).all())
   "
   ```
3. If migrations are incomplete, back up, delete and re-init. The database file lives
   at `<app_dir>/data/claudetrade.db` (`<app_dir>` is `%LOCALAPPDATA%\ClaudeTrade` on
   Windows, `~/.claudetrade` on macOS/Linux, unless overridden):
   ```bash
   claudetrade db backup           # keep a copy first
   rm ~/.claudetrade/data/claudetrade.db      # macOS/Linux
   # del "%LOCALAPPDATA%\ClaudeTrade\data\claudetrade.db"   # Windows
   claudetrade init                 # re-create and re-run migrations
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

## UI and Visualization

### "streamlit: port already in use" / "port 8501 is in use"

**Error**: `claudetrade ui` fails to bind, or the browser tab shows a connection error.

**Solution**:
```bash
# Change the port for one run
claudetrade ui --port 8502

# Or change it permanently in config.toml
[ui]
port = 8502
```
To find and stop whatever is already using port 8501:
```bash
# macOS/Linux
lsof -i :8501   # find the PID
kill -9 <PID>

# Windows (PowerShell)
Get-NetTCPConnection -LocalPort 8501 | Select-Object -ExpandProperty OwningProcess
Stop-Process -Id <PID> -Force
```

### UI backtest "Export as CSV/Excel" button does nothing

**Not a bug you can fix locally** — the Streamlit backtesting screen's export buttons
are placeholders that only show an informational message; they do not write a file
(see `src/claudetrade/ui/screens/backtesting.py`). Use the CLI instead, which does
write files:
```bash
claudetrade backtest --export ./exports --report ./exports/report.md
```

---

## Windows-Specific Issues

### "'python' is not recognized as an internal or external command"

**Cause**: Python was installed without adding it to `PATH`, or the terminal was opened
before the install finished.

**Solution**:
1. Re-run the Python installer from https://www.python.org/, choose "Modify", and tick
   **"Add python.exe to PATH"** (or re-run the installer fresh and tick it on the first screen).
2. Close and reopen the terminal (PATH changes do not apply to already-open windows).
3. Verify: `py --version` or `python --version` should print 3.11 or higher. On Windows,
   `py` (the Python launcher) is usually more reliable than `python` if multiple Pythons
   are installed — use `py -m venv .venv` if plain `python` is not found.

### "running scripts is disabled on this system" (PowerShell execution policy)

**Error**: Activating the venv in PowerShell (`.venv\Scripts\Activate.ps1`) fails with
`... cannot be loaded because running scripts is disabled on this system.`

**Cause**: PowerShell's default execution policy blocks running local `.ps1` scripts,
including the venv's own activation script.

**Solution**: Allow locally-created scripts for the current user only (does not require
admin rights and does not weaken security for scripts downloaded from the internet):
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
Then retry `.venv\Scripts\Activate.ps1`. Alternatively, use `.venv\Scripts\activate.bat`
from Command Prompt (`cmd.exe`) instead of PowerShell — `.bat` files are not affected by
this policy.

### Where ClaudeTrade stores its data on Windows

By default (no `app_dir` set in `config.toml` and no `CLAUDETRADE_HOME` set), ClaudeTrade
stores everything under:
```
%LOCALAPPDATA%\ClaudeTrade\
  data\        # claudetrade.db (SQLite), historical bars, universe cache
  logs\        # claudetrade.log, audit.log
  exports\     # CSV/Excel output from `claudetrade backtest --export`
  backups\     # `claudetrade db backup` snapshots
  cache\
  snapshots\
```
This resolves via `default_app_dir()` in `src/claudetrade/config.py`, which checks
`%LOCALAPPDATA%` (falling back to the user profile directory if unset). To see the exact
paths for your machine, run `claudetrade init` — it prints `data dir:` and `logs dir:`.
In File Explorer, paste `%LOCALAPPDATA%\ClaudeTrade` into the address bar to open it directly.

**Caveat**: if your `config.toml` sets `app_dir` explicitly (as `config.example.toml`
does, to `~/.claudetrade`), that value is used literally and is **not** `~`-expanded —
see the "config.toml not found" entry above. Comment that line out to get the correct
Windows default.

### Corporate proxy / TLS interception

**Symptoms**: `claudetrade probe` reports hosts as `BLOCKED` with a `proxy refused` or
`connect failed` note; `claudetrade refresh` degrades with provider failures; `pip install`
fails to reach PyPI.

**Solution**:
1. Set the standard proxy environment variables before running any command (PowerShell):
   ```powershell
   $env:HTTPS_PROXY = "http://proxy.example.corp:8080"
   $env:HTTP_PROXY  = "http://proxy.example.corp:8080"
   ```
   or in Command Prompt: `set HTTPS_PROXY=http://proxy.example.corp:8080`
2. If the network uses TLS-inspecting middleboxes, `httpx` (used for all outbound calls)
   needs the corporate CA certificate trusted. Point Python's certificate bundle at a
   combined PEM that includes your corporate CA, e.g. via the `SSL_CERT_FILE` environment
   variable, or ask your IT administrator for the correct certificate bundle path.
3. Run `claudetrade probe` after each change to confirm whether the fix worked — it
   distinguishes a blocked network from a missing credential in its output.
4. If you're in a managed environment where the proxy allow-list is administered
   centrally, `probe`'s "Hosts to allow-list" line lists exactly which hostnames need
   adding; this is something only an administrator can grant, not something the app can
   work around.
5. If live data sources are unreachable and can't be fixed quickly, explicitly switch
   the app to offline demo mode: set `market_data.provider = "synthetic"`,
   `earnings.provider = "synthetic"`, and `reddit.provider = "synthetic"`. Market data
   defaults to Stooq, so synthetic mode is an intentional opt-in and its tickers are
   fabricated.

---

## General Debugging

### "Enable debug logging"

**Solution**:
```toml
[logging]
level = "DEBUG"
console = true
```

Then check the logs (`<app_dir>/logs/claudetrade.log`):
```bash
tail -f ~/.claudetrade/logs/claudetrade.log                        # macOS/Linux
# Get-Content "$env:LOCALAPPDATA\ClaudeTrade\logs\claudetrade.log" -Wait   # Windows PowerShell
```

### "Check audit log"

**Solution**:
```bash
python -c "
from claudetrade.db.session import get_database
from claudetrade.config import AppConfig
from claudetrade.db.models import AuditLog

db = get_database(AppConfig.load())
with db.read_session() as s:
    for row in s.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(10):
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
