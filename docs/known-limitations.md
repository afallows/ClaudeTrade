# Known Limitations

This document lists what is NOT implemented in the current codebase.

## Unimplemented Features

### Live Trading

- **Status**: Not implemented
- **Blocker**: No broker adapter exists
- **Config guard**: `mode = "live"` requires both `live_trading_authorised = true` AND a `broker` name; if either is missing, the mode is rejected
- **Workaround**: Use paper trading for simulation
- **Effort to implement**: High; requires OAuth/FIX/API integration with a real broker (Interactive Brokers, Alpaca, etc.)

### CLI

- **Status**: Implemented
- **Current state**: `claudetrade` (see `src/claudetrade/cli.py`) provides `version`, `init`,
  `status`, `probe`, `refresh`, `scan`, `backtest`, `ui`, `secrets set|list|delete`,
  `paper status|positions|kill-switch`, `db migrate|backup|restore`, and
  `verify ledger|survivorship`. Run `claudetrade --help` for the full list.
- **Not implemented**: A standalone `claudetrade export` command and a
  `claudetrade validate-config` command do not exist. Export is a flag on
  `claudetrade backtest` (`--report`, `--export`) rather than its own command.

### Streamlit UI Dashboard

- **Status**: Implemented
- **Current state**: `claudetrade ui` launches a five-screen Streamlit dashboard
  (`src/claudetrade/ui/`: dashboard, scanner, ticker detail, backtesting, settings).
- **Known gap**: The backtesting screen's "Export as CSV" / "Export as Excel" buttons
  are placeholders — they show an informational message rather than writing a file
  (`src/claudetrade/ui/screens/backtesting.py`). Use `claudetrade backtest --export <dir>`
  from the CLI for a working export, or `claudetrade.backtest.reporting.export_csv` /
  `export_excel` directly.

### Opening a Paper Trade

- **Status**: Partially implemented
- **Current state**: `PaperBroker.submit_signal` (`src/claudetrade/paper/broker.py`) and the
  underlying `PaperPortfolio` accounting (`src/claudetrade/paper/portfolio.py`) are fully
  implemented and covered by tests, but neither the CLI nor the UI exposes a way to call it.
  `claudetrade paper status/positions/kill-switch` only inspect the account (which
  auto-creates itself with `risk.account_size_usd` starting cash on first access) and gate
  new entries; none of them opens a position.
- **Workaround**: Call `PaperBroker.submit_signal(...)` (or the guarded `submit_order(...)`
  from the `BrokerProvider` interface it implements) from the Python API directly — see
  `tests/test_broker_contract.py::TestPaperBrokerLifecycle` for working examples — or
  generate signals with `claudetrade scan` and use those as an entry list for manual
  paper tracking.
- **Effort to implement**: Low; add a `claudetrade paper submit <symbol>` command (or a
  "Paper trade this" button in the UI scanner/dashboard) that looks up a recorded signal
  and calls the existing `submit_signal`.

### Background Scheduler

- **Status**: Partially implemented — hourly social/attention collection is wired
  up; scheduled market refreshes and scans are not.
- **What runs**: `src/claudetrade/scheduler.py`'s `SocialCollectionScheduler`, started
  from the web API server's FastAPI lifespan (`src/claudetrade/webapi/app.py`). While
  the app is open it collects social posts and ApeWisdom attention every
  `scheduler.social_collection_interval_minutes` (default 60, plus jitter), takes the
  same cross-process refresh lock a manual refresh does — skipping, not queueing, when
  it is held — and is visible through `GET /api/system/refresh/status` and the MCP
  `get_refresh_status` tool with `entry_point = "scheduler"`. It **never** runs the
  market pass. `claudetrade sentiment collect` runs exactly one collection on demand.
- **Why only social**: Reddit `/new`, X recent-search and ApeWisdom's rolling 24h
  snapshot have no history endpoints, so social history can only be accumulated
  forward — a missed hour is permanently lost. Price bars, corporate actions and
  earnings can all be backfilled, so they do not need an hourly loop, and running one
  would cost ~20 rate-limit-bound minutes per hour for data that changes once a day.
- **Still not implemented**: `APScheduler` remains a declared dependency with no jobs
  registered. `SchedulerConfig.enabled` and the `*_cron` fields are inert — no code
  constructs an `apscheduler` scheduler, and there is no `claudetrade` subcommand that
  runs continuously.
- **Workaround for the rest**: Use cron (Linux/macOS) or Task Scheduler (Windows) to
  call `claudetrade refresh` / `claudetrade scan` on a timer. Use
  `claudetrade sentiment collect` on a timer if you want social history to keep
  accumulating while the app is closed.
- **Effort to implement**: Low; hook up the remaining config settings to APScheduler jobs.

### Machine Learning Signal Fusion

- **Status**: Not implemented (scikit-learn is optional)
- **Current state**: All signals are purely rules-based
- **Workaround**: Backtest individual strategies and select the best performers
- **Effort to implement**: Medium; would require feature engineering, model training, cross-validation

### Options and Derivatives

- **Status**: Not implemented
- **Blocker**: Domain model assumes only equity long/short
- **Workaround**: Focus on equity strategies
- **Effort to implement**: Very high; would require option pricing models, Greeks computation, volatility surface estimation

### Intraday Trading

- **Status**: Not implemented
- **Blocker**: Only daily bars are modelled; intraday bars exist in schema but are not used
- **Current model**: Signals on daily close; execution on next day's open
- **Workaround**: Use longer holding periods (6–20 days)
- **Effort to implement**: Very high; requires minute-bar data, intraday provider integration, tick-level fill simulation

### Short Borrow Modelling

- **Status**: Not implemented (assumptions only)
- **Current state**: Borrow cost is fixed (default 3% annualised); borrow availability is NOT checked
- **Real-world impact**: Many micro-caps are impossible or very expensive to borrow; short signal execution may fail
- **Workaround**: Short only names with market cap ≥ $300M (crude proxy)
- **Effort to implement**: Medium; would require borrow availability API integration

### Live Earnings Date Updates

- **Status**: Not implemented
- **Current state**: Earnings dates are from static CSV or synthetic; no live calendar API integration
- **Data staleness**: Earnings dates can be moved; live system must fetch updates
- **Workaround**: Manually update earnings CSV or use scheduled provider refresh
- **Effort to implement**: Low; add a calendar data provider (e.g., Seeking Alpha, IEX Cloud)

### Delisting Recovery Data

- **Status**: Partially implemented (fixed assumed factor)
- **Current state**: If a trade is open when a security is delisted, it is closed at the last bar's close
- **Real-world issue**: Delisted names may trade OTC or be acquired; recovery is not modelled
- **Workaround**: Assume delisted names are total losses (conservative)
- **Effort to implement**: Low-medium; add a delisting recovery factor (e.g., 10% of last close)

---

## Partial Implementations

### LLM-Based Sentiment Classification

- **Status**: Integrated but optional
- **Current state**: Anthropic and OpenAI adapters exist; rule-based classifier is the fallback
- **Limitation**: LLM responses are non-deterministic; cost control is manual; no model fine-tuning
- **Coverage**: Only 20 sample posts per symbol per run are sent to LLM; others use rules
- **Effort to improve**: Medium; add prompt tuning, local LLM fallback (Ollama), fine-tuning on labelled data

### Social Media Breadth

- **Status**: Reddit and X only; no other platforms
- **Gap**: TikTok, Discord, Telegram, stocktwits have trading communities; not covered
- **Limitation**: Smaller universe of sources → more risk of manipulation
- **Effort to expand**: High; requires OAuth/API for each platform

### Entity Resolution

- **Status**: Cashtag, company name, fuzzy matching, aliases
- **Gap**: Complex cases (mergers, spinoffs, symbol changes) are not handled
- **Limitation**: May misresolve "Apple Inc." vs "Apple Records"; may miss old tickers
- **Effort to improve**: Medium; add merger/spinoff logic; maintain historical symbol mappings

### Backtest Reporting

- **Status**: CSV, JSON, and summary statistics exported
- **Gap**: No detailed trade-by-trade commentary; no tearsheets; no attribution analysis
- **Limitation**: Hard to debug why a strategy underperformed
- **Effort to improve**: Low; add stratification reports (by sector, cap, regime, entry signal)

### Data Quality Checks

- **Status**: Staleness detection, null value checks, volume anomalies
- **Gap**: No outlier detection; no provider comparison validation
- **Limitation**: Corrupted data may pass checks if it's recent and has volume
- **Effort to improve**: Low; add bounds checks (e.g., price move > 20% intraday → flag), provider consistency checks

---

## Design Constraints

### No Leverage Modelling

- **Current state**: Account size is fixed; no margin account or leverage
- **Impact**: Backtests conservative; real traders can 2–3x capital
- **Effort to add**: Medium; would require margin call simulation, forced liquidation logic

### No Tax Efficiency

- **Current state**: No wash-sale rules, no tax-loss harvesting, no holding-period classification
- **Impact**: Reported returns are pre-tax; real net returns are lower
- **Effort to add**: High; requires jurisdiction-specific tax rules, long-term vs. short-term tracking

### No Portfolio Rebalancing

- **Current state**: Each signal is independent; no portfolio-level weighting or rebalancing
- **Impact**: No systematic way to reduce sector concentration or volatility
- **Effort to add**: Medium; add target allocation logic, periodic rebalancing

### No Regime-Specific Strategy Selection

- **Current state**: All strategies run in all regimes; no dynamic strategy enable/disable
- **Impact**: Some strategies underperform in certain regimes
- **Effort to add**: Low; add regime-specific strategy weights in config

### Point-in-Time, Not Real-Time

- **Current state**: Signals computed once per day at close
- **Impact**: Misses intraday opportunities; delayed reaction to news
- **Effort to add**: Very high; requires intraday data and fast computation

---

## Data Source Gaps

### Stooq Limitations

- Free tier only; paid tier might offer different data
- No delisting information
- No short-interest data
- Potential data gaps on less-liquid names

**Workaround**: Provide CSV for names with gaps.

### Reddit and X Limitations

- Only 6 months of searchable history (API restriction)
- Engagement counts are mutable; cannot reconstruct historical sentiment
- Author metrics can be faked (purchased followers, inactive accounts)
- Small subreddits and niche accounts have low volume

**Workaround**: Combine multiple social sources; use longer lookback for trend, shorter lookback for recent momentum.

### Synthetic Data Limitations

- Completely fabricated; not representative of real markets
- Suitable for engine validation only

**Workaround**: Use synthetic for development; switch to real data for research.

---

## Performance and Scalability

### Single-Threaded Signal Generation

- Current state: Strategies are evaluated sequentially
- Impact: Scanning 2000 symbols takes 30–60 seconds
- Effort to improve: Medium; add multiprocessing or async

### No Intraday Provider

- Current state: Only daily bars from Stooq, CSV, or synthetic
- Impact: Cannot validate intraday entry logic or detect intraday catalysts
- Effort to add: High; integrate a provider (Polygon, Alpaca, etc.)

### Backtest Performance for Long History

- Current state: Walk-forward over 10 years of daily data can take hours
- Impact: Limited ability to iterate on strategy changes during development
- Effort to improve: Medium; caching, parallel windows, incremental computation

---

## Known Bugs and Workarounds

### Earnings Date Leakage

- **Issue**: If an earnings is announced 30 seconds before market close and the system runs at 16:45, the date has effectively leaked
- **Mitigation**: Earnings dates are filtered conservatively; system checks `as_of` to prevent direct look-ahead
- **Workaround**: Assume earnings dates are known; live system should skip unknown dates

### Social Engagement Mutability

- **Issue**: Upvote counts, comment counts change after posts are fetched
- **Impact**: Historical sentiment cannot be perfectly reconstructed
- **Mitigation**: Record the timestamp of each fetch; accept that historical sentiment is approximate
- **Workaround**: Re-fetch old posts periodically if strict reconstruction is needed

### Intrabar Price Ambiguity

- **Issue**: If price touches a stop during a session but closes above it, is the trade exited or not?
- **Current behavior**: Assume touched → exited (conservative, biased to stops being hit)
- **Impact**: Stops are more likely to trigger in simulation than in reality
- **Workaround**: Widen stops by 0.5% to account for this pessimism

---

## Future Work (Roadmap)

1. **Background scheduler** (wire the remaining `SchedulerConfig` cron fields up to
   APScheduler jobs; the hourly social/attention collection already runs in-process)
2. **Live trading** (a real `BrokerProvider` implementation; the ABC and a
   non-functional `NullLiveBroker` stub already exist in `src/claudetrade/brokers/`,
   starting with Alpaca or Interactive Brokers)
3. **Wire up the UI's backtest export buttons** to the existing `export_csv`/`export_excel` functions
4. **Intraday bars and strategies** (minute-bar provider integration)
5. **ML-based signal fusion** (scikit-learn classification)
6. **Options support** (if demand exists)
7. **International markets** (extend beyond US equities)
8. **Tax efficiency reporting** (wash-sale, holding-period classification)
9. **Mobile app** (real-time alerts and portfolio view)

CLI commands and the Streamlit dashboard shipped and are covered above under
"Unimplemented Features" only where a specific piece (like UI export) still has a gap.

Priority is determined by user feedback and feature requests.
