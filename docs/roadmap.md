# Roadmap

ClaudeTrade is under active development. This roadmap outlines planned features and known gaps.

## Current Status (v0.1.0)

**Core functionality**: ✓ Complete
- 5 rules-based strategies (long and short)
- Signal generation, scoring, and lifecycle management
- Walk-forward backtesting with transaction costs
- Sentiment aggregation (rule-based and optional LLM)
- Market regime classification
- Risk controls and position sizing
- SQLite database with PostgreSQL path

**Major gaps**: 
- No CLI (API-only)
- No UI/dashboard
- No live trading
- No intraday execution
- No scheduler integration

## Near-term (v0.2.0, Q2 2024)

**CLI commands**:
- `claudetrade refresh` — Fetch latest market, earnings, and social data
- `claudetrade scan` — Generate signals for today
- `claudetrade backtest` — Run walk-forward validation on a date range
- `claudetrade export` — Write signals and trades to CSV/Excel
- `claudetrade secrets` — Manage credentials
- `claudetrade validate-config` — Check configuration syntax

**Streamlit dashboard** (basic):
- Live signals table with filtering
- Trade history view
- Performance metrics summary
- Settings panel for quick config changes

**Effort**: Medium (40–60 hours)

## Mid-term (v0.3.0, Q4 2024)

**Background scheduler**:
- Scheduled data refreshes (market, social, earnings)
- Daily signal generation
- Paper trading position updates
- Automated backtests on new data

**Live trading framework**:
- Broker adapter base class
- Alpaca integration (starting point; simplest API)
- Order submission and tracking
- Position reconciliation
- Emergency kill-switch

**Effort**: High (80–120 hours)

## Future (v1.0+)

**Machine learning**:
- Feature importance analysis
- Signal fusion (rule + ML ensemble)
- Hyperparameter optimization
- Model drift detection

**Intraday strategies**:
- Minute-bar data provider integration
- Intraday entry patterns (momentum, breakouts)
- Tick-level execution simulation

**Expanded data sources**:
- TikTok and Discord sentiment (if APIs stabilise)
- Options data (implied volatility, open interest)
- Derivatives strategies (covered calls, spreads)
- International markets (London, Tokyo, Hong Kong)

**Regulatory and compliance**:
- Tax reporting (wash-sale detection, long/short classification)
- Audit trail enhancement (trade attribution, explain decisions)
- Compliance rule engine (pattern-day trading, margin, etc.)

**Performance**:
- Distributed backtesting (run windows in parallel)
- Real-time data pipelines (Kafka, Faust)
- ML model serving (embed in signal engine)

**Effort**: Very high (> 200 hours for each major feature)

---

## Contributing

Contributions are welcome. Priority areas:

1. **CLI implementation** (immediate impact; moderate complexity)
2. **Streamlit UI** (improves usability significantly)
3. **Broker adapters** (enables live trading for motivated users)
4. **Additional strategies** (low barrier; high research value)
5. **Documentation** (always appreciated)

See the project repository for contribution guidelines.

---

## Known Limitations That May Be Addressed

- **Borrow cost and availability**: Integrate with a lending market API
- **Short squeeze detection**: Enhance sentiment classifiers
- **Delisting recovery**: Model post-delisting OTC trading
- **Options support**: Integrate option pricing models
- **International markets**: Add timezone and holiday handling

---

## What Will NOT Be Built

- **Proprietary ML models**: Keep research open-source
- **Live GUI on every platform**: Desktop first; web later
- **Crypto trading**: Out of scope (different ecosystem)
- **Forex or commodities**: Out of scope (different risk models)
- **Fully automated portfolio management**: System is decision-support, not autonomous
- **Paid SaaS**: Remain open-source and free

---

## Release Schedule

- **v0.1.0**: Q1 2024 (current)
- **v0.2.0**: Q2 2024 (CLI + basic UI)
- **v0.3.0**: Q4 2024 (scheduler + live trading)
- **v1.0.0**: 2025 (production-ready)

Dates are aspirational and may slip. The backlog is public; feature requests and bug reports drive priorities.

---

## How to Influence the Roadmap

1. **Open a GitHub issue** describing a feature or limitation you care about
2. **Upvote or comment** on existing issues to signal interest
3. **Contribute code** to priority items
4. **Share your use case**: Tell us how you use ClaudeTrade and what's missing

The maintainers review issues quarterly and adjust priorities based on community feedback.
