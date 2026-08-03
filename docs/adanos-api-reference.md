# Adanos API Reference (scraped 2026-08-02)

Everything ClaudeTrade knows about the Adanos (`adanos.org`) sentiment API:
the keyless site-level endpoints its own web pages call, and the official
authenticated API. Compiled from the vendor's docs (`api.adanos.org/docs`,
OpenAPI spec), product pages, pricing/terms/changelog pages, and live
endpoint captures made on 2026-08-02. The connector consuming this is
`src/claudetrade/providers/social/adanos.py`; operator-facing setup notes
live in `docs/api-providers.md`.

**Five platforms** (each with near-identical endpoint families):

| Platform | Official base path | Site proxy path | Activity metric | Coverage metric | Engagement metric |
|---|---|---|---|---|---|
| Reddit Stocks | `/reddit/stocks/v1` | `/api/proxy` | `mentions` | `subreddit_count`, `unique_posts` | `total_upvotes` |
| X/Twitter Stocks | `/x/stocks/v1` | `/api/proxy-x` | `mentions` | `unique_tweets` | `total_upvotes` (likes) |
| News Stocks | `/news/stocks/v1` | `/api/proxy-news` | `mentions` | `source_count` | none |
| Polymarket Stocks | `/polymarket/stocks/v1` | `/api/proxy-polymarket` | `trade_count` | `market_count`, `unique_traders` | `total_liquidity` |
| Reddit Crypto | `/reddit/crypto/v1` | (not used by ClaudeTrade) | `mentions` | — | — |

## 1. Site-level public endpoints (keyless, verified live)

The vendor's own pages (`/x-top-100-stocks`, `/reddit-top-100-stocks`,
`/polymarket-stock-sentiment`, news pages) fetch JSON from
`https://adanos.org/api/<proxy>/...` with **no authentication**. Observed
live 2026-08-02, all returning HTTP 200 JSON arrays:

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

Usage posture (ClaudeTrade's "site mode"): these are the vendor's own page
endpoints, used at page-equivalent cadence (one request per feed per hourly
collection cycle). The terms prohibit circumventing rate limits; an API key
is the guaranteed-compliant path and the connector prefers it when
configured (`adanos.prefer_official_api` + `adanos_api_key`).

## 2. Official API

**Base**: `https://api.adanos.org` · **Auth**: `X-API-Key: sk_live_...`
header on every request. Docs: `https://api.adanos.org/docs` (Scalar UI over
OpenAPI). ETag/`304 Not Modified` supported on trending and detail
endpoints since v1.32.0 — cache-validate to save quota.

### Endpoint family (per platform, `{base}/{platform}/v1/...`)

| Endpoint | Notes |
|---|---|
| `GET /trending` | Ranked by buzz score. Params: `from`, `to` (YYYY-MM-DD, UTC, inclusive), `limit` (1-100, default 20), `offset`, `type` (stock/etf/all). `days` is deprecated. |
| `GET /stock/{ticker}` | Detail: daily trend array, sentiment breakdown, top mentions/authors/subreddits/tweets (platform-appropriate). Polymarket adds a `pulse` mood block. |
| `GET /stock/{ticker}/explain` | AI trend explanation (llama-3.1-8b-instant), cached 6h, served with `cached` flag + `generated_at`. |
| `GET /stock/{ticker}/mentions` | **Professional tier only.** Raw post/tweet/market rows, `limit` 1-100, `offset`, `include_inherited` (Reddit). |
| `GET /trending/sectors` | Sector aggregation, top 5 tickers each, has `trend_history`. |
| `GET /trending/countries` | Country aggregation, same shape. |
| `GET /search?q=` | Ticker/company-name search; FIXED 7-UTC-day window (no date params since v1.38.0), `limit` 1-50. |
| `GET /compare?tickers=A,B` | Up to 10 tickers, trending-shaped rows; structured 400 on limit violation. |
| `GET /market-sentiment` | Service-level snapshot: active tickers, top momentum drivers, overall trend. |
| `GET /stats` | Dataset coverage metrics, standardized fields across platforms (v1.36.0). |

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
