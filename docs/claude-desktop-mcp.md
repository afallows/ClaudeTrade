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
| `get_signals(min_score, limit)` | read-only | Current ledger signals: symbol, strategy, direction, score, confidence, entry/stop/targets, days to earnings. |
| `get_sentiment(symbol, days)` | read-only | Daily sentiment/mention rows for one symbol over the last N days. |
| `get_trending(limit)` | read-only | Symbols ranked by recent mention volume. |
| `get_market_status()` | read-only | Regime, current Eastern time, whether the market is pre-market/open/after-hours/closed, last refresh time, symbol coverage, provider health. |
| `run_scan()` | **write** | Runs a full scan for today's session and records new signals to the immutable ledger (same as `claudetrade scan`). |
| `trigger_refresh()` | **write, background** | Starts a data refresh (market data, earnings, sentiment) on a background thread; can take several minutes on a large universe. |
| `get_refresh_status()` | read-only | Progress of a refresh started by `trigger_refresh`. |
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

## Concurrent use with the web UI

SQLite is run in WAL (write-ahead log) mode, so `claudetrade mcp` can read
from the same database while `claudetrade ui` is also running — you do not
need to stop one to use the other. `run_scan` and `trigger_refresh` write
through the normal application logic (the immutable signal ledger, the same
refresh path `claudetrade refresh` uses), so their effects show up in the web
UI too, and vice versa.

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
