# Requirements Specification

This document maps functional and non-functional requirements to their implementation status and code locations.

## Functional Requirements

### Data Ingestion and Providers

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Load daily OHLCV bars from a market data source | Implemented | `src/claudetrade/providers/market/` | Synthetic, CSV, and Stooq adapters; fallback chain with degradation |
| Load earnings calendar and results | Implemented | `src/claudetrade/providers/earnings/` | Synthetic and CSV providers |
| Fetch social sentiment from Reddit | Partial | `src/claudetrade/providers/social/reddit.py` | OAuth only; requires credentials; gracefully disabled if unavailable |
| Fetch social sentiment from X (Twitter) | Partial | `src/claudetrade/providers/social/x_provider.py` | Requires paid API tier; gracefully disabled if unavailable |
| Generate synthetic data for testing | Implemented | `src/claudetrade/providers/` (all `synthetic.py` modules) | Deterministic, seeded, for validation only |
| Rate limit calls to external providers | Implemented | `src/claudetrade/providers/base.py` (`RateLimiter` class) | Token-bucket limiter with configurable calls/minute |
| Resolve ticker mentions in social posts | Implemented | `src/claudetrade/sentiment/entity_resolution.py` | Cashtag, company name, fuzzy matching, aliases |
| Sanitise social media text | Implemented | `src/claudetrade/utils/text.py` | Remove usernames, URLs; neutralise instruction sequences |

### Signal Generation

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Implement 5 rules-based strategies | Implemented | `src/claudetrade/strategies/a_*.py` through `e_*.py` | Sentiment breakout, pullback, capitulation, hype-failure short, post-earnings drift |
| Score signals on 13 technical and sentiment components | Implemented | `src/claudetrade/signals/scoring.py` | Inverted risk components; hard gates against data misuse |
| Rank candidates by overall score | Implemented | `src/claudetrade/signals/engine.py` | Normalised weights; confidence separate from score |
| Classify market regime (bull/bear/high-vol) | Implemented | `src/claudetrade/regime/market_regime.py` | Trend, breadth, volatility classification |
| Apply position-sizing multipliers by regime | Implemented | `src/claudetrade/risk/sizing.py` | Risk-off and high-vol multipliers |
| Enforce position sizing limits | Implemented | `src/claudetrade/risk/limits.py` | Max position size, sector exposure, correlation checks |
| Track signal lifecycle (actionable → expired) | Implemented | `src/claudetrade/signals/engine.py` (`SignalStatus` enum) | Price-based status transitions; time expiry |
| Write signals to an immutable ledger | Implemented | `src/claudetrade/signals/ledger.py` | Append-only; no in-place updates; SQLite trigger enforcement |

### Sentiment Analysis

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Aggregate sentiment with time decay | Implemented | `src/claudetrade/sentiment/aggregation.py` | Exponential half-life; configurable window |
| Rule-based sentiment classifier | Implemented | `src/claudetrade/sentiment/classifiers.py` | Lexicon-based; multi-label (bullish, bearish, hype, fear, etc.) |
| Optional LLM-powered sentiment | Partial | `src/claudetrade/providers/ai/` | OpenAI and Anthropic adapters; optional; degrades to rules |
| Detect manipulation risk (duplicates, source concentration) | Implemented | `src/claudetrade/sentiment/aggregation.py` | Duplicate text hashing; source concentration scoring |
| Detect bot-like behaviour | Partial | `src/claudetrade/sentiment/aggregation.py` | Author metrics (age, karma, followers); not a classifier, just metrics |
| Sentiment-based filters on candidates | Implemented | `src/claudetrade/config.py` (`FilterConfig`) | Min unique authors, min confidence, max manipulation risk |

### Backtesting

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Replay strategies over historical data | Implemented | `src/claudetrade/backtest/engine.py` | Walk-forward orchestration |
| Simulate transaction costs (commissions, spreads, slippage) | Implemented | `src/claudetrade/backtest/costs.py` | SEC fees, FINRA TAF, spread, impact modelling |
| Model partial fills | Implemented | `src/claudetrade/backtest/execution.py` | Limit fills by participation rate; mark-to-market on partial |
| Grade trades (win/loss/breakeven) | Implemented | `src/claudetrade/domain.py` (`Trade.outcome()`) | Breakeven excluded from ratio; manual threshold configurable |
| Report performance metrics | Implemented | `src/claudetrade/backtest/metrics.py` | Win/loss ratio, expectancy, Sharpe, Sortino, Calmar, profit factor, confidence intervals |
| Enforce anti-gaming controls | Implemented | `src/claudetrade/backtest/metrics.py`, `src/claudetrade/risk/sizing.py` | Min reward:risk floor, mandatory time stops, force-close positions, delisted as losses, breakeven excluded |
| Walk-forward validation | Implemented | `src/claudetrade/backtest/walkforward.py` | Configurable train/test windows and step size |
| Report validation warnings | Implemented | `src/claudetrade/backtest/metrics.py` (`validation_warnings`) | Detects concentration, degenerate ratios, negative expectancy hidden by high win rate |

### Paper Trading (Simulation)

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Track open positions and fills | Implemented | `src/claudetrade/paper/tracker.py` | Simulated fills using configured cost model |
| Mark positions to market daily | Implemented | `src/claudetrade/paper/tracker.py` | Equity curve, drawdown tracking |
| Record trade exits and P&L | Implemented | `src/claudetrade/paper/tracker.py` | Exit reason tracking (stop, target, time, etc.) |
| Persist paper trading state to database | Implemented | `src/claudetrade/db/models.py` (`PaperTradeRow`) | Read-only historical view |

### Database and Persistence

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Store OHLCV bars | Implemented | `src/claudetrade/db/models.py` (`PriceBar`) | Indexed by symbol and session |
| Store earnings events | Implemented | `src/claudetrade/db/models.py` (`EarningsEventRow`) | Confirmed/estimated dates; as_of column for no look-ahead |
| Store social posts (sanitised) | Implemented | `src/claudetrade/db/models.py` (`SocialPostRow`) | Text hash, duplicate group, injection risk score |
| Store sentiment aggregates | Implemented | `src/claudetrade/db/models.py` (`SymbolSentimentDaily`) | Time-decayed, per-source and aggregated |
| Store signals (immutable) | Implemented | `src/claudetrade/db/models.py` (`SignalRow`) | SQLite trigger prevents updates/deletes |
| Store audit log | Implemented | `src/claudetrade/db/models.py` (`AuditLog`) | Append-only; code version, action, entity |
| Support SQLite and PostgreSQL | Implemented | `src/claudetrade/db/session.py`, `src/claudetrade/config.py` | Connection pooling; ORM abstracts database differences |
| Run schema migrations | Implemented | `src/claudetrade/db/migrations.py` | Custom idempotent runner; tracks applied versions |

### Configuration and Secrets

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Load configuration from TOML file | Implemented | `src/claudetrade/config.py` | Pydantic settings; env var overrides |
| Resolve secrets from environment or OS store | Implemented | `src/claudetrade/secrets.py` | Environment vars, keyring (macOS/Linux/Windows), graceful fallback |
| Prevent secrets in config files | Implemented | `src/claudetrade/config.py` | Defensive: strips `secrets` key from TOML before loading |
| Generate config hash for reproducibility | Implemented | `src/claudetrade/config.py` (`config_hash` property) | Stamped onto every signal and backtest result |

### Logging and Audit

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Structured logging (JSON) | Implemented | `src/claudetrade/logging_setup.py` | JSON format configurable |
| Log rotation | Implemented | `src/claudetrade/logging_setup.py` | Configurable max size and backup count |
| Redact secrets from logs | Implemented | `src/claudetrade/logging_setup.py`, `src/claudetrade/secrets.py` | No API keys in stack traces or formatted output |
| Append-only audit log | Implemented | `src/claudetrade/logging_setup.py` (`audit_event`), `src/claudetrade/db/models.py` (`AuditLog`) | Tracks credential access, signal generation, trade events |

---

## Non-Functional Requirements

### Performance and Scalability

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Scan and generate signals for up to 2000 symbols daily | Implemented | `src/claudetrade/config.py` (`UniverseConfig.max_symbols`) | Batch processing; configurable |
| Backtest 10 years of daily data in reasonable time | Partial | `src/claudetrade/backtest/engine.py` | Walk-forward reduces per-window cost; full 10-year test may take hours |
| Support up to 250 LLM calls per run | Implemented | `src/claudetrade/config.py` (`AIConfig.max_calls_per_run`) | Batch requests; caching |

### Reliability and Degradation

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Graceful degradation when a data source fails | Implemented | `src/claudetrade/providers/registry.py` (`FallbackMarketProvider`) | Fallback chain; reduced capability flagged, not fatal |
| Report degradation explicitly | Implemented | `src/claudetrade/pipeline.py` (`PipelineResult.degraded_sources`) | Caller sees which sources are unavailable |

### Reproducibility

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Make backtest results reproducible | Implemented | `src/claudetrade/domain.py` (`Signal` reproducibility fields) | Code version, config hash, data snapshot hash, strategy version |
| Make signal generation deterministic | Implemented | `src/claudetrade/pipeline.py` | Same config, same data, same signals (within numerical precision) |
| Snapshot data for reproducible backtests | Implemented | `src/claudetrade/data/snapshot.py` | Manifest-based; no copy; references exact rows and versions |

### Data Integrity

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Detect stale data | Implemented | `src/claudetrade/data/quality.py` | Configurable staleness threshold; warning issued if exceeded |
| Prevent survivorship bias in backtests | Implemented | `src/claudetrade/domain.py` (`SecurityInfo.delisted_date`) | Delisted names retained; counted as losses if trades are open |
| Prevent look-ahead bias | Partial | `src/claudetrade/providers/base.py` (protocol `supports_point_in_time`) | Providers declare point-in-time support; backtester tracks `as_of` dates |
| Enforce no in-place signal updates | Implemented | `src/claudetrade/signals/ledger.py`, `src/claudetrade/db/models.py` | SQLite trigger; append-only revisions |

### Security

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Store credentials securely | Implemented | `src/claudetrade/secrets.py` | OS credential store (keyring); environment variables; never in config |
| Defend against prompt injection | Partial | `src/claudetrade/providers/ai/` | Social text sanitisation; injection-risk heuristic; high-risk posts blocked |
| Sanitise formula injection in exports | Implemented | `src/claudetrade/backtest/reporting.py` (export functions) | Check for formula-like content before writing CSV/Excel |
| Audit credential access | Implemented | `src/claudetrade/logging_setup.py` (audit log) | Every `get_secret` call logged |

### Maintainability

| Requirement | Status | Implementation | Notes |
|-------------|--------|----------------|-------|
| Type hints on all public APIs | Implemented | `src/claudetrade/` (all modules) | `mypy` type checking configured |
| Documented interfaces (docstrings) | Partial | `src/claudetrade/` (core modules complete; some helpers minimal) | Design decisions in ADRs |
| Modular architecture | Implemented | `src/claudetrade/` (provider adapters, strategies, signals separate) | Adapter pattern for extensibility |
| No hard-coded magic numbers | Implemented | `src/claudetrade/config.py` | All thresholds configurable; strategy constants named |

---

## Requirements Traceability

### By Module

**`claudetrade.domain`**: Shared types; defines Signal, Trade, Bar, SymbolSentiment, etc.  
**`claudetrade.config`**: Configuration models; credential name lookups (not values).  
**`claudetrade.secrets`**: Credential resolution from env/keyring; never stored in-memory persistently.  
**`claudetrade.providers.*.`**: Adapter pattern for market, earnings, social, AI data.  
**`claudetrade.strategies.*.`**: Five rules-based strategy implementations; entry/exit logic.  
**`claudetrade.signals.scoring`**: Component scoring; hard gates.  
**`claudetrade.signals.engine`**: Signal ranking, lifecycle, expiry.  
**`claudetrade.signals.ledger`**: Immutable signal store; append-only revisions.  
**`claudetrade.sentiment.*.`**: Aggregation, classification, entity resolution, manipulation detection.  
**`claudetrade.features.builder`**: Technical indicator computation.  
**`claudetrade.regime.market_regime`**: Market regime classification and sizing multipliers.  
**`claudetrade.backtest.engine`**: Walk-forward orchestration; strategy replay.  
**`claudetrade.backtest.execution`**: Fill simulation; cost application.  
**`claudetrade.backtest.metrics`**: Performance accounting; win/loss ratio; validation warnings.  
**`claudetrade.paper.tracker`**: Simulated position tracking and P&L.  
**`claudetrade.risk.sizing`**: Position sizing models.  
**`claudetrade.risk.limits`**: Risk limit enforcement.  
**`claudetrade.db.models`**: Schema; append-only enforcement.  
**`claudetrade.db.migrations`**: Schema versioning and idempotent application.  
**`claudetrade.data.ingest`**: Provider → database ETL.  
**`claudetrade.data.quality`**: Freshness checks, anomaly detection.  
**`claudetrade.data.context`**: Point-in-time context builder for strategies.  
**`claudetrade.data.snapshot`**: Reproducible data snapshots for backtests.  
**`claudetrade.pipeline`**: End-to-end orchestration of refresh and scan.  

---

## Known Gaps

The following requirements are **not yet implemented** (see [docs/known-limitations.md](known-limitations.md) for details):

- **Live trading**: No broker adapter is present. Live mode is rejected if `live_trading_authorised=true` but no broker is configured.
- **CLI**: No command-line entry point yet (infrastructure exists; commands not yet defined).
- **UI**: No Streamlit dashboard yet (optional `streamlit` dependency is listed; code not written).
- **Scheduler**: APScheduler dependency is included but no background task runners are implemented.
- **Machine learning**: Optional `scikit-learn` dependency listed; ML-based signal fusion not implemented.
- **Options support**: Only equity long/short is modelled.
- **Intraday execution**: Only daily close prices and next-open fills; no intraday routing.

These gaps are intentional: the core is complete and tested; optional features are listed for future development.
