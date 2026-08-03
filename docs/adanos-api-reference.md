# Adanos API Reference (scraped 2026-08-02; site-proxy on-demand routes added 2026-08-03)

Everything ClaudeTrade knows about the Adanos (`adanos.org`) sentiment API:
the keyless site-level endpoints its own web pages call, and the official
authenticated API. Compiled from the vendor's docs (`api.adanos.org/docs`,
OpenAPI spec, `llms.txt`), product pages, pricing/terms/changelog pages, and
live endpoint captures made on 2026-08-02 (trending) and 2026-08-03
(on-demand per-ticker/service routes -- owner-verified plus a follow-up
coordinator probe against TSLA). The connector consuming this is
`src/claudetrade/providers/social/adanos.py`; operator-facing setup notes
live in `docs/api-providers.md`. Machine-readable summary of the official
API: [`https://api.adanos.org/llms.txt`](https://api.adanos.org/llms.txt).

**Five platforms** (each with near-identical endpoint families):

| Platform | Official base path | Site proxy path | Activity metric | Coverage metric | Engagement metric |
|---|---|---|---|---|---|
| Reddit Stocks | `/reddit/stocks/v1` | `/api/proxy` | `mentions` | `subreddit_count`, `unique_posts` | `total_upvotes` |
| X/Twitter Stocks | `/x/stocks/v1` | `/api/proxy-x` | `mentions` | `unique_tweets` | `total_upvotes` (likes) |
| News Stocks | `/news/stocks/v1` | `/api/proxy-news` | `mentions` | `source_count` | none |
| Polymarket Stocks | `/polymarket/stocks/v1` | `/api/proxy-polymarket` | `trade_count` | `market_count`, `unique_traders` | `total_liquidity` |
| Reddit Crypto | `/reddit/crypto/v1` | (not used by ClaudeTrade) | `mentions` | — | — |

## 1. Site-level public endpoints (keyless)

The vendor's own pages (`/x-top-100-stocks`, `/reddit-top-100-stocks`,
`/polymarket-stock-sentiment`, news pages, and per-ticker detail pages)
fetch JSON from `https://adanos.org/api/<proxy>/...` with **no
authentication**. This is a page-equivalent mirror of the official API, not
a separate product -- the base-path segment (`proxy-x`, `proxy`,
`proxy-polymarket`, `proxy-news`) is the same one `_SITE_PATHS` in the
connector uses for every route family below, i.e. the suffix parity is
exact: whatever path a base supports for `/trending` it also supports (or
doesn't) for `/stock`, `/stock/.../explain` and `/market-sentiment`.

### 1a. Trending (verified live 2026-08-02, all four bases)

All returning HTTP 200 JSON arrays:

```
GET https://adanos.org/api/proxy-x/trending?limit=100&from=YYYY-MM-DD&to=YYYY-MM-DD
GET https://adanos.org/api/proxy/trending?limit=100&from=...&to=...          (Reddit)
GET https://adanos.org/api/proxy-news/trending?limit=3&from=...&to=...
GET https://adanos.org/api/proxy-polymarket/trending?limit=20&from=...&to=...
GET https://adanos.org/api/proxy-x/trending/sectors?limit=5&from=...&to=...
GET https://adanos.org/api/proxy-x/trending/countries?limit=5&from=...&to=...
GET https://adanos.org/api/proxy-polymarket/stats
```

Row shapes (observed):

- **X**: `ticker, company_name, buzz_score (0-100), trend
  ("rising"|"falling"|"stable"), mentions, sentiment_score (-1..1, nullable),
  bullish_pct, bearish_pct, total_upvotes, unique_tweets,
  trend_history (7 floats, oldest→newest)`
- **Reddit**: as X but `unique_posts`, `subreddit_count` instead of
  `unique_tweets`.
- **News**: as X but `source_count` (distinct outlets) and **no engagement
  field**.
- **Polymarket**: `ticker, company_name, buzz_score, trend, trade_count,
  market_count, current_market_count, unique_traders, sentiment_score,
  bullish_pct, bearish_pct, total_liquidity, trend_history`.

### 1b. Per-ticker/service on-demand routes (verified live 2026-08-03)

Confirmed by both the owner and an independent coordinator probe (TSLA,
against every base) that the site proxy mirrors three more route families,
not just `/trending`:

```
GET https://adanos.org/api/<proxy_base>/stock/<TICKER>?days=N
GET https://adanos.org/api/<proxy_base>/stock/<TICKER>/explain
GET https://adanos.org/api/<proxy_base>/market-sentiment
```

**Verified/unverified per base** (suffix parity is NOT total -- explain has
one confirmed gap):

| Base (`proxy_base`) | `/stock/<T>` | `/stock/<T>/explain` | `/market-sentiment` |
|---|---|---|---|
| `proxy-x` | Verified | Verified | Verified |
| `proxy` (Reddit) | Verified | Verified (owner) | Verified |
| `proxy-news` | Verified | Verified (owner) | Verified |
| `proxy-polymarket` | Verified | **Confirmed ABSENT** (`{"error": "Not found"}`) -- official API only | Verified |

`/compare` (`?tickers=A,B`) is confirmed **NOT proxied** on any base
(`{"error": "Not found"}`) -- official API only, not wired into the
connector.

**`/stock/<TICKER>` response shape** -- a COMMON header plus per-base
extras (all fields owner+coordinator verified against TSLA/NVDA 2026-08-03):

- Common: `ticker, company_name, found (bool), buzz_score, sentiment_score,
  positive_count, negative_count, neutral_count, trend, bullish_pct,
  bearish_pct, period_days, daily_trend[]` -- each `daily_trend` entry is
  `{date, <activity>, sentiment_score, buzz_score, bullish_pct,
  bearish_pct}`, where `<activity>` is the per-base count field below.
  **`daily_trend`'s length is NOT guaranteed to be 7** -- a quiet day can be
  omitted (observed on the News base: only 6 entries in a 7-day window).
- `proxy-x` extras: `mentions, unique_tweets, total_upvotes`; `top_tweets`
  (up to 10, `{text_snippet, sentiment_score, ...}`); the owner separately
  observed `top_authors` (up to 5) which the coordinator's capture did not
  -- tolerate both being present or absent.
- `proxy` (Reddit) extras: `mentions, unique_posts, subreddit_count,
  total_upvotes`; `top_subreddits` (up to 5, `{subreddit, mentions,
  sentiment_score, buzz_score, count}`).
- `proxy-news` extras: `mentions, source_count` (**no engagement field**);
  `top_sources` (up to 5, `{source, mentions, sentiment_score, buzz_score,
  count}`); `top_mentions` (`{text_snippet, sentiment_score, ...}`).
- `proxy-polymarket` extras: `trade_count, market_count,
  current_market_count, unique_traders, total_liquidity`; `daily_trend`'s
  activity key is `trade_count` here instead of `mentions`.

**`/stock/<TICKER>/explain` response shape**: `{ticker, company_name,
explanation, cached (bool), generated_at (ISO 8601), model}` -- `model`
observed as `"meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"`.

**`/market-sentiment` response shape** -- same common-header-plus-extras
pattern as `/stock`, but service-level (no `ticker`/`found`), with
`trend_history` (7 entries) and `drivers` (top 5 momentum tickers,
`{ticker, buzz_score, sentiment_score}` plus the base's activity field)
replacing `daily_trend`: `{buzz_score, trend, active_tickers,
sentiment_score, positive_count, negative_count, neutral_count,
bullish_pct, bearish_pct, trend_history[7], drivers[5]}` plus per-base
activity fields (`proxy-x`: `mentions, unique_tweets, unique_authors,
total_upvotes`; `proxy`: `mentions, unique_posts, subreddit_count,
total_upvotes`; `proxy-news`: `mentions, unique_articles, source_count`;
`proxy-polymarket`: `trade_count, market_count, current_market_count,
unique_traders, total_liquidity`).

**Probe pattern / found-flag semantics** -- three response shapes matter,
and they are NOT interchangeable (see `AdanosProvider._is_structured_vendor_
answer` in the connector):

1. `{"found": false, ...}` (HTTP 200) -- the vendor tracks this ticker but
   has no data for it in the requested window. A normal, structured
   "no data" answer.
2. `{"detail": {"error_code": "unsupported_ticker", "message": "..."}}`
   (observed under HTTP 404, but the connector checks the body regardless of
   status) -- the vendor does not track this ticker at all. A different,
   equally normal "unsupported ticker" answer.
3. `{"error": "...", ...}` (HTTP 200 *or* a non-2xx status, and matching
   neither shape above) -- the ROUTE ITSELF does not exist on this proxy
   base (e.g. `/compare`, or `/explain` on `proxy-polymarket`).
   Endpoint-absent, not data.

Only the third shape should be treated as a failure worth falling back from;
the first two are genuine, final answers.

**Caveats** (undocumented behaviour, operators should not over-rely on it):
this is an undocumented mirror of the vendor's own page traffic, not a
published product -- it may be IP rate-limited beyond what any response
header discloses, and its shape can change without notice (unlike the
official API's versioned changelog). The connector keeps the existing
courtesy rate limiter, treats any drift (unexpected shape, unrecognised
error body) as a rung failure rather than guessing, and never hammers it
harder than page-equivalent cadence.

### 1c. Usage posture

These are the vendor's own page endpoints, used at page-equivalent cadence
(one trending request per feed per hourly collection cycle; on-demand calls
at whatever cadence the caller requests them, courtesy-rate-limited same as
everything else). The terms prohibit circumventing rate limits; an API key
is the guaranteed-compliant path, used automatically as a fallback whenever
the site rung fails and one is configured (`adanos_api_key`), or as the
primary path for both trending and on-demand calls when
`adanos.prefer_official_api = true`.

## 2. Official API

**Base**: `https://api.adanos.org` · **Auth**: `X-API-Key: sk_live_...`
header on every request. Docs: `https://api.adanos.org/docs` (Scalar UI over
OpenAPI). ETag/`304 Not Modified` supported on trending and detail
endpoints since v1.32.0 — cache-validate to save quota.

### Endpoint family (per platform, `{base}/{platform}/v1/...`)

| Endpoint | Notes |
|---|---|
| `GET /trending` | Ranked by buzz score. Params: `from`, `to` (YYYY-MM-DD, UTC, inclusive), `limit` (1-100, default 20), `offset`, `type` (stock/etf/all). `days` is deprecated. |
| `GET /stock/{ticker}` | Detail: daily trend array, sentiment breakdown, top mentions/authors/subreddits/tweets (platform-appropriate). Polymarket adds a `pulse` mood block. Wired as `AdanosProvider.fetch_stock_detail` / MCP `get_adanos_detail` -- this is now the FALLBACK rung of a site-first ladder (revised 2026-08-03, see `docs/api-providers.md`'s "Hybrid mode / spending the free tier"), tried only when the mirrored site-proxy `/stock/{ticker}` route (section 1b above) fails. |
| `GET /stock/{ticker}/explain` | AI trend explanation (llama-3.1-8b-instant), cached 6h, served with `cached` flag + `generated_at`. Wired as `AdanosProvider.fetch_explain` / MCP `get_adanos_explain`; same site-first-ladder posture, except on Polymarket, where the connector skips the site rung entirely (confirmed absent, see section 1b) and goes straight here. |
| `GET /stock/{ticker}/mentions` | **Professional tier only.** Raw post/tweet/market rows, `limit` 1-100, `offset`, `include_inherited` (Reddit). Not wired -- no site-proxy equivalent, Professional-tier only. |
| `GET /trending/sectors` | Sector aggregation, top 5 tickers each, has `trend_history`. Not wired. |
| `GET /trending/countries` | Country aggregation, same shape. Not wired. |
| `GET /search?q=` | Ticker/company-name search; FIXED 7-UTC-day window (no date params since v1.38.0), `limit` 1-50. Not wired. |
| `GET /compare?tickers=A,B` | Up to 10 tickers, trending-shaped rows; structured 400 on limit violation. **Confirmed NOT proxied by the site** (`{"error": "Not found"}`, both owner and coordinator probes, 2026-08-03) -- official API only, and not wired into the connector either way. |
| `GET /market-sentiment` | Service-level snapshot: active tickers, top momentum drivers, overall trend. Wired as `AdanosProvider.fetch_market_sentiment` / MCP `get_adanos_market_sentiment` -- same site-first ladder as `/stock`, verified mirrored on all four site-proxy bases (section 1b above). |
| `GET /stats` | Dataset coverage metrics, standardized fields across platforms (v1.36.0). Not wired. |

Professional tier also gets `POST /sentiment/v1/analyze` — direct text
sentiment analysis (added v1.41.0).

### Semantics worth knowing (from the changelog)

- `buzz_score` at top level covers the FULL requested period;
  `trend_history[-1]` is the latest single day — they are NOT the same
  number (v1.48.1, breaking).
- `trend` compares the current 3 UTC days against the previous 3 UTC days
  (v1.33.0).
- Reddit `mentions` includes thread-context *inherited* mentions (v1.35.0);
  request explicit-only via the Professional `/mentions` endpoint.
- Polymarket `sentiment_score` prefers YES-token orderbook midpoints
  (v1.44.0); null for zero-trade markets. `unique_traders` may be null when
  data does not cover the window (v1.48.0).
- Null field values mean "no signal", not schema drift. Empty result sets
  return 200 with empty arrays; unsupported assets return 404.
- Deprecated aliases (`total_mentions`, `sentiment`, `upvotes`) were REMOVED
  in v1.25.0 — parse only the canonical names above.
- The vendor ships breaking changes roughly monthly (see changelog cadence);
  the connector fails closed on schema drift rather than guessing.

### Rate limits and headers

| Tier | Price | Requests/month | Burst/min | History | Commercial use |
|---|---|---|---|---|---|
| Free | $0 | 250 | 100 | 30 days | **No** |
| Hobby | $29/mo | 250,000 | 1,000 | 90 days | **No** |
| Professional | $299/mo | 2,500,000 | 1,000 | 365 days | Yes (+ raw mentions, text API, SLA) |
| Enterprise | custom | custom | custom | custom | Yes |

Every authenticated response carries:
`X-RateLimit-Limit-Monthly`, `X-RateLimit-Remaining-Monthly`,
`X-RateLimit-Used-Monthly`, `X-RateLimit-Reset-Monthly` (ISO 8601),
`X-RateLimit-Limit-Burst`, `X-RateLimit-Remaining-Burst`,
`X-RateLimit-Reset-Burst`, `X-Account-Type` (free/hobby/professional/premium).

**Quota windows reset per ACCOUNT, not per calendar month** (v1.41.3).
ClaudeTrade's monthly budget store keys its counter by calendar month but
self-corrects from `X-RateLimit-Remaining-Monthly` on every response, so
the drift is bounded to one cycle; the header is authoritative.

### Errors

- `401` — missing / invalid / revoked key (distinct messages for each).
- `403` — historical window exceeds the tier's depth.
- `422` — parameter validation, unsupported `source`, period availability.
- `429` — burst or monthly cap exceeded.
- `503` / `504` — database unavailable / analytics deadline exceeded.

The official API also has a public, no-auth `GET /health` (documented in
`llms.txt`) -- not wired into the connector, noted here only for operators
who want a manual liveness check independent of a key.

## 3. Registration and key management

Register at `https://adanos.org/register`: name, email, optional company,
intended use case, ToS/privacy agreement. Keys are delivered by a one-time
email link (allow up to 5 minutes; check spam), not shown inline —
`sk_live_` prefix. Recovery flow exists ("Already have a key? Recover").
Store the key with `claudetrade secrets set adanos_api_key` (never in
config files); the Configuration page has a field for it.

## 4. Terms summary (adanos.org/terms, 2026-08-02)

Commercial use permitted only on Professional+ (Free/Hobby are
non-commercial). No redistribution of raw API data as a competing service;
derived analysis belongs to the vendor; underlying social content stays
subject to platform terms. No circumventing rate limits or multi-account
automation. ClaudeTrade's use — one owner's local research tool — sits
comfortably inside the free tier's terms in site mode; a Professional key is
required if any output is ever commercialized.

## 5. Official SDKs (not used by ClaudeTrade)

`pip install adanos` · `npm install finance-sentiment` ·
`composer require adanos/adanos-php` · `pip install adanos-cli`.
ClaudeTrade uses its own thin httpx adapter instead, for the same reason as
every other provider: fail-closed error taxonomy, rate limiting and budget
enforcement under this codebase's control.
