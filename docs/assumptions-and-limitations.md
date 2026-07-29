# Assumptions and Limitations

This document explicitly states what ClaudeTrade assumes about the world and what it does not model.

## Core Assumptions

### Market-Related Assumptions

1. **Daily bars are sufficient for swing trading** — Most position holds are 6–20 days. Intrabar execution detail (precise fills within a session) is not modelled.

2. **Simplified NYSE holiday calendar** — The system uses a hardcoded list of ~10 holidays per year. Unexpected market closures (emergency halts, regulatory actions) are not modelled.

3. **Next-day entry is realistic** — Signals computed on bar close execute the next bar at the specified reference price (open, open-limit, or stop-trigger). In reality, a 16:45 signal might miss the next 9:30 open if there is news overnight.

4. **Delisting price is recoverable** — When a security is delisted, backtest assumes the last bar's close is available for exit. In reality, delisted names may trade OTC, often at prices far from their last official close.

5. **Borrow availability is unlimited** — The system does not model borrow scarcity or cost variation. Hard-to-borrow names are excluded by market-cap proxy only; actually attempting to short them may fail. Borrow cost is fixed (3% annualised by default), not varying by supply.

6. **Partial fills follow participation limits** — When an order is large, the backtest limits fills to 2% of daily volume. Real brokers have different rules and market impact estimates vary.

7. **Earnings dates are known in advance** — The `as_of` column prevents look-ahead within a backtest, but the dates themselves come from estimates; if an earnings was announced 5 seconds before market close and the system runs at 16:45, that information has already leaked.

### Data-Related Assumptions

8. **Social engagement counts are final** — Upvote counts, comment counts, etc. are treated as immutable. In reality, they change hours and days later, so historical sentiment cannot be perfectly reconstructed.

9. **Author metrics are reliable** — Follower count, karma, account age are sourced from the platform at fetch time. These can be faked (inactive accounts, purchased followers).

10. **Sentiment classifier is correct** — Rule-based and LLM-based classifiers are heuristics. They misclassify sarcasm, complex negation, and technical language.

11. **Entity resolution is perfect** — When resolving "$AAPL" or "Apple" to a ticker, the system is usually right but can be wrong (Apple Inc. vs Apple Records; conflicting results from multiple symbol references in one post).

12. **Historical data is accurate** — Stooq, CSV, and synthetic providers are assumed correct. Real-world issues (data gaps, reporting errors, retroactive corrections) are not modelled.

### Strategy Assumptions

13. **Entry timing precision** — Strategies define an "entry zone" (e.g., price between $100 and $102) and the backtest assumes you can fill anywhere in that zone. In reality, you might be able to buy $101 but not $101.50 because the stock only touched $101.20 that session.

14. **Stops are honoured** — Backtests assume if price touches a stop level, the trade is exited at that price. In reality, a gap can skip the stop entirely (e.g., earnings gap), resulting in larger losses.

15. **Targets are not reached twice** — Once a target is hit and a partial position is exited, the remaining position uses a trailing stop or time stop. The system does not enter the trade again if price pulls back below the target.

16. **Volume-weighted fill assumption** — When simulating fills, the system does not model actual order books, depth, or time-of-day effects. All fills are assumed to happen at the bar's reference price.

### Risk Model Assumptions

17. **Volatility is stationary** — Position sizing uses ATR (Average True Range) as a volatility estimate, assuming 14 days of history is predictive of the next days. In regime shifts, this can be dangerously wrong.

18. **Correlations are static** — Sector exposure limits assume correlations are stable. During market stress, correlations spike and diversification fails.

19. **Drawdown recovers** — Risk limits (daily loss, weekly loss) assume you can trade again tomorrow if the limit is not hit. A black-swan event could make that assumption wrong.

### Operational Assumptions

20. **Commission and spread assumptions are realistic** — Default costs assume commission-free brokers (standard in 2024) and 3 bps half-spread. Different brokers vary widely.

21. **Borrow cost is 3% annualised** — This is a historical average. High short interest names cost much more; hard-to-borrow names are impossible.

22. **You have the discipline to follow signals** — Paper trading and backtests assume perfect rule-following. Humans override systems when scared or greedy.

---

## What Is NOT Modelled

### Execution Reality

- **Market microstructure**: No order book, depth, or queue position simulation
- **Intraday price action**: Only daily closes matter; the detailed path within a day is unknown
- **Slippage variation**: Fixed slippage model; does not account for market conditions, order size surprises, or time-of-day
- **Liquidity stress**: Assumes normal market conditions; no "stock halted", "circuit breaker", or "liquidity crises"
- **Options and derivatives**: Only equity long/short is modelled
- **Margin calls and forced liquidation**: Account is assumed to have sufficient margin indefinitely

### Market Environment

- **Regulatory changes**: Tax-loss harvesting windows, short-sale circuits, trade halts are not modelled
- **Exceptional events**: Market crashes, wars, pandemics—system uses historical correlations which may not hold
- **Overnight gaps**: If price gaps over a stop level, the trade is not exited at the stop; it's filled at the next bar's open (gap slippage applied)
- **Tick size and minimum moves**: Sub-cent precision is not constrained; real markets round to $0.01

### Data Limitations

- **Survivorship bias (partially addressed)**: Delisted names are retained but may have gaps in historical data or be listed under different tickers before rebranding
- **Reporting delays**: Earnings surprise is only known after the report date, but the exact timing (9:30 AM vs. after close) is estimated
- **Data errors and retroactive corrections**: If a bar is later corrected (e.g., a dividend adjustment), the backtest does not rerun
- **Corporate actions (partially)**: Splits and dividends are not modelled perfectly; some corporate actions (reverse splits, spin-offs) are complex and system simplifies them

### Risk and Psychology

- **Behavioral biases**: The system does not account for drawdown psychology, revenge trading, or overconfidence
- **Event risk**: Concentrated risk around earnings, FDA decisions, or other discrete events
- **Tail risk**: Value-at-Risk and other risk measures assume normal distributions; real returns have fat tails
- **Regime change**: Backtests assume the past predicts the future; structural market shifts invalidate historical patterns

### Machine Learning

- **Model overfitting (detected but not eliminated)**: Walk-forward validation reduces overfitting but does not eliminate it; testing on the holdout set only happens once
- **Distribution shift**: Models trained on one regime may fail in another
- **Feature importance**: Feature selection is manual; unsupervised or automatic feature engineering is not attempted

### Implementation and Operations

- **High-frequency feedback loops**: The system does not re-check sentiment or technical levels mid-session; signals are checked once daily at close
- **Partial fills and scaling**: The system does not model scale-in or scale-out; each signal is either fully entered or fully rejected
- **Trade management flexibility**: Strategies have fixed stop/target rules; human adjustments are not supported
- **Cost estimation uncertainty**: Commission, spread, and slippage are estimated, not known in advance
- **Broker API coverage**: No live broker connection is implemented; paper and backtest are simulation only

---

## Honest Performance Caveats

### Backtests Cannot Be Trusted Without Qualification

1. **Past performance is not indicative of future results** (cliché but true)
2. **Backtests cannot model the discrete events** (earnings surprises, tweets, regulatory decisions) that actually drive real-world trading
3. **Transaction costs are estimates** — real costs may vary by 2–3x
4. **Execution is idealized** — real fills may be worse than simulated
5. **Slippage grows with AUM** — a $50k backtest result looks different when you have $500k

### Signal Quality Depends on Data

1. **Social sentiment is coarse** — upvotes and post count are noisy, not truth
2. **Earnings surprises are measured against estimates** — the estimate itself is uncertain
3. **Technical indicators are lagging** — by definition, they reflect history, not future
4. **Regime classification is post-hoc** — classifying bull/bear requires recent data; classification lags actual regime change

### Rules Cannot Capture Context

1. **Five strategies cover only a subset of opportunities** — other patterns exist that these strategies miss
2. **Hard gates are sometimes too hard** — you might miss genuine opportunities because a filter was too strict
3. **Sentiment gates are sometimes too soft** — you might enter a pump-and-dump despite manipulation filters
4. **Entry timing is approximate** — the backtest assumes you can fill in the entry zone; real markets are stickier

---

## Mitigations

To work within these limitations:

1. **Use walk-forward validation** — Test on unseen data (in-sample vs. out-of-sample)
2. **Track multiple metrics** — Win/loss ratio + expectancy + profit factor + Sharpe ratio; no single metric is sufficient
3. **Validate on multiple time periods** — Does it work in bull, bear, and sideways markets?
4. **Segment by market conditions** — Report results separately by regime, sector, and cap bucket
5. **Conservative costs** — Assume costs are higher than you think
6. **Lower leverage** — Use 1–2% risk per trade, not 5%
7. **Diversify** — Don't put all capital into one strategy or sector
8. **Review signals daily** — Backtest is a starting point; research and common sense are final filters
9. **Paper trade first** — Live forward-test in simulation before risking real capital
10. **Assume the backtest is biased optimistic** — Expect live results to be 30–50% worse than backtest

---

## No Financial Advice

**This system generates research signals for educational purposes only. It is NOT investment advice. Every signal is a hypothesis, not a guarantee. You are responsible for your own trades, losses, and compliance.**

Specific limitations:

- Backtests cannot know future borrow costs or availability
- Signals may fail in regimes different from the training period
- Social sentiment is aggregated from anecdotes, not facts
- Earnings surprises are measured against estimates, not true economic impact

**Always independently verify before risking capital.**
