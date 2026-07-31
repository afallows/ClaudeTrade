# The Backtest Report

One command produces the evidence an owner should read before trusting any of
the five strategies' recommendations:

```
claudetrade backtest report
```

This walk-forward backtests every registered strategy, in isolation, over
whatever bars/sentiment/earnings are already stored in this installation's
database (the full available history by default), and writes:

- `exports/backtest-report-<date>.md` -- for a human to read
- `exports/backtest-report-<date>.json` -- the same content, machine-readable

Ask Claude Desktop "how have the strategies performed historically?" (via the
`get_backtest_report` MCP tool -- see `docs/claude-desktop-mcp.md`) and it
reads the same JSON back. It never runs a backtest itself; if no report
exists yet it says so and tells you to run the command above.

## What it claims

For each strategy, out-of-sample (never the training window a threshold was
tuned against):

- **Win rate**, with a 95% confidence interval
- **Expectancy per trade**, already net of commissions, fees, spread,
  slippage and borrow cost -- never a gross figure
- **Profit factor**, **max drawdown**, and **average holding period**
- The **rejection funnel** for every walk-forward window, so a low trade
  count is always attributable to a specific stage (gates, score threshold,
  sizing, portfolio limits, ...) rather than left as an unexplained zero
- An **equity curve summary** (start, end, and a reconciliation check that
  the tracked equity actually matches the sum of completed trades' P&L --
  the same invariant `test_portfolio_reconciliation.py` protects)

## What it deliberately refuses to claim

**A significance gate, not a leaderboard.** Every strategy section leads with
one of two headlines, before any number:

- `STATISTICALLY SIGNIFICANT (walk-forward out-of-sample)` -- the strategy
  cleared *both* gates: enough completed out-of-sample trades
  (`min_trades_for_validation`, 30 by default), *and* a bootstrap confidence
  interval on expectancy that excludes zero.
- `INSUFFICIENT EVIDENCE` -- one or both gates were not cleared. The
  point-estimate numbers are still shown below the headline (nothing is
  hidden), but the headline is the takeaway a skim gets, and it never reads
  as a claim of edge it hasn't earned.

A strategy is also headlined `INSUFFICIENT EVIDENCE` whenever the stored
history is too short to form even one walk-forward train+test window
(`walk_forward_train_days` + `walk_forward_test_days`, 630 calendar days by
default). In that case the report falls back to a single in-sample pass over
the whole window, shown for information only and labelled
`in_sample_single_pass_fallback` -- an in-sample number is not out-of-sample
proof no matter how good it looks, so it is never allowed to earn the
"significant" headline.

**Zero trades is a complete answer.** A strategy with no completed trades in
the window reports exactly that -- "0 completed trades" plus the rejection
funnel's top reasons -- never a table of `0.00`/`NaN` cells standing in for
"nothing happened". The same applies to any individual metric that comes back
`None`: Sharpe/Sortino/Calmar render as `unavailable (<reason>)`, not as a
silent `0.0` or `inf`.

**Reproducibility, not vibes.** The header states the exact data this run
used: symbol count, session range, stored sentiment-row count, the
configuration hash, and the code version. Re-running the same command against
the same database with the same config and code reproduces the same report.

**Synthetic data proves nothing about real markets.** If
`market_data.provider = "synthetic"` (see `providers/market/synthetic.py`),
every number in the report is fabricated by design -- useful for exercising
the reporting machinery end to end with zero API keys, never as evidence
about how a strategy would trade real securities.

## Options

```
claudetrade backtest report [--start YYYY-MM-DD] [--end YYYY-MM-DD] \
    [--strategies name1,name2] [--output-dir PATH]
```

- `--start` / `--end` default to the earliest/latest stored price bar
  ("everything available").
- `--strategies` defaults to every registered strategy (not just the ones
  currently enabled for live scanning in `config.toml`) -- this report
  answers "how much should any of these be trusted", a broader question than
  "what's live today".
- `--output-dir` defaults to the configured exports directory
  (`paths.exports_dir`).

See also `claudetrade backtest` (no subcommand) for a single combined-strategy
backtest run, and `docs/strategy-methodology.md` for what each strategy's
entry/exit rules and setup score actually are.
