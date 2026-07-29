# Architecture

ClaudeTrade is a layered system that separates concerns: data acquisition, feature computation, signal generation, and validation (backtesting). Every layer is deterministic and testable independently.

## System Overview

```mermaid
graph TB
    subgraph sources["Data Sources"]
        market["Market Data<br/>Synthetic/CSV/Stooq"]
        earnings["Earnings<br/>Synthetic/CSV"]
        reddit["Reddit<br/>OAuth/Synthetic"]
        x["X/Twitter<br/>API/Synthetic"]
        ai["AI Provider<br/>Anthropic/OpenAI/Null"]
    end

    subgraph ingest["Data Ingestion Layer"]
        provider_chain["Fallback Provider Chain"]
        quality["Quality Checks<br/>Staleness, Anomalies"]
        entity["Entity Resolution<br/>Ticker Mentions"]
    end

    subgraph features["Feature Computation"]
        tech["Technical Indicators<br/>OHLCV → RSI, ATR, MA, etc."]
        sentiment["Sentiment Aggregation<br/>Time-Decayed, Multi-Label"]
        regime["Market Regime<br/>Bull/Bear Classification"]
    end

    subgraph context["Point-in-Time Context Builder"]
        context_builder["StrategyContext<br/>Snapshot @ Session Date"]
    end

    subgraph signals["Signal Generation"]
        strategies["5 Strategies<br/>Entry/Exit Logic"]
        scoring["Component Scoring<br/>13 Components"]
        engine["Signal Engine<br/>Ranking, Lifecycle, Expiry"]
        ledger["Signal Ledger<br/>Immutable Append-Only"]
    end

    subgraph validation["Backtesting & Validation"]
        backtest["Walk-Forward<br/>Engine"]
        execution["Fill Simulation<br/>Cost Modelling"]
        metrics["Performance Metrics<br/>Win/Loss, Sharpe, etc."]
    end

    subgraph paper["Paper Trading Simulation"]
        tracker["Position Tracker<br/>Open, Exits, P&L"]
    end

    subgraph db["Database"]
        model_layer["SQLAlchemy ORM<br/>SQLite/PostgreSQL"]
        schema["Schema<br/>Append-Only Enforcement"]
    end

    subgraph output["Output & Reporting"]
        ui["UI/Streamlit<br/>Dashboard"]
        exports["Exports<br/>CSV, JSON, Excel"]
    end

    sources -->|API calls| ingest
    ingest -->|Validated data| db
    db -->|Historical bars| features
    db -->|Earnings, social, sentiment| features
    features -->|Computed features| context
    ai -->|Sentiment labels<br/>optional| features
    context -->|Point-in-time snapshot| strategies
    strategies -->|Proposals| scoring
    scoring -->|Scored signals| engine
    engine -->|Ranked signals| ledger
    ledger -->|Persistent| db
    db -->|Historical data| backtest
    backtest -->|Simulated fills| execution
    execution -->|Closed trades| metrics
    ledger -->|Live signals| tracker
    tracker -->|Positions, fills| db
    db -->|Signals, trades| output
    metrics -->|Results| output
```

## Layering Rules

### The Critical Invariant: Point-in-Time Contexts

Strategies receive **point-in-time contexts**, never time-series. A `StrategyContext` captures the exact state of a security on one session: current price, recent bars, indicators, earnings dates, sentiment—all as of the close of that session.

This design:

1. **Prevents look-ahead bias**: A strategy cannot inadvertently reference future data.
2. **Makes backtests and live scans identical**: Both build contexts the same way and call the same strategy code.
3. **Enables reproducibility**: The context is a deterministic product of its inputs (bars, features, sentiment).

**Example**:

```python
ctx = ContextBuilder(db, config).build(
    symbol="AAPL",
    session=date(2024, 1, 15),  # The "as of" date
)
# ctx.price is AAPL's close on 2024-01-15
# ctx.bars[-1].session is 2024-01-15
# Indicators look back from 2024-01-15, never forward
```

### No Look-Ahead

Data providers declare `supports_point_in_time: bool`:

- **True** (market data, earnings): The provider supports `as_of` queries; historical data can be replayed accurately.
- **False** (current social engagement, some AI classifiers): The provider only serves today's values; using it in backtests requires noting the limitation.

Backtests filter the provider list and report which ones are used, so look-ahead contamination is visible.

### Graceful Degradation

Every adapter implements the `ProviderStatus` protocol, which includes an `available: bool` field. When a primary provider fails, the fallback chain is tried:

```
Primary (e.g., Stooq) fails
  ↓
Try Fallback 1 (e.g., CSV) — if available, use it
  ↓
Try Fallback 2 (e.g., Synthetic) — last resort
  ↓
Report unavailable; caller disables dependent features
```

No data source failure halts the pipeline. Missing social data doesn't prevent a signal; it just can't confirm sentiment.

## Key Components

### Providers

**Location**: `src/claudetrade/providers/`

Each provider type (market, earnings, social, AI) implements a protocol:

- **MarketDataProvider**: `get_daily_bars()`, `get_security_info()`, `get_corporate_actions()`, `list_universe()`
- **EarningsProvider**: `get_earnings_events()`
- **SocialProvider**: `fetch_posts(symbols, lookback_hours)`
- **AIProvider**: `complete()`, `complete_batch()`

All raise `ProviderError` (or subclasses) on failure; callers catch and degrade.

**Adapters**:

| Provider | Module | Status | Notes |
|----------|--------|--------|-------|
| Market: Synthetic | `market/synthetic.py` | Implemented | Fabricated data; seeded for reproducibility |
| Market: CSV | `market/csv_provider.py` | Implemented | Local files; no rate limit |
| Market: Stooq | `market/stooq.py` | Implemented | Free data; HTTP fallback |
| Earnings: Synthetic | `earnings/synthetic.py` | Implemented | Fabricated events |
| Earnings: CSV | `earnings/csv_provider.py` | Implemented | Local calendar |
| Social: Synthetic | `social/synthetic.py` | Implemented | Fabricated posts; seeded |
| Social: Reddit | `social/reddit.py` | Partial | OAuth only; requires credentials |
| Social: X | `social/x_provider.py` | Partial | Requires paid API tier |
| AI: Null | `ai/null_provider.py` | Implemented | Falls back to rules |
| AI: OpenAI | `ai/openai_provider.py` | Partial | Optional; requires API key |
| AI: Anthropic | `ai/anthropic_provider.py` | Partial | Optional; requires API key |

### Features and Regime

**Location**: `src/claudetrade/features/`, `src/claudetrade/regime/`

- **FeatureBuilder**: Computes technical indicators (RSI, ATR, moving averages, Donchian channels, ADX, OBV, relative strength, etc.) from a time-series of bars.
- **RegimeClassifier**: Classifies the market environment (bull quiet, bull volatile, neutral, bear volatile, bear quiet) from trend, breadth, and volatility metrics.

Both are deterministic: same input → same output, always.

### Strategies

**Location**: `src/claudetrade/strategies/`

Five rules-based strategies, each a subclass of `Strategy`:

| Strategy | Entry | Exit | Stops |
|----------|-------|------|-------|
| **A: Sentiment Breakout** | Breakout above resistance + volume + accelerating unique-author sentiment | Targets at 1.5R and 2.75R; trailing stop at 2.5 ATR | Initial stop at resistance support; 15 sessions max |
| **B: Sentiment Pullback** | Pullback to moving average in uptrend + confirmation bar + cooling sentiment | Targets at prior high and 2.5R; trailing stop at 2.0 ATR | Initial stop below structure; 12 sessions max |
| **C: Capitulation Reversal** | Reversal bar after price washout + capitulation language + climax volume | Targets at 1.6R and 3.0R; trailing stop at 1.8 ATR | Stop below washout low; 10 sessions max; **position size = 0.5x** |
| **D: Hype-Failure Short** | Failed breakout + hype + manipulation risk + bearish confirmation | Targets at 1.5R and 2.8R below entry; trailing stop at 1.5 ATR | Stop above failed high; 8 sessions max |
| **E: Post-Earnings Drift** | Settlement + surprise + direction alignment + volatility normalisation | Targets at 1.6R and 2.8R in surprise direction; trailing stop at 2.5 ATR | Stop beyond event bar; 20 sessions max |

Each strategy:

- Implements `evaluate(ctx: StrategyContext) → StrategyProposal | None`
- Declines on explicit reasons (logged)
- Returns a proposal with entry zone, stops, targets, setup score, and evidence
- Documents its own weaknesses

### Signal Scoring and Lifecycle

**Location**: `src/claudetrade/signals/`

The **SignalEngine** takes strategy proposals and scores them:

1. **Component Scoring**: 13 components (technical, momentum, volume, sentiment, earnings risk, manipulation risk, etc.) → 0–100 each.
2. **Hard Gates**: Price/volume/earnings/liquidity gates reject candidates even if sentiment is high.
3. **Weighted Sum**: Components are normalised and summed per the configured weights.
4. **Confidence**: Separate from score; reflects data quality and sample size.
5. **Ranking**: Candidates sorted by score; top N returned.
6. **Lifecycle**: Each signal has a status (actionable, approaching, extended, triggered, expired, rejected).
7. **Immutable Ledger**: Signals are written once; status changes are appended as new revisions.

### Backtesting and Metrics

**Location**: `src/claudetrade/backtest/`, `src/claudetrade/backtest/metrics.py`

The backtest engine replays strategies over historical data:

1. **Walk-Forward Loop**: Train window → test window → step; repeated over full date range.
2. **Execution Simulation**:
   - Signals computed on bar close; execution starts next bar.
   - Entry uses configured reference (open, open-limit, stop trigger).
   - Stops and targets use intrabar fills (conservative: assume triggered if price touched).
3. **Cost Modelling**: Commission, spread, slippage, SEC fees, FINRA TAF, short borrow.
4. **Trade Grading**:
   - Closed trades only (open trades skipped).
   - Breakeven threshold configurable (default ±0.05%).
   - Breakeven trades excluded from win/loss counts.
   - Delisted names with open positions counted as losses.
5. **Performance Metrics**:
   - Win/loss ratio, win rate, expectancy (both $ and R-multiples).
   - Sharpe, Sortino, Calmar ratios.
   - Profit factor, drawdown, return percentiles.
   - Confidence intervals (bootstrap for win rate; normal approximation for expectancy).
6. **Validation Warnings**:
   - Win/loss ratio is degenerate (< 50 trades, zero losses, etc.).
   - Top 3 trades represent > 50% of profit (concentration).
   - Expectancy is negative despite high win rate (anti-gaming gate).

### Database and Migrations

**Location**: `src/claudetrade/db/`

- **SQLAlchemy ORM**: All models portable between SQLite and PostgreSQL.
- **Append-Only Enforcement**:
  - `signals` and `signal_revisions` tables never receive UPDATE or DELETE.
  - SQLite migration 002 installs a trigger to reject these operations.
  - `audit_log` is append-only by design (no foreign key back-references).
- **Migration Runner**: `migrations.py` manages schema versioning idempotently.

**Key Tables**:

| Table | Purpose | Append-Only | Notes |
|-------|---------|-------------|-------|
| `securities` | Reference data (delisted_date retained) | No | Index by symbol, exchange, delisted_date |
| `price_bars` | Daily OHLCV | No | Index by (symbol, session); unique per source |
| `earnings_events` | Calendar + results; `as_of` for point-in-time | No | `as_of` column critical for no look-ahead |
| `social_posts` | Sanitised posts (text hash, injection_risk) | No | Text hash for dedup; raw_ref pointer for re-fetch |
| `ticker_mentions` | Resolved symbol refs (confidence) | No | Unique per (post, symbol) |
| `sentiment_records` | Per-post, per-symbol classifier output | No | Unique per (post, symbol, classifier) |
| `symbol_sentiment_daily` | Time-decayed daily aggregate | No | Unique per (symbol, session, source) |
| `signals` | Immutable signal records | **Yes** | SQLite trigger prevents UPDATE/DELETE |
| `signal_revisions` | Status changes (actionable → expired) | **Yes** | Append-only; tracks signal lifecycle |
| `audit_log` | Credential access, signal events, trades | **Yes** | Append-only by design |

---

## PostgreSQL Migration Path (ADR-0003)

The system is written to be portable to PostgreSQL:

1. **ORM Only**: No SQLite-specific SQL in strategy or backtest code.
2. **Column Types**: All types have PostgreSQL equivalents (Integer, Float, String, JSON, Date, DateTime).
3. **Migrations**: Schema versioning is database-agnostic; migration runner is generic.

To migrate:

```toml
# config.toml
[database]
url = "postgresql://user:pass@localhost/claudetrade"
```
```bash
# Then run:
claudetrade db migrate
```

Data can be exported from SQLite and imported into PostgreSQL using standard tools.

---

## Configuration Resolution

```
defaults (AppConfig fields) ← TOML file ← environment variables
```

**Example**:

```toml
# config.toml
[risk]
max_risk_per_trade_pct = 1.0
```

```bash
# Override via env var
export CLAUDETRADE_RISK__MAX_RISK_PER_TRADE_PCT=0.5
```

```python
config = AppConfig.load()
# config.risk.max_risk_per_trade_pct == 0.5
```

---

## Reproducibility

Every generated signal carries a reproducibility triple:

- **code_version**: Package version + git SHA (e.g., `0.1.0+g1a2b3c4d`)
- **config_hash**: SHA256 of the effective public configuration (excludes paths, includes all settings)
- **data_snapshot_hash**: Manifest of database rows used in the computation (not a copy; a reference)
- **strategy_version**: Strategy code version tag

This triple means: **"Given this code, config, and data, this signal is deterministic."**

Backtests use the same triple to claim reproducibility.

---

## Anti-Gaming Measures

### Hard Floors

- **Minimum reward:risk ratio** (default 1.6:1): Reject signals where the expected loss exceeds the target profit.
- **Minimum sample size for validation**: 30 trades for beta, 50 for confident reporting.
- **Breakeven exclusion**: Trades within ±0.05% net return are excluded from both win and loss counts, preventing 1-unit wins from hiding 2-unit losses.

### Mandatory Stops

- **Time stops**: Every strategy has a maximum holding period (6–20 days). No indefinite holds.
- **Force-close at end of backtest**: Any open position is closed at market, graded as win/loss, and counted.

### Degenerate Ratio Detection

When a backtest produces a win/loss ratio with < 50 trades, zero losses, or negative expectancy despite high win rate, the metrics clearly mark it as degenerate and flag warnings.

---

## Testing Strategy

Tests are organised by layer:

- **Unit tests** (providers, strategies, sentiment): Isolated component testing.
- **Integration tests** (pipeline, backtest): Full workflow with synthetic data.
- **Slow tests** (backtests): Marked with `@pytest.mark.slow`; skipped by default.

All tests use synthetic, deterministic data sources (no live API calls by default).

---

## Future Extensibility

### Adding a New Strategy

1. Create `src/claudetrade/strategies/z_my_strategy.py`
2. Subclass `Strategy`; implement `evaluate(ctx: StrategyContext) → StrategyProposal | None`
3. Register with `@register_strategy` decorator
4. Add to `signals.enabled_strategies` in config

### Adding a New Data Provider

1. Implement the protocol (e.g., `MarketDataProvider`)
2. Register in `src/claudetrade/providers/registry.py`
3. Add config fields to `AppConfig` (e.g., `MarketDataConfig`)
4. Provider is auto-discovered via the registry

### Adding Performance Metrics

1. Compute the metric in `src/claudetrade/backtest/metrics.py`
2. Add field to `PerformanceMetrics` dataclass
3. Export in result reports and UI

---

## Dependency Graph

```
[domain] (no deps)
  ← [config]
  ← [secrets]
  ← [logging_setup]
    ← [providers.base]
    ← [providers.registry]
      ← [providers.market.*]
      ← [providers.earnings.*]
      ← [providers.social.*]
      ← [providers.ai.*]
    ← [db.models]
    ← [db.session]
    ← [data.*]
    ← [features.*]
    ← [sentiment.*]
    ← [regime.*]
    ← [strategies.*]
    ← [signals.*]
    ← [backtest.*]
    ← [paper.*]
    ← [risk.*]
    ← [pipeline]
```

All dependencies are forward-only; no circular imports. This makes testing, refactoring, and extension straightforward.
