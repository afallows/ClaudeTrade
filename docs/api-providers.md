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

**Symbol mapping**: Stooq namespaces every symbol by market. A bare ticker is
mapped automatically -- US listings get a `.us` suffix (`AAPL` -> `aapl.us`);
Canadian listings (`exchange = "TSX"` or `"TSXV"`) get a `.to` suffix (`SHOP`
-> `shop.to`). The exchange is looked up from the packaged seed universe (see
[Universe Selection](#universe-selection) below) when it isn't supplied
explicitly. A symbol that already carries its own suffix (e.g. `BMW.DE`) is
passed through untouched.

**What to expect from Canadian (TSX/TSXV) coverage**: it is real but partial
and this repository cannot verify it from a sandboxed environment with no
network access -- coverage was not (and could not be) confirmed against the
live endpoint while writing this. Stooq mirrors many, but not all, TSX-listed
names. Before relying on a specific Canadian symbol, run:

```bash
claudetrade probe
claudetrade refresh --symbols SHOP,RY --start 2024-01-01
claudetrade status
```

and check `claudetrade status` for `data_quality` warnings/errors against
those symbols (an empty bar series for a requested symbol shows up there, not
as a crash -- see "Unknown or uncovered symbols" below).

**Unknown or uncovered symbols**: a symbol stooq has no data for (unknown
ticker, or its free-tier daily quota exhausted) degrades that one symbol to an
empty bar series and the rest of the requested batch continues -- it does not
fail the whole refresh. The gap shows up as a `data_quality` finding
(`api_data_gap`, and per-symbol `missing_bars`) rather than an exception.

**Limitations**:

- Free data; no commercial redistribution rights
- Rate limited (public API, shared across users)
- Stale data possible if the service is under load
- Delisting information may be incomplete
- No bulk universe/reference-data endpoint in the free tier -- `list_universe`
  serves the packaged seed universe described below, not a live listing from
  stooq itself

**Licence**: Stooq data is free for personal research only. Commercial use requires a paid licence.

---

## Universe Selection

**Module**: `src/claudetrade/data/universe.py`, seed data under
`src/claudetrade/data/universes/*.csv`

By default the scannable universe is built from two packaged CSV files shipped
inside the application:

| File | Coverage | Rows |
| --- | --- | --- |
| `us_default.csv` | Roughly the S&P 500 plus some liquid mid-caps, US exchanges (NASDAQ/NYSE/AMEX) | ~500 |
| `ca_default.csv` | TSX 60 plus other liquid TSX names | ~110 |

**These are hand-curated seed lists, not a live index feed.** Index
constituents drift constantly -- additions, removals, mergers, renames -- and
these files will go stale over time; each carries a generation-date comment at
the top. Edit them freely (add, remove, or correct rows) if you want a
different starting universe; the column format matches the CSV universe source
below (`symbol,name,exchange,sector,market_cap_bucket,country`).
`market_cap_bucket` (`mega`/`large`/`mid`) is an approximate size label, not a
live market capitalisation figure -- do not treat it as current.

**When they apply**: with `universe.source = "database"` (the default), the
packaged universes are used to seed the scannable universe only while the
database has no stored securities yet -- i.e. before the first
`claudetrade refresh` completes. Once securities are stored, those take
precedence and are merged with any packaged symbol not yet stored, so newly
added packaged names remain visible even after a refresh. This is also what
`StooqMarketProvider.list_universe()` returns, since stooq's free tier has no
bulk reference-data endpoint of its own -- it is why a fresh install pointed at
`market_data.provider = "stooq"` has hundreds of US and Canadian symbols to
pull on the very first `claudetrade refresh` instead of an empty universe.

**Configuration**:

```toml
[universe]
source = "database"                          # default
packaged_universes = ["us_default", "ca_default"]  # set to [] to disable the fallback
permitted_exchanges = ["NYSE", "NASDAQ", "AMEX", "TSX", "TSXV"]  # TSX/TSXV on by default
```

**Known cross-market symbol collisions**: a handful of well-known tickers are
used by *different* companies on the US and Canadian markets (e.g. `T` is
AT&T on NYSE and Telus on TSX; `K` is Kellanova on NYSE and Kinross Gold on
TSX). Because the universe is keyed by bare symbol, the packaged CSVs
deliberately omit the Canadian side of each such collision rather than have
one silently overwrite the other -- an accuracy trade-off documented here
rather than hidden.

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

### News RSS/Atom (Default, Live, No Credentials)

**Module**: `src/claudetrade/providers/social/news_rss.py`

Reads a configurable list of RSS/Atom feeds that publishers explicitly serve
for syndication -- this is the source this package adds to broaden sentiment
beyond Reddit's official API, which trial feedback flagged as flaky
(rate-limited, OAuth-gated, occasionally unavailable). It is **not** scraping:
no authentication is bypassed, no paywall is defeated, and no vendor rate
limit or terms-of-service boundary is tested. A feed exists because its
owner published it for exactly this purpose.

Unlike Reddit and X, this source defaults to its live adapter
(`provider = "news_rss"`), not the offline synthetic generator, because there
are no credentials to be missing and no paid tier to gate behind an opt-in --
the same reasoning that makes Stooq market data usable with `enabled = true`
out of the box.

**Default feeds** (chosen because their owners document them as public
syndication feeds -- an exchange/regulator, a central bank, a wire service,
and a public broadcaster):

| Feed | Why it's a lawful default |
| --- | --- |
| `https://www.sec.gov/news/pressreleases.rss` | US securities regulator; official press-release RSS, published for public syndication. |
| `https://www.federalreserve.gov/feeds/press_all.xml` | US central bank; official press-release RSS. |
| `https://www.prnewswire.com/rss/financial-services-latest-news.rss` | Wire service; publishes per-category RSS specifically for syndication -- this is its own advertised distribution channel, not a side effect of its website. |
| `https://feeds.npr.org/1006/rss.xml` | Public broadcaster; publishes per-section RSS (this one is the Business section) via a documented feeds directory. |

**Honest limitation**: this package was built and tested against recorded
fixture XML (`tests/fixtures/news_rss/`) in an egress-blocked environment --
none of the above URLs were reached at runtime while writing it. Operators
should confirm each feed still resolves and still serves the expected
format (`claudetrade probe`, or simply fetching the URL) before relying on it,
and are free to edit `feed_urls` to any other feed their organisation is
comfortable treating as a syndication channel.

**Configuration**:

```toml
[news]
enabled = true
provider = "news_rss"
feed_urls = [
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]
rate_limit_per_minute = 30
request_timeout_s = 20.0
lookback_hours = 72
```

**Credentials**: None required.

**Ticker relevance**: headlines rarely carry cashtags (`$AAPL`), so this
adapter does not attempt ticker resolution itself -- it hands sanitised text
to the same `sentiment.entity_resolution.TickerResolver` every other source
runs through. The resolver's company-name/alias path is what makes a headline
like "Shopify beats earnings expectations" resolve to `SHOP` (a directory
entry with `name = "Shopify"` is enough); the bare-symbol path is what would
be needed for a headline that used the ticker itself instead of the company
name, and is scored much more cautiously (see
`sentiment/entity_resolution.py`'s module docstring). Company/product names
that do not match a directory entry's `name`/`aliases` will simply not
resolve -- this is the resolver's existing, documented limitation, not
something added by this provider.

**Deduplication**: wire stories are routinely syndicated verbatim across
multiple of the configured feeds with different `guid`/`link` values. Items
are deduplicated first by `external_id` (per item, within a single fetch)
and then by the sanitised text's `text_hash` (across feeds) -- the first copy
seen is kept, its `duplicate_group` is set to that hash, and later copies are
dropped rather than passed on to double-count the same story in sentiment
aggregation.

**A note on credibility weighting**: `SocialPost` engagement fields
(`score`/`num_comments`/`num_reposts`/`num_replies`) and author metrics
(`author_age_days`/`author_karma`/`author_followers`) are structurally absent
for a wire story -- there is no vote count or account history to report, so
all of these are `0`/`None`. `sentiment.aggregation._credibility_score` scores
absent metrics as their zero-value floor (age/karma/followers component all
`0.0`), and `engagement_weighted` scales by `log1p(engagement)` which is also
`0` for a post with no engagement counts at all. The practical effect: a news
post contributes normally to `raw_sentiment`, `unique_author_sentiment`,
`sentiment_acceleration` and the label averages (all of which use only the
time-decay weight), but contributes **zero** weight to `engagement_weighted`
and `credibility_weighted` -- an authoritative wire story is treated
identically to a brand-new, karma-less throwaway account for those two
measures specifically, not because it is *distrusted* but because "no
metrics reported" and "worst possible metrics" are not currently
distinguished by that scoring function. This is an existing characteristic of
`sentiment/aggregation.py` (out of scope for this provider package to
change) rather than a defect introduced here; it is worth knowing if
`engagement_weighted`/`credibility_weighted` specifically are used to
evaluate how much news content is influencing a symbol's sentiment.

**Limitations**:

- No engagement signal exists for a wire story (see credibility note above)
- No author to pseudonymise; the "author" hash is a salted digest of the
  feed's own domain, not a person
- Feeds serve only recent items (typically the last few dozen stories per
  feed); this is not a historical archive
- No guaranteed uptime or format stability -- a publisher can change its feed
  format or retire it at any time, same as any other free, unauthenticated
  endpoint

**Licence**: Feeds are read only from URLs their owners publish for
syndication. Content is standard news-article text; no redistribution beyond
this application's own sentiment pipeline is performed. Operators remain
responsible for confirming any additional feed they add is genuinely
published for syndication by its owner.

### Hosted Sentiment Aggregator (Stub, Not Implemented)

**Module**: `src/claudetrade/providers/social/hosted_api.py`

`HostedSentimentProvider` is a documented adapter **seam**, not a working
integration -- its constructor always raises `NotConfiguredError`, even when
fully configured. It exists so a future paid aggregator (broader outlet
coverage, deeper history) has an obvious place to be wired in, without a fake
implementation standing in for it in the meantime. See the module's
docstring for what a real implementation must provide: stated historical
depth (ADR-0007's rejection of `openbb-adanos`'s 90-day cap is the
cautionary example), a clear statement of whether the vendor returns
per-post data (which must go through the same sanitisation/hashing pipeline
as every other adapter) or pre-aggregated scores (which must not be forced
into fabricated per-post rows), and the vendor's licensing terms per
ADR-0007 Decision 5.

**Configuration** (present on `NewsConfig` but inert until a real
implementation replaces the stub):

```toml
[news]
hosted_base_url = "https://example-vendor.invalid/api"
hosted_credential = "hosted_sentiment_api_key"
hosted_enabled = true
```

Setting all three still raises `NotConfiguredError` today -- that is the
point of a stub.

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

- Refresh data manually: `claudetrade refresh`
- Check if the provider is delayed
- Increase `stale_after_hours` if you're okay with older data

---

## Data Retention and Privacy

- **Social media**: Posts are sanitised; usernames are hashed. Raw text is not logged.
- **News**: Item text is sanitised the same way; there is no username to hash, so the author hash is a salted digest of the feed's own domain (publisher-level, not personal data).
- **Sentiment**: Aggregated; individual posts are not visible in output.
- **Audit log**: Credential access is logged but not the value.
- **Database**: Market and earnings data is retained indefinitely for backtesting.

See [docs/security-and-privacy.md](security-and-privacy.md) for full privacy considerations.
