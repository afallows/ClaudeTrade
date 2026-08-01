# Database Schema

The database is implemented in SQLAlchemy ORM, portable between SQLite and PostgreSQL. This document describes every table, its purpose, and key design decisions.

**Key principle**: Append-only enforcement for signals, revisions, and audit logs prevents after-the-fact tampering.

## Meta Tables

### `schema_version`

Tracks applied migrations; the migration runner is idempotent.

| Column | Type | Purpose |
|--------|------|---------|
| `version` | Integer (PK) | Migration sequence number |
| `name` | String | Human-readable name |
| `applied_at` | DateTime | UTC timestamp of application |
| `checksum` | String | Hash for integrity verification |

**Append-only**: Yes (insert only; no updates)

---

### `settings_kv`

Operator settings that belong in the database rather than config (window layout, watchlists, acknowledged warnings).

| Column | Type | Purpose |
|--------|------|---------|
| `key` | String (PK) | Setting name |
| `value` | JSON | Setting value (can be object or array) |
| `updated_at` | DateTime | Last update timestamp |

**Append-only**: No (settings can be changed)

---

### `audit_log`

Append-only security and integrity log.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | Auto-incrementing record ID |
| `created_at` | DateTime | UTC timestamp |
| `actor` | String | System or user ID |
| `action` | String | Event type (e.g., "secret_read", "signal_generated") |
| `entity` | String | Object type (e.g., "credential", "signal", "trade") |
| `entity_id` | String | Specific object ID |
| `detail` | JSON | Event-specific metadata |
| `code_version` | String | Package version at time of event |

**Append-only**: Yes (enforced by design; no foreign keys allow delete)

**Indices**: `created_at`, `action`

---

## Reference Data

### `securities`

Listed (or formerly listed) securities. **Never delete rows; mark delisted instead.**

| Column | Type | Purpose |
|--------|------|---------|
| `symbol` | String (PK) | Ticker symbol |
| `name` | String | Company name |
| `exchange` | String | NYSE, NASDAQ, AMEX, etc. |
| `sector` | String | Industry classification |
| `industry` | String | More specific classification |
| `market_cap_usd` | Float | Market capitalization (nullable) |
| `shares_outstanding` | Float | Shares issued (nullable) |
| `is_etf` | Boolean | Whether this is an ETF |
| `is_leveraged_or_inverse` | Boolean | Leveraged or inverse ETF (warning flag) |
| `listed_date` | Date | When trading began (nullable) |
| `delisted_date` | Date | When trading ceased (nullable, **critical**) |
| `avg_dollar_volume_20d` | Float | 20-day average daily volume (nullable) |
| `short_interest_pct` | Float | % of shares short (nullable) |
| `implied_volatility_30d` | Float | 30-day IV (nullable) |
| `source` | String | Provider name |
| `updated_at` | DateTime | Last refresh time |

**Append-only**: No (reference data updates)

**Indices**: `exchange`, `sector`, `delisted_date` (critical for survivorship-bias-free backtests)

**Design note**: `delisted_date` is populated and retained. Backtests use it to prevent survivorship bias: if a signal is open when a name delists, the trade is closed at delisting price and marked as a loss.

---

### `symbol_aliases`

Former tickers, company name variants, common abbreviations.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String (FK) | Current symbol (references `securities.symbol`) |
| `alias` | String | Name variant (e.g., "Facebook" → "META") |
| `alias_normalised` | String | Lowercase, no spaces (for indexing) |
| `kind` | String | "name", "former_symbol", "nickname" |
| `valid_from` | Date | Date range when this alias applies (nullable) |
| `valid_to` | Date | (nullable) |
| `created_at` | DateTime | Record creation time |

**Append-only**: No (reference data updates)

**Indices**: `alias_normalised` (ticker mention resolution uses this)

---

## Market Data

### `price_bars`

Daily OHLCV bars, one row per (symbol, session, source).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `session` | Date | Trading date |
| `open` | Float | Open price |
| `high` | Float | High price |
| `low` | Float | Low price |
| `close` | Float | Close price |
| `adj_close` | Float | Dividend/split-adjusted close (nullable) |
| `volume` | Float | Trading volume |
| `source` | String | Provider name ("synthetic", "csv", "stooq") |
| `ingested_at` | DateTime | When this row was inserted |

**Append-only**: No (bars can be updated/corrected)

**Indices**: `(symbol, session)`, `symbol`, `session`

**Unique constraint**: `(symbol, session, source)` — one bar per source per day

**Design note**: Multiple sources can provide bars for the same symbol/date; the fallback provider chooses which source to use.

---

### `intraday_bars`

Intraday bars (separate table keeps daily queries fast).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `ts` | DateTime | Bar timestamp (UTC) |
| `interval_minutes` | Integer | Bar interval (e.g., 5) |
| `open`, `high`, `low`, `close` | Float | OHLC |
| `volume` | Float | Volume |
| `source` | String | Provider name |

**Append-only**: No

**Indices**: `(symbol, ts)`, `ts`

---

### `corporate_actions`

Splits, dividends, symbol changes, delistings.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `session` | Date | Effective date |
| `kind` | String | "split", "dividend", "symbol_change", "delisting" |
| `ratio` | Float | Split ratio or dividend amount (nullable) |
| `amount` | Float | Dividend per share (nullable) |
| `detail` | Text | Human-readable description |
| `source` | String | Provider name |

**Append-only**: No

**Unique constraint**: `(symbol, session, kind)`

---

## Earnings Data

### `earnings_events`

Earnings dates, estimates, and actuals. **Critical `as_of` column prevents look-ahead.**

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `report_date` | Date | Date earnings were/will be reported |
| `session` | String | "bmo", "amc", "during", "unknown" |
| `confirmed` | Boolean | True if exchange-confirmed, False if estimated |
| `eps_estimate` | Float | Consensus EPS estimate (nullable) |
| `eps_actual` | Float | Reported EPS (nullable) |
| `revenue_estimate` | Float | Consensus revenue estimate (nullable) |
| `revenue_actual` | Float | Reported revenue (nullable) |
| `surprise_pct` | Float | (actual - estimate) / abs(estimate) (nullable) |
| `source` | String | Provider name |
| `as_of` | DateTime | **When this row's information became known** |

**Append-only**: No (actuals are filled in after report)

**Indices**: `(symbol, report_date)`, `symbol`, `report_date`, `as_of`

**Unique constraint**: `(symbol, report_date, source)`

**Design note**: The `as_of` column is critical for preventing look-ahead bias in backtests. A backtest at session=2024-01-20 can only see earnings events where `as_of <= 2024-01-20`, which prevents trading on information that wasn't known yet.

---

## Social Media Data

### `social_posts`

Sanitised social posts. Raw usernames are never stored; only author hashes.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `source` | String | "reddit", "x", "other" |
| `external_id` | String | Platform post ID |
| `created_at` | DateTime | Post timestamp (UTC) |
| `fetched_at` | DateTime | When we ingested it |
| `text` | Text | Sanitised post text |
| `text_hash` | String | SHA256 of text (for duplicate detection) |
| `community` | String | Subreddit or thread name |
| `score` | Integer | Upvotes/likes (mutable at source) |
| `num_comments` | Integer | Comment count (mutable) |
| `num_reposts` | Integer | Retweet count (mutable) |
| `num_replies` | Integer | Reply count (mutable) |
| `author_hash` | String | Salted SHA256 hash (never username) |
| `author_age_days` | Float | Account age in days (nullable) |
| `author_karma` | Float | Platform reputation score (nullable) |
| `author_followers` | Float | Follower count (nullable) |
| `is_comment` | Boolean | Is a reply to another post |
| `parent_id` | String | Parent post ID (nullable) |
| `is_removed` | Boolean | Deleted or removed by platform |
| `is_crosspost` | Boolean | Is a crosspost |
| `crosspost_parent` | String | Original post ID (nullable) |
| `duplicate_group` | String | Hash group of near-duplicates (nullable) |
| `injection_risk` | Float | Heuristic score 0–1 for prompt injection risk |
| `raw_ref` | String | Permalink for re-fetching (nullable) |

**Append-only**: No (posts can be updated, deleted, or reclassified)

**Indices**: `(source, external_id)`, `created_at`, `text_hash`, `author_hash`, `duplicate_group`

**Design note**: Author names are never stored. The `author_hash` uses a per-session salt, so the same author in different runs gets different hashes. Engagement metrics are mutable; they cannot be considered historical truth.

---

### `ticker_mentions`

Resolved symbol references within posts (confidence scored).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `post_id` | Integer (FK) | Reference to `social_posts.id` |
| `symbol` | String | Resolved ticker |
| `confidence` | Float | Resolution confidence 0–1 |
| `method` | String | "cashtag", "company_name", "symbol_context", "alias" |
| `matched_text` | String | Text that matched (e.g., "$AAPL" or "Apple") |
| `context` | Text | Surrounding context (for debugging) |
| `created_at` | DateTime | Resolution timestamp |

**Append-only**: No (resolutions can be corrected)

**Indices**: `symbol`, `post_id`

**Unique constraint**: `(post_id, symbol)`

**Design note**: Multiple symbols can be mentioned in one post, so this is a many-to-one table. Mentions below `min_ticker_confidence` are dropped before aggregation.

---

## Sentiment

### `sentiment_records`

Per-post, per-symbol classifier output (raw scores, not aggregated).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `post_id` | Integer (FK) | Post ID |
| `symbol` | String | Symbol being classified |
| `classifier` | String | "rules" or "ai" |
| `model` | String | Model name (e.g., "claude-sonnet-5") |
| `prompt_version` | String | Prompt template version |
| `scores` | JSON | Multi-label sentiment scores (bullish, bearish, hype, fear, ...) |
| `confidence` | Float | Classifier confidence 0–1 |
| `created_at` | DateTime | Classification timestamp |

**Append-only**: No (classifications can be recomputed)

**Unique constraint**: `(post_id, symbol, classifier)` — one entry per classifier per (post, symbol)

---

### `symbol_sentiment_daily`

Time-decayed daily sentiment aggregate per symbol and source.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `session` | Date | Trading date |
| `source` | String | "reddit", "x", "all" |
| `post_count` | Integer | Number of posts |
| `comment_count` | Integer | Number of comments |
| `unique_authors` | Integer | Distinct authors |
| `raw_sentiment` | Float | Mean polarity -1 to +1 |
| `engagement_weighted` | Float | Sentiment weighted by engagement |
| `credibility_weighted` | Float | Sentiment weighted by author credibility |
| `unique_author_sentiment` | Float | Sentiment by distinct author |
| `sentiment_acceleration` | Float | Rate of change in sentiment |
| `mention_acceleration` | Float | Rate of change in mention count |
| `bull_bear_ratio` | Float | Bull posts / bear posts |
| `dispersion` | Float | Opinion spread 0–1 |
| `source_concentration` | Float | % of posts from top source 0–1 |
| `duplicate_ratio` | Float | % of posts that are near-duplicates 0–1 |
| `bot_risk` | Float | Heuristic bot-like behaviour score 0–1 |
| `manipulation_risk` | Float | Concentration + duplicates heuristic 0–1 |
| `confidence` | Float | Data quality/sample size 0–1 |
| `total_engagement` | Float | Sum of all engagement metrics |
| `labels` | JSON | Aggregated sentiment labels (hype, fear, capitulation, etc.) |
| `computed_at` | DateTime | Aggregation timestamp |

**Append-only**: No (aggregates can be recomputed)

**Indices**: `(symbol, session)`, `symbol`, `session`

**Unique constraint**: `(symbol, session, source)`

**Design note**: Computed from posts whose `created_at <= session` close time (no look-ahead). Can be recomputed if the aggregation logic changes.

---

## Features and Regime

### `features`

Point-in-time feature vectors for each (symbol, session).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `symbol` | String | Ticker |
| `session` | Date | Trading date |
| `feature_version` | String | Version of the feature formula |
| `values` | JSON | Dict of feature names to values (RSI, ATR, MA, etc.) |
| `computed_at` | DateTime | Computation timestamp |

**Append-only**: No (features can be recomputed)

**Indices**: `(symbol, session)`

**Unique constraint**: `(symbol, session, feature_version)`

**Design note**: Feature version allows old rows to survive a formula change without mixing definitions inside a backtest.

---

### `market_regimes`

Classified market environment on a given session.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `session` | Date | Trading date |
| `regime` | String | "bull_quiet", "bull_volatile", "neutral", "bear_volatile", "bear_quiet", "unknown" |
| `model_version` | String | Classification model version |
| `trend_score` | Float | Trend strength 0–1 |
| `breadth` | Float | Market breadth 0–1 |
| `volatility_percentile` | Float | Vol percentile 0–1 |
| `realised_vol_annual` | Float | Annualised volatility |
| `risk_appetite` | Float | Risk-on/risk-off 0–1 |
| `detail` | JSON | Leading/lagging sectors, notes |
| `computed_at` | DateTime | Computation timestamp |

**Append-only**: No

**Unique constraint**: `(session, model_version)`

---

## Signals and Ledger

### `signals`

Immutable generated signals. **Protected by database trigger (SQLite) / foreign key constraints (PostgreSQL).**

| Column | Type | Purpose |
|--------|------|---------|
| `signal_id` | String (PK) | Unique UUID |
| `created_at` | DateTime | Generation timestamp |
| `session` | Date | Trading session |
| `symbol` | String | Ticker |
| `company_name` | String | Company name |
| `strategy` | String | Strategy name |
| `strategy_version` | String | Strategy code version |
| `direction` | String | "long", "short", "flat" |
| `initial_status` | String | "actionable", "approaching", "extended", "rejected" |
| `reference_price` | Float | Price at signal generation |
| `price_as_of` | DateTime | Timestamp of price |
| `overall_score` | Float | 0–100 score |
| `confidence` | Float | 0–1 data-quality confidence |
| `components` | JSON | Per-component scores (13 components) |
| `plan` | JSON | TradePlan: entry zone, stops, targets, sizing |
| `regime` | String | Market regime at time of signal |
| `next_earnings_date` | Date | Next earnings (nullable) |
| `days_to_earnings` | Integer | Days until next earnings (nullable) |
| `earnings_confirmed` | Boolean | Is next earnings date confirmed? |
| `thesis` | Text | Human-readable thesis |
| `invalidation` | JSON | List of invalidation conditions |
| `exit_conditions` | JSON | List of exit rules |
| `evidence` | JSON | List of supporting evidence |
| `risks` | JSON | List of identified risks |
| `data_freshness_hours` | Float | Age of oldest input data |
| `data_warnings` | JSON | Data quality warnings |
| `expires_after` | Date | Expiry date (nullable) |
| `code_version` | String | App version at generation |
| `config_hash` | String | Config digest at generation |
| `strategy_version` | String | Strategy version |
| `data_snapshot_hash` | String | Data snapshot identifier (reproducibility) |
| `ai_metadata` | JSON | LLM response data (optional) |
| `extras` | JSON | Strategy-specific additional data |

**Append-only**: **YES** (trigger enforces no UPDATE/DELETE)

**Indices**: `(symbol, session)`, `strategy`, `session`, `overall_score`

**Design note**: The reproducibility triple (code_version, config_hash, data_snapshot_hash) appears here so any stored signal can be traced back to the exact code, config, and data that produced it.

---

### `signal_revisions`

Status changes and corrections to signals (appended, never updated).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | |
| `signal_id` | String (FK) | Reference to `signals.signal_id` |
| `created_at` | DateTime | Revision timestamp |
| `previous_status` | String | Old status |
| `new_status` | String | New status |
| `reason` | Text | Why the status changed (e.g., "price extended beyond entry zone") |
| `manual` | Boolean | Was this change manual or automatic? |

**Append-only**: **YES**

**Design note**: The current status of a signal is determined by its latest revision, not by querying the signal row itself. This immutability prevents accidentally hiding failed signals by rewriting their status.

---

## Trading (Paper and Backtest)

### `paper_trades`

Simulated trades from paper trading mode.

| Column | Type | Purpose |
|--------|------|---------|
| `trade_id` | String (PK) | Unique trade ID |
| `signal_id` | String (FK) | Origin signal |
| `symbol` | String | Ticker |
| `strategy` | String | Strategy name |
| `direction` | String | "long", "short" |
| `entry_session` | Date | Entry date |
| `entry_price` | Float | Entry fill price |
| `shares` | Integer | Position size |
| `stop_loss` | Float | Stop loss level |
| `targets` | JSON | Target price levels |
| `exit_session` | Date | Exit date (nullable for open) |
| `exit_price` | Float | Exit fill price (nullable) |
| `exit_reason` | String | Why closed (stop, target, time, earnings, etc.) |
| `fills` | JSON | List of Fill records |
| `commission_total` | Float | Total commission paid |
| `fees_total` | Float | SEC/FINRA fees |
| `slippage_total` | Float | Slippage cost |
| `borrow_cost_total` | Float | Short borrow cost |
| `mfe_pct` | Float | Maximum Favorable Excursion (%) |
| `mae_pct` | Float | Maximum Adverse Excursion (%) |
| `mfe_r` | Float | MFE in risk multiples |
| `mae_r` | Float | MAE in risk multiples |
| `initial_risk_per_share` | Float | Risked per share at entry |
| `thesis_intact_at_exit` | Boolean | Did the thesis hold at exit? (nullable) |
| `regime_at_entry` | String | Market regime at entry |
| `sector` | String | Industry sector |
| `market_cap_bucket` | String | Size bucket (micro, small, mid, large) |
| `days_to_earnings_at_entry` | Integer | Days to next earnings at entry (nullable) |
| `confidence_at_entry` | Float | Signal confidence at entry |
| `sentiment_source` | String | Which source confirmed sentiment |
| `notes` | JSON | Trade notes (slippage, partial fills, etc.) |
| `created_at` | DateTime | Record creation timestamp |

**Append-only**: No (trades can be updated with final fills/exits)

**Design note**: This table mirrors the `Trade` domain type exactly; it's the persistent representation of simulated and backtested trades.

---

## Operations

### `refresh_runs`

Cross-process record of every data refresh, and the single-flight lock that
keeps two of them from overlapping. The CLI, the web API server and the MCP
server each run their own process against the same database file, so refresh
state cannot live in any one process's memory: whichever entry point did not
start the run would report "idle" while another was actively writing, and
would happily start a second concurrent refresh racing the first one's writes.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | Integer (PK) | Run identifier |
| `entry_point` | String | Which process started it: `cli`, `webapi` or `mcp` |
| `status` | String | `running`, `done` or `failed` |
| `phase` | String | Current ingestion phase (`securities`, `prices`, `sentiment`, ...) |
| `symbols_done` / `symbols_total` | Integer | Coarse progress within the phase |
| `started_at` | DateTime | UTC start instant |
| `heartbeat_at` | DateTime | Liveness signal, written at most ~once per 2 s |
| `finished_at` | DateTime | UTC completion instant (NULL while running) |
| `last_error` | Text | Failure message, or `stale lock taken over` |

**Append-only**: No (a run's own row is updated as it progresses)

**Design notes**:
- A partial unique index (`uq_refresh_running`, migration 005) permits at most
  **one** row with `status = 'running'`. That constraint — not a
  check-then-insert — is what makes acquisition atomic across processes: of
  two racing acquirers, exactly one INSERT succeeds and the loser is refused
  with the winner's details.
- `heartbeat_at` is what makes the lock self-healing. A `running` row whose
  heartbeat is older than ~120 s belongs to a process that died mid-refresh;
  the next acquirer takes the lock over and marks the abandoned row `failed`
  with `stale lock taken over`, so a crash never wedges refreshes permanently
  and the abandoned run stays visible rather than vanishing.
- All writes go through `claudetrade.db.refresh_state_store` in their own
  short transactions, so the bookkeeping can never itself hold a lock that a
  multi-minute refresh would queue behind.

---

## Summary

| Table | Append-Only | Purpose |
|-------|-------------|---------|
| `schema_version` | Yes | Migration tracking |
| `audit_log` | Yes | Immutable audit trail |
| `securities` | No | Reference; delisting tracked |
| `symbol_aliases` | No | Name resolution |
| `price_bars` | No | OHLCV data |
| `intraday_bars` | No | Minute-bar data |
| `corporate_actions` | No | Splits, dividends |
| `earnings_events` | No | Calendar + as_of for no look-ahead |
| `social_posts` | No | Sanitised posts |
| `ticker_mentions` | No | Entity resolution |
| `sentiment_records` | No | Per-post classification |
| `symbol_sentiment_daily` | No | Daily aggregates |
| `features` | No | Technical indicators |
| `market_regimes` | No | Regime classification |
| `signals` | **Yes** | Immutable research signals |
| `signal_revisions` | **Yes** | Status change history |
| `paper_trades` | No | Simulated/backtest trades |
| `refresh_runs` | No | Cross-process refresh state + single-flight lock |

The append-only tables (`schema_version`, `audit_log`, `signals`, `signal_revisions`) guarantee that historical records cannot be tampered with. This is enforced at the database layer, not just in code.
