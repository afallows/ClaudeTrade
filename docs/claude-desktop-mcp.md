# Claude Desktop + ClaudeTrade (MCP server)

ClaudeTrade ships a local [MCP](https://modelcontextprotocol.io) (Model
Context Protocol) server: `claudetrade mcp`. It lets the Claude Desktop app
talk to your own, already-running ClaudeTrade installation directly — ask it
things like "what's trending this morning" or "run a scan and summarise the
top candidates" without opening the web UI.

This is written for Windows, since that's where Claude Desktop runs; the
`claudetrade mcp` command itself is cross-platform (see the note at the
bottom for macOS/Linux paths).

**What this is not**: a way to expose ClaudeTrade over the network, or a
second copy of your data. The server talks to Claude Desktop over stdio
(standard input/output of the process Claude Desktop itself launches) — see
[Security](#security) below.

---

## Prerequisites

- ClaudeTrade already installed and working from a terminal (see
  [docs/windows-install.md](windows-install.md)) — `claudetrade version`
  should print without error.
- The optional `mcp` package installed into the same virtual environment:
  ```
  pip install claudetrade[mcp]
  ```
  (If you installed with `pip install -e .` from source, run that from the
  `ClaudeTrade` folder with the venv activated instead:
  `pip install -e .[mcp]`.) Every other ClaudeTrade command works without
  this package; only `claudetrade mcp` needs it, and it says so clearly if
  it's missing rather than starting anyway.
- Claude Desktop for Windows, installed and signed in.

## Step 1: Find your `claudetrade` command's full path

Claude Desktop launches `claudetrade mcp` itself — it does not use your
terminal's `PATH`, so the config file below needs the *full path* to the
`claudetrade.exe` inside your virtual environment. If you followed
[docs/windows-install.md](windows-install.md) and cloned/extracted ClaudeTrade
to `C:\ClaudeTrade`, that path is:

```
C:\ClaudeTrade\.venv\Scripts\claudetrade.exe
```

Confirm it exists and works before continuing:
```
C:\ClaudeTrade\.venv\Scripts\claudetrade.exe version
```
If your install lives somewhere else, substitute your own folder — the
important part is `...\.venv\Scripts\claudetrade.exe`.

(A frozen/PyInstaller build's `claudetrade.exe` — see
[docs/windows-build.md](windows-build.md) — does not currently bundle the
optional `mcp` package; use the venv path above for the MCP server today.)

## Step 2: Edit `claude_desktop_config.json`

In Claude Desktop: **Settings → Developer → Edit Config**. This opens (or
creates) `claude_desktop_config.json` in your text editor — on Windows it
lives at `%APPDATA%\Claude\claude_desktop_config.json`.

Add a `claudetrade` entry under `mcpServers`. If the file is empty/new, this
is the whole thing:

```json
{
  "mcpServers": {
    "claudetrade": {
      "command": "C:\\ClaudeTrade\\.venv\\Scripts\\claudetrade.exe",
      "args": ["mcp"]
    }
  }
}
```

If you already have other servers configured, add `"claudetrade": { ... }`
as another key inside the existing `mcpServers` object rather than replacing
the file. Note the doubled backslashes (`\\`) — required JSON escaping for a
Windows path.

If your `config.toml` is not at ClaudeTrade's default location, add
`"--config", "C:\\path\\to\\config.toml"` to the `args` array (matching every
other `claudetrade` command's `--config`/`-c` option).

## Step 3: Restart Claude Desktop

Fully quit Claude Desktop (not just close the window — use the tray icon or
Task Manager) and reopen it. MCP servers are only started at launch. Once it's
back, `claudetrade` should appear as a connected server in **Settings →
Developer**, and its tools become available in new conversations.

## Step 4: Try it

Example prompts:

- *"What's trending in ClaudeTrade sentiment this morning?"*
- *"Is the market open right now, and what does ClaudeTrade's regime say?"*
- *"Run a scan and give me today's top three candidates with their entry
  plans."*
- *"What's the recent sentiment on NVDA — is mention volume picking up?"*
- *"Start a data refresh and let me know when it's done."*
- *"How have the strategies performed historically?"* (run
  `claudetrade backtest report` on this machine first — see
  `docs/backtest-report.md`)

## What the server can do

`claudetrade mcp` bootstraps its own copy of the pipeline (the same
`Pipeline.bootstrap(config)` call every other entry point makes) and exposes
these tools. All of them return plain JSON — no charts, no pandas objects.

| Tool | Reads/Writes | What it returns |
| --- | --- | --- |
| `get_signals(min_score, limit, sort)` | read-only | Current ledger signals, **best-scoring first** (so `limit=N` means the N best, matching the web Screener); `sort='created_at'` gives newest-first for audit. Includes `total_matching`/`truncated` so a page is distinguishable from the whole answer. Fields: symbol, strategy, direction, score, confidence, entry/stop/targets, days to earnings. |
| `get_sentiment(symbol, days)` | read-only | Daily sentiment/mention rows for one symbol over the last N days. |
| `get_trending(limit, source)` | read-only | Symbols ranked by recent mention *volume*, most-mentioned first. `source='auto'` prefers ApeWisdom's Reddit/4chan counts when present (broader corpus, pre-resolved tickers) and falls back to locally-resolved posts. Absolute volume, so it returns the same large caps most days -- for what is *changing*, use `get_rising_sentiment`. |
| `get_rising_sentiment(limit, recent_sessions, baseline_sessions, min_recent_mentions)` | read-only | Symbols whose mention rate is accelerating against their **own** recent baseline, so a quiet name waking up ranks above a permanently-loud one. Each row carries mention change, recent vs baseline rate, and sentiment change where polarity was actually measured. Includes a coverage block stating how much stored history backs the ranking. |
| `get_sentiment_history(symbol, days)` | read-only | One symbol's daily mention/sentiment series, gap-filled across trading sessions so it can be charted or differenced directly. `observed` marks a real stored row, distinguishing a measured zero from absent data. |
| `get_market_status()` | read-only | Regime, current Eastern time, whether the market is pre-market/open/after-hours/closed, last refresh time, symbol coverage, provider health, and `sentiment_readiness` — how many sessions of social history this installation has actually accumulated, as a tier. |
| `run_scan()` | **write** | Runs a full scan for today's session and records new signals to the immutable ledger (same as `claudetrade scan`). |
| `trigger_refresh()` | **write, background** | Starts a data refresh (market data, earnings, sentiment) on a background thread; can take several minutes on a large universe. Refuses (naming the holder) while a refresh started from *any* entry point is running. |
| `get_refresh_status()` | read-only | Progress of the current refresh or automatic social collection, whichever entry point started it — CLI, web UI, this server, or the web server's hourly collector; `entry_point` names the owner and `scheduled: true` means nobody asked for it. |
| `get_backtest_report()` | read-only | The latest `claudetrade backtest report` (see `docs/backtest-report.md`): per-strategy walk-forward win rate/expectancy/profit factor/drawdown, each gated behind a prominent significance verdict. Never runs a backtest itself — returns `available: false` with instructions if none has been generated yet. |

Every read-only tool queries the exact same ledger/database objects the web
UI and CLI use (`pipeline.ledger`, the daily sentiment table, provider
status) — nothing here recomputes a score or a filter rule independently.
`get_signals` includes the same standing research-only disclaimer the CLI
and UI show, once per response.

### "Before/at market open" — how to ask about it

`get_market_status()` is the tool to reach for first: it reports the current
Eastern time and whether the market is `pre_market`, `open`, `after_hours` or
`closed` right now, alongside the regime and how fresh the stored data is.
A good morning routine is: ask for market status, then trending symbols, then
sentiment on whatever stands out, then (optionally) a scan.

Market status also carries `sentiment_readiness`, which is worth reading once
before trusting any mention trend. Social history cannot be backfilled — the
sources only serve the last few days — so it is accumulated forward, one
collection at a time, by the hourly collector inside the web API server (and
by `claudetrade sentiment collect`). The tier says how much of that baseline
exists: `warming_up` (fewer than 20 sessions), `provisional` (20+), `partial`
(60+), `ready` (120+). It is a label, never a gate: no tool refuses to answer
because of it.

## Concurrent use with the web UI and CLI

SQLite is run in WAL (write-ahead log) mode, so `claudetrade mcp` can read
from the same database while `claudetrade ui` is also running — you do not
need to stop one to use the other. `run_scan` and `trigger_refresh` write
through the normal application logic (the immutable signal ledger, the same
refresh path `claudetrade refresh` uses), so their effects show up in the web
UI too, and vice versa.

**One refresh at a time, across all three.** A data refresh is the one
operation that must not overlap itself: the CLI, the web UI and this server
write the same database file, so a second concurrent refresh would race the
first one's writes. The lock lives in the database, so it holds across
processes:

- `get_refresh_status()` reports a refresh **whichever entry point started
  it** — a `claudetrade refresh` running in your terminal shows up here, with
  its phase and symbol progress, not as "idle".
- `trigger_refresh()` refuses while another entry point holds the lock and
  says who holds it, when they started and how far along they are.
- If the process holding the lock dies, the lock goes stale and is taken over
  automatically after about two minutes; nothing needs manual unlocking.

**Every tool call is time-bounded.** Reads are answered or they return a
structured `{"timed_out": true, ...}` error — they never hang the client, even
while a refresh is hammering the database, and one slow call never blocks the
others. `run_scan` gets its own much larger deadline because a full-universe
scan is legitimately slow; if it does report `timed_out`, it is still running
in the background and its signals will land in the ledger. Both deadlines are
configurable under `[mcp]` in `config.toml` (`tool_timeout_seconds`, default
30; `scan_timeout_seconds`, default 300).

## Security

`claudetrade mcp` uses the **stdio transport**: Claude Desktop starts it as a
local subprocess and talks to it over that process's standard input/output.
There is no network port opened and no new remote-access surface — this is
consistent with the rest of the application's localhost-only, single-owner,
personal-use posture (see [docs/security-and-privacy.md](security-and-privacy.md)).
The server runs under your own Windows account with your own file
permissions; anyone who could run this MCP server could already run
`claudetrade` directly from a terminal.

## Troubleshooting

- **Claude Desktop shows the server as failed/disconnected**: run the exact
  `command`/`args` from your config directly in a terminal (e.g.
  `C:\ClaudeTrade\.venv\Scripts\claudetrade.exe mcp`) — any startup error
  (missing `mcp` package, a broken `config.toml`) prints there instead of
  being hidden inside Claude Desktop's own log viewer.
- **"The 'mcp' package is not installed"**: run
  `pip install claudetrade[mcp]` in the same virtual environment
  `claudetrade` runs from, then restart Claude Desktop.
- **Tools return empty results**: `claudetrade mcp` reads whatever is already
  in your database — it does not refresh automatically on startup. Run
  `claudetrade refresh` (or the `trigger_refresh` tool) first if the database
  is new or stale.
- **macOS/Linux**: the same `claudetrade mcp` command works; use the venv's
  `bin/claudetrade` (e.g. `/path/to/ClaudeTrade/.venv/bin/claudetrade`)
  instead of `.venv\Scripts\claudetrade.exe`, and Claude Desktop's config file
  lives at `~/Library/Application Support/Claude/claude_desktop_config.json`
  (macOS) — Claude Desktop is not currently available for Linux.
