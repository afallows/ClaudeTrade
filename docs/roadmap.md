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
- Full CLI (`claudetrade`: init, status, probe, refresh, scan, backtest, ui,
  secrets, paper, db, verify — see `src/claudetrade/cli.py`)
- Streamlit dashboard (`claudetrade ui`; five screens: dashboard, scanner,
  ticker detail, backtesting, settings)

**Major gaps**:
- No live trading (a `BrokerProvider` interface and a non-functional stub
  adapter exist in `src/claudetrade/brokers/`; no real venue is wired up)
- No intraday execution
- No scheduler integration (`SchedulerConfig` exists but is not wired to any job runner)
- The UI's backtest "Export as CSV/Excel" buttons are placeholders (the CLI's
  `claudetrade backtest --export` works)
- No CLI/UI command opens a paper trade — `PaperBroker.submit_signal` is
  implemented and tested but unreachable outside the Python API (see
  [docs/known-limitations.md](known-limitations.md#opening-a-paper-trade))

## Near-term

**Next UI rendition — ticker page modelled on CIBC Investor's Edge (owner TO-DO, 2026-07-30)**:

The owner's reference is the Investor's Edge stock snapshot page (GOOGL example
reviewed). Target a single rich ticker-detail view combining:

- **Chart controls**: timeframe pills (Intraday/1W/1M/3M/6M/YTD/1Y/3Y/5Y/Max)
  over the price chart, with a full-history range-brush mini-chart beneath —
  the specific control style the owner called out as good
- **Statistics panel**: prev close, open, day/52-week range, volume,
  market cap, P/E TTM, EPS TTM, beta, currency
- **Financial events sidebar**: dated timeline of upcoming earnings (with EPS
  estimate) and dividends (amount, yield)
- **Dividends block**: yield, TTM/forward amounts, ex-div/pay dates, payout ratio
- **Analyst consensus**: buy/hold/sell donut with rating count, average price
  target with % upside, and the low/avg/high 12-month projection fan chart
- **Headlines feed** with thumbnails
- **Sentiment alongside**: our differentiator — the Reddit/Stocktwits/news
  sentiment series and mention volume displayed next to the above, which
  Investor's Edge does not have

Data availability note: the TipRanks `dataForTicker` payload we now consume as
primary already carries most of this (analyst consensus + price targets +
projection series via `ptConsensus`/`consensusOverTime`, earnings via
`portfolioHoldingData`, dividends via `nextDividendDate`/`yearlyDividend*`,
market cap, company description). Close-only prices limit candlesticks to the
OHLCV chain; the range-brush works fine on closes.

**Remaining CLI/UI gaps**:
- `claudetrade validate-config` — Check configuration syntax (not implemented)
- Wire the UI's export buttons to the existing `backtest.reporting.export_csv`/`export_excel`
- `claudetrade paper submit <symbol>` (or a UI "paper trade this" button) to
  actually call `PaperBroker.submit_signal`

**Effort**: Low (a few hours each)

## Mid-term

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

1. **Broker adapters** (enables live trading for motivated users; the CLI, scan/backtest
   pipeline and UI are already in place and do not need to change)
2. **Additional strategies** (low barrier; high research value)
3. **Background scheduler wiring** (moderate complexity; config already exists)
4. **Documentation** (always appreciated)

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

- **v0.1.0**: current — core engine, CLI, and Streamlit UI
- **v0.2.0**: scheduler wiring + live-trading broker adapter
- **v1.0.0**: production-ready

Dates are aspirational and may slip. The backlog is public; feature requests and bug reports drive priorities.

---

## How to Influence the Roadmap

1. **Open a GitHub issue** describing a feature or limitation you care about
2. **Upvote or comment** on existing issues to signal interest
3. **Contribute code** to priority items
4. **Share your use case**: Tell us how you use ClaudeTrade and what's missing

The maintainers review issues quarterly and adjust priorities based on community feedback.
