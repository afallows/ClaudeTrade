# Windows Smoke Test Checklist

A manual, end-to-end checklist for a non-developer tester to work through on a
Windows 10/11 machine. Complete [docs/windows-install.md](windows-install.md)
first, or start from Step 1 below which repeats the essential setup commands.
Each step names the exact command, what "it worked" looks like, what "it
didn't" looks like, and which troubleshooting entry to check.

Tick each box as you go. If a step fails, stop there, check the linked
troubleshooting entry, and only continue once it's resolved (later steps
generally depend on earlier ones).

---

## 1. [ ] Fresh install

**Command**:
```
py -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
claudetrade version
```

**Pass looks like**: `claudetrade version` prints `claudetrade 0.1.0
(code_version=...)` followed by the research-only disclaimer, with no
traceback.

**Fail looks like**: `'python'`/`'py'` not recognized; `'claudetrade' is not
recognized`; a `pip` error partway through the install.

**Troubleshooting**: [Python 3.11 not found](troubleshooting.md#python-311-not-found),
["'python' is not recognized..."](troubleshooting.md#python-is-not-recognized-as-an-internal-or-external-command),
[ModuleNotFoundError: No module named 'claudetrade'](troubleshooting.md#modulenotfounderror-no-module-named-claudetrade--claudetrade-is-not-recognized).

---

## 2. [ ] `claudetrade init` — database creation

**Command**: `claudetrade init`

**Pass looks like**: Prints `database:`, `schema:  v3 (applied [1, 2, 3])`,
`data dir:`, `logs dir:`, and `config hash:` lines. No traceback.

**Fail looks like**: A Python traceback, or a `PermissionError` writing to
`%LOCALAPPDATA%`.

**Troubleshooting**: [config.toml not found](troubleshooting.md#configtoml-not-found),
[Where ClaudeTrade stores its data on Windows](troubleshooting.md#where-claudetrade-stores-its-data-on-windows).

---

## 3. [ ] `claudetrade probe` — network/credential check

**Command**: `claudetrade probe`

**Pass looks like**: A table with columns `SOURCE HOST NETWORK CREDENTIAL
NOTE`, ending in either "All probed hosts are reachable." or a yellow warning
naming which hosts are blocked. Either outcome is a **pass** for this step —
`probe` is a diagnostic, not a gate, and the rest of this checklist works even
if every host shows `BLOCKED` (synthetic data needs no network).

**Fail looks like**: The command itself crashes with a traceback (as opposed
to reporting hosts as blocked, which is normal and not a failure).

**Troubleshooting**: [Corporate proxy / TLS interception](troubleshooting.md#corporate-proxy--tls-interception).

---

## 4. [ ] `claudetrade refresh` — pull data

**Command**: `claudetrade refresh --start 2024-01-01 --end 2024-12-31`

**Pass looks like**: A JSON block with a `summary` object showing a nonzero
`universe` count and (usually) a nonzero `sentiment_rows` count. A yellow
"Some sources were unavailable..." line may appear — that's a pass too (it's
the graceful-degradation path, not a failure); check `degraded` in the JSON to
see which source, if you're curious.

**Fail looks like**: A traceback, or a JSON summary where `universe` is `0`
(if `universe.source = "database"`, the default, this means the market
provider returned nothing — check `claudetrade status` for provider errors).

**Troubleshooting**: ["stale data" warning](troubleshooting.md#stale-data-warning),
["provider failed" entries](troubleshooting.md#provider-failed-connection-timeout).

---

## 5. [ ] `claudetrade scan` — ranked candidates or an honest empty list

**Command**: `claudetrade scan`

**Pass looks like** (either of these is a pass):
- A ranked table with columns `SYMBOL STRATEGY DIR SCORE CONF R:R SHARES
  STATUS` and one row per candidate, **or**
- The line `No candidate cleared the thresholds. An empty list is a valid
  result.` — this is a legitimate, honest outcome, not a bug. Whether you get
  candidates depends on the synthetic data's random seed and the configured
  score/confidence/reward:risk thresholds.

Both outcomes print the disclaimer and a `session ... | regime ... | N symbols
evaluated` summary line first; `N` should match (or be close to) the
`universe` count from Step 4.

**Fail looks like**: `scan produced no result` printed in red, with a nonzero
exit code. This specific message means the CLI got no `ScanResult` object at
all — almost always because Step 4 (`refresh`) was skipped or ingested zero
securities, not because thresholds were too strict. Re-run Step 4 first.

**Troubleshooting**: ["no signals generated" or "all signals rejected"](troubleshooting.md#no-signals-generated-or-all-signals-rejected).

---

## 6. [ ] `claudetrade ui` opens in the browser; all 5 screens load

**Command**: `claudetrade ui`

**Pass looks like**: Terminal prints `starting the interface on port 8501
...` followed by Streamlit's banner ending in a `Local URL:
http://localhost:8501` line; a browser tab opens (or you open one manually) to
that address, showing "ClaudeTrade" in the sidebar. Click through all five
items in the sidebar radio list and confirm each renders without a red
"exception" traceback box:
- [ ] **Dashboard** — market regime and candidate summary
- [ ] **Scanner** — filterable candidate table
- [ ] **Ticker Detail** — pick a symbol (e.g. one from Step 5's table, or
      type any symbol ingested in Step 4) and confirm a chart renders
- [ ] **Backtesting** — see Step 7 below
- [ ] **Settings** — configuration, secrets status, risk limits

**Fail looks like**: The terminal shows a Python traceback instead of
Streamlit's banner; the browser can't connect; or a screen shows Streamlit's
red exception box instead of content.

**Troubleshooting**: [streamlit: port already in use](troubleshooting.md#streamlit-port-already-in-use--port-8501-is-in-use).

Leave this terminal window running for the remaining UI-based steps below;
open a **second** terminal (with the venv activated again) for CLI steps.

---

## 7. [ ] Backtest runs and renders metrics + funnel

Two ways to check this — do both.

**CLI** (in your second terminal): `claudetrade backtest --start 2024-01-01 --end 2024-12-31`

**Pass looks like**: `building contexts for N symbols...`, then `running
backtest over N sessions...`, then the disclaimer, then a markdown metrics
report (win rate, expectancy, Sharpe/Sortino/Calmar, profit factor, etc.),
then a `## Rejection Funnel` section listing every candidate and why it was or
wasn't traded. Zero completed trades is a valid, clearly-labelled outcome, not
a failure — the funnel explains why.

**UI**: On the **Backtesting** screen, configure a date range and run a
backtest from there; confirm the metrics table and rejection-funnel content
render without an exception.

**Fail looks like**: `universe is empty -- run 'claudetrade refresh' first`
(re-run Step 4), or a traceback.

**Troubleshooting**: ["backtest very slow" or "freezes"](troubleshooting.md#backtest-very-slow-or-freezes),
["win/loss ratio is degenerate"](troubleshooting.md#winloss-ratio-is-degenerate).

---

## 8. [ ] Paper account inspection (not a full "lifecycle" — see note)

**Command**: `claudetrade paper status` then `claudetrade paper positions`

**Pass looks like**: `paper status` prints `account 'default': equity
100,000.00 cash 100,000.00 realised 0.00` (or your configured
`risk.account_size_usd`) plus a JSON performance block — `0` for every trade
count on a fresh account is correct. `paper positions` prints `no open paper
positions`.

**Note — this is a known, documented gap, not a bug you're expected to work
around**: there is currently **no CLI command or UI button that opens a new
paper trade**. `claudetrade paper status/positions/kill-switch` only *inspect*
the account and gate new entries; the underlying `PaperBroker.submit_signal`
that would actually open a position is implemented and tested
(`tests/test_broker_contract.py`) but isn't wired to any user-facing command
yet. So a genuine end-to-end "open a paper trade, watch it get marked to
market, close it" lifecycle **cannot be exercised from the CLI or UI today** —
only the inspection half can. See
[docs/known-limitations.md](known-limitations.md#opening-a-paper-trade).

**Also check**: `claudetrade paper kill-switch` (engages) and `claudetrade
paper kill-switch --release` (releases) — both should print a coloured
confirmation line and not error.

**Fail looks like**: A traceback on any of the four commands above.

---

## 9. [ ] Export works (via CLI backtest, not the UI buttons)

**Command**: `claudetrade backtest --start 2024-01-01 --end 2024-12-31 --export .\exports --report .\exports\report.md`

**Pass looks like**: After the backtest output, two lines: `CSV exported to
exports` and `report written to exports\report.md`. Check the `exports`
folder now contains `trades.csv`, `equity_curve.csv`, `metrics.csv`, and
`report.md`.

**Known gap — do not test this via the UI**: the Backtesting screen's "Export
as CSV" / "Export as Excel" buttons are placeholders. Clicking them shows an
informational message ("CSV export would contain...") but **does not write a
file**. This is a real, pre-existing gap in the UI, not a broken install —
use the CLI command above instead. See
[docs/known-limitations.md](known-limitations.md#streamlit-ui-dashboard).

**Fail looks like**: No files appear in the target folder after the CLI
command reports success (that would be a genuine bug, unlike the UI buttons
above).

---

## 10. [ ] Database backup works

**Command**: `claudetrade db backup`

**Pass looks like**: `backup written to <path>\backups\claudetrade-<UTC
timestamp>.ctbak.db`. Confirm the file exists at that path and is a nonzero
size (comparable to your `data\claudetrade.db` file).

**Fail looks like**: A traceback, or a 0-byte file.

**Troubleshooting**: ["database is locked" (SQLite)](troubleshooting.md#database-is-locked-sqlite).

---

## 11. [ ] `claudetrade verify ledger` — integrity check

**Command**: `claudetrade verify ledger`

**Pass looks like**: Green text: `all signals verified: none have been
modified since they were written`. (If Step 5 produced zero signals, this
still passes — there's simply nothing to check, and the command says so
implicitly by not failing.)

**Fail looks like**: Red text listing failed signal IDs, and a nonzero exit
code. This would indicate real data corruption, not a documentation or
install problem — back up the database (Step 10) before investigating further.

---

## 12. [ ] `claudetrade status` — final coverage check

**Command**: `claudetrade status`

**Pass looks like**: `mode: paper (live trading is not implemented)`, a list
of enabled sources, a provider table (`ok`/`down` per provider), and a
`stored data:` block with nonzero `securities`, `price_bars`, and (usually)
`social_posts` and `signals` counts reflecting everything you did in Steps
4–7.

**Fail looks like**: All counts still `0` after Step 4 succeeded (would
indicate the refresh wrote to a different database than `status` is reading —
check for multiple `config.toml` files or a stray `CLAUDETRADE_CONFIG`/
`CLAUDETRADE_HOME` environment variable).

---

## 13. [ ] `claudetrade secrets list` — credential visibility (no values shown)

**Command**: `claudetrade secrets list`

**Pass looks like**: One line per known credential name (`anthropic_api_key`,
`reddit_client_id`, `reddit_client_secret`, `x_bearer_token`,
`notify_webhook_url`), each showing `yes`/`no` for "configured" and a masked
tail (`****ab12`) if set — never the real value. If you completed
[docs/windows-install.md Step 7](windows-install.md#step-7-optional-turn-on-real-data-with-reddit-and-stooq),
`reddit_client_id`/`reddit_client_secret` should show `yes`.

**Fail looks like**: A traceback, or a credential you know you set showing `no`.

**Troubleshooting**: ["credential not found"](troubleshooting.md#credential-not-found).

---

## 14. [ ] Windows launcher scripts

**Command** (from a fresh Command Prompt, venv **not** pre-activated):
```
scripts\run_ui.bat
```

**Pass looks like**: The script activates `.venv` itself, prints "Starting
ClaudeTrade UI on port 8501...", and launches the same UI as Step 6.

**Fail looks like**: `Virtual environment not found at <path>` when `.venv`
genuinely exists at the project root (would indicate the path arithmetic in
the script is broken again — see the comment at the top of
`scripts\run_ui.bat` explaining the exact failure mode this previously had).

---

## 15. [ ] Shut down cleanly

**Command**: `Ctrl+C` in every terminal running `claudetrade ui` or a launcher
script; close the terminal windows.

**Pass looks like**: No lingering `streamlit`/`python` processes (check Task
Manager if unsure); port 8501 free again (`claudetrade ui` should start
cleanly next time without a "port in use" error).

---

## Summary

| # | Step | Exercises |
|---|------|-----------|
| 1 | Fresh install | Python/pip/venv, package install |
| 2 | `init` | Database creation, migrations, app-dir resolution |
| 3 | `probe` | Network/credential diagnostics |
| 4 | `refresh` | Data ingestion pipeline |
| 5 | `scan` | Signal generation, ranking |
| 6 | `ui` | Streamlit app, all 5 screens |
| 7 | `backtest` | Backtest engine, metrics, rejection funnel |
| 8 | `paper status`/`positions`/`kill-switch` | Paper account inspection (not entry — documented gap) |
| 9 | `backtest --export` | CSV/report export (CLI works; UI buttons are placeholders) |
| 10 | `db backup` | Database backup |
| 11 | `verify ledger` | Signal-ledger integrity |
| 12 | `status` | End-to-end data coverage summary |
| 13 | `secrets list` | Credential storage round-trip |
| 14 | `scripts\run_ui.bat` | Windows launcher script |
| 15 | Shutdown | Clean process/port teardown |

If every box is ticked, the install is sound and every implemented feature has
been exercised at least once. Two things above are **known, pre-existing
gaps** rather than install problems if you hit them: no CLI/UI way to open a
paper trade (Step 8), and non-functional UI export buttons (Step 9, use the
CLI). Both are tracked in [docs/known-limitations.md](known-limitations.md).
