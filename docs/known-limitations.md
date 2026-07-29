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

- **Status**: Not implemented
- **Current state**: Entry point defined in `pyproject.toml` but no commands are defined
- **Workaround**: Use the Python API directly
- **Effort to implement**: Medium; CLI commands (refresh, scan, backtest, export) would wrap the pipeline API

### Streamlit UI Dashboard

- **Status**: Not implemented (optional dependency listed)
- **Workaround**: Use the Python API or export results to spreadsheets
- **Effort to implement**: High; requires UI components for signals, trades, metrics, settings

### Background Scheduler

- **Status**: APScheduler is included but no scheduled tasks are implemented
- **Current state**: Config has scheduler settings but they are ignored
- **Workaround**: Use cron (Linux/macOS) or Task Scheduler (Windows) to call the pipeline
- **Effort to implement**: Low; hook up the config settings to APScheduler tasks

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

1. **CLI commands** (refresh, scan, backtest, export, secrets)
2. **Streamlit dashboard** (signals, trades, settings, metrics)
3. **Background scheduler** (APScheduler integration)
4. **Live trading** (broker adapter pattern; starting with Alpaca or Interactive Brokers)
5. **Intraday bars and strategies** (minute-bar provider integration)
6. **ML-based signal fusion** (scikit-learn classification)
7. **Options support** (if demand exists)
8. **International markets** (extend beyond US equities)
9. **Tax efficiency reporting** (wash-sale, holding-period classification)
10. **Mobile app** (real-time alerts and portfolio view)

Priority is determined by user feedback and feature requests.
