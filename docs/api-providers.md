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

**Configuration**:

```toml
[market_data]
provider = "tipranks"          # the default
fallbacks = ["yahoo", "csv"]

[earnings]
provider = "tipranks"          # also the default

[tipranks]
rate_limit_per_minute = 30
request_timeout_s = 20.0
cache_ttl_trading_days = 1     # response cache under paths.cache_dir/tipranks/
use_getquotes_batch = false    # optional Canadian cap batching, see below
```

**Credentials**: None required (no authentication).

**ToS posture (ADR-0008 Decision 1) -- read before relying on this in
production**: this is an unauthenticated partner-widget endpoint, **not** a
published, contracted API. TipRanks could restrict, reshape, rate-limit or
withdraw it at any time with no notice and no deprecation window. This is
the same posture this codebase already applies to stooq's free CSV endpoint,
Yahoo's undocumented chart JSON, and Stocktwits' keyless stream: personal/
research use only, a conservative self-imposed rate limit (default
30/minute), and a **fail-closed** response to anything that looks like a
block or an unexpected shape -- see the fail-closed rules below. Nothing
here bypasses authentication, defeats a paywall, or solves a challenge.

**Symbol notation**: a bare US ticker is passed through unchanged (`AAPL`).
A Canadian (TSX/TSXV) one is rewritten to `TSE:<SYMBOL-WITH-DOTS>` -- this
codebase's hyphenated share-class convention (`TECK-B`) becomes TipRanks'
dotted one (`TSE:TECK.B`) -- confirmed against a real Canadian-listing
fixture (`tests/fixtures/tipranks/dataForTicker_TECK_B.json`), whose
`overview.ticker` echoes exactly that form back. This is a *different*
convention from both stooq's (`teck-b.to`) and Yahoo's (`TECK-B.TO`) --
each adapter owns its own mapping table.

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

**Market caps**: prefers `overview.marketCapUSD`; falls back to
`overview.marketCap` as-is with **no currency gating** -- the >= $1B
universe floor is currency-agnostic by explicit owner decision (a nominal
$1B in either USD or CAD clears it). Nested blocks that also happen to carry
a `marketCap` field (e.g. `portfolioHoldingData.nextDividendDate.marketCap`
in the Canadian fixture, a *different*, CAD-only figure from the top-level
cap) are never used as a cap source.

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

**GetQuotes batching (optional, off by default)**: TipRanks also exposes a
CIBC-integration batch endpoint,
`https://marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=TSE:A,TSE:B,...`,
that can reduce Canadian cap enrichment to a handful of calls instead of one
per symbol. A real probe (7 mixed US/TSX tickers) confirmed it works and
that requested tickers are echoed back exactly as sent, but this repository
still has no committed fixture of the exact response body, so the parser
(`providers.market.tipranks._parse_getquotes_response`) stays defensive and
the feature stays behind `tipranks.use_getquotes_batch = false`.
**Confirmed currency trap**: GetQuotes' own `marketCap` field is in the
listing's *local* currency, not USD (`TSE:TECK.B`'s GetQuotes `marketCap` is
~40.6B CAD with an `exchangeRate` of ~0.712, while `dataForTicker`'s
`marketCapUSD` is the already-converted ~28.9B USD figure -- the two are
consistent once converted). The parser therefore only trusts a GetQuotes cap
via `marketCapUSD` when present, or `marketCap * exchangeRate` when both
fields are present and positive; a bare, un-converted `marketCap` is never
used for a non-USD listing. Canadian cap coverage never depends on this
optimisation succeeding -- `dataForTicker` (with the `TSE:SYMBOL` notation)
is the primary path for every symbol regardless.

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
`>= universe.min_market_cap_usd` (default $1B) -- see
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
min_market_cap_usd = 1000000000              # ADR-0008 Decision 3 runtime floor, default $1B
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
except `tipranks`, simply contributes nothing rather than erroring). The
resolved figure is stored on `Security.market_cap_usd`. The enriched floor is
also applied **before** price, corporate-action, earnings and sentiment
requests, so a company that currently resolves below $1B does not consume a
bars-history request. The benchmark is retained because regime and
relative-strength calculations require it even though it is an ETF.

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
requests with the descriptive app UA. **Live-probe evidence (2026-07-30)**
showed Reddit's public JSON endpoint 403ing every non-browser client tested
(both a generic UA and this codebase's descriptive app UA) while a
logged-in Chrome tab got a clean 200 -- the gate is the session cookie, not
the User-Agent string.

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
   **Application**).
3. Find the cookie named `reddit_session`; copy its **Value**.
4. Store it via the credential store, never in `config.toml`:

```bash
claudetrade secrets set reddit_session_cookie
# or, environment variable:
export CLAUDETRADE_SECRET_REDDIT_SESSION_COOKIE="..."
```

No separate enable flag is needed -- like the password and client-credentials
grants above, this mode is picked up automatically the moment the cookie
resolves and the password-grant credentials do not (see the mode order
above). `reddit.session_rate_limit_per_minute` (default 30) governs its
pace, same conservative, human-scale budget as the public-JSON fallback.

**Fail-closed behaviour (cookie-session and public-JSON modes, ADR-0008
Decision 1)**: any HTTP 401/403 (a 401/403 here usually means the pasted
cookie has expired or been logged out -- re-export a fresh one), a non-JSON
response body, or any other unexpected/non-2xx response immediately disables
the source **for the rest of that fetch cycle** -- no retry loop, no
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
  cookie will eventually expire and need re-exporting
- The public-JSON fallback carries no delivery guarantee at all -- it can be
  throttled or blocked by Reddit at any time with no notice, by design

**Licence**: Reddit data is subject to Reddit's API terms and user agreement. Commercial use of aggregated social data may require permission. The cookie-session and public-JSON paths are additionally ToS-gray for automated use -- see above.

### X/Twitter (Paid API v2, or an opt-in cookie-session mode)

**Module**: `src/claudetrade/providers/social/x_provider.py`

Two independent live paths, tried in this order at construction time:

1. **Official API v2** (`bearer_credential`). Requires a paid tier for
   meaningful search volume. Always preferred when configured -- ADR-0008
   Decision 1 requires the official API remain first-choice.
2. **Cookie-session mode** (`x.session_enabled = true`), only reached when
   no bearer token is configured. See below.

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

#### X cookie-session mode (opt-in, off by default -- ADR-0008 Decision 1)

**PLAIN ACCOUNT-RISK STATEMENT**: this mode automates the owner's own
logged-in x.com session against the internal, unversioned GraphQL endpoints
the web client itself uses. **Doing this violates X's Terms of Service and
can lead to suspension of that X account.** This application never bundles
or defaults these credentials, never solves a CAPTCHA or challenge, and
never rotates a fingerprint or proxy to work around a block -- it disables
the source for the rest of the cycle instead. The owner accepts this risk
for their own account; this mode is off by default
(`x.session_enabled = false`) and must be explicitly turned on.

**How to export your session cookies** (Chrome/Edge/Firefox devtools):

1. Log in to x.com in your browser as normal.
2. Open devtools (F12) -> **Application** tab (Chrome/Edge) or **Storage**
   tab (Firefox) -> **Cookies** -> `https://x.com`.
3. Find the cookie named `auth_token`; copy its **Value**.
4. Find the cookie named `ct0` (the CSRF token cookie); copy its **Value**.
5. Store both via the credential store, never in `config.toml`:

```bash
claudetrade secrets set x_auth_token
claudetrade secrets set x_ct0
# or, environment variables:
export CLAUDETRADE_SECRET_X_AUTH_TOKEN="..."
export CLAUDETRADE_SECRET_X_CT0="..."
```

**Configuration**:

```toml
[x]
session_enabled = true
auth_token_credential = "x_auth_token"
ct0_credential = "x_ct0"
session_symbols = ["AAPL", "MSFT"]     # cashtag-searched; leading $ added automatically
session_max_results_per_query = 40
session_rate_limit_per_minute = 6      # deliberately stricter than the official API default
session_request_timeout_s = 20.0
```

**Endpoint stability (read before relying on this mode)**: the GraphQL
endpoint path and query ID this adapter calls are internal to x.com's web
client and change without notice or a deprecation window -- see the
clearly-marked constants block at the top of `x_provider.py`
(`_SEARCH_GRAPHQL_QUERY_ID` etc.). **This sandbox has no network egress and
could not capture a live, current query ID while building this adapter**;
the shipped constant is illustrative/unverified and will need to be replaced
with a fresh capture from your own browser's devtools (Network tab, filter
`graphql`, log in, run a search, copy the request the client itself makes)
before this mode returns real data. Until then, or whenever x.com changes
its internal API again, session-mode requests fail closed (typically a
404/400 on the stale path, or an unparseable response shape) rather than
guessing -- see the fail-closed behaviour below. Updating the constant is
expected, ordinary maintenance, not a bug fix.

**Fail-closed behaviour (ADR-0008 Decision 1)**: any HTTP 401/403 (expired,
logged-out, or challenged cookies), any non-JSON response, any response that
doesn't match the expected timeline shape (including a changed internal API,
see above), or a 429 immediately disables the source **for the rest of that
fetch cycle** -- no retry loop, no fingerprint/proxy rotation, no CAPTCHA
handling. Re-export fresh cookies (401/403) or wait for the next scheduled
cycle (429) to resume. This is exercised in `tests/test_x_provider.py`.

**Limitations**:

- Off by default; must be explicitly enabled and configured
- Unofficial, unsupported, and can stop working at any time without notice
- No engagement-metrics parity guarantee with the official API's `public_metrics`
- The official API path above remains intact and is always preferred the
  moment a bearer token is configured

### Stocktwits (Live, Keyless, Opt-in)

**Module**: `src/claudetrade/providers/social/stocktwits.py`

Stocktwits' own documented, **keyless** public symbol-stream API
(`api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json`) -- ADR-0008
Decision 1's "official API first-choice" applies in the fullest sense here:
this is not scraping at all, no authentication is bypassed, and no ToS
boundary is tested. Off by default only because the vendor's published
unauthenticated budget (200 requests/hour) is easy to exhaust across a large
universe, not because of any risk to the account or credentials (there are
none).

**Sentiment tags are a prior hint, not ground truth**: a message may carry a
self-declared `entities.sentiment.basic` tag ("Bullish"/"Bearish") the
*author* attached to their own post. This is mapped onto
`SocialPost.sentiment_prior` (`"bullish"` / `"bearish"` / `None`) as a prior
hint only -- the ensemble sentiment classifier still runs on the post's text
unconditionally for every post, Stocktwits included. Treating a self-declared
label as truth would let anyone paint their own post's sentiment without the
classifier ever checking it against the actual content.

**Browser-style request headers (best-effort only)**: **live-probe evidence
(2026-07-30)** showed this endpoint returning HTTP 403 to PowerShell/.NET
clients regardless of User-Agent (both a generic UA and this codebase's
descriptive app UA), but HTTP 200 JSON to a plain browser tab -- consistent
with an edge filter keyed on browser-shaped request headers and/or TLS
fingerprint, not a credential or API-contract check (the API itself remains
keyless and open, per the vendor's own documentation). This adapter
therefore sends a realistic desktop-browser User-Agent plus
`Accept`/`Accept-Language` headers matching what a browser sends, instead of
the descriptive app UA used elsewhere in this codebase. **This is
best-effort only**: a header swap cannot fix a TLS-fingerprint-based block,
since the HTTP client's TLS handshake doesn't look like a browser's no
matter what headers ride on top of it. If the edge still blocks despite the
browser-style headers, the existing fail-closed path below
(`SourceBlockedError` on 401/403/non-JSON) handles it exactly as before, and
`claudetrade diagnostics`/the Diagnostics screen report the source blocked
-- no cookie support is added for this source, unlike Reddit's cookie-session
mode above.

**Configuration**:

```toml
[stocktwits]
enabled = true
watchlist_symbols = ["AAPL", "MSFT", "TSLA"]
max_symbols_per_cycle = 20      # hard budget guard, see below
rate_limit_per_minute = 3       # 180/hour -- a margin below the 200/hour vendor cap
request_timeout_s = 20.0
```

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

**Licence**: Stocktwits' public stream is documented for keyless basic
reads; this adapter performs only that. Commercial or high-volume use may
require Stocktwits' paid API tier and is out of scope here.

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
provider = "null"
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
