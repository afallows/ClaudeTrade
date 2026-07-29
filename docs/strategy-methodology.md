# Strategy Methodology and Performance Evaluation

This document describes the five trading strategies, the scoring framework, and the performance metrics used to evaluate them honestly.

## The Five Strategies

All strategies are rules-based, deterministic, and documented with their limitations.

### Strategy A: Sentiment-Confirmed Breakout

**File**: `src/claudetrade/strategies/a_sentiment_breakout.py`  
**Direction**: Long only  
**Thesis**: A breakout is more likely to follow through when new people started paying attention shortly before it, and the move is funded with real volume.

#### Entry Conditions

- Price breaks above a resistance level (identified as touches or Donchian high)
- Breakout volume is ≥ 1.5x the 20-day average
- ADX ≥ 18 (trending environment, not choppy)
- If social data available: sentiment accelerating, mention count accelerating, ≥ 5 unique authors, manipulation risk < 0.60

#### Exit Conditions

- **Initial stop**: Below structural support or 2 ATR basis, whichever is safer
- **Target 1**: Entry + 1.5 R (half position)
- **Target 2**: Entry + 2.75 R (remainder)
- **Trailing stop**: 2.5 ATR after first target
- **Time stop**: 15 sessions maximum
- **Pre-earnings exit**: Before confirmed earnings

#### Positioning

- Entry zone: Level ± 0.25 ATR on the low, Level + 0.35 ATR on the high
- Risk per share = Entry reference - Stop
- Position size = Account % risk / Risk per share

#### Setup Score Components

- Relative volume: up to 15 points
- Relative strength percentile: up to 10 points
- Sentiment acceleration: up to 15 points
- Base setup: 55 points

**Documented Weaknesses**:

- Breakouts fail often; expect modest win rate carried by reward:risk, not high hit rate
- Requires social data; degrades to volume-confirmed breakout without sentiment (score capped at 62)
- Hype component high risk; elevated hype reduces score by 8 points

---

### Strategy B: Sentiment Pullback

**File**: `src/claudetrade/strategies/b_sentiment_pullback.py`  
**Direction**: Long only  
**Thesis**: In an established uptrend, a controlled pullback on falling volume is supply exhaustion. Cooling-but-still-positive sentiment signals the crowd has stopped adding, not that it turned.

#### Entry Conditions

- 20-day MA > 50-day MA (uptrend established)
- Price not below 200-day MA (not in a downtrend)
- ADX ≥ 20 (trending environment)
- Higher-high, higher-low structure intact
- Pullback 3–15% from recent 20-day high
- Down-volume during pullback ≤ 1.1x average
- RSI 35–55 (oversold within trend, not broken)
- Price within 4% of nearest moving average (support proximity)
- **Confirmation bar**: Current close > current open AND current close > prior close
- If social data available: Sentiment positive but cooling (accel ≤ 0.30), manipulation risk < 0.60

#### Exit Conditions

- **Initial stop**: Below structural support (smaller of support level or swing low), max 0.5 ATR basis
- **Target 1**: Prior swing high
- **Target 2**: Entry + 2.5 R
- **Trailing stop**: 2.0 ATR after first target
- **Time stop**: 12 sessions maximum
- **Pre-earnings exit**: Before confirmed earnings

#### Positioning

- Entry zone: Price ± 0.3 ATR on low, Price + 0.5 ATR on high
- Risk per share = Entry reference - Stop
- Position size = Account % risk / Risk per share

#### Setup Score Components

- Down-volume suppression: up to 20 points
- Proximity to support: up to 10 points
- Sentiment confirmation: 8 points (if available)
- Base setup: 58 points

**Documented Weaknesses**:

- Performs poorly in choppy, trendless markets where every "pullback" is another leg of a range
- Requires confirmation bar; true reversals are filtered (misses entries)
- ADX and structure gates reduce failures but do not eliminate them

---

### Strategy C: Capitulation Reversal

**File**: `src/claudetrade/strategies/c_capitulation_reversal.py`  
**Direction**: Long only  
**Thesis**: When a name is heavily discussed, uniformly hated, extended far below averages, and then prints a reversal bar on climax volume, the marginal seller has often finished.

#### Entry Conditions

- Price is ≥ 12% below the 50-day MA
- RSI ≤ 32 (extreme oversold)
- Breakout volume (recent) ≥ 1.8x the 20-day average
- **Price evidence of exhaustion**: Reversal bar (closes in top third) OR engulfing OR higher low
- Sentiment data required: raw sentiment ≤ -0.25 (decisively negative)
- Mention acceleration ≥ 0.35 (climax volume in discussion, not quiet decline)
- Capitulation language score ≥ 0.40
- Regulatory catalyst score ≤ 0.45 (refuse unresolved fundamental catastrophe)
- Manipulation risk < 0.60

#### Exit Conditions

- **Initial stop**: Below recent washout low (2–3 bar low), max 1.5 ATR basis
- **Target 1**: Entry + 1.6 R (60% position)
- **Target 2**: Entry + 3.0 R (40% position)
- **Trailing stop**: 1.8 ATR after first target
- **Time stop**: 10 sessions maximum (reversals that stall are wrong)
- **Sentiment deterioration exit**: If sentiment turns negative on rebound

#### Positioning

- Entry zone: Price ± 0.2 ATR on low, Price + 0.6 ATR on high
- Risk per share = Entry reference - Stop
- **Position size = 0.5x** (deliberate reduction due to wide outcome distribution)

#### Setup Score Components

- Volume at washout: up to 10 points
- RSI overshoot: up to 10 points
- Capitulation signal: up to 8 points
- Opinion dispersion (healthy disagreement): 4 points
- Base setup: 52 points

**Documented Weaknesses**:

- **Most dangerous strategy** in the set; mean reversion works until it doesn't
- Failures are exactly the cases that lose most (left-tail risk)
- Size reduction (0.5x) and tight time stop (10 days) are mandatory, not optional
- Unresolved fundamental problems are filtered but not perfectly (sentiment + price alone cannot detect fraud, insolvency, etc.)

---

### Strategy D: Hype-Failure Short

**File**: `src/claudetrade/strategies/d_hype_failure_short.py`  
**Direction**: Short only  
**Thesis**: A vertical, promotion-driven advance that fails at breakout tends to unwind quickly because the marginal buyer was attracted by the move, not a thesis.

#### Entry Conditions

- 20-day ROC ≥ 25% (vertical advance)
- **Failed breakout**: Pattern detector flag OR price rolled back ≥ 3% from recent high
- Price below 9-day EMA (loss of fast momentum)
- **Bearish confirmation bar**: Close < open AND close < prior low
- Market cap ≥ $300M, avg daily volume ≥ $5M (borrow-availability proxies)
- Sentiment data required: sentiment acceleration ≥ 0.45 (hype spike, not organic)
- Hype score ≥ 0.55
- **Manipulation risk ≥ 0.40** (inversion: promotion is the setup, not a filter)
- Catalyst quality ≤ 0.40 (real catalyst supports the upside; poor catalyst predicts reversal)
- Short-squeeze chatter ≤ 0.55 (squeeze risk too high if trending)
- Shorts must be enabled in config

#### Exit Conditions

- **Initial stop**: Above failed high (1% buffer), or 1.6 ATR basis above reference
- **Target 1**: Entry - 1.5 R (50% position)
- **Target 2**: Entry - 2.8 R (50% position)
- **Trailing stop**: 1.5 ATR after first target
- **Time stop**: 8 sessions maximum (unwinds are fast or not happening)
- **Squeeze emergency exit**: On squeeze-like conditions

#### Positioning

- Entry zone: Price + 0.2 ATR on high, Price - 0.5 ATR on low
- Risk per share = Stop - Reference (inverted; stop is above)
- Position size = Account % risk / Risk per share

#### Setup Score Components

- Manipulation risk intensity: up to 12 points
- ROC magnitude: up to 10 points
- Duplicate text ratio: up to 8 points
- Squeeze chatter deduction: -15 points if elevated
- Base setup: 50 points

**Documented Weaknesses**:

- **Unbounded loss**: A short squeeze has no ceiling; stop is mandatory
- **Borrow reality not modelled**: Heavily promoted micro-caps often impossible to borrow; backtest cannot know historical availability
- **Timing**: Promotions can extend far longer than seems possible; early is indistinguishable from wrong
- Short-squeeze chatter reduces score, not eliminates it; some squeeze risk remains

---

### Strategy E: Post-Earnings Announcement Drift (PEAD)

**File**: `src/claudetrade/strategies/e_post_earnings_drift.py`  
**Direction**: Long or Short (direction from surprise sign)  
**Thesis**: Prices drift in the direction of large earnings surprises for weeks, because the market reprices gradually.

#### Entry Conditions

- A confirmed or estimated earnings report exists
- 2–12 sessions have passed since the report (settled enough, not too stale)
- EPS surprise magnitude ≥ 5% (large enough to drive drift)
- Event-day market reaction magnitude ≥ 3% (genuine repricing, not noise)
- Surprise sign agrees with event-day move (positive surprise on up day, negative on down day)
- Volatility has settled (current bar range ≤ 1.6x the 14-day ATR)
- Price is holding the gap (above 9-day EMA for long, below for short)
- Next earnings is ≥ 5 days away (can hold the position)
- Shorts must be enabled if direction is SHORT

#### Exit Conditions

- **Initial stop**: 2 ATR below (long) or above (short) current price, or beyond event bar
- **Target 1**: Entry + 1.6 R (50% position)
- **Target 2**: Entry + 2.8 R (50% position)
- **Trailing stop**: 2.5 ATR after first target
- **Time stop**: 20 sessions maximum
- **Pre-earnings exit**: Before next confirmed earnings report

#### Positioning

- Entry zone: Price ± 0.3–0.4 ATR
- Risk per share = Entry reference - Stop
- Position size = Account % risk / Risk per share

#### Setup Score Components

- Surprise magnitude: up to 15 points
- Event move magnitude: up to 8 points
- Sentiment alignment: 7 points (if available)
- Base setup: 55 points

**Documented Weaknesses**:

- Effect has weakened over time as it became widely known
- Surprise measurement is approximate (vendor consensus cannot be verified here)
- Estimated earnings dates carry leakage risk (market knew before the estimate was official)
- Date accuracy depends on the data source quality

---

## Signal Scoring Framework

Every candidate is scored on **13 components**, each mapped to 0–100 where higher is always better.

### Component Definitions

| Component | Meaning | Data Source |
|-----------|---------|-------------|
| **technical_setup** | Strategy's own conviction in the setup | Strategy proposal |
| **price_momentum** | ROC agreement across horizons + relative strength | Technical features |
| **volume_confirmation** | Volume and OBV agreement with move | Technical features |
| **reddit_sentiment** | Sentiment directional agreement; neutral if unavailable | Social posts |
| **x_sentiment** | Sentiment directional agreement; neutral if unavailable | Social posts |
| **sentiment_acceleration** | Rate of change in sentiment attention | Aggregation |
| **attention_acceleration** | Rate of change in post mention count | Aggregation |
| **catalyst_quality** | Quality and specificity of upcoming catalysts | Sentiment labels |
| **earnings_risk** | **Inverted**: 100 = no risk, 0 = high risk | Calendar + days |
| **liquidity** | Bid-ask spread, dollar volume | Features |
| **market_regime** | Environment multiplier (bull quiet, bear volatile, etc.) | Regime classifier |
| **manipulation_risk** | **Inverted**: 100 = no risk, 0 = high risk | Sentiment metrics |
| **data_confidence** | Quality and sample size of inputs | Data freshness + counts |

### Hard Gates (Cannot Be Overcome by Sentiment Alone)

1. **Price/volume gate**: Must have evidence of real interest and volume
2. **Earnings gate**: Cannot hold through upcoming earnings (configurable)
3. **Liquidity gate**: Must be able to enter and exit at reasonable costs
4. **Data freshness gate**: Stale data triggers data-quality flags
5. **Manipulation gate**: High manipulation risk rejects candidates (except intentionally for Strategy D)

### Confidence (Separate from Score)

Confidence reflects **data quality**, not the strength of the thesis. A signal can be:

- **High score, high confidence**: Strong setup with reliable data
- **High score, low confidence**: Strong setup but thin/stale data
- **Low score, high confidence**: Weak setup but reliable data

The signal engine uses both score and confidence for ranking and filtering.

### Weighted Sum Calculation

```
overall_score = (
    sum of (component * weight) for each component
) / sum of weights
```

Weights are normalised at use, so they sum to 1.0. This makes the overall score a pure average of 0–100, interpretable as "how ready is this candidate."

---

## Position Sizing

Position size is computed by the `PositionSizer` in `src/claudetrade/risk/sizing.py`:

```
position_size = (account_size * max_risk_pct) / risk_per_share
```

**Risk per share** = entry reference price - stop loss price.

**Anti-gambling adjustments**:

1. **Reward:risk floor**: Reject signals where reward:risk < 1.6:1
2. **Regime multiplier**: Bearish and high-vol regimes get 0.5x and 0.7x sizing
3. **Concentration checks**: Reduce size if sector or correlated exposure would exceed limits
4. **Capitulation special case**: Strategy C gets 0.5x sizing by design (wide outcome distribution)

---

## Performance Metrics

The backtest engine reports extensive metrics, not just win/loss ratio.

### Winning and Losing Trades

- **Trade**: Completed long or short position (entry + exit)
- **Winning trade**: Closed with net P&L > +0.05% of entry notional
- **Losing trade**: Closed with net P&L < -0.05% of entry notional
- **Breakeven trade**: Closed within ±0.05%; excluded from both win and loss counts

**Why breakeven is excluded**: A trade within the noise band around zero provides no information. Including it would:
- Inflate win count if profitable positions cluster just above 0.05%
- Hide losses that are classified as "breakeven" and thus invisible to the ratio

### Key Metrics

| Metric | Meaning | Notes |
|--------|---------|-------|
| **Win/loss ratio** | Winning trades / losing trades | `inf` if zero losses (degenerate); always reported literally |
| **Win rate** | Winning trades / (winning + losing trades) | Excludes breakeven |
| **Expectancy ($ and R)** | Average net P&L per trade (in dollars and R-multiples) | Negative expectancy with high win rate is flagged |
| **Profit factor** | Gross profit / gross loss | > 1.5 is good; < 1.0 is a losing system |
| **Sharpe ratio** | Return / volatility | Risk-adjusted return |
| **Sortino ratio** | Return / downside volatility | Penalises downside only |
| **Calmar ratio** | Return / max drawdown | Drawdown-adjusted return |
| **Max drawdown** | Largest peak-to-trough decline | In % and days |
| **Largest win / loss** | Single best and worst trade | Identifies outlier risk |
| **Average holding period** | Mean days in position | Validates assumptions |

### Anti-Gaming Validation Warnings

The system explicitly detects and flags "many small wins, one huge loss" pathologies:

1. **Degenerate win/loss ratio**: < 50 trades, zero losses, or ratio > 3.0 with < 100 trades
2. **Top 3 trades > 50% of profit**: Concentrated winner risk
3. **Negative expectancy**: Profit factor < 1.0 despite win rate > 50%
4. **Single largest loss > 2 x expectancy**: Tail risk not captured by mean

When any of these fire, the metrics section includes explicit warnings. A "perfect" win/loss ratio is a red flag, not an achievement.

---

## Backtest Execution Details

### Walk-Forward Methodology

The backtest uses walk-forward validation to avoid overfitting:

1. **Split date range into windows**: Train (504 days) → Test (126 days) → Step (126 days)
2. **For each window**:
   - Signal generation uses only training data
   - Backtest executes signals on test data (unseen)
   - Performance metrics computed on test trades only
3. **Hold-out sample**: Final 25% of the date range is reserved and never used
4. **Segment results**: Metrics are reported for train, test, and holdout separately

This prevents lookahead and trains on the right data for each test period.

### Execution Model

- **Signal computation**: On bar close; only historical data available
- **Order placement**: Next bar, using the configured entry reference (next open, next open limit, stop trigger)
- **Stop execution**: Intrabar; if price touches the stop during the day, the trade is filled at the stop (conservative)
- **Partial fills**: Enabled by default; large orders limited by participation rate
- **Cost application**: Commission, spread, slippage, SEC fees, FINRA TAF, borrow cost
- **Position exit**: Market on the specified exit reason (stop, target, time, earnings, etc.)

### Delisting and Survival Bias

- Delisted names are retained in the universe (not silently removed)
- If a signal is open and the name delists, the trade is closed at the delisting price (marked as a loss)
- This prevents survivorship bias: the backtest cannot hide loser names by deleting them

---

## Testing and Validation

Every backtest must meet these gates before results are considered valid:

1. **Minimum 30 trades per strategy** (beta phase)
2. **Minimum 50 trades for confident reporting** (production)
3. **Win rate confidence interval**: 95% CI reported (bootstrap resampling)
4. **Expectancy confidence interval**: 95% CI reported (normal approximation)
5. **No open trades at end of backtest** (forced close, graded)
6. **No look-ahead detected** (data provider audit)

Results below these thresholds are flagged as preliminary and should be interpreted with caution.

---

## Summary

The five strategies are deliberately simple and rules-based. They are documented with their failure modes. Performance is reported honestly: win/loss ratio is always accompanied by expectancy, profit factor, and validation warnings. High win rate with negative expectancy is explicitly flagged, not hidden.

This framework prevents the single most common pit fall of backtesting: taking profits early and letting losers run, which produces high win rates and low profits, then misleading you into thinking you've found the holy grail.
