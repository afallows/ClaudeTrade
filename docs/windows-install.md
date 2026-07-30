# Installing and First-Running ClaudeTrade on Windows

This is a step-by-step guide for someone trialling ClaudeTrade on Windows 10 or 11
who does not normally write code. It does not assume `git` is installed. Every
command below was checked against the actual CLI (`src/claudetrade/cli.py`) —
if what you see on screen differs meaningfully from what's described here,
that's a documentation bug; check [docs/troubleshooting.md](troubleshooting.md) first.

**Market prices are live by default.** ClaudeTrade uses Stooq daily history for
the US + TSX universe and requires internet access, but no Stooq account or API
key. Synthetic (fabricated) prices are available only when explicitly selected
for an offline demo.

---

## Fastest path: one script

If you just want the app running and don't need to understand each step, this
is the whole install:

1. Get the source onto the machine — either download and extract the `.zip`
   from the repository, or, if you have `git`:
   ```
   git clone https://github.com/afallows/claudetrade.git
   ```
2. Open the extracted/cloned `ClaudeTrade` folder in File Explorer and
   **double-click `scripts\setup.bat`**.
3. Done. The script checks for Python 3.11+ (installing it via `winget` if
   it's missing), creates the virtual environment, installs everything,
   creates the database, loads the last 90 days of data, and opens the app —
   all in one run. A console window stays open showing progress; leave it
   open while you use the app, and press a key in it (or close the app
   window) when you're done.

Re-running `scripts\setup.bat` later is safe and fast — every step skips or
repeats cleanly if it's already done. See `scripts\setup.ps1 -?` (or its
header comment) for optional flags: `-SkipData` (skip the data load on a
re-run), `-Classic` (open the legacy Streamlit UI instead of the desktop
app), and `-NoLaunch` (set up everything but don't open the UI).

If `scripts\setup.bat` doesn't work for you, or you want to understand or
control each step yourself, the rest of this guide walks through exactly
what it automates, one command at a time, starting from Step 1 below.

---

## Step 1: Install Python 3.11 or newer

1. Go to https://www.python.org/downloads/ and download the latest Python 3.11.x
   or 3.12.x installer for Windows (the big yellow "Download Python 3.x.x" button).
2. Run the installer. **On the very first screen, tick the checkbox at the
   bottom: "Add python.exe to PATH".** This is the single most common source of
   problems for people trying this for the first time — if you miss it, Windows
   won't be able to find the `python` command afterwards.
3. Click "Install Now" and let it finish.
4. Open a **new** terminal window (PowerShell or Command Prompt — Windows Terminal,
   which ships with Windows 11, works well). It must be a window you open *after*
   installing Python; windows opened before the install won't see the updated PATH.
5. Verify the install:
   ```
   py --version
   ```
   This should print something like `Python 3.11.9` or `Python 3.12.4`. If you get
   `'py' is not recognized...`, see the "Windows-Specific Issues" section of
   [docs/troubleshooting.md](troubleshooting.md#windows-specific-issues).

## Step 2: Get the ClaudeTrade source

If you were given a `.zip` of the repository, right-click it and choose
"Extract All...", then open the extracted `ClaudeTrade` folder in your terminal.
If you have `git` installed, you can instead run:
```
git clone https://github.com/afallows/claudetrade.git
cd ClaudeTrade
```
The rest of this guide assumes your terminal's current directory is the
`ClaudeTrade` folder — the one containing `pyproject.toml` and `README.md`.

## Step 3: Create and activate a virtual environment

A virtual environment keeps ClaudeTrade's Python packages separate from anything
else on your machine.

```
py -m venv .venv
```

Activate it. The command differs by shell:

- **Command Prompt (`cmd.exe`)**:
  ```
  .venv\Scripts\activate.bat
  ```
- **PowerShell**:
  ```
  .venv\Scripts\Activate.ps1
  ```
  If this fails with a message about "running scripts is disabled on this
  system", PowerShell's execution policy is blocking it — see
  [docs/troubleshooting.md](troubleshooting.md#running-scripts-is-disabled-on-this-system-powershell-execution-policy)
  for the one-line fix, or just use Command Prompt instead.

Either way, your prompt should now start with `(.venv)`. **Every command below
assumes the venv is activated** — if you close and reopen your terminal, you'll
need to activate it again before running `claudetrade`.

## Step 4: Install the dependencies and the application

Two separate installs are needed: one for the third-party packages ClaudeTrade
depends on, and one for the ClaudeTrade package itself (which also registers the
`claudetrade` command).

```
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

- `requirements.txt` installs pandas, SQLAlchemy, Streamlit, and everything else
  ClaudeTrade needs to run, including the UI (Streamlit/Plotly/openpyxl are
  already included — there's no separate UI install step).
- `pip install -e .` installs the ClaudeTrade package itself in "editable" mode
  and creates the `claudetrade` command. (Skipping this step is the single most
  common cause of `'claudetrade' is not recognized` — `requirements.txt` alone
  does not install the package.)

Verify it worked:
```
claudetrade version
```
Expected output (the exact numbers after `code_version=` will differ):
```
claudetrade 0.1.0 (code_version=0.1.0)
RESEARCH SIGNALS ONLY -- not investment advice, not a guaranteed outcome. Outputs are generated by an automated research tool and must be independently verified before any capital is risked.
```

## Step 5: `claudetrade init` — create the database

```
claudetrade init
```
This creates the local SQLite database and applies its schema. Expected output:
```
database:  sqlite+pysqlite:///C:\Users\<you>\AppData\Local\ClaudeTrade\data\claudetrade.db
schema:    v3 (applied [1, 2, 3])
data dir:  C:\Users\<you>\AppData\Local\ClaudeTrade\data
logs dir:  C:\Users\<you>\AppData\Local\ClaudeTrade\logs
config hash: <16 hex characters>
```
(Line-by-line log lines like `INFO ... database connected` may also print above
this — that's normal.)

### Where your data lives

By default (no config file, no environment variable overrides), ClaudeTrade
stores everything under:
```
%LOCALAPPDATA%\ClaudeTrade\
```
which is typically `C:\Users\<you>\AppData\Local\ClaudeTrade`. This comes from
`default_app_dir()` in `src/claudetrade/config.py`, which checks the
`LOCALAPPDATA` environment variable Windows sets automatically. You can paste
`%LOCALAPPDATA%\ClaudeTrade` directly into File Explorer's address bar to open
it. Inside you'll find `data\` (the SQLite database and cached market data),
`logs\`, `exports\`, `backups\`, `cache\` and `snapshots\` — all created on
demand.

Run `claudetrade init` again any time to double-check these paths.

## Step 6: `claudetrade probe` — check what's reachable

```
claudetrade probe
```
This tests whether the live data hosts (Stooq, Reddit, X, Anthropic, OpenAI)
are reachable from your machine, and separately reports whether credentials are
configured for the ones that need them. It does not require any credentials to
run and doesn't fail if everything is blocked — it's a diagnostic, not a
prerequisite. Expected output looks like:
```
Probing live data endpoints...

SOURCE  HOST                              NETWORK     CREDENTIAL  NOTE
market  stooq.com                         reachable   not needed  HTTP 200
market  query1.finance.yahoo.com          reachable   not needed  HTTP 200
reddit  www.reddit.com                    reachable   MISSING     HTTP 200
reddit  oauth.reddit.com                  reachable   MISSING     HTTP 401: needs credentials
x       api.x.com                         reachable   MISSING     HTTP 401: needs credentials
ai      api.anthropic.com                 reachable   MISSING     HTTP 401: needs credentials
ai      api.openai.com                    reachable   MISSING     HTTP 401: needs credentials

All probed hosts are reachable.

Credentials are stored with:  claudetrade secrets set <name>
Reddit needs a free script app (client id + secret); X search needs a paid tier.
```
On a machine behind a corporate firewall or proxy, some rows may say `BLOCKED`
instead of `reachable` — that's expected and does not stop the rest of this
guide from working, although Stooq itself must be reachable for the default
live-price refresh. If
you do need one of them reachable, see
[docs/troubleshooting.md](troubleshooting.md#corporate-proxy--tls-interception).

## Step 7 (optional): Configure sentiment or override market data

Stooq market history is already enabled by default. Earnings and Reddit retain
offline synthetic defaults. Use this section when you want real Reddit
sentiment or need to override the market provider.

### Stooq market data — enabled by default, no account needed

Stooq is a free market-data source that needs no API key or account. To confirm
or customize it, edit (or create) your config file at
`%LOCALAPPDATA%\ClaudeTrade\config.toml` (copy `config.example.toml` as a
starting point — see the note below) and set:
```toml
[market_data]
provider = "stooq"
fallbacks = ["csv", "synthetic"]
```

Setting the provider is the only step needed to get both US and Canadian
coverage: the app ships with a packaged seed universe (~500 US names, roughly
the S&P 500, and ~110 Canadian TSX names — see
[docs/api-providers.md#universe-selection](api-providers.md#universe-selection))
that is used automatically for the first `claudetrade refresh` once no
database of securities exists yet. You do not need to list symbols by hand.
Canadian coverage on stooq's free endpoint is real but partial and was not
independently verified while writing this guide (no network access from the
authoring environment); run `claudetrade probe` and check `claudetrade status`
for `data_quality` findings against the specific TSX symbols you care about.
This does **not** change the default: `market_data.provider` stays
`"synthetic"` until you edit `config.toml` yourself.

**If you copy `config.example.toml`**: delete or comment out the line
`app_dir = "~/.claudetrade"` under `[paths]` before saving it. That value is
taken literally rather than expanded — on Windows it would create data under a
folder literally named `~` in whatever directory you happen to run `claudetrade`
from, instead of your real per-OS default. Leaving it out uses the correct
`%LOCALAPPDATA%\ClaudeTrade` default automatically.

### Reddit sentiment — free script app

Reddit's official OAuth API is free but requires a client ID and secret.

1. Log into Reddit and go to https://www.reddit.com/prefs/apps

   **If that page fails** (blank page, dead "create app" button, 500 errors,
   or a CAPTCHA loop — all known problems with the redesigned page as of
   2026), use the legacy frontend for the same form:
   https://old.reddit.com/prefs/apps. Also check, in order: your account
   email is verified (an unverified account gets a silently-dead create
   button); try an incognito window with ad/privacy blockers off (the form
   POST is often eaten with no visible error); and fill *every* field
   including the ones marked optional. Note that `developers.reddit.com`
   (the "Developer Platform"/Devvit) is a different product and cannot
   issue these credentials.
2. Click "are you a developer? create an app..." (or "create another app...")
3. Fill in a name (e.g. "claudetrade-research"), select **"script"** as the app
   type, and put any placeholder URL in "redirect uri" (e.g.
   `http://localhost:8080`) — script apps don't use it, but the field is required.
4. Click "create app". You'll see two values you need:
   - **client ID**: the string directly under the app name/type, just under
     "personal use script"
   - **client secret**: the field labelled "secret"
5. Store them in ClaudeTrade's OS credential store (Windows Credential Manager)
   rather than in the config file — secrets are never written to `config.toml`:
   ```
   claudetrade secrets set reddit_client_id
   claudetrade secrets set reddit_client_secret
   ```
   Each command hides your typing and prompts once; nothing is echoed to the
   screen or written to shell history.
6. Verify they're recognised (this does not reveal the values):
   ```
   claudetrade secrets list
   ```
   Expect `reddit_client_id` and `reddit_client_secret` to show `yes` in the
   "configured" column with `keyring` as the source.
7. In `config.toml`, turn Reddit on and point it at the real provider:
   ```toml
   [reddit]
   enabled = true
   provider = "reddit"
   ```
   See [docs/api-providers.md](api-providers.md#reddit-live-oauth-owner-cookie-session-or-an-opt-in-unauthenticated-fallback)
   for the full set of Reddit options (subreddits, lookback window, rate limits).

**Alternative: cookie-session mode (no script app needed)**. If creating a
script app is inconvenient, or the `/prefs/apps` page is misbehaving (see the
note above), Reddit sentiment can instead authenticate with your own
logged-in browser session's `reddit_session` cookie:

1. Log in to reddit.com in Chrome/Edge, open devtools (F12) -> **Application**
   tab -> **Storage** -> **Cookies** -> `https://www.reddit.com`, and copy
   the **Value** of the cookie named `reddit_session`.
2. Store it the same way as any other credential:
   ```
   claudetrade secrets set reddit_session_cookie
   ```
3. Turn Reddit on (`enabled = true`, `provider = "reddit"`) as in step 7
   above -- no other config change is needed; this mode is picked up
   automatically once the client id/secret and username/password are absent
   and the cookie resolves.

This is your own personal Reddit session, for personal use only -- see
[docs/api-providers.md](api-providers.md#reddit-cookie-session-mode-owners-own-personal-session-adr-0008-decision-1)
for the full ToS-posture caveat and fail-closed behaviour before relying on
it.

X/Twitter sentiment requires a **paid** API tier and is off by default; see
[docs/api-providers.md](api-providers.md#xtwitter-paid-api-v2) if you have one.
AI-assisted sentiment classification (Anthropic/OpenAI) is optional and also
off by default (`ai.provider = "null"`, meaning the deterministic rule-based
classifier is used) — see
[docs/api-providers.md](api-providers.md#ai-providers) to enable it.

## Step 8: `claudetrade refresh` — pull data

```
claudetrade refresh --start 2024-01-01 --end 2024-12-31
```
This is a realistic first date range: about a year of daily bars, which is
enough for the technical indicators (which need roughly 200 trading days of
history) to have real values without waiting on a much longer pull. With the
default Stooq market provider this requires network access and can take time
because Stooq serves daily history one symbol at a time.

If you omit `--start`/`--end` entirely, `refresh` defaults to the last 90
calendar days ending today — enough for a quick look with a real provider
(Step 7) without pulling years of history for hundreds of symbols on the first
run. Indicators with a longer lookback (e.g. a 200-day moving average) will not
have enough history from a 90-day pull alone; pass explicit `--start`/`--end`
for backtesting or anything that needs deeper history.

Expected
output is a JSON summary, for example:
```json
{
  "summary": {
    "universe": 121,
    "signals": 0,
    "sentiment_rows": 1840,
    "snapshot": "a1b2c3d4e5f6",
    "degraded": {},
    "warnings": []
  },
  "ingest": { ... }
}
```
The exact `universe` and `sentiment_rows` counts will vary. If you see a yellow
"Some sources were unavailable; the run continued in reduced-capability mode."
message, that's the graceful-degradation path working as designed — check
`claudetrade status` afterwards to see which source it was, and
[docs/troubleshooting.md](troubleshooting.md) for that provider.

## Step 9: `claudetrade scan` — generate today's candidates

```
claudetrade scan
```
Expected output starts with the disclaimer, then a one-line summary, then either
a ranked table or an honest "nothing cleared the bar" message:
```

RESEARCH SIGNALS ONLY -- not investment advice, not a guaranteed outcome. Outputs are generated by an automated research tool and must be independently verified before any capital is risked.

session 2026-07-29 | regime <bull/bear/neutral/high_vol/...> | 121 symbols evaluated

No candidate cleared the thresholds. An empty list is a valid result.
```
or, if any candidates clear the configured score/confidence/reward:risk bars:
```
SYMBOL  STRATEGY                DIR   SCORE   CONF   R:R  SHARES  STATUS
AAPL    sentiment_breakout      LONG   61.2   0.58  1.80     42  actionable
...
```
**Important**: `scan` reads its universe from the database, which is only
populated by `refresh`. If you run `scan` before `refresh` (or before `refresh`
has ingested anything), you'll see `scan produced no result` and the command
exits with an error — that is different from the graceful "No candidate cleared
the thresholds" message above, and means you need to run `refresh` first, not
that something is broken.

## Step 10: `claudetrade ui` — open the dashboard

```
claudetrade ui
```
Expected output:
```
starting the interface on port 8501 ...
```
followed by Streamlit's own startup banner, ending with a line like:
```
  Local URL: http://localhost:8501
```
Open that address in your browser (it usually opens automatically). You should
see a five-screen app in the left sidebar: **Dashboard**, **Scanner**,
**Ticker Detail**, **Backtesting**, **Settings**. Leave this terminal window
open while you use the UI; press `Ctrl+C` in it to stop the server when done.

If port 8501 is already in use, run `claudetrade ui --port 8502` instead, or see
[docs/troubleshooting.md](troubleshooting.md#streamlit-port-already-in-use--port-8501-is-in-use).

---

## What's next

- [docs/windows-smoke-test.md](windows-smoke-test.md) — a step-by-step checklist
  to exercise every part of the app (backtest, paper account, exports, backups).
- [docs/api-providers.md](api-providers.md) — full detail on every data provider.
- [docs/troubleshooting.md](troubleshooting.md) — solutions to common problems,
  including a dedicated Windows section.
- [docs/known-limitations.md](known-limitations.md) — an honest list of what
  isn't built yet (live trading, background scheduling, a few UI/CLI gaps).
- `claudetrade --help` and `claudetrade <command> --help` — the authoritative,
  always-up-to-date reference for every option.

## A note on this guide's honesty

This document was written by reading `src/claudetrade/cli.py` directly and
running every `--help` invocation shown as evidence (not by running `refresh`,
`scan`, or `backtest` against real data — see
[docs/windows-build.md](windows-build.md) for why: this guide was produced on a
Linux development machine, not on Windows, so the exact on-screen wording in
Steps 8–10 should be treated as "what the source code says it will print",
double-checked against a real init/status/scan/probe run in the same
environment — not as a byte-for-byte transcript from a Windows machine. If your
output differs in wording but not in meaning, that's expected; if a command
doesn't exist at all or takes different flags than shown here, please file an
issue — that would be a real documentation bug.
