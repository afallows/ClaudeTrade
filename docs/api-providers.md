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
anthropic_api_key_credential = "anthropic_api_key"
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

### Polygon.io (RECOMMENDED primary for bars, Online, free tier)

**Module**: `src/claudetrade/providers/market/polygon.py`

**The entire US equity market's OHLCV in ONE request per trading date.**

```
GET https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}?adjusted=true&apiKey=KEY
```

This inverts the cost model of every other bars source here. TipRanks and
Yahoo pay one HTTP call *per symbol*, so a ~2,400-symbol universe refresh is
thousands of calls -- which is why the owner's refresh took 5-10 minutes to
reach ~33% with many symbol failures, and why the database only ever gained
one session of history per day. Polygon pays one call *per date* regardless
of universe size: a daily refresh is one grouped call, and a two-year
historical backfill is about 500.

That history is the point. The strategies hard-veto a symbol with fewer than
60 price bars (`strategies/base.py`, `min_history_bars`) and the context
builder needs 30+, so with only a few sessions stored a scan rejects the
whole universe with `insufficient_history` -- 2,355 symbols evaluated,
11,775 rejections, 100% of them for that one reason. At one session per day
the scanner stays dead for weeks. `claudetrade db backfill` fixes that in
one pass.

**Recommended configuration** (once you have a key):

```toml
[market_data]
provider = "polygon"                          # bars: one grouped call per date
fallbacks = ["tipranks", "yahoo", "csv"]      # refdata/caps/earnings + per-symbol bar gaps

[polygon]
rate_limit_per_minute = 5                     # free tier; raise to match a paid plan
```

The default shipped config keeps `provider = "tipranks"` so a zero-key
install works out of the box. Switching the primary to `polygon` is safe
*before* you have a key: with no key resolvable the adapter reports itself
unconfigured and every bars call degrades straight to the fallbacks.

**Credentials** -- create a free key at polygon.io, then any one of:

```bash
# 1. The plain environment variable every Polygon client library documents
export POLYGON_API_KEY="..."

# 2. The OS credential store (recommended for a desktop install)
claudetrade secrets set polygon_api_key
# or: export CLAUDETRADE_SECRET_POLYGON_API_KEY="..."

# 3. config.toml -- supported, but discouraged (that file is meant to be shareable)
#    [polygon]
#    api_key = "..."
```

They are checked in that order. A value in `config.toml` is redacted from
`AppConfig.public_dict()`, so it never reaches the config hash, the logs, or
persisted run metadata.

**One-time historical backfill** (the F23 fix):

```bash
claudetrade db backfill --years 2
claudetrade scan
```

Walks trading dates **newest first**, so the scanner becomes useful as soon
as the most recent ~60 sessions land, long before the full range finishes.
Roughly 252 calls per year (~500 for two years); at the free tier's ~5
calls/minute that is about 1.7 hours for two years, and the progress line
prints an ETA that accounts for the pacing. Safe to Ctrl-C: every date
commits in its own short transaction, and a re-run skips dates that already
have stored bars, so it resumes where it left off. `--force` re-fetches
covered dates and replaces those rows in place (scoped to symbols Polygon
actually returned, so TSX bars sourced from the cascade are never
destroyed). Bars are stored with source tag `polygon_grouped`.

**Free tier**: ~5 requests/minute, end-of-day (delayed) data. Both are fine
for a session-based swing scanner that refreshes after the close. The
adapter paces itself with the shared `RateLimiter` and honours a 429's
`Retry-After` rather than looping.

**Per-date response cache** (`<cache_dir>/polygon/`, one JSON file per date):
a cache hit costs zero HTTP calls. Historical grouped responses are
immutable, so there is no TTL -- with two exceptions that keep recent dates
honest: an empty response is never cached (for today it just means EOD data
has not landed yet), and the current session is only cached once it has
closed and settled (an intraday grouped row is a partial-day aggregate).
This is what makes a chunked refresh cheap (the first chunk fetches each
date, later chunks hit the cache) and a backfill re-run free.

**Refresh window narrowing**: because this provider declares `bulk_daily`,
`data/ingest.py` narrows a refresh's fetch window to the sessions the
database is actually missing (re-fetching the latest stored session, so a
provisional current-session bar gets repaired). The full window is kept when
no bulk provider is primary, when the bulk primary is unconfigured, or when
the database has no bars at all.

**Bars source only.** Reference data, market caps and earnings still come
from TipRanks through the cascade: `get_security_info` deliberately returns
nameless stubs (which `FallbackMarketProvider` treats as unfilled),
`get_market_caps` is not supported, and `get_corporate_actions` returns an
honest empty result. `list_universe` serves the packaged seed universes,
same as stooq/yahoo.

**Symbol mapping**: US share classes use Polygon's dot notation
(`BRK-B` -> `BRK.B`), via the same deliberately narrow single-trailing-letter
rule TipRanks uses; nothing else is rewritten. Canadian (TSX/TSXV) listings
are simply not in a `locale=us` grouped response -- they come back with no
bars and the cascade fills them from TipRanks/Yahoo, per symbol.

**Limitations**:

- US equities only (`locale/us/market/stocks`). TSX names never appear.
- `adjusted=true` returns a split-adjusted OHLC series; there is no separate
  raw + dividend-adjusted pair on this endpoint, so `Bar.adj_close` is left
  `None` and `effective_adj_close` falls back to the close.
- Free tier is end-of-day delayed -- today's bar lands after the close.
- No intraday bars, market caps, corporate actions, or live reference data
  in this adapter.
- This sandbox cannot reach `api.polygon.io` to verify any of the above live
  (egress policy answers 403); every behaviour is exercised against
  shape-faithful fixtures transcribed from Polygon's published response
  schema over a mocked transport (`tests/test_polygon_provider.py`,
  `tests/fixtures/polygon/`), never fabricated data.

**Licence**: An official, published, contracted REST API -- unlike the
unauthenticated TipRanks/Yahoo endpoints. Free tier is for personal and
research use per polygon.io's own terms of service; check those terms before
any commercial or redistributive use.

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

### Stooq (Online, opt-in only -- see the anti-bot caveat below)

**Module**: `src/claudetrade/providers/market/stooq.py`

Free historical data from Stooq.

**NOT a default fallback.** Two real-world findings, both from the owner's
own machine, moved stooq out of `market_data.fallbacks`' default value:

1. A first production refresh logged `stooq returned 404 for AAPL,MSFT,...`.
   Diagnosis: the symbol/suffix mapping (lower-case + `.us`/`.to`) was
   already correct on the real fetch path; what was actually missing was a
   `User-Agent` header -- the client sent none at all, so httpx's own
   generic default went out on the wire, and stooq's edge answered that
   default with a 404. Fixed by sending a browser-like `User-Agent`.
2. A follow-up live probe (both a US and a TSX symbol, `User-Agent` fix
   already applied) found stooq now answers with **HTTP 200 and an HTML,
   JavaScript SHA-256 proof-of-work challenge page** (posts to `/__verify`,
   then reloads) instead of the CSV body -- an anti-bot wall, not a request
   defect. Per ADR-0008 Decision 1 this application never solves a
   challenge: the adapter detects the HTML shape (content-type and/or a
   leading `<!doctype`/`<html` body marker -- **the status code alone
   is 200 and cannot distinguish this from a real response**) and raises
   `SourceBlockedError` before the body ever reaches the CSV parser.

Because that wall's presence appears to depend on the requesting network's
reputation with stooq (and could change at any time in either direction),
`stooq` remains fully registered and usable -- just not as an unattended
default. Add it back explicitly if your own network path to stooq.com is not
challenged:

```toml
[market_data]
provider = "tipranks"
fallbacks = ["yahoo", "stooq", "csv"]   # add stooq back explicitly if desired
rate_limit_per_minute = 60
request_timeout_s = 20.0
```

**Credentials**: None required (no authentication).

**Symbol mapping**: Stooq namespaces every symbol by market. A bare ticker is
mapped automatically -- US listings get a `.us` suffix (`AAPL` -> `aapl.us`);
Canadian listings (`exchange = "TSX"` or `"TSXV"`) get a `.to` suffix (`SHOP`
-> `shop.to`). The exchange is looked up from the packaged seed universe (see
[Universe Selection](#universe-selection) below) when it isn't supplied
explicitly. A symbol that already carries its own suffix (e.g. `BMW.DE`) is
passed through untouched. (Stooq's suffix mapping still recognises `TSXV`
as a technical capability of `stooq_symbol()`; the application's own seeds
and default `permitted_exchanges` no longer include TSX Venture at all --
see the exchange-scope note under [Universe Selection](#universe-selection).)

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
- **May be behind an anti-bot browser challenge** (see above) depending on
  the requesting network's reputation with stooq's edge -- this adapter
  detects and fails closed rather than working around it; there is no
  code-level fix for this the application can apply
- Stale data possible if the service is under load
- Delisting information may be incomplete
- No bulk universe/reference-data endpoint in the free tier -- `list_universe`
  serves the packaged seed universe described below, not a live listing from
  stooq itself

**Licence**: Stooq data is free for personal research only. Commercial use requires a paid licence.

### Yahoo Finance (Bars fallback, Online -- no market-cap capability)

**Module**: `src/claudetrade/providers/market/yahoo.py`

Daily OHLCV from Yahoo Finance's public (undocumented) `v8/finance/chart`
JSON endpoint -- the same one the Yahoo Finance website itself calls. This
adapter's own production refresh log showed every `v7/finance/quote` batch
call failing with `yahoo returned 401 for AAPL,MSFT,...` (that endpoint now
requires cookie+crumb authentication), while the *same run* filled roughly
a hundred symbols' bars from "fallback yahoo" via the chart endpoint. The
fix: **the quote/quoteSummary API has been removed from this adapter
outright** -- chart is the only thing it calls now, and `get_market_caps` is
no longer overridden (see below).

**Configuration**:

```toml
[market_data]
provider = "tipranks"        # real market caps/refdata/earnings
fallbacks = ["yahoo", "csv"] # yahoo is the bars fallback
```

**Credentials**: None required (no authentication).

**No market-cap capability any more**: `get_market_caps` is not overridden
on this class -- it inherits `MarketDataProvider`'s protocol default (an
empty mapping, "not supported"), the same as synthetic/csv. Real market caps
now come from `providers.market.tipranks.TipRanksProvider` (see
[TipRanks](#tipranks-primary-online) below and
[Runtime Market-Cap Filter](#runtime-market-cap-filter-adr-0008-decision-3)).

**Symbol mapping**: a bare US ticker is passed through unchanged; a Canadian
(TSX/TSXV) one gets a `.TO` suffix, resolved from the packaged seed
universe's exchange column when not supplied explicitly. Share classes
already use this codebase's hyphen notation (`BRK-B`), which happens to be
Yahoo's own convention too -- **confirmed by a live probe from the owner's
machine**: a TSX share-class symbol (`TECK-B`) is requested, and correctly
served, as `TECK-B.TO` (dash preserved, `.TO` appended), distinct from
TipRanks' own `TSE:TECK.B` (dotted) notation for the same security. Two
different vendors, two different conventions -- each mapping table lives in
its own adapter module (`yahoo.yahoo_symbol`, `stooq.stooq_symbol`,
`tipranks.tipranks_ticker`) rather than being unified into one, since a
"universal" ticker format that has to special-case three vendors' quirks is
more error-prone than three small, independently testable ones.

**Known integration gap (resolved)**: earlier drafts of this module noted it
was not yet registered in `providers.registry._MARKET_PROVIDERS` -- it now
is (`"yahoo": YahooMarketProvider`), and is one of the two default
`market_data.fallbacks` entries.

**Limitations**:

- **Undocumented, unofficial endpoint.** Not a published/contracted API; no
  SLA; the response shape can change without notice.
- Free; no redistribution rights.
- Rate limited by this adapter conservatively (default 30 calls/minute) since
  there is no published limit to respect.
- No bulk universe/reference-data endpoint -- `list_universe` serves the
  packaged seed universes, same as stooq.
- No corporate-actions coverage in this adapter.
- No reference-data (`get_security_info`) beyond the packaged seed universe
  -- there is no quote endpoint left to call for a live company name/sector.
- This sandbox cannot reach `query1.finance.yahoo.com` to verify any of the
  above live (egress policy answers 403); every behaviour above is exercised
  against transcribed real response shapes over a mocked transport instead
  (see `tests/test_yahoo_provider.py`), never fabricated data.

**Licence**: Personal/research use only, same posture as stooq's free tier;
unsuitable for commercial use.

### TipRanks (Primary, Online)

**Module**: `src/claudetrade/providers/market/tipranks.py`

**Primary source** for market caps, reference data and earnings (and a
last-resort-only bars capability): `TipRanksProvider` reads
`https://widgets.tipranks.com/api/etoro/dataForTicker?ticker={SYMBOL}` -- an
unauthenticated, keyless JSON endpoint (the same one TipRanks' own eToro
integration widget calls) that returns one rich `overview` object per
symbol. One HTTP call per symbol serves all four capabilities, because they
are all different views onto that same object.

Market caps and a current-session bar are now sourced primarily from a
second, genuinely *batched* endpoint, GetQuotes -- one HTTP call serves an
entire chunk of tickers, not one per symbol -- with the per-symbol
`dataForTicker` path above as the fallback for whatever it does not cover.
See "GetQuotes batching" below; this is the change that cuts a
~2,400-symbol universe refresh from roughly one `dataForTicker` call per
symbol down to a handful of batch calls plus a much smaller fallback tail.

**Configuration**:

```toml
[market_data]
provider = "tipranks"          # the default
fallbacks = ["yahoo", "csv"]
max_workers = 8                 # per-symbol fetch parallelism, shared across TipRanks + Yahoo
yahoo_rate_limit_per_minute = 120

[earnings]
provider = "tipranks"          # also the default

[tipranks]
rate_limit_per_minute = 60      # raised from the original 30 -- see below
request_timeout_s = 20.0
cache_ttl_trading_days = 1      # response cache under paths.cache_dir/tipranks/
unknown_ticker_ttl_days = 30    # negative/limited-result cache TTL, see below
use_getquotes_batch = true      # primary batched cap + current-bar path, see below
getquotes_batch_size = 200      # symbols per GetQuotes call
```

**Credentials**: None required (no authentication).

**ToS posture (ADR-0008 Decision 1) -- read before relying on this in
production**: this is an unauthenticated partner-widget endpoint, **not** a
published, contracted API. TipRanks could restrict, reshape, rate-limit or
withdraw it at any time with no notice and no deprecation window. This is
the same posture this codebase already applies to stooq's free CSV endpoint,
Yahoo's undocumented chart JSON, and Stocktwits' keyless stream: personal/
research use only, a conservative self-imposed rate limit, and a
**fail-closed** response to anything that looks like a block or an
unexpected shape -- see the fail-closed rules below. Nothing here bypasses
authentication, defeats a paywall, or solves a challenge.

`rate_limit_per_minute` was raised from the original launch default of
30/minute to **60/minute** after the owner confirmed their own brokerage app
calls this same public eToro widget endpoint at that kind of cadence with no
observed pushback -- still a fraction of an idly-refreshing browser tab, and
fully self-imposed and operator-configurable, not a vendor-published
ceiling. This was the single biggest driver of a real first refresh across
~2,400 symbols taking 80+ minutes (roughly `symbol_count / rate_limit`).
`market_data.max_workers` (default 8) additionally lets the per-symbol fetch
loop (`TipRanksProvider`/`YahooMarketProvider`) overlap request *latency*
across symbols on a shared, thread-safe `RateLimiter` -- it does not itself
raise the enforced calls/minute ceiling, only how much of the wall clock is
spent idly waiting on one request at a time. `yahoo_rate_limit_per_minute`
(default 120) is Yahoo's own separate bucket -- the bars cascade tries it
before TipRanks (see `bars_last_resort` below), so it was the other major
contributor to that 80-minute figure.

**Symbol notation**: a bare US ticker is passed through unchanged (`AAPL`),
**except** a dash-suffixed single-letter share class, which TipRanks also
expects in dot notation: `BRK-B` -> `BRK.B`, `BF-B` -> `BF.B` -- confirmed
by a real refresh log showing plain 404s for both under this codebase's own
dash convention (`tipranks_ticker`'s `_US_CLASS_SHARE_RE` matches only a
genuine single-letter suffix, so e.g. `LILAP` is never mistaken for one).
**Yahoo, by contrast, wants the dash form for these same symbols** (see
`YahooMarketProvider.yahoo_symbol`) -- this mapping is local to the TipRanks
adapter only. A Canadian (TSX/TSXV) listing is rewritten to
`TSE:<SYMBOL-WITH-DOTS>` -- this codebase's hyphenated share-class
convention (`TECK-B`) becomes TipRanks' dotted one (`TSE:TECK.B`) --
confirmed against a real Canadian-listing fixture
(`tests/fixtures/tipranks/dataForTicker_TECK_B.json`), whose
`overview.ticker` echoes exactly that form back. This is a *different*
convention from both stooq's (`teck-b.to`) and Yahoo's (`TECK-B.TO`) --
each adapter owns its own mapping table.

**Unknown-ticker caching (`unknown_ticker_ttl_days`, default 30)**: a
confirmed "TipRanks has no data for this symbol" result -- a clean HTTP 404
from BOTH `dataForTicker` and the `historicalprices` fallback probe below --
is cached under this much longer TTL instead of the ordinary
`cache_ttl_trading_days`. Without this, a genuinely delisted/renamed name (a
real refresh log showed ANSS, JNPR, FLT, SQ, K, WBA, HES, DFS, PARA and
others) gets re-probed on every single refresh forever. A name that later
regains coverage is picked up within that 30-trading-day window at worst --
an accepted trade-off, not an oversight.

**Closed-end funds and other "prices_only" symbols**: some real, still-listed
securities (closed-end funds like MHD, GAB, BDJ, BKT are the common case) have
no analyst/overview coverage at all -- `dataForTicker` 404s for them -- but
DO have real price history at
`https://widgets.tipranks.com/api/etoro/historicalprices?ticker={SYMBOL}`
(verbatim owner-captured fixture committed at
`tests/fixtures/tipranks/historicalprices_MHD.json`). When `dataForTicker`
404s, this adapter tries that endpoint before caching the symbol as fully
unknown; a non-empty result is cached as a distinct `"prices_only"` state
(same long TTL as unknown). For such a symbol: market cap stays unresolved
(`unknown_cap_policy` applies as for any unresolved cap) and earnings are
unavailable, but bars are served from `historicalprices` -- real OHLCV,
**preferred over close-only synthesis** -- at the same last-resort cascade
position as the close-only bars below. Parsing rules: `volume == 0` rows are
Jan-1 holiday padding and are dropped; `price` (the dividend/split-adjusted
close) maps to `adj_close`, `close` maps to `close`. **Cadence guard**: the
real cadence observed in the fixture is biweekly (~14 calendar days), not
daily -- if the requested date range's rows have a median gap over 4
calendar days, the series is flagged as downsampled (a `sparse_bars`
data-quality WARNING) rather than served silently as if it were ordinary
daily data; the rows are still returned as-is (no interpolation), and the
existing `MIN_CONTEXT_BARS` / exact-session-match guards in
`data.context.ContextBuilder` mean a series this sparse naturally fails to
produce a usable context for most sessions regardless.

**Earnings (the headline capability)**: `get_upcoming_earnings` /
`get_historical_earnings` map `overview.portfolioHoldingData.
nextEarningsReport` / `.lastReportedEps` onto `EarningsEvent`. Only the
single next/last report is available (not a multi-quarter calendar) -- an
honest, narrower capability than the synthetic generator's full quarterly
series or a hand-maintained CSV. Two mapping details worth knowing:

- **The inner `ticker` field inside those blocks is not the requested
  symbol.** Confirmed from the Canadian fixture: requesting `TSE:TECK.B`
  returns `lastReportedEps.ticker == "TECK"` (the *US cross-listing*
  ticker). Every `EarningsEvent` this adapter returns is keyed by the symbol
  the caller actually asked for, never by this field.
- **`timeOfDay` is PROVISIONAL.** Two values are stated with confidence
  (`1` = before market open, `4` = after market close); a third (`2`,
  observed on a *confirmed* historical report in the Canadian fixture) is
  mapped to `EarningsSession.DURING` by elimination -- an educated guess,
  not a confirmed vendor documentation fact. Any other value maps to
  `EarningsSession.UNKNOWN` rather than failing the parse. Revisit
  `providers.market.tipranks._TIME_OF_DAY_MAP` if TipRanks' own
  documentation, or further real captures, clarify this.

**Market caps -- GetQuotes first, `dataForTicker` fallback (the primary
speed win of this change)**: `get_market_caps` tries the batched GetQuotes
endpoint (below) for the WHOLE requested symbol list first -- turning what
used to be one `dataForTicker` call per symbol into roughly a dozen batch
calls for a full ~2,400-symbol universe refresh -- then falls back to the
per-symbol `dataForTicker` path (`overview.marketCapUSD`, else
`overview.marketCap` as-is with **no currency gating** -- the market-cap
universe floor is currency-agnostic by explicit owner decision, a nominal
figure in either USD or CAD clears it) for anything GetQuotes did not
resolve. Nested blocks that also happen to carry a `marketCap` field (e.g.
`portfolioHoldingData.nextDividendDate.marketCap` in the Canadian fixture, a
*different*, CAD-only figure from the top-level cap) are never used as a cap
source. `tipranks.use_getquotes_batch` defaults to `true` now -- see
"GetQuotes batching" below for the confirmed envelope shape, the USD
normalisation rule, and the current-session-bar capability built on the same
endpoint.

**Reference data**: `get_security_info` maps `overview.companyName` / market
/ `companyData.sector` / `.industry` / cap. `overview.market` is
inconsistently cased across listings (`"NASDAQ"` for a US name, `"tsx"`
lower-case for a Canadian one, confirmed against both fixtures) -- matching
is always case-insensitive.

**Daily bars -- close-only, LAST RESORT ONLY.** `overview.prices` is a list
of `{"date", "d", "p"}` -- a closing print per session, nothing else.
Synthesising fake open/high/low/volume would silently corrupt every
downstream ATR/gap/volume feature, so this adapter never does that:
`get_daily_bars` emits `Bar(open=high=low=close=p, volume=0)` and logs (and
queues, via `drain_quality_warnings()`) a `close_only_bars` data-quality
WARNING per symbol so the degrade is visible, never silent. This capability
is reached only when both `yahoo` and (if configured) `stooq` have nothing
for a symbol: `TipRanksProvider.bars_last_resort = True` is a plain
attribute `providers.registry.FallbackMarketProvider.get_daily_bars` checks
to defer this provider to the very end of the bars cascade *for bars only*,
even though it is the configured primary provider for every other
capability (market caps, reference data, earnings still try it first, as
written in `market_data.provider`/`fallbacks`).

**Fail-closed rules (ADR-0008 Decision 1)**, confirmed against a real probe:

| Response | Meaning | Behaviour |
| --- | --- | --- |
| HTTP 404 | Confirmed: unknown/garbage ticker | Degrades that one symbol only, cached as "no data" |
| HTTP 401/403 | Blocked | `SourceBlockedError` |
| HTTP 429 | Rate limited | `RateLimitError` |
| HTTP 5xx | Outage | `ProviderError(retryable=True)` -- not treated as a block |
| Non-JSON body / missing `overview` key | Unexpected shape | `SourceBlockedError` |
| `overview` present but empty/null | Unknown ticker | Same per-symbol degrade as 404 |

**Response cache**: every `overview` fetched is cached as one JSON file per
symbol under `paths.cache_dir/tipranks/`, with a 1-trading-day TTL
(`tipranks.cache_ttl_trading_days`, checked via
`utils.timeutils.trading_days_between` so it survives a weekend but
invalidates on the next real trading session) -- this is what keeps a
whole-universe refresh of thousands of symbols to one call per symbol per
trading day, shared across all four capabilities, rather than one call per
symbol per capability per run. An "unknown ticker" result is cached too, so
a universe's always-a-few unresolvable names don't get re-probed every run.

**GetQuotes batching -- CONFIRMED, now the PRIMARY market-cap path (owner
directive, confirmed by live probes)**: TipRanks also exposes a
CIBC-integration batch endpoint,
`https://marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=A,B,C,...`,
that is genuinely batched -- one HTTP call serves every ticker in the
request, unlike `dataForTicker`'s one-call-per-symbol. It returns a
real-time snapshot per ticker (current-session OHLCV plus caps), never
history. The envelope shape is now CONFIRMED from the owner's own probes:

- Top level: `{"quotes": [...], "errors": [...], "metadata": {"count",
  "success", "errors"}}`.
- Each `quotes[]` row: `{ticker, currency, exchangeRate, isomic,
  marketName, price, open, low, high, volume, changeAmount, changePercent,
  lastTradeDate, lastClose, marketCap, realTimeMarketCap, isRealTime,
  isMarketOpen, isPremarket, isAfterMarket, prePostMarket,
  lastCacheUpdate}`.
- Requested tickers are echoed back exactly as sent, including `TSE:`
  notation for Canadian names -- the same `tipranks_ticker` mapping
  `dataForTicker` uses is reused here, comma-joined per chunk.
- A ticker TipRanks has nothing for comes back either in `errors[]` or
  simply absent from `quotes[]` -- both are skipped, never fatal to the
  rest of the batch.

`tipranks.use_getquotes_batch` now defaults to **`true`** (it was an
off-by-default, Canadian-only, UNVERIFIED optimisation before this
confirmation) -- `get_market_caps` chunks the FULL requested symbol list at
`tipranks.getquotes_batch_size` (default 200) and tries GetQuotes before
falling back to `dataForTicker` for whatever it did not cover. This
repository still has no committed fixture of a *raw* captured response body
(the owner's probe output was relayed as a confirmed field list/shape, not
pasted verbatim) -- see each `tests/fixtures/tipranks/getquotes_*.json`
file's own `_fixture_note`. Every field access stays defensive regardless.

**Confirmed currency trap**: GetQuotes has no `marketCapUSD` field at all
(unlike `dataForTicker`'s `overview`) -- `marketCap`/`realTimeMarketCap` are
always in the listing's *local* currency (`TSE:TECK.B`'s GetQuotes
`marketCap` is CAD with a non-1 `exchangeRate`, and `local_cap *
exchangeRate` recovers the same USD figure `dataForTicker.marketCapUSD`
reports directly; a US entry's `exchangeRate` is confirmed to be 1). The
exact rule (`providers.market.tipranks._getquotes_market_cap_usd`): prefer
`realTimeMarketCap` over `marketCap` when both are present and positive;
if `currency` is `"USD"` (or absent) use that raw cap as-is; otherwise
multiply by `exchangeRate` if it is present and positive; a non-USD cap with
no usable `exchangeRate` contributes nothing -- a raw, un-normalised
non-USD cap is never returned by this adapter, from either endpoint. Market
cap coverage never depends on GetQuotes succeeding -- `dataForTicker` (with
the `TSE:SYMBOL` notation) remains the fallback path for every symbol, US
and Canadian alike, and any GetQuotes failure (bad shape, network error, a
whole chunk failing) is caught and logged, falling straight back with no
user-visible effect beyond the wasted batch call(s).

**Current-session bar (`get_current_session_bars`)**: the same GetQuotes
row also yields today's (or the most recently completed session's) OHLCV
bar -- `open`/`high`/`low` as reported, `price` as the close, `volume` as
reported, session date from `lastTradeDate`. This is a capability distinct
from `get_daily_bars` (still close-only/last-resort, unaffected by any of
this) and is not part of the `MarketDataProvider` protocol; `DataIngestor.
ingest_prices` merges it in explicitly and conservatively (see "Current-bar
merge rule" immediately below) to reduce reliance on Yahoo's historical
chart for "today" specifically, since that endpoint typically lags by one
session until after the close.

**Current-bar merge rule (`DataIngestor._merge_current_session_bars`) --
deliberately CONSERVATIVE**: after the normal `get_daily_bars` cascade
(Yahoo primarily) and the dedicated benchmark fetch both run, this appends a
TipRanks GetQuotes current-session bar for any symbol whose collected series
has no bar for today's session at all. It is append-only: it never replaces
or overwrites an existing bar, even for today's own session, and does NOT
implement "prefer GetQuotes over Yahoo when the market is open or GetQuotes
is newer" -- that richer rule was judged not worth the look-ahead/
double-counting risk (a downstream signal being computed from one value and
then silently recomputed from a different one later in the same run) for
this change. Deduping is by exact session-date equality, so even a symbol
included in the GetQuotes query by an imprecise "today" pre-filter is still
safe: a GetQuotes bar is only ever appended when nothing in the collected
series already carries that exact date. A GetQuotes failure here (bad
shape, network error, provider missing/misconfigured) degrades silently to
"no bar added" -- it never fails the run.

**Why there's no TipRanks daily-history feed (the CIBC finding)**: a
separate probe into the CIBC brokerage integration TipRanks' GetQuotes and
`historicalprices` endpoints originate from confirmed that TipRanks serves
CIBC's *ratings/analyst widgets*, not CIBC's own price-chart data -- there is
no TipRanks endpoint, confirmed or hypothesised, that returns a genuine
daily OHLCV series. `historicalprices` (above) is a fixed ~biweekly cadence
regardless of any request parameter, not a daily source, and GetQuotes is a
single real-time snapshot, not history. Yahoo's chart endpoint remains the
only daily-history source in this codebase; TipRanks' role is real-time
(GetQuotes) and last-resort close-only/`prices_only` backfill, never the
historical backbone.

**Analyst sentiment (`get_analyst_snapshots`, `providers.market
.tipranks_analyst`) -- harvested from the SAME `dataForTicker` response,
ZERO new HTTP calls**: `overview` carries a rich analyst-consensus layer
this adapter used to discard entirely (only refdata/caps/earnings were
read). `TipRanksProvider.get_analyst_snapshots` reads more of the exact
response every other capability on this class already fetches/caches --
never a separate request, never a separate cache entry. Before adding this,
the on-disk cache record was checked end to end and confirmed to already
store the FULL `overview` dict verbatim (`_store_cache_record` never trims
it) -- so no cache-record version bump was needed; every field below was
already surviving a cache round-trip in every cache file this adapter has
ever written.

Fields consumed, and where each comes from:

- `consensus_rating`/`consensus_rate` -- the `overview.consensuses[]` row
  with `isLatest == 1` and `bench == 1` (both committed fixtures carry
  exactly one such row). `consensus_rating` is TipRanks' own opaque 1-5
  scale; this adapter stores it as reported and does **not** assert a
  Strong-Buy-to-Strong-Sell direction, since that direction is not
  independently confirmed from either fixture.
- `buy_count`/`hold_count`/`sell_count`/`analyst_count` -- `overview.
  latestRankedConsensus` (`nB`/`nH`/`nS`), the RANKED-analyst subset --
  CONFIRMED distinct from the broader `consensuses[]` row on the INTC
  fixture (ranked `nH=23` vs. the unranked row's `nH=24`). `analyst_count`
  is the sum of these same three ranked counts, deliberately not `overview.
  numOfAnalysts` (an unrelated, much larger all-time/global TipRanks
  figure), so the four numbers stay internally consistent.
- `price_target_mean`/`high`/`low`/`currency` -- `overview.ptConsensus[]`,
  preferring a `bench == 1` row, falling back to the first row present (both
  fixtures carry one `bench == 0` row, so that fallback is the path
  actually exercised today).
- `consensus_over_time` -- `overview.consensusOverTime[]`, capped at
  `tipranks_analyst.CONSENSUS_OVER_TIME_MAX` (24) most-recent points,
  chronological.
- `recent_rating_actions` -- flattened from `overview.experts[]` AND
  `overview.notRankedExperts[]` (both pools are inspected), filtered to
  `eTypeId == 1` (TipRanks' own professional-analyst type) and capped at
  `tipranks_analyst.RECENT_RATING_ACTIONS_MAX` (30) most-recent actions.
  **Non-analyst expert types are excluded**: `eTypeId == 3` is a Stocktwits
  social-media author (confirmed via the INTC fixture's own
  `notRankedExperts` row, headlined "...-Bearish"), and this is an
  allow-list (only `1` passes), not a deny-list of the one excluded value
  observed -- an unrecognised `eTypeId` is excluded by default.
- `last_eps_surprise_pct`/`next_earnings_estimate_eps` -- the same
  `portfolioHoldingData.lastReportedEps.surprise` /
  `nextEarningsReport.eps` fields `_map_earnings_event` already reads,
  duplicated onto the snapshot so a caller does not have to also query
  `EarningsEventRow`.

**`ratingId`/`actionId` semantics -- confirmed vs. best-effort, stated
honestly**:

- `ratingId`: `1 == "buy"` is CONFIRMED by the INTC fixture's Vivek Arya row,
  whose own headline text reads "Buy Rating Reaffirmed"; `3 == "sell"` is
  CONFIRMED by the excluded `notRankedExperts` Stocktwits row, headlined
  "...-Bearish". `2 == "hold"` follows by elimination (only three rating ids
  are observed across both fixtures, and this is consistent with every
  `nB`/`nH`/`nS` count seen) -- not independently headline-confirmed, but
  not a guess either.
- `actionId`: **NOT documented by TipRanks anywhere reachable from this
  adapter.** Exactly two values are confirmed from headline text: `3` on
  the TECK.B fixture's Brian MacArthur row ("upgraded to Outperform from
  Market Perform") maps to `"upgrade"`; `5`, seen on three rows across both
  fixtures whose headlines describe an unchanged rating ("Buy Rating
  Reaffirmed", a same-firm price-target raise with no rating change), maps
  to `"reiterate"`. `8` appears only on the excluded non-analyst Stocktwits
  row and is left unmapped. **No initiate or downgrade value has been
  observed in either committed fixture** -- any `actionId` this module has
  not confirmed is stored as the raw id with `action_label=None` rather
  than guessed, per ADR-0008 Decision 1's "never fabricate meaning for an
  unconfirmed field" posture.

**Storage**: `db.models.AnalystSnapshotRow` (`analyst_snapshots` table,
migration 011), one row per `(session, symbol)` -- a MUTABLE daily snapshot,
not the immutable signal ledger: a re-refresh within the same session
upserts/replaces the existing row rather than duplicating it, the same
posture `adanos_snapshots` (migration 010) already applies. `data.ingest
.DataIngestor.ingest_analyst_snapshots` wires this into the market pass
immediately after `ingest_earnings`, keyed to the current trading session; a
symbol with no analyst-coverage layer at all contributes no row (an empty
snapshot is never stored), and any fetch/parse failure degrades per symbol
(logged, counted in `IngestReport.analyst_snapshots_upserted`/
`provider_failures`, never aborting the refresh).

**Diffs**: `data.analyst.analyst_delta(current, previous)` is a pure,
read-time comparison between two stored snapshots -- rating-count changes,
a coverage-count delta, a consensus-rating delta, a price-target-mean
delta (absolute and percent), and any rating actions dated after the
previous snapshot's own session. `data.analyst
.latest_and_previous_snapshots` is the batched read every caller (the
Streamlit ticker-detail screen, the `get_analyst_sentiment` MCP tool) goes
through -- two SQL queries total regardless of how many symbols are asked
for, mirroring `signals.research.ResearchLedger
.latest_research_revisions`'s own batched-join discipline (there is a
documented past incident, F26, from a per-symbol read loop in production).

**Not fed to `ComponentScores`/strategy scoring** -- explicitly deferred;
this is a read-only research surface (Streamlit + the MCP tool) only.

**Limitations**:

- **Unauthenticated partner-widget endpoint, not a published/contracted
  API.** No SLA; could be restricted or withdrawn without notice.
- Only the single next/last earnings report per symbol -- not a full
  calendar or surprise history.
- `timeOfDay` mapping is provisional for the value `2` (see above).
- Bars are close-only and last-resort; ATR/gap/volume features are degraded
  (flagged, never silent) whenever this is the only source that returned
  anything for a symbol.
- No currency field on `SecurityInfo` -- a Canadian listing's own currency
  (CAD) is not persisted at the security-reference level, only its already-
  USD-converted market cap is used for the universe floor.
- Relative-strength comparisons against the USD benchmark (SPY) mix
  currencies uncorrected for a TSX listing's own price series -- documented
  here as a known limitation, not silently "fixed" by a conversion this
  adapter cannot verify.
- This sandbox cannot reach `widgets.tipranks.com` or
  `marketsv3.tipranks.com` (egress fully blocked) -- every behaviour above
  is exercised against two real fixtures the owner captured from their own
  machine (`tests/fixtures/tipranks/dataForTicker_INTC.json` and
  `dataForTicker_TECK_B.json`) over a mocked transport, never fabricated
  data (see `tests/test_tipranks_provider.py`).

**Institutional sentiment (`get_institutional_snapshots`, `providers.market
.tipranks_institutional`) -- harvested from the SAME `dataForTicker`
response, ZERO new HTTP calls**: sibling to the analyst-sentiment harvest
immediately above, same posture -- `overview` also carries a separate
insider-transaction and hedge-fund-holdings layer this adapter used to
discard entirely. Both committed fixtures carry REAL insider and hedge-fund
data (unlike some other TipRanks sub-payloads, there is no observed "nulls
path" for this block specifically).

Fields consumed, and where each comes from:

- `insider_monthly` -- `overview.corporateInsiderTransactions[]`, monthly
  aggregates capped at `tipranks_institutional.INSIDER_MONTHLY_MAX` (12),
  chronological. Each row carries both a RAW tally (`trans_buy_amount`/
  `trans_sell_amount`) and TipRanks' own "informative" subset
  (`informative_buy_amount`/`informative_sell_amount` -- open-market
  buys/sells, as opposed to option exercises, gifts, or scheduled 10b5-1
  sales); either can be individually `null` on a given row even though both
  fields are always structurally present.
- `insider_net_3m_usd` -- **this module's own derived figure**, summed over
  the `tipranks_institutional.INSIDER_NET_FLOW_MONTHS` (3) most-recent
  monthly buckets, preferring each side's `informative_*_amount` and
  falling back to the raw `trans_*_amount` only when the informative figure
  is `null` for that side. Deliberately **not** the vendor's own
  `overview.insiderslast3MonthsSum` (kept separately as
  `insider_net_3m_usd_vendor` for display/cross-check) -- the vendor
  total's own informative-vs-raw mixing rule is undocumented, so this
  module computes its own figure rather than trust an opaque one for
  scoring. The two are not expected to match exactly.
- `insider_confidence_stock_score`/`insider_confidence_sector_score` --
  `overview.insidrConfidenceSignal.stockScore`/`.sectorScore` (the vendor's
  own typo, preserved only in the raw field name). **Not vendor-documented**
  as a 0..1 scale; treated as best-effort on the strength of two
  corroborating signals: it is the same number as `overview.
  portfolioHoldingData.insiderSentimentData.stockScore` (co-located with the
  CONFIRMED-0..1 `hedgeFundSentimentData.score` under one parent object),
  and both fixtures show a below-0.5 value (INTC 0.29, TECK.B 0.08)
  alongside a negative vendor 3-month sum (net insider SELLING) -- the same
  direction `hedgeFundData.sentiment` independently confirms. Corroborating,
  not proof.
- `recent_insider_transactions` -- `overview.insiders[]`, the
  `tipranks_institutional.RECENT_INSIDER_TRANSACTIONS_MAX` (5) largest
  transactions by `|estimatedSharesValue|`, for display/audit only (not an
  input to scoring, which reads the monthly aggregates instead). Role flags
  (`isOfficer`/`isDirector`/`isTenPercentOwner`), the vendor's own
  human-readable `insiderOperationDescription` (e.g. `"Buy"`,
  `"Grant/Award/Other Disposal"`), and an SEC EDGAR/SmartInsider `link` are
  carried through for evidence. The numeric `action`/`insiderOperationId`/
  `insiderOperationTypeId` codes are **not** confirmed by either fixture
  (unlike analyst's `ratingId`/`actionId`) and are stored raw, never
  guessed.
- `hedge_fund_sentiment`/`hedge_fund_trend_action`/`hedge_fund_trend_value`
  -- `overview.hedgeFundData.sentiment`/`.trendAction`/`.trendValue`.
  `sentiment` is CONFIRMED 0..1 (cross-checked against
  `portfolioHoldingData.hedgeFundSentimentData.score`, an identical value
  alongside a separate opaque `rating`).
- `hedge_fund_holdings_by_quarter` -- `overview.hedgeFundData
  .holdingsByTime[]`, capped at `tipranks_institutional.
  HEDGE_FUND_HOLDINGS_MAX` (20) quarterly points, chronological. **SEC
  13F-lagged by construction** -- the most recent row is routinely 1-3
  months stale even the day it first appears in the vendor feed.
- `notable_holder_moves` -- `overview.hedgeFundData.institutionalHoldings[]`,
  the `tipranks_institutional.NOTABLE_HOLDER_MOVES_MAX` (5) largest reported
  moves by `|changeAmount|`, with the vendor's own manager `stars` rating
  carried through. The numeric `action` code is not confirmed by either
  fixture and is stored raw.
- `num_of_insiders`/`market_cap_usd` -- `overview.numOfInsiders`/
  `.marketCapUSD` (the same market-cap field the universe floor and the
  analyst-sentiment normalization elsewhere already read), used here for
  scoring normalization only.

**Scoring (`tipranks_institutional.institutional_score(snapshot, as_of) ->
[-1, +1] | None`)** -- a pure function, no I/O, safe to call at read time
against any stored snapshot (the Streamlit block and the MCP tool both
recompute it live rather than trust a possibly-stale stored value). Two
axes, each a weighted blend of two components:

- **Insider axis** (base weight `0.65` of the blend -- weighted ABOVE the
  hedge-fund axis: an insider transaction is filed within days, SEC Form 4
  T+2, and is one individual's real capital commitment): `0.6 *
  log_damped_flow_ratio(insider_net_3m_usd, market_cap_usd) + 0.4 *
  scaled(insidrConfidenceSignal.stockScore)`. The flow ratio puts BOTH the
  net dollar flow and the market cap on a log scale
  (`sign(net) * log1p(|net|) / log1p(cap)`, clamped to [-1, 1]) rather than
  a raw `net / cap` ratio, which would read every mega-cap as permanently
  ~0 regardless of real insider activity (INTC's own vendor-reported 3-month
  sum against its $435B cap is a raw ratio of about `-6e-6`) -- the log
  transform instead compares the two in "orders of magnitude" terms, so a
  meaningful flow relative to a company's OWN size scale still registers.
  Staleness: linear decay from full weight (newest monthly bucket dated
  this session) to zero at `tipranks_institutional.
  INSIDER_STALENESS_FULL_DECAY_DAYS` (90 days, ~1 quarter -- TipRanks'
  insider feed is itself monthly cadence, so a full quarter with no fresh
  bucket means the feed has gone quiet for this symbol).
- **Hedge-fund axis** (base weight `0.35`): `0.6 *
  scaled(hedgeFundData.sentiment) + 0.4 * clamp((latest_quarter
  .netSharesChange / latest_quarter.holdingAmount) * 3.0)`. The vendor's own
  `sentiment` is understood to already synthesize broader holdings history
  than one quarter's flow, so it carries the larger half. Staleness: linear
  decay from full weight (latest quarter dated this session) to zero at
  `tipranks_institutional.HEDGE_FUND_STALENESS_FULL_DECAY_DAYS` (182 days,
  ~2 calendar quarters) -- directly implementing "near zero at 2 quarters
  old"; a single stale quarter (~91 days, roughly unavoidable given SEC 13F
  filing lag) still carries about half weight.
- Both `scaled(...)` mappings are `2x - 1`, projecting the vendor's 0..1
  scale onto -1..+1.
- The two axes are blended by their STALENESS-DISCOUNTED weights
  (`base_weight * staleness_weight`), renormalized over whichever axis
  actually has a value -- an absent (or fully stale) axis redistributes its
  weight to the other rather than pulling the blend toward zero, and BOTH
  absent/fully-stale yields `score=None`, never a fabricated `0.0`.
  `institutional_score` always returns the full breakdown (`insider_
  subscore`/`hedge_fund_subscore`/`*_weight_applied`/`*_age_days`), not just
  the blended number, so a caller can show its work.

**Storage**: `db.models.InstitutionalSnapshotRow` (`institutional_snapshots`
table, migration 012), one row per `(session, symbol)` -- same mutable
daily-snapshot posture as `analyst_snapshots`: a re-refresh within the same
session upserts/replaces the row. The computed score/subscores/weights/ages
are stored alongside the raw fields at INGEST time (`as_of` = that row's own
session), so a stored row is self-contained and diffable even if the
formula's constants change later. `data.ingest.DataIngestor
.ingest_institutional_snapshots` wires this into the market pass immediately
after `ingest_analyst_snapshots`, keyed to the current trading session; a
symbol with no institutional content at all contributes no row, and any
fetch/parse failure degrades per symbol (logged, counted in
`IngestReport.institutional_snapshots_upserted`/`provider_failures`, never
aborting the refresh).

**Diffs**: `data.institutional.institutional_delta(current, previous)` is a
pure, read-time comparison between two stored snapshots -- net-flow change,
hedge-fund-sentiment change, and any notable holder moves/insider
transactions dated after the previous snapshot's own session (a score delta
is computed separately by the caller, recomputing `institutional_score`
against each snapshot's own `as_of_session`, since the raw domain snapshot
carries no `score` field to diff directly). `data.institutional
.latest_and_previous_snapshots` is the same two-query batched read
`data.analyst.latest_and_previous_snapshots` uses, one table over.

**Not fed to `ComponentScores`/strategy scoring** -- explicitly deferred,
same as analyst sentiment; this is a read-only research surface (Streamlit
+ the `get_institutional_sentiment` MCP tool) only.

**Limitations**: same unauthenticated-partner-widget caveat as analyst
sentiment above (no SLA); hedge-fund holdings are SEC-lagged by
construction (routinely 1-3 months stale on arrival); `insidrConfidenceSignal
.stockScore`'s 0..1 scale is corroborated, not vendor-confirmed;
`InsiderTransaction.action`/`HedgeFundHolderMove.action` numeric codes are
stored raw, unmapped, on both fixtures. Exercised only against the same two
committed fixtures the analyst-sentiment tests use (this sandbox cannot
reach TipRanks directly) -- see
`tests/test_tipranks_institutional_parsing.py` and
`tests/test_institutional_score.py`.

**Licence**: Personal/research use only, same posture as stooq/Yahoo's free
tiers; unsuitable for commercial use; fails closed per ADR-0008 Decision 1.

---

## Universe Selection

**Module**: `src/claudetrade/data/universe.py`, seed data under
`src/claudetrade/data/universes/*.csv`

**ADR-0008 Decision 3: the universe is computed, not shipped.** The packaged
CSVs below are bootstrap seeds only. The authoritative universe is the one
computed at refresh time: every US + Canadian listing on a permitted exchange
for which the market-data path can establish a real market cap, filtered to
`>= universe.min_market_cap_usd` (default $500M) -- see
[Runtime Market-Cap Filter](#runtime-market-cap-filter-adr-0008-decision-3)
below. A name missing from the seeds but present in provider data joins the
universe on the next refresh; a seeded name that delists falls out. The
seeds exist only so a fresh install has hundreds of symbols to pull on its
very first `claudetrade refresh` instead of an empty universe.

By default the scannable universe seeds from two packaged CSV files shipped
inside the application:

| File | Coverage | Rows |
| --- | --- | --- |
| `us_default.csv` | NYSE / Nasdaq / NYSE American (AMEX) common stocks, real market cap >= $1B (see the file's own banner for the exact fetch/filter methodology and date) | ~2,200 |
| `ca_default.csv` | TSX (main board) large/mid-cap Canadian companies | ~220 |

**Exchange scope (owner-set, hard boundary)**: only NYSE, Nasdaq, NYSE
American (AMEX) and TSX proper. **TSX Venture (TSXV), CSE and NEO are
explicitly out of scope** -- neither seeded nor permitted by default (see
`UniverseConfig.permitted_exchanges`, which no longer includes `TSXV`).
`tests/test_universe.py::TestPackagedUniverseFiles::test_no_forbidden_exchanges_in_either_seed_file`
and `test_permitted_exchanges_is_exactly_the_owner_allowed_set` enforce this.

**These are hand-curated seed lists, not a live index feed.** Index
constituents drift constantly -- additions, removals, mergers, renames -- and
these files will go stale over time; each carries a generation-date comment at
the top documenting exactly what was fetched, from where, and when (or, for
the Canadian file, why a live fetch was not possible and what was used
instead -- see below). Edit them freely (add, remove, or correct rows) if you
want a different starting universe; the column format matches the CSV
universe source below (`symbol,name,exchange,sector,market_cap_bucket,country`).
`market_cap_bucket` (`mega`/`large`/`mid`/`small`) is an approximate size
label, not a live market capitalisation figure -- the runtime filter below is
what is actually authoritative.

**How the 2026-07-30 expansion was sourced** (owner complaint: "I don't see
names like INTC or AMD"): `us_default.csv` was merged with rows derived from
a real, live-fetched dataset -- `rreichel3/US-Stock-Symbols`
(github.com/rreichel3/US-Stock-Symbols), a nightly-updated mirror of NASDAQ's
own public stock-screener listing, fetched via `raw.githubusercontent.com` on
2026-07-30 -- filtered to real, provider-reported `marketCap >= $1B`,
non-Canada domicile, and non-instrument junk (warrants/units/notes/preferred/
SPAC shells) excluded, multi-share-class duplicates collapsed to one row. The
originally-planned Wikipedia-index-list approach (Russell 1000 / S&P 400 /
S&P 600) was **not used** because `WebFetch` returned HTTP 403 for every host
tried in this sandbox at implementation time, Wikipedia included, and a
non-Wikipedia control URL (`example.com`) also 403'd, ruling out a
Wikipedia-specific block rather than a general WebFetch outage; `github.com`
and `raw.githubusercontent.com` were reachable and became the real-data path
instead. Arguably this is a more literal reading of the underlying ask ("all
... stocks over 1 Billion market cap") than an index-membership proxy would
have been. `ca_default.csv`'s expansion had no equivalent live, comprehensive,
current TSX-constituent dataset reachable from this sandbox and is compiled
from trained knowledge instead, cross-checked against the same live US-side
dataset wherever the same company is also US-cross-listed; see that file's
own banner for the full account, including the specific per-symbol judgement
calls made to resolve US/Canada ticker collisions and cross-listings.

**Cross-listing rule**: a Canada-domiciled company dual-listed on TSX and a
US exchange is represented **once**, in `ca_default.csv`, under its TSX
ticker -- never also in `us_default.csv`. `tests/test_universe.py`'s
`test_no_symbol_collision_across_packaged_files` and
`test_cross_listed_canadian_names_appear_only_in_ca_file` (spot-checking
Shopify and Barrick) enforce this.

**Known cross-market symbol collisions**: a handful of well-known tickers are
used by *different* companies on the US and Canadian markets (e.g. `T` is
AT&T on NYSE and TELUS on TSX; `K` is Kellanova on NYSE and Kinross Gold on
TSX; several more surfaced during the 2026-07-30 expansion -- e.g. `PPL` is
PPL Corporation on NYSE vs Pembina Pipeline on TSX, `KEY` is KeyCorp vs
Keyera). Because the universe is keyed by bare symbol, the packaged CSVs
deliberately omit the Canadian side of each such collision rather than have
one silently overwrite the other -- an accuracy trade-off documented here
(and inline in each seed-generation script) rather than hidden.

**When they apply**: with `universe.source = "database"` (the default), the
packaged universes are used to seed the scannable universe only while the
database has no stored securities yet -- i.e. before the first
`claudetrade refresh` completes. Once securities are stored, those take
precedence and are merged with any packaged symbol not yet stored, so newly
added packaged names remain visible even after a refresh. This is also what
`StooqMarketProvider.list_universe()` / `YahooMarketProvider.list_universe()`
/ `TipRanksProvider.list_universe()` all return, since none of the three has
a bulk reference-data endpoint of its own -- it is why a fresh install has
thousands of US and hundreds of Canadian symbols to pull on the very first
`claudetrade refresh` instead of an empty universe regardless of which of
them is configured as primary. `StooqMarketProvider` additionally builds a
size-filtered inventory through `load_stooq_universe()` (only NYSE, Nasdaq,
NYSE American and TSX common stocks whose bootstrap size is at least $1B;
more than 2,000 US and 200 TSX stocks) for when it is used explicitly.

### Why do I see tickers such as `AMFI`, `ANFI`, or `ANPR`?

Those four-letter names are generated by the **synthetic** provider; they are
deliberately fictional and are not failed lookups against a real source. A
fresh install defaults to `market_data.provider = "tipranks"`, so seeing
these symbols means an older configuration explicitly still selects
synthetic data or the database contains rows from an earlier synthetic run.
Existing synthetic rows do not become real merely because the provider
setting changes; use a fresh data directory/database, or remove the
synthetic data and run `claudetrade refresh`.

**Configuration**:

```toml
[universe]
source = "database"                          # default
packaged_universes = ["us_default", "ca_default"]  # set to [] to disable the fallback
permitted_exchanges = ["NYSE", "NASDAQ", "AMEX", "TSX"]  # TSXV/CSE/NEO excluded
min_market_cap_usd = 500000000               # ADR-0008 Decision 3 runtime floor, default $500M (owner-lowered 2026-07-31)
unknown_cap_policy = "include"               # "include" | "exclude" -- see below
```

For strict live operation with the default chain:

```toml
[market_data]
provider = "tipranks"
fallbacks = ["yahoo", "csv"]

[universe]
max_symbols = 3000
min_market_cap_usd = 1000000000
unknown_cap_policy = "exclude"
```

---

## Runtime Market-Cap Filter (ADR-0008 Decision 3)

**Modules**: `src/claudetrade/data/ingest.py` (`DataIngestor.enrich_market_caps`),
`src/claudetrade/data/universe.py` (`UniverseSelector.for_session`),
`src/claudetrade/providers/market/tipranks.py`

This is the durable fix, not the expanded seeds above (which are bootstrap
coverage only). At refresh time, `DataIngestor.enrich_market_caps` tries to
establish a real market cap for every candidate security via the market-data
path: the configured primary provider (**TipRanks by default -- the only
adapter that resolves a positive figure by default now**, since Yahoo's
former quote-API-backed cap capability required cookie+crumb auth in
production and was removed outright), then, if it is a cascading fallback
wrapper, each of its fallbacks in turn (a provider's `get_market_caps` is an
**optional** capability -- see `providers.base.MarketDataProvider.get_market_caps`
-- so a provider that does not support it, the default for every adapter
except `tipranks`, simply contributes nothing rather than erroring). Inside
`TipRanksProvider.get_market_caps` itself, the batched GetQuotes endpoint is
tried for the whole symbol list first, with the per-symbol `dataForTicker`
path as the fallback for anything it did not cover -- see "GetQuotes
batching" in the TipRanks section above; this is what turns a ~2,400-symbol
refresh's cap sweep from thousands of individual calls into a handful of
batches plus a much smaller fallback tail. The resolved figure is stored on
`Security.market_cap_usd`. The enriched floor is also applied **before**
price, corporate-action, earnings and sentiment requests, so a company that
currently resolves below $1B does not consume a bars-history request. The
benchmark is retained because regime and relative-strength calculations
require it even though it is an ETF.

`UniverseSelector.for_session` then applies `universe.min_market_cap_usd`
(default $1B) against that stored figure:

- `market_cap_usd >= min_market_cap_usd` -> included.
- `market_cap_usd < min_market_cap_usd` -> excluded, reason `below_min_market_cap`.
- `market_cap_usd is None` (no configured provider could establish one) ->
  governed by `universe.unknown_cap_policy`:
  - `"include"` (default): **not** excluded for this reason. Silently
    dropping a name just because its cap could not be established would
    reintroduce survivorship-style bias at the universe layer -- the same
    failure mode the point-in-time delisting check in the same method
    already guards against for names that stopped trading.
  - `"exclude"`: excluded, reason `unknown_market_cap` -- an explicit opt-in
    for an operator who prefers under-coverage to scanning an unpriced name.

**Either way, an unresolved cap is always flagged**, never silently
absorbed: `enrich_market_caps` records an `unknown_market_cap` data-quality
finding (`WARNING` severity) for every security it could not price, visible
via `claudetrade status` / the data-quality report, regardless of which
`unknown_cap_policy` is in effect. This is deliberately a *separate* concept
from `FilterConfig.min_market_cap_usd` (the older, lower, $500M
candidate-quality screen re-applied later at signal-scoring time in
`signals.scoring`) -- raising or lowering `universe.min_market_cap_usd`
changes who is eligible to be scanned at all; it does not touch that
downstream gate.

**Market-cap sources today**: `tipranks` (the default primary) is the only
adapter that resolves a positive figure by default -- stooq/yahoo/synthetic/
csv all contribute nothing (the protocol default). Use
`unknown_cap_policy = "exclude"` when the request set must strictly contain
only currently resolved caps.

---

## Earnings Providers

### TipRanks (Default)

**Module**: `src/claudetrade/providers/market/tipranks.py`
(`TipRanksProvider` implements both `MarketDataProvider` and the structural
`EarningsProvider` protocol -- see the [TipRanks](#tipranks-primary-online)
section above for the full account: the headline win, `timeOfDay` mapping
caveats, the earnings-ticker-mismatch gotcha, caching and ToS posture).

Real next-scheduled and last-reported earnings, replacing the previous
synthetic default.

**Configuration**:

```toml
[earnings]
provider = "tipranks"   # the default
fallbacks = ["csv"]
```

**Limitations**: only one upcoming and one historical report per symbol
(not a full calendar); `timeOfDay` mapping is provisional for the observed
value `2`. See the TipRanks section above for the complete list.

### Synthetic (Offline/demo)

**Module**: `src/claudetrade/providers/earnings/synthetic.py`

Fabricated earnings events. Deterministic (seeded). No longer the default,
but fully available -- this is what `tests/conftest.py` pins for every test,
and what an operator running fully offline/demo should select explicitly.

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

### Reddit (Live OAuth, owner cookie-session, or an opt-in unauthenticated fallback)

**Module**: `src/claudetrade/providers/social/reddit.py`

Four modes, tried in this order at construction time (ADR-0008 Decision 1):

1. **Password grant** -- a script app's client id/secret *plus* the owner's
   own Reddit username/password. An official OAuth flow, preferred whenever
   all four credentials resolve.
2. **Cookie-session mode** (`reddit.session_cookie_credential`) -- the
   owner's own logged-in `reddit_session` browser cookie, pasted from
   devtools. Reads the same `www.reddit.com/r/<sub>/new.json` endpoint as the
   public-JSON fallback below, but authenticated as the owner with a
   browser-style User-Agent instead of anonymously. **Live-probe evidence
   (2026-07-30)**: Reddit's public JSON endpoint returns HTTP 403 to any
   non-browser client regardless of User-Agent, but 200 JSON to a real,
   logged-in browser tab -- i.e. it gates on the session cookie, not on the
   UA string. Used when the password-grant credentials aren't both
   configured but the cookie is; preferred over the client-credentials
   grant. This is the owner's own session for personal use only (ADR-0008
   Decision 1: "own credentials only" -- never a shared/default account or
   someone else's cookie), and shares the public-JSON path's fail-closed
   behaviour exactly. See below for how to export the cookie.
3. **Client-credentials grant** -- a script app's client id/secret alone
   (app-only, no user login). Used when neither the password grant nor the
   cookie session are available but the client id/secret are.
4. **Public JSON fallback** (`reddit.public_json_fallback = true`) --
   unauthenticated reads of `www.reddit.com/r/<sub>/new.json`. Only used when
   *none* of the above resolve. **Honest ToS status**: this is not a
   sanctioned integration path the way OAuth is -- reading Reddit's public
   listing JSON without authentication is ToS-gray for automated/scheduled
   use, tolerated in practice for casual, low-volume, identifying-UA
   traffic. It exists only as a last resort, is off by default, and is
   hard-capped at 30 requests/minute regardless of config. The moment OAuth
   credentials or a session cookie work, this class prefers them
   automatically.

**Setup (OAuth, either grant)**:

1. Go to https://www.reddit.com/prefs/apps
2. Create an app (choose "script" type)
3. Note the **client ID** and **client secret**
4. For the password grant, also use the owner's own Reddit account username
   and password (never a shared or default account -- ADR-0008 Decision 1's
   "own credentials only" constraint)

**Configuration**:

```toml
[reddit]
enabled = true
provider = "reddit"
client_id_credential = "reddit_client_id"
client_secret_credential = "reddit_client_secret"
username_credential = "reddit_username"      # only used by the password grant
password_credential = "reddit_password"      # only used by the password grant
subreddits = ["stocks", "investing", "StockMarket", "SecurityAnalysis", "options", "swingtrading"]
posts_per_subreddit = 100
comments_per_post = 50
lookback_hours = 72
rate_limit_per_minute = 60
user_agent = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
store_author_names = false

# Opt-in, last-resort fallback -- see the ToS caveat above before enabling.
public_json_fallback = false
public_json_rate_limit_per_minute = 30       # hard-capped at 30 regardless of this value
```

**Store credentials**:

```bash
# Environment variables
export CLAUDETRADE_SECRET_REDDIT_CLIENT_ID="your_client_id"
export CLAUDETRADE_SECRET_REDDIT_CLIENT_SECRET="your_client_secret"
export CLAUDETRADE_SECRET_REDDIT_USERNAME="your_reddit_username"       # password grant only
export CLAUDETRADE_SECRET_REDDIT_PASSWORD="your_reddit_password"       # password grant only

# Or OS credential store
claudetrade secrets set reddit_client_id
claudetrade secrets set reddit_client_secret
claudetrade secrets set reddit_username
claudetrade secrets set reddit_password
```

#### Reddit cookie-session mode (owner's own personal session, ADR-0008 Decision 1)

Reads the same public listing endpoint as the public-JSON fallback, but
authenticated with the owner's own `reddit_session` cookie -- copied from a
logged-in browser -- plus a browser-style User-Agent, instead of anonymous
requests with the descriptive app UA.

**CONFIRMED WORKING from a non-browser client (owner-validated 2026-07-31)**:
a properly-attached `reddit_session` cookie plus a browser-style User-Agent
gets a clean HTTP 200 with real JSON from a plain HTTP client -- this is not
a browser-only endpoint, it is a *cookie-gated* one, and a correctly
configured, current cookie works from this codebase's own `httpx` client
exactly as it does from a logged-in browser tab. (Two earlier apparent
403s during validation both turned out to be tooling artifacts, not a real
block: PowerShell 5.1 silently drops a `Cookie` header passed via
`-Headers`, and a separate run used an empty cookie value -- neither
reflects how this adapter actually sends the header.)

**This is the owner's own personal Reddit session, for personal use only**
(ADR-0008 Decision 1: "own credentials only" -- never a shared/default
account or someone else's cookie). It automates reading Reddit while
authenticated as a real logged-in account rather than through the sanctioned
OAuth API, so treat it the same way as the public-JSON fallback's ToS
posture above: tolerated for personal, low-volume use, not a sanctioned
integration path.

**How to get your `reddit_session` cookie value** (Chrome/Edge devtools):

1. Log in to reddit.com in your browser as normal.
2. Open devtools (F12) -> **Application** tab -> **Storage** -> **Cookies**
   -> `https://www.reddit.com` (Firefox: **Storage** tab instead of
   **Application**). Both `reddit_session` and `token_v2` (see below) are
   **HttpOnly** cookies -- they are invisible to `document.cookie` in the
   Console; the Application/Storage -> Cookies panel is the only place to
   read their values.
3. Find the cookie named `reddit_session`; copy its **Value** exactly as
   shown (it is already URL-encoded where needed -- paste it verbatim, do
   not re-encode or otherwise edit it).
4. Store it via the credential store, never in `config.toml`:

```bash
claudetrade secrets set reddit_session_cookie
# or, environment variable:
export CLAUDETRADE_SECRET_REDDIT_SESSION_COOKIE="..."
```

`reddit_session` alone is sufficient -- try it by itself first. Reddit's own
web frontend additionally sends a second cookie, `token_v2` (an HttpOnly
OAuth JWT), captured the same way from the same Cookies panel. It is
**optional** and only worth adding if `reddit_session` alone stops working:

```bash
claudetrade secrets set reddit_token_v2
# or: export CLAUDETRADE_SECRET_REDDIT_TOKEN_V2="..."
```

Cookie lifetimes differ significantly: `reddit_session` is long-lived (weeks
or more), while `token_v2` is a short-lived JWT (typically hours to about a
day). Because `token_v2` expires so much faster, configuring it does not
make cookie-session mode *more* reliable over time -- it is an extra value
that itself needs re-exporting far more often than `reddit_session` does, so
only add it if you have observed `reddit_session` alone being rejected. When
both are configured, the Cookie header sent is
`reddit_session=<value>; token_v2=<value>` (both values sent verbatim, in
that order); with only `reddit_session` configured, the header is unchanged
from before `token_v2` support existed (`reddit_session=<value>` alone).

No separate enable flag is needed -- like the password and client-credentials
grants above, this mode is picked up automatically the moment the cookie
resolves and the password-grant credentials do not (see the mode order
above). `reddit.session_rate_limit_per_minute` (default 30) governs its
pace, same conservative, human-scale budget as the public-JSON fallback.

**Testing your cookie from the app**: the Configuration screen has a "Test"
button next to the Reddit credential fields (`POST
/api/system/credentials/reddit/test`), which makes one small live request
using whichever mode your current credentials select and reports
`{ok, mode, status_detail}` without ever echoing the credential value back.
Use it after pasting a fresh cookie to confirm it works before waiting for
a full scheduled refresh.

**If the cookie test reports blocked**: given the confirmation above, a
block from a correctly-configured client now most likely means the pasted
`reddit_session` value has **expired or was mistyped/truncated** -- re-export
a fresh one from DevTools (step 3 above) and test again. It is not expected
to mean TLS-fingerprinting or a browser-only requirement; that theory was
considered during validation and ruled out. If you have `token_v2`
configured and only recently started seeing blocks, its shorter lifetime
makes it the more likely culprit -- try removing it (`reddit_session` alone
is sufficient) before re-exporting both. If cookie-session mode remains
blocked after a fresh export, the password-grant OAuth path
(`reddit_client_id`/`reddit_client_secret`/`reddit_username`/`reddit_password`,
see above) is the sanctioned, durable alternative once your Reddit API app
is approved; news RSS keeps sentiment flowing in the meantime regardless.

**Fail-closed behaviour (cookie-session and public-JSON modes, ADR-0008
Decision 1)**: any HTTP 401/403 (a 401/403 here usually means the pasted
cookie(s) have expired or been logged out -- re-export a fresh one), a
non-JSON response body, or any other unexpected/non-2xx response immediately
disables the source **for the rest of that fetch cycle** -- no retry loop, no
fingerprint or proxy rotation, no CAPTCHA handling. The next scheduled cycle
tries again from scratch. A 429 is handled the same way as the OAuth path (a
`RateLimitError` carrying `Retry-After`, also ending the cycle). This is
exercised in `tests/test_reddit_provider.py`.

**Limitations**:

- OAuth grants are rate limited to 60 calls/minute (public tier); the
  cookie-session and public-JSON paths are conservatively capped (see above)
- Posts are searchable only in the last 6 months (API limitation)
- Engagement counts (score, num_comments) are mutable at the source; historical sentiment cannot be perfectly reconstructed
- Author names are stored as salted hashes only (never plaintext), per config `store_author_names = false`
- Cookie-session mode carries no delivery guarantee at all -- it can be
  throttled or blocked by Reddit at any time with no notice, and the pasted
  cookie(s) will eventually expire and need re-exporting (`reddit_session`
  on the order of weeks; `token_v2`, if used, on the order of hours)
- The public-JSON fallback carries no delivery guarantee at all -- it can be
  throttled or blocked by Reddit at any time with no notice, by design

**Licence**: Reddit data is subject to Reddit's API terms and user agreement. Commercial use of aggregated social data may require permission. The cookie-session and public-JSON paths are additionally ToS-gray for automated use -- see above.

### X/Twitter (Paid API v2, or the owner's own session -- on by default when credentialed)

**Module**: `src/claudetrade/providers/social/x_provider.py`

**Auto-enabled (owner directive, 2026-07-31)**: mirroring Reddit's
cookie-session self-selection, both `x.enabled` and `x.session_enabled`
default to `true` -- "use if credentialed", not "on unconditionally". With
no bearer token and no session cookies configured, this source stays
disabled cleanly exactly as before; the moment either resolves from the
secrets store, it activates on the next refresh with no separate flag to
flip. Both remain explicit, operator-settable disable knobs: `x.enabled =
false` turns X off outright regardless of credentials, and
`x.session_enabled = false` keeps the official-API path (if configured)
while refusing to ever attempt the ToS-risking cookie-session path even if
`x_auth_token`/`x_ct0` are stored.

Two independent live paths, tried in this order at construction time:

1. **Official API v2** (`bearer_credential`). Requires a paid tier for
   meaningful search volume. Always preferred when configured -- ADR-0008
   Decision 1 requires the official API remain first-choice.
2. **Cookie-session mode** (`x.session_enabled`, default `true`), only
   reached when no bearer token is configured and both session cookies
   resolve. See below.

**Setup (official API)**:

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

#### X cookie-session mode (auto-enabled when credentialed -- ADR-0008 Decision 1)

**PLAIN ACCOUNT-RISK STATEMENT**: this mode automates the owner's own
logged-in x.com session against the internal, unversioned GraphQL endpoints
the web client itself uses. **Doing this violates X's Terms of Service and
can lead to suspension of that X account.** This application never bundles
or defaults these credentials, never solves a CAPTCHA or challenge, and
never rotates a fingerprint or proxy to work around a block -- it disables
the source for the rest of the cycle instead. The owner accepts this risk
for their own account, for personal use only, and per the owner's explicit
2026-07-31 directive this mode now **auto-activates** the moment both
session cookies resolve from the secrets store -- `x.session_enabled`
defaults to `true` (an explicit disable knob, not a required opt-in), the
same posture Reddit's cookie-session mode already has. Set
`x.session_enabled = false` to refuse this path outright regardless of
whether the cookies are configured.

**How to export your session cookies** (Chrome/Edge/Firefox devtools -- this
is also exactly what the Configuration screen's inline instructions and the
"Test" button next to the X credential fields expect):

1. Log in to x.com in your browser as normal, using your own account.
2. Open devtools (F12) -> **Application** tab (Chrome/Edge) or **Storage**
   tab (Firefox) -> **Cookies** -> `https://x.com`.
3. Find the cookie named `auth_token`; copy its **Value**. This cookie is
   **long-lived** (weeks), similar to Reddit's `reddit_session` cookie.
4. Find the cookie named `ct0` (the CSRF token cookie); copy its **Value**.
   `ct0` is the CSRF token paired with `auth_token` for this same session --
   both are sent together on every session-mode request (see
   `x_provider.py`'s `_fetch_session_query`), so re-export both together if
   session mode starts failing after a while.
5. Store both via the credential store, never in `config.toml`, or via the
   Configuration screen's X credential fields:

```bash
claudetrade secrets set x_auth_token
claudetrade secrets set x_ct0
# or, environment variables:
export CLAUDETRADE_SECRET_X_AUTH_TOKEN="..."
export CLAUDETRADE_SECRET_X_CT0="..."
```

6. Use the Configuration screen's **Test** button next to the X credential
   fields (or `POST /api/system/credentials/x/test`) to confirm the
   cookies work -- see "Validating the internal endpoint constants" below.

**Configuration** (`session_query_id` and `session_symbols` both need
setting -- the rest are the defaults):

```toml
[x]
enabled = true                         # default; explicit disable knob
session_enabled = true                 # default; explicit disable knob
auth_token_credential = "x_auth_token"
ct0_credential = "x_ct0"
session_query_id = ""                  # REQUIRED -- your own browser capture, see below
session_symbols = ["AAPL", "MSFT"]     # cashtag-searched; leading $ added automatically
session_max_results_per_query = 40
session_rate_limit_per_minute = 6      # deliberately stricter than the official API default
session_request_timeout_s = 20.0
```

**Validating the internal endpoint constants (`x_provider.py`'s "expected
maintenance" note)**: session mode calls x.com's internal, unversioned
GraphQL API, whose path and query ID (`x.session_query_id`) x.com changes
without notice -- see the "Endpoint
stability" note below for the full explanation and why this sandbox could
not verify a live query ID while building this adapter. The
Configuration screen's **Test** button (`POST
/api/system/credentials/x/test`) is the owner's fast, low-risk way to
validate those constants against the real API: it makes exactly one small
live fetch using the configured mode and reports pass/fail plus a short
detail string, without waiting for a full scheduled refresh to discover the
same failure. A `SourceBlockedError` result there is the first, fastest
signal that the constants block needs a fresh browser capture.

**Endpoint stability -- and why `session_query_id` is required**: the
GraphQL endpoint path and query ID this adapter calls are internal to
x.com's web client and change without notice or a deprecation window. **No
query ID ships with this application**, and none can: it rotates, and a
current one only exists in your own browser's traffic.

Capture it once:

1. Log in to x.com in a normal browser.
2. Open devtools -> **Network**, filter on `graphql`.
3. Search for any cashtag (e.g. `$AAPL`).
4. Find the `SearchTimeline` request and copy the opaque path segment
   immediately *before* `/SearchTimeline` -- that is the query ID.
5. Put it in `config.toml` as `x.session_query_id`. It is not a credential
   (it is account-independent and carries no authority), so it belongs in
   config rather than the secrets store.

Until it is set, session mode fails closed **without issuing a request** and
says so explicitly. When x.com rotates it, requests start returning 404 and
the error names the query ID as the likely cause. Re-capturing is expected,
ordinary maintenance, not a bug fix.

An earlier release shipped a placeholder query-ID constant instead. It 404'd
with an empty body and no `content-type` header, and because the
content-type check ran ahead of the status-code check, the failure was
reported as a login wall -- telling operators to re-export cookies that were
perfectly valid. The checks are now ordered status-code-first, and the two
messages are pinned by
`tests/test_x_provider.py::TestSessionQueryIdDiagnosis`.

**Fail-closed behaviour (ADR-0008 Decision 1)**: any HTTP 401/403 (expired,
logged-out, or challenged cookies), any other 4xx/5xx (a stale or unset
`session_query_id`, which is reported as such rather than as an
authentication failure), any non-JSON *2xx* response (the login-wall
shape), any response that doesn't match the expected timeline shape
(including a changed internal API, see above), or a 429 immediately disables
the source **for the rest of that fetch cycle** -- no retry loop, no fingerprint/proxy rotation, no CAPTCHA
handling. Re-export fresh cookies (401/403) or wait for the next scheduled
cycle (429) to resume. This is exercised in `tests/test_x_provider.py`.

**Limitations**:

- Off by default; must be explicitly enabled and configured
- Unofficial, unsupported, and can stop working at any time without notice
- No engagement-metrics parity guarantee with the official API's `public_metrics`
- The official API path above remains intact and is always preferred the
  moment a bearer token is configured

### Stocktwits (Live, Keyless, On by Default)

**Module**: `src/claudetrade/providers/social/stocktwits.py`

Stocktwits' own documented, **keyless** public symbol-stream API
(`api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json`) -- ADR-0008
Decision 1's "official API first-choice" applies in the fullest sense here:
no credential is used, none is bypassed, and no paywall or authentication
boundary is touched. **On by default** (owner directive, 2026-07-31): the
vendor's published unauthenticated budget (200 requests/hour) is respected by
`rate_limit_per_minute` and `max_symbols_per_cycle` rather than by leaving
the source switched off.

**Sentiment tags are a prior hint, not ground truth**: a message may carry a
self-declared `entities.sentiment.basic` tag ("Bullish"/"Bearish") the
*author* attached to their own post. This is mapped onto
`SocialPost.sentiment_prior` (`"bullish"` / `"bearish"` / `None`) as a prior
hint only -- the ensemble sentiment classifier still runs on the post's text
unconditionally for every post, Stocktwits included. Treating a self-declared
label as truth would let anyone paint their own post's sentiment without the
classifier ever checking it against the actual content.

**Browser-TLS (JA3) impersonation** -- see ADR-0008 Decision 1
**Amendment 1** (owner directive, 2026-07-31): **live-probe evidence
(2026-07-30)** showed this endpoint returning HTTP 403 to PowerShell/.NET and
Python clients regardless of User-Agent (both a generic UA and this
codebase's descriptive app UA), but HTTP 200 JSON to a plain, logged-out
browser tab on the same machine and IP. **Confirmed cause**: Cloudflare bot
management gating on the client's **TLS ClientHello fingerprint (JA3)**. A
stdlib-`ssl`-backed client (`httpx`, `requests`, .NET) presents a
cipher/extension ordering no browser produces, and the edge rejects the
connection before any application-layer header is considered -- which is why
the earlier header-only approach could never have worked. This is a
client-shape heuristic on a keyless-open endpoint, not an auth or
API-contract check.

The adapter therefore issues its GET through
[`curl_cffi`](https://github.com/lexiforest/curl_cffi)'s browser
impersonation (`impersonate = "chrome"` by default -- the library's alias for
its current newest Chrome profile, so it tracks `curl_cffi` upgrades instead
of pinning a version that goes stale). That reproduces the browser's TLS
ClientHello and HTTP/2 settings, i.e. it makes our client look like the
client this endpoint **already serves**.

`curl_cffi` also supplies the profile-consistent `User-Agent` and
`sec-ch-ua*` client hints, so this adapter deliberately does **not** override
the User-Agent: a hand-written UA that disagrees with the profile's client
hints or its TLS fingerprint is itself a bot signal. What the adapter adds is
the request-context headers a stocktwits.com XHR carries (`Accept`,
`Accept-Language`, `Referer`, `Origin`, `Sec-Fetch-*`).

**What this deliberately does not do**: no Cloudflare clearance cookie is
captured, stored or replayed (no `cf_clearance`/`__cf_bm` harvesting, no
session persistence -- each request stands alone); no CAPTCHA solving; no
fingerprint *rotation* (one honest, configured, documented profile, never a
shuffling pool) and no proxy rotation; no credential of any party. Rates stay
conservative and human-scale with jitter, and the per-cycle symbol cap still
applies. **Impersonation makes a block unlikely, not impossible** -- if the
edge blocks anyway, the fail-closed path below is unchanged
(`SourceBlockedError`, cycle over, no retry loop), and "Stocktwits
unavailable" remains a fully supported state.

**`curl_cffi` is an OPTIONAL dependency**, lazy-imported at request time
(same pattern as the `anthropic`/`openai` SDKs, or `scikit-learn` for the ML
extras). Without it installed: the module still imports, the provider still
constructs, the registry still builds it, the whole test suite still passes
-- Diagnostics simply reports the source **unavailable** with the install
hint (`pip install curl_cffi`), and any fetch attempt raises
`SourceBlockedError` naming the missing package rather than a bare
`ImportError`. Install it with:

```
pip install curl_cffi
```

**Sentiment coverage in practice**: Reddit's cookie-session mode
(owner-verified working, see above) and the four default news RSS feeds
remain this application's other social-sentiment sources; with the TLS
fingerprint matched, Stocktwits is expected to contribute reliably rather
than opportunistically -- but the application stays fully functional with it
contributing nothing at all, which is exactly what happens if `curl_cffi` is
absent or the edge tightens.

**Configuration**:

```toml
[stocktwits]
enabled = true                  # on by default since 2026-07-31
impersonate = "chrome"          # curl_cffi browser profile supplying the TLS/JA3 fingerprint
watchlist_symbols = ["AAPL", "MSFT", "TSLA"]
max_symbols_per_cycle = 20      # hard budget guard, see below
rate_limit_per_minute = 3       # 180/hour -- a margin below the 200/hour vendor cap
request_timeout_s = 20.0
```

`impersonate` accepts any profile `curl_cffi` supports -- the aliases
`"chrome"`, `"safari"`, `"firefox"`, `"edge"` (each resolving to that
browser's newest bundled profile) or an explicit build such as
`"chrome142"` / `"safari180"` / `"firefox135"`. It is worth changing only if
the edge starts blocking the default; **rotating** it per request is not
supported and is explicitly out of bounds (ADR-0008 Decision 1).

**Rate budget**: Stocktwits documents 200 requests/hour for unauthenticated
reads. The default `rate_limit_per_minute = 3` (180/hour) keeps a working
margin. `max_symbols_per_cycle` bounds how many symbols one refresh will
fetch regardless of universe size -- one request per symbol, no deep
pagination -- and the symbols actually fetched are prioritised by the order
of the `symbols` hint the caller passes to `fetch_posts()` (the pipeline
passes recent-signal / watchlist symbols first), falling back to
`watchlist_symbols` when no hint is supplied. A broad universe therefore
degrades to "covered the names that mattered this cycle", never a silent,
even rationing across everything.

**Fail-closed behaviour (ADR-0008 Decision 1)**: any HTTP 401/403, a
non-JSON response, or a response missing the expected `messages` field
immediately disables the source **for the rest of that fetch cycle** -- no
retry loop, no fingerprint/proxy rotation. A 404 for a single symbol (unknown
ticker, or a real symbol with no chatter) is treated as ordinary "nothing
here" and only skips that one symbol, matching this codebase's existing
precedent for per-symbol gaps (see `providers.market.stooq`'s unknown-symbol
handling). A 429 raises the same `RateLimitError` shape as every other
source and also ends the cycle. This is exercised in
`tests/test_stocktwits_provider.py`.

**Credentials**: None required.

**Limitations**:

- No historical backfill beyond whatever the stream endpoint currently
  returns (typically the most recent ~20-30 messages per symbol) -- this is
  not an archive
- Engagement counts (`likes.total`, `conversation.replies`) are mutable at
  the source, same caveat as Reddit/X
- Author handles are stored as salted hashes only, never plaintext
- The self-declared sentiment tag is optional per-message; most messages
  carry no tag at all, in which case `sentiment_prior` is `None`
- Requires the optional `curl_cffi` package; without it the source reports
  itself unavailable (everything else keeps working)
- The impersonation profile is only as current as the installed `curl_cffi`.
  As Chrome advances, an old bundled profile drifts from the real browser
  fleet and can start being challenged -- `pip install -U curl_cffi` is the
  first thing to try if Stocktwits starts showing as blocked

**Licence**: Stocktwits' public stream is documented for keyless basic
reads; this adapter performs only that. Commercial or high-volume use may
require Stocktwits' paid API tier and is out of scope here.

### ApeWisdom -- Reddit + 4chan mention counts (Default, Live, No Credentials)

**Module**: `src/claudetrade/providers/social/apewisdom.py`

Reads [apewisdom.io](https://apewisdom.io/api)'s free, keyless JSON API, which
counts how often each ticker is mentioned across a community in the last 24
hours, alongside the same count 24 hours earlier.

This is a different **kind** of source from every other entry in this section,
and the difference matters more than the transport does.

| | Post sources (Reddit, X, Stocktwits, News) | ApeWisdom |
|---|---|---|
| Returns | individual posts | per-ticker tallies |
| Tickers | resolved here, from prose | already resolved upstream |
| Direction | classified from text | **none — attention only** |
| Corpus | narrow, rate-limited windows | whole communities, continuously |
| Backfillable | yes, within provider limits | **no** — rolling 24h window, no history endpoint |

**Why it is not a `SocialProvider`.** The protocol returns `SocialPost`
objects. ApeWisdom has no post text, no authors and no timestamps, so
implementing `fetch_posts` would mean inventing all three — and the invented
values would not sit inertly: `unique_authors`, `duplicate_ratio`, `bot_risk`
and `manipulation_risk` are all derived from post-level identity and text, so
fabricated posts would feed the manipulation model confident-looking fiction.
That is precisely the failure the synthetic providers were removed for. It
implements `fetch_attention()` returning `domain.SymbolAttention` instead.

**What it is good for.** Its rows are tickers, so it structurally cannot
produce the common-word junk (`AS`, `YOU`, `DAY`) that local extraction minted
out of ordinary English; it observes whole communities where the local Reddit
and X fetches see narrow, rate-limited windows; and it keeps producing data
when Reddit is rate-limited, X has no cookie, and the Stocktwits watchlist is
empty. `get_trending`'s default `source="auto"` therefore prefers it and falls
back to locally-resolved posts.

**What it explicitly cannot tell you** is direction. Attention is a separate
axis from polarity — `sentiment.aggregation` is explicit that counting
mentions toward bullishness "is exactly the mistake this module exists to
avoid". Stored rows use their own `apewisdom:<filter>` source labels and never
write the combined `"all"` aggregate that strategies score against, so **this
source cannot move a signal's score**. In `get_trending` its bull/bear ratio
and confidence report as `null`, not as a neutral-looking `1.0`.

**Privacy/licence**: aggregate counts only. No post text, no usernames, and no
personal data is retrieved or stored.

```toml
[apewisdom]
enabled = true
filters = ["all-stocks", "4chan"]   # combined equity subreddits, and /biz/
max_pages_per_filter = 2
min_mentions = 5
```

Other filters ApeWisdom publishes include `wallstreetbets`, `stocks`,
`options`, `investing`, and `stockmarket`; crypto-only filters are
deliberately not defaults, since this application screens US equities.

### Adanos -- X/Reddit/Polymarket/News aggregator (Default, Live, No Credentials Required)

**Module**: `src/claudetrade/providers/social/adanos.py`

Reads [adanos.org](https://adanos.org)'s pre-aggregated per-ticker buzz and
sentiment across **four** feeds -- X/Twitter, Reddit, Polymarket and
financial news -- refreshed hourly. Same family as ApeWisdom above (a hosted
aggregator serving finished rows, not individual posts) but richer in the
one way that matters: alongside volume, Adanos reports real polarity.

| | ApeWisdom | Adanos |
|---|---|---|
| Platforms | Reddit, 4chan (combined tally) | X, Reddit, Polymarket, News (separate rows) |
| Volume | mentions, upvotes | `buzz_score`, mentions/trade count, `trend`, 7-point `trend_history` |
| Direction | **none — attention only** | `sentiment_score` (-1..1), `bullish_pct`/`bearish_pct` |
| Storage | `symbol_sentiment_daily` (`apewisdom:<filter>` source) | its own table, `adanos_snapshots` |
| Backfillable | no -- rolling 24h window, no history endpoint | no -- hourly rolling snapshot, no history endpoint |

**The news feed differs from the other three in one field.** Instead of an
engagement total (`total_upvotes` for X/Reddit, `total_liquidity` for
Polymarket), news rows carry `source_count` -- the number of distinct news
outlets reporting on the ticker. There is no engagement analogue for a news
aggregation, so `source_count` is stored through the same
`AdanosSnapshotRow.engagement` column the other three feeds already use for
their own per-platform number (see `providers.social.adanos._ENGAGEMENT_FIELD`)
rather than a dedicated column -- one more platform-specific meaning for a
column that already carries two, not a schema change for a single float.

**Why this is not a `SocialProvider` (same reasoning as ApeWisdom, restated
because it matters more here).** Adanos serves pre-aggregated rows with no
underlying post text, author or timestamp. `providers.social.hosted_api`'s
module docstring warns at length against forcing a pre-aggregated vendor into
fabricated `SocialPost` rows -- doing so would feed `unique_authors`,
`bot_risk`, `duplicate_ratio` and `manipulation_risk` (all computed from
post-level identity and text) confident-looking fiction. This provider
returns `domain.AdanosSnapshot` objects through `fetch_snapshots()`, and the
ingest path (`data.ingest.DataIngestor.ingest_adanos`) stores them in their
own `adanos_snapshots` table -- not `symbol_sentiment_daily`'s `"all"`
aggregate that strategies score against, and not `SymbolAttention`/
`ingest_attention` either (that path is hard-coded to an
`apewisdom:<community>` label and has no columns for polarity).

**Two access modes**:

* **Site mode (default, keyless).** One request per enabled feed per
  collection cycle against adanos.org's own public-page proxy endpoints --
  the same JSON its website calls, no key required. This is the
  page-equivalent-cadence courtesy posture ApeWisdom also uses.
* **Official mode**, when both `prefer_official_api = true` and
  `api_key_credential` resolves to a real key: the keyed `api.adanos.org`
  API instead, with an `X-API-Key` header. Gated by a persistent monthly
  budget (default 250 requests/month, 15 reserved) tracked in a small JSON
  state file under `paths.cache_dir/adanos/`. Once remaining budget reaches
  the reserve floor, official mode **fails closed for the rest of the
  calendar month** -- it never falls back to making extra site-proxy calls
  to compensate. The counter self-corrects from the vendor's own
  `X-RateLimit-Remaining-Monthly` response header on every official call.

**Free tier**: 250 requests/month, 100/min burst, 30 days of history
(official API). Site-proxy mode has no documented numeric limit beyond
"page-equivalent cadence" -- one request per feed per collection cycle is
what a logged-out browser tab would generate.

**Polymarket and news official endpoints**:
`api.adanos.org/polymarket/stocks/v1/trending` and
`api.adanos.org/news/stocks/v1/trending` are both inferred from the X/Reddit
URL pattern, not independently confirmed (the news *site* endpoint,
`proxy-news`, was itself confirmed live 2026-08-02). If either 404s at
runtime, only that one feed degrades (reported as `adanos_polymarket` or
`adanos_news` in `degraded_sources`) -- the other feeds are unaffected.

**Licensing** (adanos.org/terms, checked 2026-08-02): commercial use is
permitted subject to the vendor's terms; raw API data may not be
redistributed as a competing service, and rate limits may not be
circumvented. Local personal-research use; no redistribution. Site-proxy
mode is the vendor's own public page endpoint, used at page-equivalent
cadence -- an API key is the guaranteed-compliant path and is preferred
whenever one is configured.

```toml
[adanos]
enabled = true
feed_x = true
feed_reddit = true
feed_polymarket = true
feed_news = true
prefer_official_api = false        # true = use the keyed API for BULK TRENDING when api_key_credential resolves
api_key_credential = "adanos_api_key"
monthly_budget = 250
monthly_reserve = 15
detail_platform_default = "x"      # platform on-demand detail/explain/enrichment use by default
enrich_top_candidates = 3          # 0 disables post-scan enrichment
enrich_enabled = true
```

**Credentials**: none required for the default site mode.
`claudetrade secrets set adanos_api_key` (optional) enables official mode
once `prefer_official_api = true` is also set. **A key also unlocks hybrid
mode's on-demand calls (below) regardless of `prefer_official_api`** -- see
the next subsection.

#### Hybrid mode / spending the free tier

The owner's requirement this feature exists to satisfy, verbatim: *"I want
to utilize the free tier and then use the official api ALONG with the site
mode. I'm using the free tier API which would get used up quickly."* Two
rules follow from that, and the code enforces both:

1. **Bulk trending collection never spends the free tier unless you ask it
   to.** `fetch_snapshots` (what a data refresh calls) stays on the keyless
   site endpoints -- which serve the exact same trending rows -- unless
   `prefer_official_api = true` is explicitly set. There is no reason to pay
   for data the site proxy already gives away for free.
2. **On-demand, per-ticker calls are what the free tier is FOR.** The moment
   `api_key_credential` resolves to a real key -- independent of
   `prefer_official_api` -- `AdanosProvider` exposes two additional calls
   that only make sense with a key:
   - `fetch_stock_detail(ticker, platform)` -- full per-ticker detail (daily
     trend, sentiment breakdown, top mentions/authors), surfaced as the MCP
     tool `get_adanos_detail`.
   - `fetch_explain(ticker, platform)` -- the vendor's AI trend explanation
     (cached 6h server-side), surfaced as `get_adanos_explain`.

   Both spend one request from the same `monthly_budget`/`monthly_reserve`
   pool trending's official mode uses, and both are ALWAYS budget-guarded --
   they return a structured `{"accepted": false, "reason": ...}` refusal
   (never an unmetered fallback) once remaining budget reaches
   `monthly_reserve`, naming the reset date. `AdanosProvider.budget_status()`
   (surfaced free, read-only, via the `get_adanos_budget` MCP tool) reports
   used/remaining/reserve/month at any time.

**The one automatic consumer of this budget: bounded top-candidate
enrichment.** After a scan completes, if `enrich_enabled` is true and a key
resolves, the pipeline fetches ONE `fetch_stock_detail` call (on
`detail_platform_default`) for each of the session's top
`enrich_top_candidates` distinct, best-scoring symbols
(`pipeline.Pipeline._enrich_adanos_top_candidates` ->
`AdanosProvider.enrich_top_candidates`). Results are cached to
`cache_dir/adanos/detail/{symbol}-{session}.json` -- a re-scan of the same
session, or a later `get_adanos_detail` call for the same symbol, is served
from that cache with `from_cache: true` and spends nothing. Enrichment is
wrapped end to end and can never fail a scan: a per-symbol error (network,
vendor, disk) is logged at INFO and skipped, and the budget guard stops
early (not one-by-one) once the reserve floor is hit.

**Budget arithmetic at the defaults** (`monthly_budget = 250`,
`monthly_reserve = 15`, `enrich_top_candidates = 3`): 3 candidates/session x
~22 trading sessions/month =~ 66 official calls/month for enrichment alone,
comfortably under `monthly_budget - monthly_reserve` = 235, leaving roughly
169 requests/month of headroom for interactive `get_adanos_detail`/
`get_adanos_explain` calls (e.g. via an MCP client) in the same month.
Raise/lower `enrich_top_candidates` (0-10) to trade automatic coverage
against that headroom; set `enrich_enabled = false` to keep the key for
purely interactive use.

**Surfacing**: not currently wired into `get_trending`'s `source` option --
that function's query is built directly against `symbol_sentiment_daily`'s
columns (`post_count`, `bull_bear_ratio`), which `adanos_snapshots`'
per-platform shape does not share. Adanos data is visible today through
`claudetrade probe`, provider status, and direct queries against
`adanos_snapshots`; ranking it alongside ApeWisdom/local mentions in
`get_trending` (and any deeper fusion into scoring) is a later, deliberate
change, not a side effect of adding the provider.

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
| `https://www.prnewswire.com/rss/financial-services-latest-news-list.rss` | Wire service; publishes per-category RSS specifically for syndication -- this is its own advertised distribution channel, not a side effect of its website. |
| `https://feeds.npr.org/1006/rss.xml` | Public broadcaster; publishes per-section RSS (this one is the Business section) via a documented feeds directory. |

**Honest limitation**: this package was built and tested against recorded
fixture XML (`tests/fixtures/news_rss/`) in an egress-blocked environment --
none of the above URLs were reached at runtime while writing it. Operators
should confirm each feed still resolves and still serves the expected
format (`claudetrade probe`, or simply fetching the URL) before relying on it,
and are free to edit `feed_urls` to any other feed their organisation is
comfortable treating as a syndication channel.

**Candidate feeds considered for this expansion but NOT added (unverifiable
from this sandbox)**: ADR-0008's source expansion asked for MarketWatch,
Yahoo Finance and Nasdaq feeds to be added *if* each could be confirmed to
publish a documented public RSS feed. This sandbox's egress is fully blocked
at the proxy layer (every `WebFetch` attempt returned HTTP 403, including
against `example.com` as a control and against `ir.nasdaq.com`, ruling out a
per-host block rather than a general outage; `curl` through the configured
proxy failed identically with `CONNECT tunnel failed, response 403` for
every host tried, including hosts already in the default list above). Web
search (a separate path, not subject to the same block) surfaced plausible
candidate URLs, but none from the publisher's own first-party documentation
page reachable from here, and search results even flagged uncertainty about
whether MarketWatch's own classic feed still functions
([Feedspot's MarketWatch feed roundup](https://rss.feedspot.com/marketwatch_rss_feeds/)
pointed to a Dow Jones-hosted redirect rather than a stable, documented
first-party URL). Per the instruction to omit anything unverifiable rather
than assert it, none of the three were added:

| Candidate | Why it was considered | Why it was not added |
| --- | --- | --- |
| MarketWatch top stories | Historically published via a Dow Jones-hosted RSS feed | Current, stable first-party URL not confirmable from this sandbox; third-party aggregators disagree on the live URL |
| Yahoo Finance news | `feeds.finance.yahoo.com/rss/2.0/headline` is a commonly cited endpoint | Could not confirm it is still live/first-party-documented, and it also appears to require a stock-symbol query parameter rather than serving a general news feed, which doesn't fit this list's "top stories" shape |
| Nasdaq original content | `nasdaq.com/feed/rssoutbound?category=Original` is a commonly cited endpoint; Nasdaq's own IR site (`ir.nasdaq.com/tools/rss-feeds`) documents *some* RSS feeds | Could not reach either page to confirm the `Original`-category endpoint specifically still resolves and serves the expected format |

An operator with real network access can verify any of these (or any other
feed) and add it to `feed_urls` directly -- this list remains fully
operator-editable; nothing here requires a code change to extend.

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

**A note on credibility weighting (fixed)**: `SocialPost` engagement fields
(`score`/`num_comments`/`num_reposts`/`num_replies`) and author metrics
(`author_age_days`/`author_karma`/`author_followers`) are structurally absent
for a wire story -- there is no vote count or account history to report, so
all of these are `0`/`None`. This used to mean `sentiment.aggregation
._credibility_score` scored absent metrics as their zero-value floor
(age/karma/followers component all `0.0`), and `engagement_weighted` scaled
by `log1p(engagement)`, also `0` for a post with no engagement counts at
all -- so a news post contributed **zero** weight to `engagement_weighted`
and `credibility_weighted`, identical to a brand-new, karma-less throwaway
account, because "no metrics reported" and "worst possible metrics" were not
distinguished by that scoring function.

Both scoring functions now distinguish the two cases. `_credibility_score`
treats a post whose author fields are *all* `None` as reporting no metrics
at all and assigns it a per-source baseline instead
(`SentimentConfig.credibility_baseline_by_source`, defaulting to `0.6` for
`news` and `0.3` for `reddit`/`x` -- a post with *some* metrics present, even
a metric explicitly reported as zero, is real information and still uses the
original computed score, never the baseline). The engagement-weighted
average now gives a `SocialSource.NEWS` post a neutral, modest-engagement
weight (`log1p(1.0) == 1.0`, scaled by the usual time decay) rather than
`0`; this is gated on the post's *source*, not on its engagement count being
zero, so a genuinely ignored Reddit/X post still correctly weighs `~0`. See
`sentiment/aggregation.py::_credibility_score` / `_engagement_weight` and
`tests/test_sentiment_aggregation.py` for the full behaviour and rationale.

**Limitations**:

- No engagement signal exists for a wire story (see credibility note above);
  it now carries a configurable neutral weight instead of a hard zero
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

**Full setup walkthrough (key creation, cost caps, privacy, what AI does and
does not influence): see [`docs/ai-setup.md`](ai-setup.md).** This section
is the provider-adapter reference; that guide is the operator-facing
how-to, including the Configuration screen's "AI Analysis" section and its
Test button.

AI is an **opt-in ensemble adjunct** for sentiment classification, never
the decision-maker -- `sentiment.classifiers.RuleSentimentClassifier` is
the mandatory, always-on rule-based floor regardless of `ai.provider`.

### None (Default, Rules-Based)

No external AI calls. Sentiment is computed deterministically using rule-based classifiers.

**Configuration**:

```toml
[ai]
provider = "none"
```

**Characteristics**:

- Zero cost
- Fully deterministic
- Faster than LLM-based classification
- Lexicon-based; may miss complex context or sarcasm

### Anthropic Claude

**Module**: `src/claudetrade/providers/ai/anthropic_provider.py`

Uses the **official `anthropic` Python SDK** (an optional dependency --
`pip install claudetrade[anthropic]`, lazy-imported so the base install
never needs it; a clear, actionable error is raised only if you select
`provider = "anthropic"` without it installed). Calls
`client.messages.create(...)` with **structured outputs**
(`output_config.format`, a JSON Schema) so the per-post sentiment JSON
parses reliably; `temperature`/`top_p`/`top_k` are never sent (removed on
current Claude models -- sending them returns an error). Thinking is
explicitly disabled for this call (a short, bounded classification task
does not benefit from it). Typed SDK exceptions
(`anthropic.RateLimitError`, `anthropic.APIStatusError`,
`anthropic.APIConnectionError`) are all mapped into the same
degrade-to-rules contract every other failure mode uses -- this module
never raises into the pipeline.

**Setup**:

1. Go to **platform.claude.com** (formerly console.anthropic.com)
2. Create an account (or sign in) and open **API Keys**
3. Create a new key -- it starts with `sk-ant-`
4. Paste it into the Configuration screen's "Anthropic (Claude) API key"
   field, or store it via the credential store (below)

**Configuration**:

```toml
[ai]
provider = "anthropic"
model = ""                                # blank = "claude-opus-5" (the built-in default)
anthropic_api_key_credential = "anthropic_api_key"
max_output_tokens = 1024
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

**Model choice**: the built-in default is `claude-opus-5` ($5.00/$25.00 per
million input/output tokens). `claude-haiku-4-5` ($1.00/$5.00 per million
tokens) is the economical choice for high-volume per-post classification --
set `model = "claude-haiku-4-5"` explicitly. This tradeoff is an operator
decision this module does not make for you.

**Cost Tracking**:

```toml
input_cost_per_mtok_usd = 5.0     # Claude Opus 5's published rate; update to match your model
output_cost_per_mtok_usd = 25.0   # e.g. 1.0 / 5.0 for claude-haiku-4-5
daily_cost_limit_usd = 5.0
```

The system tracks costs locally and refuses requests once the daily limit is exceeded.

**Limitations**:

- Requires the `anthropic` package installed (`pip install
  claudetrade[anthropic]`) and an API key/account
- Token usage is billable
- No real-time rate limiting on the server; respect the configured limits
- LLM outputs are non-deterministic

**Licence**: Subject to Anthropic's API terms and privacy policy.

### OpenAI

**Module**: `src/claudetrade/providers/ai/openai_provider.py`

Uses the **official `openai` Python SDK** (an optional dependency -- `pip
install claudetrade[openai]`, lazy-imported the same way as the Anthropic
adapter). Calls `client.chat.completions.create(...)` with JSON-schema
structured output mode against the same sentiment schema the Anthropic
adapter uses. The same typed-exception degrade contract applies
(`openai.RateLimitError`, `openai.APIStatusError`,
`openai.APIConnectionError`).

**Setup**:

1. Go to **platform.openai.com**
2. Create an account (or sign in), add a payment method, and open **API keys**
3. Create a new key -- it starts with `sk-`
4. Paste it into the Configuration screen's "OpenAI (ChatGPT) API key"
   field, or store it via the credential store (below)

**Configuration**:

```toml
[ai]
provider = "openai"
model = ""                                # blank = the built-in default; verify at platform.openai.com
openai_api_key_credential = "openai_api_key"
max_output_tokens = 1024
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

OpenAI's model lineup and pricing move faster than this document can track
-- check https://platform.openai.com before relying on the built-in default
model name, and update the per-token costs to match whichever model you
configure:

```toml
input_cost_per_mtok_usd = 0.15    # example only -- check current pricing
output_cost_per_mtok_usd = 0.60
daily_cost_limit_usd = 5.0
```

**Limitations**:

- Requires the `openai` package installed (`pip install
  claudetrade[openai]`) and an API key/account with a payment method
- Token usage is billable
- LLM outputs are non-deterministic

**Licence**: Subject to OpenAI's API terms and privacy policy.

---

## Provider Selection Advice

### For Backtesting and Development

For an explicit offline demo, select **synthetic** providers (not the live default):

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
provider = "none"
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
provider = "none"
```

This lets you test with real bars but still runs offline.

### For Live Research (Small Budget)

Use the **default chain (TipRanks + Yahoo) + Reddit + rule-based AI**:

```toml
[market_data]
provider = "tipranks"
fallbacks = ["yahoo", "csv"]

[earnings]
provider = "tipranks"

[reddit]
enabled = true
provider = "reddit"

[x]
enabled = false

[ai]
provider = "none"
```

This costs nothing and provides real data + social sentiment.

### For Comprehensive Research

Enable all live sources:

```toml
[market_data]
provider = "tipranks"
fallbacks = ["yahoo", "csv"]

[earnings]
provider = "tipranks"

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

### RECOMMENDED: fast refreshes and real price history (free)

The single highest-value change for anyone running this on real data. It
costs one free API key and one overnight backfill, and it is what turns a
scanner that rejects every symbol for `insufficient_history` into a working
one.

```toml
[market_data]
provider = "polygon"                        # bars: ONE grouped call per trading date
fallbacks = ["tipranks", "yahoo", "csv"]    # refdata, caps, earnings, per-symbol gaps

[earnings]
provider = "tipranks"

[polygon]
rate_limit_per_minute = 5                   # free tier; raise to match a paid plan
```

```bash
# 1. Free key from polygon.io, stored in the OS credential store
claudetrade secrets set polygon_api_key      # or: export POLYGON_API_KEY="..."

# 2. One-time history load: ~500 calls, ~1.7 hours at the free tier's 5/min.
#    Newest-first, so the scanner works long before it finishes; Ctrl-C safe.
claudetrade db backfill --years 2

# 3. From here on, a daily refresh is ONE grouped call
claudetrade refresh
claudetrade scan
```

Cost estimate: free. The Polygon free tier's EOD delay and 5 calls/minute
are both fine for a session-based scanner that refreshes after the close.
See [Polygon.io](#polygonio-recommended-primary-for-bars-online-free-tier)
for the full details, and `claudetrade db fetch-health` for the per-symbol
fetch quarantine that keeps dead tickers from burning calls on every
refresh.

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
- **AI providers** (`ai.provider != "none"`): the sanitised post text and target symbol -- never usernames, author ids, karma, follower counts, or post history -- are sent to the configured provider's API (Anthropic or OpenAI). See [docs/ai-setup.md](ai-setup.md) for the full privacy note. With the default `ai.provider = "none"`, nothing is ever sent to an external AI provider.
- **Audit log**: Credential access is logged but not the value.
- **Database**: Market and earnings data is retained indefinitely for backtesting.

See [docs/security-and-privacy.md](security-and-privacy.md) for full privacy considerations.
