# ClaudeTrade: Swing-Trading Research Application

**CRITICAL DISCLAIMER**: This application generates *research signals only*. It is NOT investment advice. Signals are produced by an automated research system and must be independently verified before any capital is risked. The application defaults to paper (simulated) trading and backtesting. **Live trading is not implemented** and would require separate explicit authorisation and a broker adapter (neither currently present in the codebase).

## Overview

ClaudeTrade is a Python-based framework for systematic swing-trading research, backtesting, and signal generation. It combines:

- **Five rules-based trading strategies** (sentiment-confirmed breakout, pullback, capitulation reversal, hype-failure short, post-earnings drift)
- **Multi-source sentiment aggregation** (Reddit, X/Twitter, synthetic data for testing)
- **Technical analysis engine** with feature computation and regime classification
- **Rigorous backtesting** with walk-forward validation, transaction costs, and anti-gaming controls
- **Paper trading** framework for simulation before any live deployment
- **Database persistence** (SQLite by default; PostgreSQL-compatible)
- **Optional LLM-powered sentiment classification** (OpenAI, Anthropic, or rule-based only)

All computation is deterministic, reproducible, and auditable. Every signal carries a reproducibility triple (code version, config hash, data snapshot).

## Quick Start

### Prerequisites

- Python 3.11+
- Virtual environment (recommended)

### Installation (Windows, macOS, Linux)

```bash
# Clone or extract the source
git clone https://github.com/afallows/claudetrade.git
cd ClaudeTrade

# Create virtual environment
python -m venv .venv

# Activate it
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install the package in editable mode, with the UI extra (Streamlit, Plotly, openpyxl)
pip install -e ".[ui]"
```

A Windows-specific, step-by-step walkthrough for a non-developer (no editable
install, no `git`) is in [docs/windows-install.md](docs/windows-install.md).

### First Run

The `claudetrade` command is installed as a console script by the `pip install`
above. Everything below works fully offline on synthetic data with zero
credentials — that is the default configuration.

```bash
# Create the database and see where the app's data lives
claudetrade init

# Check provider/network reachability and stored data coverage
claudetrade probe
claudetrade status

# Pull data from the configured providers (synthetic by default; no network needed)
claudetrade refresh

# Generate ranked signals for today and print them
claudetrade scan

# Launch the Streamlit dashboard
claudetrade ui
```

Each command's exact output and options are documented in
[docs/windows-install.md](docs/windows-install.md) and
`claudetrade <command> --help`.

To use a custom config, either copy `config.example.toml` to your app
directory (see [Configuration](#configuration) below) or pass `--config`:

```bash
claudetrade --help
claudetrade scan --config /path/to/config.toml
```

### Running the Backtest Engine

```bash
# From the CLI: replay strategies over the last 2 years and print a report
claudetrade backtest --start 2022-01-01 --end 2024-01-01 --export ./out
```

```python
# Or from the Pipeline/BacktestEngine API directly
import datetime as dt

from claudetrade.backtest.engine import BacktestEngine
from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline

config = AppConfig.load()
pipeline = Pipeline.bootstrap(config)

universe = pipeline.universe.for_session(dt.date(2024, 1, 1))
provider = pipeline.make_context_provider(
    symbols=universe.symbols,
    start=dt.date(2022, 1, 1),
    end=dt.date(2024, 1, 1),
)
engine = BacktestEngine(config)
result = engine.run(provider, start_session=dt.date(2022, 1, 1), end_session=dt.date(2024, 1, 1))
print(result.metrics)
```

### Using the Pipeline API

```python
import datetime as dt

from claudetrade.config import AppConfig
from claudetrade.pipeline import Pipeline

config = AppConfig.load()
pipeline = Pipeline.bootstrap(config)  # opens the database and applies migrations

# Refresh market and sentiment data
refresh_result = pipeline.refresh(
    start=dt.date(2024, 1, 1),
    end=dt.date(2024, 12, 31),
)
print(f"Ingested {refresh_result.sentiment_rows} sentiment rows")

# Generate signals
scan_result = pipeline.scan(dt.date(2024, 12, 31))
print(f"Generated {len(scan_result.scan.signals)} signals")
```

## Command-Line Interface

Every workflow is reachable from the `claudetrade` command (see `src/claudetrade/cli.py`);
the UI is a convenience layer on top of the same pipeline, not a separate implementation:

```
claudetrade version               # version and code identity
claudetrade init                  # create the database and apply migrations
claudetrade status                # provider health and stored data coverage
claudetrade probe                 # test reachability of live data endpoints
claudetrade refresh               # pull data from configured providers
claudetrade scan                  # generate ranked signals for a session
claudetrade backtest              # replay strategies over history
claudetrade ui                    # launch the Streamlit dashboard
claudetrade secrets set|list|delete   # manage credentials in the OS credential store
claudetrade paper status|positions|kill-switch   # inspect/drive the paper account
claudetrade db migrate|backup|restore            # database maintenance
claudetrade verify ledger|survivorship           # integrity and reproducibility checks
```

Run `claudetrade --help` or `claudetrade <command> --help` for full option lists.
**Live trading is not implemented**; `trading.mode = "live"` is rejected unless a
broker adapter is configured, and none ships with this project.

## Configuration

All configuration is in TOML format. A fully-commented example is provided in `config.example.toml`.

Configuration is resolved in layers (later layers win):

1. **Built-in defaults** (in `src/claudetrade/config.py`) — safe, conservative, offline
2. **TOML file** — `<app_dir>/config.toml` or `$CLAUDETRADE_CONFIG`. `app_dir` defaults to
   `%LOCALAPPDATA%\ClaudeTrade` on Windows and `~/.claudetrade` on macOS/Linux (see
   `default_app_dir()` in `src/claudetrade/config.py`).
3. **Environment variables** — `CLAUDETRADE_<section>__<field>` (double underscore for nesting)

**Key directories** (created on demand under `app_dir`):

- `data/` — Historical bars, earnings calendar, universe cache
- `logs/` — Application and audit logs
- `exports/` — Spreadsheet and JSON output files
- `cache/` — Sentiment and regime computation caches
- `snapshots/` — Point-in-time data snapshots used by backtests

**Data sources** (all optional; app degrades rather than fails):

- **Market data**: Synthetic (default, offline), CSV, or Stooq
- **Earnings**: Synthetic (default) or CSV
- **Social sentiment**: Reddit OAuth (config: `reddit.enabled`), X API v2 (config: `x.enabled`), or synthetic
- **AI classification**: OpenAI, Anthropic, or rules-based fallback (default)

See [docs/api-providers.md](docs/api-providers.md) for credential setup.

## Documentation

| Document | Purpose |
|----------|---------|
| [docs/requirements-specification.md](docs/requirements-specification.md) | Functional and non-functional requirements with implementation status |
| [docs/architecture.md](docs/architecture.md) | System design, layering, data flow, and PostgreSQL migration path |
| [docs/api-providers.md](docs/api-providers.md) | Setup guide for each data provider (credentials, rate limits, limitations) |
| [docs/strategy-methodology.md](docs/strategy-methodology.md) | Five strategies: entry/exit logic, stops, and documented weaknesses |
| [docs/security-and-privacy.md](docs/security-and-privacy.md) | Credential handling, log redaction, audit logging, formula-injection defence |
| [docs/database-schema.md](docs/database-schema.md) | Schema overview: tables, append-only guarantees, reproducibility columns |
| [docs/assumptions-and-limitations.md](docs/assumptions-and-limitations.md) | Honest statement of what the system assumes and what it does not model |
| [docs/known-limitations.md](docs/known-limitations.md) | Implementation gaps and design constraints |
| [docs/terms-and-licensing.md](docs/terms-and-licensing.md) | Third-party terms of service, data licensing, and project licence |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues and solutions (including Windows-specific issues) |
| [docs/windows-install.md](docs/windows-install.md) | Step-by-step install and first-run guide for a non-developer on Windows |
| [docs/windows-smoke-test.md](docs/windows-smoke-test.md) | Checklist for manually trialling the app end-to-end on Windows |
| [docs/windows-build.md](docs/windows-build.md) | Building a standalone Windows executable with PyInstaller |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records (ADRs) explaining key design choices |

## Project Layout

```
ClaudeTrade/
├── src/claudetrade/              # Main package
│   ├── cli.py                    # `claudetrade` command (init/status/probe/refresh/scan/backtest/ui/secrets/paper/db/verify)
│   ├── domain.py                 # Shared domain types
│   ├── config.py                 # Configuration models
│   ├── pipeline.py               # End-to-end orchestration
│   ├── secrets.py                # Credential resolution
│   ├── logging_setup.py          # Audit logging
│   │
│   ├── db/                       # Database layer
│   │   ├── models.py             # SQLAlchemy schema
│   │   ├── session.py            # Connection management
│   │   ├── migrations.py         # Schema versioning
│   │   └── backup.py             # Export/restore utilities
│   │
│   ├── providers/                # Data adapters
│   │   ├── base.py               # Provider protocols
│   │   ├── registry.py           # Factory functions
│   │   ├── market/               # Daily OHLCV (synthetic, csv, stooq)
│   │   ├── earnings/             # Earnings calendar
│   │   ├── social/               # Social sentiment (reddit, x, synthetic)
│   │   └── ai/                   # LLM classification (openai, anthropic, null)
│   │
│   ├── strategies/               # Five trading strategies
│   │   ├── base.py               # Strategy base class and context
│   │   ├── a_sentiment_breakout.py
│   │   ├── b_sentiment_pullback.py
│   │   ├── c_capitulation_reversal.py
│   │   ├── d_hype_failure_short.py
│   │   └── e_post_earnings_drift.py
│   │
│   ├── signals/                  # Signal generation and ledger
│   │   ├── engine.py             # Signal ranking and lifecycle
│   │   ├── scoring.py            # Component scoring
│   │   └── ledger.py             # Immutable signal store
│   │
│   ├── sentiment/                # Sentiment aggregation
│   │   ├── aggregation.py        # Time-decayed sentiment rolls
│   │   ├── classifiers.py        # Rule-based sentiment scoring
│   │   └── entity_resolution.py  # Ticker mention extraction
│   │
│   ├── features/                 # Technical feature computation
│   │   └── feature_builder.py    # OHLCV → technical indicators
│   │
│   ├── regime/                   # Market regime classification
│   │   └── market_regime.py      # Bull/bear/neutral classification
│   │
│   ├── backtest/                 # Historical replay engine
│   │   ├── engine.py             # Walk-forward orchestrator
│   │   ├── execution.py          # Fill simulation
│   │   ├── costs.py              # Transaction cost models
│   │   ├── metrics.py            # Performance accounting (Sharpe, win/loss, etc.)
│   │   ├── portfolio.py          # Equity curve tracking
│   │   └── reporting.py          # Result serialisation (CSV/Excel export)
│   │
│   ├── paper/                    # Paper trading simulation
│   │   ├── portfolio.py          # Database-backed paper account, positions, equity curve
│   │   └── broker.py             # PaperBroker: BrokerProvider implementation for paper mode
│   │
│   ├── brokers/                  # Execution boundary (ADR-0007 Decision 4)
│   │   ├── base.py               # BrokerProvider ABC; risk-guard runs before every order
│   │   └── null_live.py          # Stub live-broker shape; not a real venue, no live trading
│   │
│   ├── risk/                     # Position sizing and limits
│   │   ├── sizing.py             # Kelly, fixed %, volatility-based
│   │   └── limits.py             # Drawdown, exposure, concentration checks
│   │
│   ├── data/                     # Data ingestion and cleaning
│   │   ├── ingest.py             # Provider → database ETL
│   │   ├── quality.py            # Data freshness and anomaly checks
│   │   ├── universe.py           # Security universe management
│   │   ├── context.py            # Point-in-time context builder
│   │   └── snapshot.py           # Reproducible data snapshots
│   │
│   ├── ui/                       # Streamlit dashboard (`claudetrade ui`)
│   │   ├── app.py                # Entry point; 5-screen navigation
│   │   └── screens/               # dashboard, scanner, ticker_detail, backtesting, settings
│   │
│   └── utils/                    # Utilities
│       ├── text.py               # Social text sanitisation
│       ├── hashing.py            # Config fingerprinting
│       └── timeutils.py          # Date/timezone utilities
│
├── tests/                        # Test suite (pytest)
├── config.example.toml           # Fully-commented config template
├── pyproject.toml                # Project metadata and dependencies
└── README.md                     # This file
```

## Data Flow

```
Market Data         Earnings            Social Media        AI Provider
(OHLCV bars)        (Calendar)          (Posts)             (Classification)
     │                  │                  │                      │
     └──────────────────┴──────────────────┴──────────────────────┘
                          │
                    [Data Ingest]
                          │
                    [Quality Checks]
                          │
          ┌───────────────┼───────────────┐
          │               │               │
      [Database]      [Features]      [Sentiment]
          │           (Technical)      (Aggregation)
          │               │               │
          └───────────────┼───────────────┘
                          │
                    [Regime Classify]
                          │
                  [Signal Engine]
                 (5 Strategies)
                          │
          ┌───────────────┼───────────────┐
          │               │               │
      [Paper Trade]   [Backtest]      [UI/Exports]
      (Simulation)   (Validation)
```

## How to Run Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest tests/

# Run with coverage
pytest --cov=claudetrade tests/

# Run only fast tests (skip slow backtests)
pytest -m "not slow" tests/

# Run a specific test file
pytest tests/test_engine.py
```

## Requirements

### Core Dependencies

- `pydantic>=2.5` — Configuration and data validation
- `SQLAlchemy>=2.0` — ORM (SQLite and PostgreSQL)
- `pandas>=2.1`, `numpy>=1.26` — Data manipulation
- `httpx>=0.27` — HTTP client for API providers
- `typer>=0.12` — CLI framework used by `claudetrade` (see `src/claudetrade/cli.py`)
- `APScheduler>=3.10` — listed dependency; no scheduled jobs are wired up yet (see
  [docs/known-limitations.md](docs/known-limitations.md)). Use the OS scheduler
  (cron, Windows Task Scheduler) to run `claudetrade refresh`/`scan` on a timer instead.
- `keyring>=25.0` — OS credential store integration

### Optional Dependencies

- **UI**: `streamlit>=1.36`, `plotly>=5.22`, `openpyxl>=3.1` (`pip install -e ".[ui]"`) — required for `claudetrade ui`
- **ML**: `scikit-learn>=1.4` (`pip install -e ".[ml]"`) — listed for future ML-based signal fusion; not currently used by any code path
- **Build**: `pyinstaller>=6.6` (`pip install -e ".[build]"`) — for producing a standalone Windows executable; see [docs/windows-build.md](docs/windows-build.md)
- **Dev**: `pytest>=8.0`, `ruff>=0.5`, `mypy>=1.10` (`pip install -e ".[dev]"`) — testing, linting, type checking

See [pyproject.toml](pyproject.toml) for the full list.

## Security and Privacy

- **Secrets**: Never stored in config, database, or logs. Use environment variables or OS credential store. See [docs/security-and-privacy.md](docs/security-and-privacy.md).
- **Audit logging**: Every credential access, signal generation, and trade is recorded in an append-only log.
- **Text sanitisation**: Social media posts are sanitised before processing to remove usernames and neutralise injection attempts.
- **Author pseudonymisation**: Reddit/X authors are stored as salted hashes, never plaintext usernames.
- **Formula-injection defence**: Spreadsheet exports are checked for formula-like content before writing.

## Licence and Attribution

The ClaudeTrade code is released under the **MIT Licence**. See the project repository for the full licence text.

**Third-party software** used by this project is subject to its own licences:

- **pandas**, **numpy**, **SQLAlchemy**: BSD/Apache-2.0
- **Pydantic**: MIT
- **httpx**: BSD
- **APScheduler**: MIT
- **keyring**: MIT/PSF
- **Streamlit**: Apache 2.0 (UI optional)
- **Plotly**: MIT
- **scikit-learn**: BSD-3-Clause

**Data licences**:

- **Stooq data** (free tier): No redistribution rights; unsuitable for commercial use without a separate paid licence.
- **Synthetic data**: Generated locally; carries no third-party licensing restrictions.
- **Reddit data**: Subject to Reddit's API terms of service and user-agreement restrictions on commercial use.
- **X (Twitter) data**: Subject to X's API terms of service; archived social data carries separate restrictions.

The operator is responsible for holding appropriate licences and respecting terms of service for any real market data and social media they configure.

## Support and Issues

For documentation, open an issue on the project repository. For API provider-specific issues, see [docs/api-providers.md](docs/api-providers.md).

---

**Last updated**: 2026-07-29  
**Application version**: 0.1.0  
**Status**: Alpha (active development)
