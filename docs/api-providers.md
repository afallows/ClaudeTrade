# Data Providers and API Setup

This guide explains how to configure each data source and what to expect from each provider.

## Credential Storage

**Secrets are never stored in config files or source code.** Instead, use one of these methods:

### Method 1: Environment Variables (Recommended for CI/Containers)

Set environment variables prefixed with `CLAUDETRADE_SECRET_`:

```bash
export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
export CLAUDETRADE_SECRET_REDDIT_CLIENT_ID="..."
export CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET="..."
```

Then in config:

```toml
[ai]
api_key_credential = "anthropic_api_key"
```

The system looks up `CLAUDETRADE_SECRET_ANTHROPIC_API_KEY` at runtime.

### Method 2: OS Credential Store (Recommended for Desktop)

Store credentials in the OS keyring:

```bash
# Set a credential
claudetrade secrets set anthropic_api_key

# Remove a credential
claudetrade secrets delete anthropic_api_key

# View configured credentials
claudetrade secrets list
```

On macOS this uses Keychain; on Windows, Credential Manager; on Linux, Secret Service.

### Method 3: Direct in Config (NOT RECOMMENDED)

You can put secret values directly in config (they will be ignored for security):

```toml
# This is ignored; always use environment or credential store
[secrets]
anthropic_api_key = "sk-ant-..."
```

---

## Market Data Providers

### Synthetic (Default, Offline)

**Module**: `src/claudetrade/providers/market/synthetic.py`

Fabricated OHLCV data for testing and development. Deterministic (seeded by config's `backtest.random_seed`). **Not for real trading.**

**Configuration**:

```toml
[market_data]
provider = "synthetic"
fallbacks = ["csv"]
```

**Limitations**:

- Data is synthetic, not real market data
- Volume patterns are fabricated
- Suitable only for engine validation and development

**Licence**: Generated locally; no third-party restrictions.

### CSV (Offline)

**Module**: `src/claudetrade/providers/market/csv_provider.py`

Load daily bars from CSV files in a local directory.

**Configuration**:

```toml
[market_data]
provider = "csv"
csv_dir = "/path/to/your/csv/files"
fallbacks = []
```

**File Format**:

One file per symbol, named `{SYMBOL}.csv`:

```
Date,Open,High,Low,Close,AdjClose,Volume
2024-01-01,150.5,151.0,149.5,150.8,150.8,1000000
2024-01-02,150.8,152.0,150.5,151.5,151.5,1100000
```

**Limitations**:

- Files must be manually updated
- No delisting information (names with splits/delistings need manual annotation)

**Licence**: You are responsible for the licence of the data you provide. Stooq free data, for example, does not permit redistribution.

### Stooq (Online)

**Module**: `src/claudetrade/providers/market/stooq.py`

Free historical data from Stooq.

**Configuration**:

```toml
[market_data]
provider = "stooq"
rate_limit_per_minute = 60
request_timeout_s = 20.0
fallbacks = ["csv"]
```

**Credentials**: None required (no authentication).

**Limitations**:

- Free data; no commercial redistribution rights
- Rate limited (public API, shared across users)
- Stale data possible if the service is under load
- Delisting information may be incomplete

**Licence**: Stooq data is free for personal research only. Commercial use requires a paid licence.

---

## Earnings Providers

### Synthetic (Default)

**Module**: `src/claudetrade/providers/earnings/synthetic.py`

Fabricated earnings events. Deterministic (seeded).

**Configuration**:

```toml
[earnings]
provider = "synthetic"
fallbacks = ["csv"]
```

**Limitations**:

- Data is synthetic; not based on real earnings dates

### CSV (Offline)

**Module**: `src/claudetrade/providers/earnings/csv_provider.py`

Load earnings calendar from a CSV file.

**Configuration**:

```toml
[earnings]
provider = "csv"
csv_path = "/path/to/earnings.csv"
```

**File Format**:

```
Symbol,ReportDate,Session,Confirmed,EPSEstimate,EPSActual,RevenueEstimate,RevenueActual,SurprisePct,AsOf
AAPL,2024-01-25,amc,true,6.05,6.05,117.0,119.6,5.5,2024-01-20T00:00:00Z
MSFT,2024-01-30,amc,true,2.69,2.86,52.0,56.2,8.1,2024-01-25T00:00:00Z
```

- **Session**: `bmo` (before market open), `amc` (after market close), `during`, `unknown`
- **Confirmed**: `true` if the date is confirmed; `false` if estimated
- **AsOf**: The datetime the earnings event became known (critical for preventing look-ahead)

**Limitations**:

- Manual update required
- Missing historical revisions (when was this date announced?)

---

## Social Media Providers

### Synthetic (Default, Offline)

**Module**: `src/claudetrade/providers/social/synthetic.py`

Fabricated social posts seeded by config's `backtest.random_seed`. Separate seeds for Reddit and X so they don't emit identical data.

**Configuration**:

```toml
[reddit]
provider = "synthetic"
enabled = false  # Synthetic is always available; enable to include in scans

[x]
provider = "synthetic"
enabled = false
```

**Limitations**:

- Posts are generated, not scraped
- Cannot research real discussion

### Reddit (Live OAuth)

**Module**: `src/claudetrade/providers/social/reddit.py`

Official Reddit OAuth API. Requires credentials.

**Setup**:

1. Go to https://www.reddit.com/prefs/apps
2. Create an app (choose "script" type)
3. Note the **client ID** and **client secret**

**Configuration**:

```toml
[reddit]
enabled = true
provider = "reddit"
client_id_credential = "reddit_client_id"
client_secret_credential = "reddit_client_secret"
subreddits = ["stocks", "investing", "StockMarket", "SecurityAnalysis", "options", "swingtrading"]
posts_per_subreddit = 100
comments_per_post = 50
lookback_hours = 72
rate_limit_per_minute = 60
user_agent = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
store_author_names = false
```

**Store credentials**:

```bash
# Environment variables
export CLAUDETRADE_SECRET_REDDIT_CLIENT_ID="your_client_id"
export CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET="your_client_secret"

# Or OS credential store
claudetrade secrets set reddit_client_id
claudetrade secrets set reddit_client_secret
```

**Limitations**:

- OAuth required; no API key fallback
- Rate limited to 60 calls/minute (public tier)
- Posts are searchable only in the last 6 months (API limitation)
- Engagement counts (score, num_comments) are mutable at the source; historical sentiment cannot be perfectly reconstructed
- Author names are stored as salted hashes only (never plaintext), per config `store_author_names = false`

**Licence**: Reddit data is subject to Reddit's API terms and user agreement. Commercial use of aggregated social data may require permission.

### X/Twitter (Paid API v2)

**Module**: `src/claudetrade/providers/social/x_provider.py`

X API v2. Requires a **paid tier** (free tier has no search capability).

**Setup**:

1. Go to https://developer.twitter.com/en/portal/dashboard
2. Create an app and apply for elevated access
3. Note your **Bearer Token** (v2 API)

**Configuration**:

```toml
[x]
enabled = true
provider = "x"
bearer_credential = "x_bearer_token"
query_terms = ["$AAPL", "$MSFT", "Apple earnings"]
max_results_per_query = 100
lookback_hours = 48
rate_limit_per_minute = 15
request_timeout_s = 20.0
store_author_names = false
```

**Store credentials**:

```bash
export CLAUDETRADE_SECRET_X_BEARER_TOKEN="your_bearer_token"
# or
claudetrade secrets set x_bearer_token
```

**Limitations**:

- Paid API tier required (free tier has no search)
- Rate limited to 15 calls/minute (v2 API standard)
- Query syntax is strict (required: `$SYMBOL` for cashtags)
- Engagement counts are mutable; historical sentiment cannot be perfectly reconstructed
- Author handles are stored as salted hashes only

**Licence**: X/Twitter data is subject to X's API terms. Redistribution restrictions apply.

---

## AI Providers

### Null (Default, Rules-Based)

**Module**: `src/claudetrade/providers/ai/null_provider.py`

No external AI calls. Sentiment is computed deterministically using rule-based classifiers.

**Configuration**:

```toml
[ai]
provider = "null"
```

**Characteristics**:

- Zero cost
- Fully deterministic
- Faster than LLM-based classification
- Lexicon-based; may miss complex context or sarcasm

### Anthropic Claude

**Module**: `src/claudetrade/providers/ai/anthropic_provider.py`

Uses Claude (Sonnet, Haiku, Opus) for sentiment classification.

**Setup**:

1. Go to https://console.anthropic.com/
2. Create an account and generate an API key
3. Note your **API Key** (starts with `sk-ant-`)

**Configuration**:

```toml
[ai]
provider = "anthropic"
model = "claude-sonnet-5"
api_key_credential = "anthropic_api_key"
max_output_tokens = 900
temperature = 0.0
request_timeout_s = 45.0
max_calls_per_run = 250
daily_cost_limit_usd = 5.0
cache_enabled = true
cache_ttl_hours = 168
batch_size = 12
```

**Store credentials**:

```bash
export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
# or
claudetrade secrets set anthropic_api_key
```

**Cost Tracking**:

```toml
input_cost_per_mtok_usd = 3.0    # Update to current pricing
output_cost_per_mtok_usd = 15.0
daily_cost_limit_usd = 5.0
```

The system tracks costs locally and refuses requests once the daily limit is exceeded.

**Limitations**:

- Requires API key and account
- Token usage is billable
- No real-time rate limiting on the server; respect the configured limits
- LLM outputs are non-deterministic (even with `temperature=0.0`, precision varies)

**Licence**: Subject to Anthropic's API terms and privacy policy.

### OpenAI

**Module**: `src/claudetrade/providers/ai/openai_provider.py`

Uses GPT-4, GPT-3.5, etc.

**Setup**:

1. Go to https://platform.openai.com/
2. Create an account and generate an API key
3. Note your **API Key** (starts with `sk-`)

**Configuration**:

```toml
[ai]
provider = "openai"
model = "gpt-4"
api_key_credential = "openai_api_key"
max_output_tokens = 900
temperature = 0.0
request_timeout_s = 45.0
max_calls_per_run = 250
daily_cost_limit_usd = 5.0
cache_enabled = false
batch_size = 12
```

**Store credentials**:

```bash
export CLAUDETRADE_SECRET_OPENAI_API_KEY="sk-..."
# or
claudetrade secrets set openai_api_key
```

**Cost Tracking**:

Update the per-token costs to match OpenAI's current pricing (changes frequently):

```toml
input_cost_per_mtok_usd = 0.005    # GPT-4 example; check OpenAI's pricing
output_cost_per_mtok_usd = 0.015
daily_cost_limit_usd = 5.0
```

**Limitations**:

- Requires API key and account with payment method
- Token usage is billable
- LLM outputs are non-deterministic

**Licence**: Subject to OpenAI's API terms and privacy policy.

---

## Provider Selection Advice

### For Backtesting and Development

Use **synthetic** providers (default):

```toml
[market_data]
provider = "synthetic"

[earnings]
provider = "synthetic"

[reddit]
provider = "synthetic"

[x]
provider = "synthetic"

[ai]
provider = "null"
```

Everything runs offline, deterministically, and requires no credentials.

### For Realistic Development (Small Universe)

Use **CSV for market data + earnings**, **synthetic for social**:

```toml
[market_data]
provider = "csv"
csv_dir = "~/my_data/bars"
fallbacks = ["synthetic"]

[earnings]
provider = "csv"
csv_path = "~/my_data/earnings.csv"

[reddit]
provider = "synthetic"

[x]
provider = "synthetic"

[ai]
provider = "null"
```

This lets you test with real bars but still runs offline.

### For Live Research (Small Budget)

Use **Stooq + Reddit + rule-based AI**:

```toml
[market_data]
provider = "stooq"
fallbacks = ["csv", "synthetic"]

[earnings]
provider = "synthetic"

[reddit]
enabled = true
provider = "reddit"

[x]
enabled = false

[ai]
provider = "null"
```

This costs nothing and provides real data + social sentiment.

### For Comprehensive Research

Enable all live sources:

```toml
[market_data]
provider = "stooq"
fallbacks = ["csv"]

[earnings]
provider = "csv"
csv_path = "~/my_data/earnings.csv"

[reddit]
enabled = true
provider = "reddit"

[x]
enabled = true
provider = "x"

[ai]
provider = "anthropic"
```

Cost estimate: Reddit API is free; X API is ~$200–500/month (depending on tier); Anthropic is ~$0.01–0.05 per signal (depends on post volume).

---

## Troubleshooting

### "credential not found"

```
credential 'anthropic_api_key' not found. Set the environment variable CLAUDETRADE_SECRET_ANTHROPIC_API_KEY, or store it with: claudetrade secrets set anthropic_api_key
```

**Solution**:

```bash
export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
# or
claudetrade secrets set anthropic_api_key
```

### "rate limit exceeded"

The provider has hit its configured rate limit.

**Solution**:

- Reduce `max_symbols_per_request` in config
- Increase `rate_limit_per_minute` if you have higher tier credentials
- Wait and retry (rate limits are per-minute and reset hourly)

### "data source unavailable"

A provider failed; the system is using fallbacks.

**Solution**:

- Check internet connectivity
- Verify credentials are correct (test with the provider's own CLI)
- Check logs: `tail -f ~/.claudetrade/logs/claudetrade.log`
- Verify the provider is online (e.g., is Stooq's website up?)

### "stale data" warning

Market data is older than `market_data.stale_after_hours`.

**Solution**:

- Refresh data manually: `python -m claudetrade.pipeline refresh`
- Check if the provider is delayed
- Increase `stale_after_hours` if you're okay with older data

---

## Data Retention and Privacy

- **Social media**: Posts are sanitised; usernames are hashed. Raw text is not logged.
- **Sentiment**: Aggregated; individual posts are not visible in output.
- **Audit log**: Credential access is logged but not the value.
- **Database**: Market and earnings data is retained indefinitely for backtesting.

See [docs/security-and-privacy.md](security-and-privacy.md) for full privacy considerations.
