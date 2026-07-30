"""Typed application configuration.

Configuration comes from three layers, later layers winning:

1. Defaults defined here (safe, conservative, offline-capable).
2. A TOML file -- ``config.toml`` in the app directory, or ``$CLAUDETRADE_CONFIG``.
3. Environment variables, prefixed ``CLAUDETRADE_`` with ``__`` as the nesting
   separator (e.g. ``CLAUDETRADE_RISK__MAX_RISK_PER_TRADE_PCT=0.5``).

**Secrets never live here.** API keys are resolved at call time through
``claudetrade.secrets``; the config only records *which* named credential to
look up. That keeps ``config.toml`` safe to commit or share.

``AppConfig.config_hash`` is a canonical digest of the effective configuration
and is stamped onto every signal and backtest run so results are reproducible.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from claudetrade.utils.hashing import content_hash

ENV_PREFIX = "CLAUDETRADE_"
DEFAULT_APP_DIRNAME = ".claudetrade"


def default_app_dir() -> Path:
    """Per-user application directory.

    On Windows this resolves under ``%LOCALAPPDATA%``; elsewhere under
    ``$XDG_DATA_HOME`` or ``~/.claudetrade``.
    """
    override = os.environ.get(f"{ENV_PREFIX}HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "ClaudeTrade"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "claudetrade"
    return Path.home() / DEFAULT_APP_DIRNAME


# --------------------------------------------------------------------------
# Sub-configurations
# --------------------------------------------------------------------------


class PathsConfig(BaseModel):
    """Filesystem locations. Relative paths resolve under ``app_dir``."""

    app_dir: Path = Field(default_factory=default_app_dir)
    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    exports_dir: Path = Path("exports")
    backups_dir: Path = Path("backups")
    cache_dir: Path = Path("cache")
    snapshots_dir: Path = Path("snapshots")

    @field_validator("app_dir", mode="before")
    @classmethod
    def _expand_app_dir(cls, value: object) -> object:
        """Expand a leading ``~`` before it is used anywhere.

        A ``config.toml`` like ``app_dir = "~/.claudetrade"`` parses as a
        *literal* path -- there is no shell here to expand it, least of all
        on Windows, where ``~`` has no special meaning to the filesystem at
        all. Without this, ``PathsConfig.resolve()`` silently creates a
        directory named ``~`` next to the process's current working
        directory instead of the user's home. Expanding here, once, means
        every consumer (``resolve()``, ``database_url()``, the CLI) sees the
        intended location without each having to remember to expand it.
        """
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    def resolve(self, which: str) -> Path:
        """Absolute path for a named directory, created on demand."""
        value: Path = getattr(self, which)
        path = value if value.is_absolute() else self.app_dir / value
        path.mkdir(parents=True, exist_ok=True)
        return path


class DatabaseConfig(BaseModel):
    """Database location and engine behaviour.

    SQLite is the default local store. The schema and all queries are written
    through the SQLAlchemy ORM with no SQLite-specific SQL, so migrating to
    PostgreSQL is a URL change plus a data copy (see ADR-0003).
    """

    url: str | None = None
    filename: str = "claudetrade.db"
    echo: bool = False
    #: SQLite pragma: WAL improves concurrent read/write from the UI + scheduler.
    sqlite_wal: bool = True
    busy_timeout_ms: int = 10_000
    pool_size: int = 5

    def effective_url(self, paths: PathsConfig) -> str:
        """Return the SQLAlchemy URL, defaulting to a file under ``data_dir``."""
        if self.url:
            return self.url
        db_path = paths.resolve("data_dir") / self.filename
        return f"sqlite+pysqlite:///{db_path}"


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_format: bool = True
    console: bool = True
    rotate_max_bytes: int = 10 * 1024 * 1024
    rotate_backup_count: int = 10
    filename: str = "claudetrade.log"
    audit_filename: str = "audit.log"


class MarketDataConfig(BaseModel):
    """Market-data provider selection.

    ``provider`` names an adapter registered in
    ``claudetrade.providers.registry``. ``fallbacks`` are tried in order when
    the primary fails or lacks a symbol, which is what keeps the app running in
    reduced-capability mode.

    ``provider = "tipranks"`` (the default) is primary for reference data,
    market caps and earnings -- see ``TipRanksConfig`` and
    ``providers.market.tipranks.TipRanksProvider``. Its own daily-bars
    capability is deliberately a *last resort*: ``TipRanksProvider`` close-only
    ``overview.prices`` series (no open/high/low/volume) is enough to keep a
    symbol's price series moving when nothing better is available, but it must
    never pre-empt a real OHLCV source. ``providers.registry.
    FallbackMarketProvider.get_daily_bars`` respects this by deferring any
    provider whose ``bars_last_resort`` attribute is ``True`` to the end of
    the per-call cascade for bars specifically -- every other capability
    (``get_market_caps``, ``get_security_info``) still tries the configured
    primary/fallback order as written, i.e. tipranks first.

    ``"stooq"`` is deliberately **not** in the default ``fallbacks`` any more.
    A real probe from the owner's machine found stooq's edge now answers CSV
    history requests -- for both a US and a TSX symbol, with the correct
    ``.us``/``.to`` suffix and a proper browser ``User-Agent`` already applied
    -- with an HTTP 200 HTML/JavaScript proof-of-work challenge page instead
    of the CSV body. Per ADR-0008 Decision 1 this application never solves a
    challenge, so stooq is unusable as an unattended default; it remains
    fully registered in ``providers.registry`` (see
    ``providers.market.stooq``'s fail-closed challenge detection) as an
    explicit opt-in for an operator whose network path to stooq.com is not
    challenged.
    """

    # Live daily history is the product default. Synthetic remains available
    # only as an explicit offline/demo choice; it must never silently populate
    # a normal installation with fabricated tickers.
    provider: str = "tipranks"
    fallbacks: list[str] = Field(default_factory=lambda: ["yahoo", "csv"])
    credential: str | None = None
    csv_dir: Path | None = None
    #: Bars older than this are flagged stale by the data-quality checks.
    stale_after_hours: float = 30.0
    max_symbols_per_request: int = 100
    request_timeout_s: float = 20.0
    rate_limit_per_minute: int = 60
    benchmark_symbol: str = "SPY"
    #: Sector ETF proxies used for relative-strength comparisons.
    sector_etfs: dict[str, str] = Field(
        default_factory=lambda: {
            "Technology": "XLK",
            "Health Care": "XLV",
            "Financials": "XLF",
            "Consumer Discretionary": "XLY",
            "Consumer Staples": "XLP",
            "Energy": "XLE",
            "Industrials": "XLI",
            "Materials": "XLB",
            "Utilities": "XLU",
            "Real Estate": "XLRE",
            "Communication Services": "XLC",
        }
    )


class EarningsConfig(BaseModel):
    """Earnings-calendar provider selection.

    ``provider = "tipranks"`` (the default) replaces the previous synthetic
    default with real upcoming/last-reported dates and surprise history from
    TipRanks' widget API (see ``TipRanksConfig`` and
    ``providers.market.tipranks.TipRanksProvider``). ``synthetic`` remains
    fully available and is what the test suite pins (``tests/conftest.py``)
    and what an operator running fully offline/demo should select explicitly.
    """

    provider: str = "tipranks"
    fallbacks: list[str] = Field(default_factory=lambda: ["csv"])
    credential: str | None = None
    csv_path: Path | None = None
    #: Treat an unconfirmed (estimated) date as if it were N days wide.
    estimated_date_uncertainty_days: int = 3
    request_timeout_s: float = 20.0


class TipRanksConfig(BaseModel):
    """TipRanks unauthenticated partner-widget API (ADR-0008 Decision 1 posture).

    ``providers.market.tipranks.TipRanksProvider`` reads
    ``widgets.tipranks.com/api/etoro/dataForTicker?ticker={SYMBOL}`` -- a
    keyless, unauthenticated JSON endpoint that is not a published, contracted
    API: it could be restricted, reshaped or withdrawn without notice. This is
    the same "free, unauthenticated, personal-use, fail-closed" posture this
    codebase already applies to stooq/Yahoo/Stocktwits, not a special case.

    One provider instance serves earnings, market caps, reference data and
    (last-resort only) daily bars, because all four are different views onto
    the same per-symbol ``overview`` object -- fetching it once per symbol per
    day and caching the raw response is what keeps a whole-universe refresh to
    one call per symbol rather than four.
    """

    #: Conservative default for an unauthenticated, undocumented endpoint --
    #: same posture as stooq/yahoo's own defaults.
    rate_limit_per_minute: int = 30
    request_timeout_s: float = 20.0
    #: Cached ``overview`` responses are reused until this many trading
    #: sessions have elapsed since they were fetched (see
    #: ``utils.timeutils.trading_days_between``), so a scan universe of
    #: thousands of symbols does not re-fetch earnings/caps/refdata on every
    #: refresh within the same trading day -- only once a new session begins.
    #: Stored under ``paths.cache_dir/tipranks/``.
    cache_ttl_trading_days: int = 1
    #: OPTIONAL, UNVERIFIED batching optimisation for Canadian market caps via
    #: ``marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=TSE:A,TSE:B,...``
    #: (the CIBC-app endpoint, not the widget). Off by default: this sandbox
    #: never obtained a real response body for this endpoint, so the parser
    #: is defensive-but-unverified (see
    #: ``providers.market.tipranks._parse_getquotes_response``). Canadian cap
    #: coverage never depends on this -- ``dataForTicker`` (with the
    #: ``TSE:SYMBOL`` ticker notation) is the primary path for every symbol,
    #: US and Canadian alike; this is purely a call-count optimisation for a
    #: large TSX universe, and any failure here falls straight back to the
    #: per-symbol ``dataForTicker`` path with no user-visible effect beyond
    #: one extra batch of calls.
    use_getquotes_batch: bool = False
    getquotes_batch_size: int = 25


class RedditConfig(BaseModel):
    """Authorised Reddit access.

    Prefers the official OAuth API, in decreasing order of preference: the
    password grant (script app + the owner's own username/password -- an
    official Reddit flow), then cookie-session mode (the owner's own
    logged-in browser session, ``session_cookie_credential``), then the
    client-credentials grant (script app alone), then -- only if
    ``public_json_fallback`` is explicitly opted into -- an unauthenticated
    read of Reddit's public ``.json`` listing endpoint. Disabled unless
    *some* path resolves, in which case the pipeline continues without this
    source rather than failing (ADR-0008 Decision 1).

    **Cookie-session mode**: reads the same public ``.json`` listing endpoint
    as the public-JSON fallback below, but authenticated with the owner's own
    ``reddit_session`` cookie (pasted from a logged-in browser's devtools) and
    a browser-style User-Agent, rather than anonymously. This is the owner's
    own session, for personal use only (ADR-0008 Decision 1: "own credentials
    only" -- never a shared/default account or someone else's cookie). It is
    automatically preferred over the client-credentials grant whenever the
    cookie resolves and the password-grant credentials do not, and shares the
    public-JSON path's fail-closed behaviour exactly (no retry, no
    fingerprint/proxy rotation, no CAPTCHA handling).

    **Honest status of the public-JSON fallback**: reading
    ``www.reddit.com/r/<sub>/new.json`` without authentication is not
    something Reddit's API terms affirmatively grant -- it is a ToS-gray
    area for automated/scheduled use, tolerated in practice for casual,
    low-volume, identifying-UA traffic but not a sanctioned integration
    path the way the OAuth API is. It exists here only as a last-resort,
    opt-in fallback for when neither OAuth path is configured or working,
    is off by default, and is capped at a conservative rate. The moment
    OAuth credentials work, this class prefers them automatically.
    """

    #: On by default pointing at the *synthetic* generator, so a fresh install
    #: exercises the whole sentiment pipeline with no credentials and no
    #: network. Set ``provider = "reddit"`` and store credentials to go live;
    #: if those credentials do not resolve the source disables itself cleanly
    #: rather than failing the run.
    enabled: bool = True
    provider: str = "synthetic"
    client_id_credential: str = "reddit_client_id"
    client_secret_credential: str = "reddit_client_secret"
    #: Owner's own Reddit account credentials (ADR-0008 Decision 1: "own
    #: credentials only" -- never a shared/default account). When these
    #: resolve *and* the client id/secret also resolve, the password grant
    #: is preferred over client-credentials, per the owner's explicit ask
    #: ("use my reddit credentials ... retain the fallback to the standard
    #: API"). Both are official OAuth flows against the same endpoints.
    username_credential: str = "reddit_username"
    password_credential: str = "reddit_password"
    #: Cookie-session mode (ADR-0008 Decision 1: the owner's own personal
    #: session, pasted from their browser's devtools -- never a shared or
    #: default cookie). Holds the value of the ``reddit_session`` cookie.
    #: Consulted only when the password-grant credentials above do not both
    #: resolve; preferred over the client-credentials grant when it does
    #: resolve. See ``docs/api-providers.md`` for how to export it (F12 ->
    #: Application -> Cookies -> reddit.com -> reddit_session).
    session_cookie_credential: str = "reddit_session_cookie"
    #: Cookie-session mode shares the same public listing endpoint as the
    #: public-JSON fallback below (an unauthenticated-by-design endpoint,
    #: just authenticated here via the owner's own cookie rather than
    #: anonymously), so it is held to the same conservative, human-scale
    #: pace rather than the OAuth budget.
    session_rate_limit_per_minute: int = 30
    user_agent: str = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
    subreddits: list[str] = Field(
        default_factory=lambda: [
            "stocks",
            "investing",
            "StockMarket",
            "SecurityAnalysis",
            "options",
            "swingtrading",
        ]
    )
    #: Reddit caps a listing page at 100 items, so a busy subreddit needs
    #: several pages to cover a full lookback window. This bounds how many
    #: pages one refresh will walk before giving up, keeping a runaway loop
    #: from burning the whole rate-limit budget on one community.
    posts_per_subreddit: int = 100
    max_pages_per_subreddit: int = 10
    comments_per_post: int = 50
    lookback_hours: int = 72
    #: Official API guidance for OAuth clients; kept conservative on purpose.
    rate_limit_per_minute: int = 60
    request_timeout_s: float = 20.0
    #: Store only salted author digests, never usernames.
    store_author_names: bool = False

    # --- Public-JSON fallback (unauthenticated, ToS-gray; opt-in) -----------
    #: Off by default. Only takes effect when BOTH OAuth paths above are
    #: unconfigured (or fail at runtime) -- official APIs remain first-choice
    #: per ADR-0008 Decision 1. See the class docstring for the honest ToS
    #: caveat before turning this on.
    public_json_fallback: bool = False
    #: Hard-capped at 30/min in the validator below -- "conservative,
    #: human-scale rate" for an unauthenticated path is a ceiling, not a
    #: suggestion an operator can configure their way past.
    public_json_rate_limit_per_minute: int = 30

    @field_validator("public_json_rate_limit_per_minute")
    @classmethod
    def _cap_public_json_rate(cls, v: int) -> int:
        return min(v, 30)


class XConfig(BaseModel):
    """Authorised X (Twitter) access.

    Two independent live paths, tried in this order:

    1. **Official API v2** (``bearer_credential``). Requires a paid tier for
       any meaningful search volume. Preferred whenever configured -- this is
       the officially sanctioned path and ADR-0008 Decision 1 requires the
       official API remain first-choice.
    2. **Cookie-session mode** (``session_enabled``), only when the official
       path has no bearer token configured. The owner's own logged-in x.com
       session cookies drive the same GraphQL endpoints the web client uses,
       for cashtag search over ``session_symbols``. **This automates a
       logged-in personal account and violates X's Terms of Service; it can
       lead to account suspension.** The owner accepts this risk for their
       own account (ADR-0008 Decision 1); the application never bundles or
       defaults credentials, never solves a challenge/CAPTCHA, and disables
       the source for the rest of the cycle on any 401/403/challenge/
       rate-limit signal rather than attempting a workaround. Off by
       default (``session_enabled = False``).

    When neither path is configured the source is disabled cleanly and the
    remaining sources continue to operate.
    """

    enabled: bool = False
    provider: str = "synthetic"
    bearer_credential: str = "x_bearer_token"
    query_terms: list[str] = Field(default_factory=list)
    max_results_per_query: int = 100
    lookback_hours: int = 48
    rate_limit_per_minute: int = 15
    request_timeout_s: float = 20.0
    store_author_names: bool = False

    # --- Cookie-session mode (ADR-0008 Decision 1; owner-accepted risk) -----
    #: Off by default. Only consulted when ``bearer_credential`` does not
    #: resolve -- the official API path is always preferred when available.
    session_enabled: bool = False
    #: Exported from the browser's devtools -> Application/Storage -> Cookies
    #: for x.com, after logging in as the owner: ``auth_token`` and ``ct0``
    #: (the CSRF token cookie). See docs/api-providers.md for the exact
    #: click-path. Stored via the normal credential store, never in config.
    auth_token_credential: str = "x_auth_token"
    ct0_credential: str = "x_ct0"
    #: Watchlist symbols searched as cashtags (``$AAPL``); the leading ``$``
    #: is added automatically if a bare symbol is given.
    session_symbols: list[str] = Field(default_factory=list)
    session_max_results_per_query: int = 40
    #: Deliberately much lower than the official API's already-conservative
    #: default -- a logged-in personal session is held to a stricter,
    #: human-scale pace than a sanctioned API client.
    session_rate_limit_per_minute: int = 6
    session_request_timeout_s: float = 20.0
    session_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClaudeTrade-research/0.1 "
        "(contact configured by operator)"
    )


class StocktwitsConfig(BaseModel):
    """Stocktwits public symbol-stream API (ADR-0008 Decision 1's "official
    APIs first-choice" path for this source): keyless for basic reads, no
    scraping, no ToS boundary crossed.

    Reads ``api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json``, which is
    Stocktwits' own documented, unauthenticated basic-read endpoint. Off by
    default: even though no credential is at risk here, the vendor's
    published unauthenticated budget (200 requests/hour) is easy to exhaust
    across a large universe, so this is an explicit opt-in with a hard cap on
    symbols scanned per cycle rather than a silent default.
    """

    enabled: bool = False
    provider: str = "stocktwits"
    #: Symbols fetched when the caller does not supply a more specific
    #: (recent-signal / watchlist) hint via ``fetch_posts(symbols=...)``.
    watchlist_symbols: list[str] = Field(default_factory=list)
    #: Hard budget per refresh cycle: at most this many symbols are fetched,
    #: prioritised by the order of the caller-supplied ``symbols`` hint (the
    #: pipeline passes recent-signal / watchlist symbols first) so a broad
    #: universe degrades to "covered the names that matter this cycle"
    #: rather than silently rationing across the whole universe evenly.
    max_symbols_per_cycle: int = 20
    #: Stocktwits documents 200 unauthenticated requests/hour; the default
    #: below (3/min == 180/hour) keeps a working margin rather than running
    #: right up against the published ceiling.
    rate_limit_per_minute: int = 3
    request_timeout_s: float = 20.0
    #: Kept for config-file backwards compatibility, but no longer sent:
    #: live-probe evidence (2026-07-30) showed this endpoint's edge rejecting
    #: this descriptive app UA (and generic non-browser UAs) with HTTP 403
    #: while accepting a real browser tab, so the provider now sends a fixed
    #: browser-style User-Agent instead -- see
    #: ``providers.social.stocktwits._BROWSER_HEADERS``.
    user_agent: str = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
    store_author_names: bool = False


class NewsConfig(BaseModel):
    """Publisher-syndicated RSS/Atom news feeds: a lawful, credential-free
    social-sentiment source.

    This exists to reduce the application's reliance on Reddit's official API
    (rate-limited, OAuth-gated, and subject to Reddit's own availability).
    RSS/Atom feeds that a publisher explicitly serves for syndication are a
    different kind of source entirely: there is no authentication to fail,
    no scraping, no ToS boundary to test -- the operator publishes the feed
    for exactly this purpose. That is also why, unlike Reddit/X, this source
    defaults to its *live* adapter (``provider = "news_rss"``) rather than the
    offline synthetic generator: there are no credentials to be missing and
    no paid tier to gate behind an opt-in.

    ``feed_urls`` ships a small default list of major exchange/regulator,
    wire-service and public-broadcaster feeds chosen because their owners
    document them as public syndication feeds (see ``docs/api-providers.md``
    for the rationale on each). This package cannot verify the URLs are still
    live from a sandboxed, egress-blocked build -- operators should confirm
    reachability with ``claudetrade probe`` after deploying and are free to
    replace the list with feeds of their own choosing.
    """

    enabled: bool = True
    provider: str = "news_rss"
    feed_urls: list[str] = Field(
        default_factory=lambda: [
            # US securities regulator: official press-release feed.
            "https://www.sec.gov/news/pressreleases.rss",
            # US central bank: official press-release feed.
            "https://www.federalreserve.gov/feeds/press_all.xml",
            # Wire service: publishes per-category RSS for syndication.
            "https://www.prnewswire.com/rss/financial-services-latest-news-list.rss",
            # Public broadcaster: publishes per-section RSS, including business.
            "https://feeds.npr.org/1006/rss.xml",
        ]
    )
    user_agent: str = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
    rate_limit_per_minute: int = 30
    request_timeout_s: float = 20.0
    lookback_hours: int = 72
    #: Salt for the publisher-level author hash (see ``NewsRssProvider``:
    #: there is no personal author to pseudonymise, only the feed's domain).
    author_salt: str = "news_rss"

    # --- Hosted (paid) sentiment aggregator seam ---------------------------
    # See providers/social/hosted_api.py::HostedSentimentProvider. Disabled
    # by default: this is a documented adapter seam, not a working
    # integration, so all three of these must be explicitly set before the
    # constructor even attempts to proceed (and even then it raises -- see
    # that module's docstring for what a real implementation must add).
    hosted_base_url: str | None = None
    hosted_credential: str | None = None
    hosted_enabled: bool = False


class AIConfig(BaseModel):
    """Optional LLM assistance.

    The system is fully functional with ``provider = "null"``: sentiment falls
    back to the deterministic rule ensemble and theses are template-generated.
    AI output can never relax a risk control.
    """

    provider: Literal["null", "openai", "anthropic"] = "null"
    model: str = "claude-sonnet-5"
    api_key_credential: str = "anthropic_api_key"
    base_url: str | None = None
    max_output_tokens: int = 900
    temperature: float = 0.0
    request_timeout_s: float = 45.0
    max_calls_per_run: int = 250
    daily_cost_limit_usd: float = 5.0
    cache_enabled: bool = True
    cache_ttl_hours: int = 168
    #: Posts scoring above this on the injection heuristic are never sent to AI.
    injection_block_threshold: float = 0.4
    #: Batch size for classification requests.
    batch_size: int = 12
    prompt_version: str = "v1"
    #: Per-1M-token prices used only for local cost accounting; update to match
    #: the provider's current published pricing.
    input_cost_per_mtok_usd: float = 3.0
    output_cost_per_mtok_usd: float = 15.0


class UniverseConfig(BaseModel):
    """Which securities are eligible to be scanned at all."""

    source: Literal["database", "csv", "static"] = "database"
    csv_path: Path | None = None
    static_symbols: list[str] = Field(default_factory=list)
    #: TSX is permitted alongside the US exchanges by default so a real-data
    #: refresh (``market_data.provider = "stooq"``) covers both markets out of
    #: the box; see ``data.universe.load_packaged_universe``. Deliberately
    #: TSX (main board) only -- TSX Venture (TSXV), CSE and NEO are more
    #: speculative venture-tier boards the owner scoped out of this
    #: application entirely (ADR-0008 Decision 3); they are neither seeded
    #: nor permitted here.
    permitted_exchanges: list[str] = Field(
        default_factory=lambda: ["NYSE", "NASDAQ", "AMEX", "TSX"]
    )
    # The packaged >=$1B US + TSX inventory currently exceeds 2,000 names.
    # Keep enough headroom that deterministic truncation cannot silently drop
    # the Canadian tail merely because the US file is loaded first.
    max_symbols: int = 3000
    include_etfs: bool = False
    #: Packaged seed universes (see ``data/universes/*.csv``) used to fill the
    #: scannable universe when ``source == "database"`` and the database has no
    #: stored securities yet -- i.e. before the first ``claudetrade refresh``.
    #: Set to ``[]`` to disable this fallback and start from a genuinely empty
    #: universe. See ``data.universe.load_packaged_universe`` for what each
    #: name covers and its honest coverage caveats.
    packaged_universes: list[str] = Field(
        default_factory=lambda: ["us_default", "ca_default"]
    )
    #: ADR-0008 Decision 3: the durable, authoritative fix for "the universe is
    #: too small" is computed at refresh time, not the packaged seed files
    #: (those are bootstrap coverage only -- see ``data/universes/*.csv``).
    #: This is the floor ``UniverseSelector.for_session`` applies against each
    #: security's *stored* (provider-sourced, real) ``market_cap_usd`` -- a
    #: deliberately separate field from ``FilterConfig.min_market_cap_usd``,
    #: which is a lower, longer-standing candidate-quality screen applied
    #: again later at signal-scoring time (see ``signals.scoring``). Raising
    #: or lowering this one changes who is even eligible to be scanned at
    #: all; it does not touch the scoring-time gate.
    min_market_cap_usd: float = 1_000_000_000.0
    #: What to do with a security for which NO configured market-data
    #: provider could establish a market cap at all (as opposed to one priced
    #: below the floor above). "include" (default) keeps it in the universe --
    #: silently dropping a name just because its cap could not be established
    #: would reintroduce survivorship-style bias at the universe layer, the
    #: same failure mode ``for_session``'s point-in-time delisting logic
    #: already guards against. "exclude" drops it instead, for an operator who
    #: would rather under-cover than risk scanning an unpriced name; either
    #: way the gap is always flagged in the data-quality report, never silent.
    unknown_cap_policy: Literal["include", "exclude"] = "include"


class FilterConfig(BaseModel):
    """Candidate filters. Defaults deliberately exclude illiquid and
    pump-and-dump-prone names."""

    min_price: float = 5.0
    max_price: float = 1000.0
    min_market_cap_usd: float = 500_000_000
    min_avg_dollar_volume_usd: float = 10_000_000
    max_bid_ask_spread_pct: float = 0.5
    exclude_penny_stocks: bool = True
    exclude_leveraged_inverse_etfs: bool = True
    exclude_binary_event_sectors: bool = False
    binary_event_sectors: list[str] = Field(default_factory=lambda: ["Biotechnology"])

    min_unique_authors: int = 5
    min_sentiment_confidence: float = 0.35
    max_manipulation_risk: float = 0.60
    max_annualised_volatility: float = 1.20
    min_atr_pct: float = 1.0
    max_atr_pct: float = 15.0

    #: Earnings guard. The default strategy set does not hold through earnings.
    min_days_to_earnings: int = 3
    max_days_to_earnings: int | None = None
    block_entry_within_days_of_earnings: int = 3

    @field_validator("min_sentiment_confidence", "max_manipulation_risk")
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be within [0, 1]")
        return v

    @model_validator(mode="after")
    def _price_order(self) -> FilterConfig:
        if self.max_price <= self.min_price:
            raise ValueError("max_price must exceed min_price")
        return self


class RiskConfig(BaseModel):
    """Account and risk limits driving position sizing and portfolio heat."""

    account_size_usd: float = 100_000.0
    max_risk_per_trade_pct: float = 0.75
    max_portfolio_heat_pct: float = 6.0
    max_position_size_pct: float = 15.0
    max_sector_exposure_pct: float = 35.0
    max_correlated_exposure_pct: float = 40.0
    correlation_threshold: float = 0.75
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    max_concurrent_positions: int = 8
    #: Cap participation in a name's average daily volume, to keep simulated
    #: (and real) fills achievable.
    max_pct_of_adv: float = 2.0
    #: Reject a signal whose reward:risk falls below this. This is the primary
    #: guard against "many tiny wins, few huge losses" win-rate gaming.
    min_reward_risk_ratio: float = 1.6
    kill_switch_engaged: bool = False

    @field_validator(
        "max_risk_per_trade_pct",
        "max_portfolio_heat_pct",
        "max_position_size_pct",
        "max_sector_exposure_pct",
        "max_correlated_exposure_pct",
        "max_daily_loss_pct",
        "max_weekly_loss_pct",
    )
    @classmethod
    def _positive_pct(cls, v: float) -> float:
        if not 0.0 < v <= 100.0:
            raise ValueError("percentage must be in (0, 100]")
        return v

    @model_validator(mode="after")
    def _heat_covers_trade(self) -> RiskConfig:
        if self.max_portfolio_heat_pct < self.max_risk_per_trade_pct:
            raise ValueError("max_portfolio_heat_pct must be >= max_risk_per_trade_pct")
        return self


class CostModelConfig(BaseModel):
    """Transaction-cost assumptions used identically by backtest and paper trading."""

    commission_per_share: float = 0.0
    commission_per_trade: float = 0.0
    commission_min: float = 0.0
    #: SEC Section 31 fee on sales, per USD of principal (update to current rate).
    sec_fee_rate: float = 0.0000278
    #: FINRA Trading Activity Fee on sales, per share, capped per trade.
    taf_per_share: float = 0.000166
    taf_max_per_trade: float = 8.30
    #: Half-spread paid on entry and exit, in basis points of price.
    half_spread_bps: float = 3.0
    #: Additional slippage in basis points, scaled by order size vs ADV.
    base_slippage_bps: float = 2.0
    impact_coefficient_bps: float = 25.0
    #: Extra slippage applied when a stop gaps through its trigger.
    gap_slippage_bps: float = 10.0
    #: Fraction of a bar's volume that a single order may consume.
    max_participation_rate: float = 0.05
    #: Annualised borrow cost applied to short positions.
    short_borrow_annual_pct: float = 3.0
    enable_partial_fills: bool = True


class SentimentConfig(BaseModel):
    """Sentiment aggregation behaviour."""

    #: Exponential time decay; a post loses half its weight after this long.
    half_life_hours: float = 18.0
    lookback_days: int = 14
    #: Minimum resolution confidence before a mention is counted at all.
    min_ticker_confidence: float = 0.60
    min_posts_for_signal: int = 8
    min_unique_authors_for_signal: int = 4
    #: Weight applied to engagement (log-scaled) in weighted sentiment.
    engagement_weight: float = 0.35
    credibility_weight: float = 0.35
    #: Credibility assigned to a post whose author metrics (age/karma/
    #: followers) are ALL ``None`` -- i.e. structurally absent, not merely
    #: low. This is what keeps "no metrics reported" (a news-wire post,
    #: which has no personal author to have karma or a follower count) from
    #: being scored identically to "worst possible metrics" (a real,
    #: karma-less throwaway account), which both floored to 0.0 before this
    #: field existed. Keyed by ``SocialSource`` value; a post with *some*
    #: (not all) metrics present never uses this baseline -- it keeps the
    #: existing computed score, since partial information is real
    #: information. A source with no entry here falls back to 0.0, the
    #: original floor-to-zero behaviour.
    credibility_baseline_by_source: dict[str, float] = Field(
        default_factory=lambda: {
            "news": 0.6,  # publisher-level content from curated feeds
            "reddit": 0.3,  # unknown author, mild prior
            "x": 0.3,  # unknown author, mild prior
        }
    )
    #: Windows (days) used for acceleration measures.
    fast_window_days: int = 2
    slow_window_days: int = 10
    #: Above this share of posts from one author/community, flag concentration.
    source_concentration_alert: float = 0.40
    duplicate_ratio_alert: float = 0.35
    use_ai_classifier: bool = True
    ai_sample_per_symbol: int = 20


class RegimeConfig(BaseModel):
    """Market-regime classification thresholds."""

    benchmark: str = "SPY"
    breadth_universe_max: int = 500
    trend_fast_ma: int = 20
    trend_slow_ma: int = 50
    trend_long_ma: int = 200
    #: Realised-volatility percentile above which the regime is 'high volatility'.
    high_vol_percentile: float = 0.80
    low_vol_percentile: float = 0.30
    vol_lookback_days: int = 252
    breadth_bullish: float = 0.55
    breadth_bearish: float = 0.40
    #: Multipliers applied per regime to sizing and thresholds.
    risk_off_size_multiplier: float = 0.5
    high_vol_size_multiplier: float = 0.7


class SignalConfig(BaseModel):
    """Signal generation, scoring and lifecycle."""

    enabled_strategies: list[str] = Field(
        default_factory=lambda: [
            "sentiment_breakout",
            "sentiment_pullback",
            "capitulation_reversal",
            "hype_failure_short",
            "post_earnings_drift",
        ]
    )
    allow_shorts: bool = True
    min_overall_score: float = 55.0
    min_confidence: float = 0.45
    max_candidates: int = 40
    #: A signal that has not triggered within this many sessions expires.
    signal_expiry_days: int = 5
    #: An entry zone is 'extended' once price runs this far past the top of it.
    extended_threshold_atr: float = 0.75
    #: Hard cap on holding period. Prevents indefinitely-open losers inflating
    #: the win/loss ratio by never being classified.
    max_holding_days: int = 20
    default_holding_days: int = 10
    #: Component weights (normalised at use). Sum need not be 1.
    component_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "technical_setup": 0.20,
            "price_momentum": 0.12,
            "volume_confirmation": 0.10,
            "reddit_sentiment": 0.08,
            "x_sentiment": 0.05,
            "sentiment_acceleration": 0.08,
            "attention_acceleration": 0.05,
            "catalyst_quality": 0.07,
            "earnings_risk": 0.08,
            "liquidity": 0.07,
            "market_regime": 0.06,
            "manipulation_risk": 0.04,
        }
    )


class StrategyCalibrationConfig(BaseModel):
    """Score-accumulation calibration for the five strategies (ADR-0007 Decision 2).

    Strategies in ``src/claudetrade/strategies/`` no longer decline at the
    first unmet absolute threshold. Each non-veto condition contributes a
    weighted score component (see ``strategies.scoring_utils.ScoreAccumulator``)
    and a strategy emits a proposal once its accumulated score clears
    ``proposal_score_threshold``. A short, strategy-documented list of
    conditions remain hard vetoes -- earnings window, insufficient history,
    liquidity, manipulation risk -- because a weighted average is the wrong
    tool for a disqualifying fact.

    The values below are PERCENTILE LEVELS (0-1) against a symbol's OWN
    trailing distribution, not bare absolute price/volume/indicator
    constants: e.g. ``breakout_volume_percentile`` asks "is today's relative
    volume in the top 30% of this symbol's own trailing 120 sessions?", never
    "is relative volume above 1.5x". A percentile is comparable across a
    quiet utility and a volatile small-cap in a way an absolute number is
    not, and it is what lets the same threshold serve every regime.

    Reversal: raising ``proposal_score_threshold`` toward 100 restores
    near-AND-gate strictness; a percentile level set to 0.0 or 1.0 removes
    that condition's ability to move the score.
    """

    #: Trailing sessions of sentiment history used by the on-the-fly
    #: ``strategies.scoring_utils.percentile_rank`` helper (sentiment has no
    #: precomputed feature column, unlike price/volume series -- see
    #: ``features.feature_builder``'s fixed 120-session window for those).
    sentiment_percentile_window: int = 90

    #: Minimum accumulated score (0-100, before the engine's 13-component
    #: blend in ``signals.scoring.score_candidate``) for a strategy to emit a
    #: proposal at all. This is deliberately a separate, lower-friction gate
    #: from ``SignalConfig.min_overall_score`` -- that gate applies to the
    #: engine's blended score across all candidates; this one only decides
    #: whether the strategy's OWN thesis is worth proposing in the first place.
    proposal_score_threshold: float = 48.0

    # --- Strategy A: sentiment_breakout -------------------------------------
    breakout_volume_percentile: float = 0.70
    breakout_trend_percentile: float = 0.55
    breakout_sentiment_accel_percentile: float = 0.65
    breakout_mention_accel_percentile: float = 0.60

    # --- Strategy B: sentiment_pullback --------------------------------------
    pullback_trend_percentile: float = 0.55
    #: Down-volume during the pullback should rank LOW among its own history.
    pullback_quiet_volume_percentile: float = 0.45
    pullback_rsi_low_percentile: float = 0.25
    pullback_rsi_high_percentile: float = 0.65

    # --- Strategy C: capitulation_reversal -----------------------------------
    #: How far below the symbol's own trailing distribution of
    #: dist_from_sma50_pct today's reading must rank (LOW percentile = most
    #: stretched below average in its own history).
    capitulation_extension_percentile: float = 0.20
    capitulation_oversold_percentile: float = 0.20
    capitulation_climax_volume_percentile: float = 0.80

    # --- Strategy D: hype_failure_short ---------------------------------------
    hype_advance_percentile: float = 0.85
    hype_sentiment_spike_percentile: float = 0.75

    # --- Strategy E: post_earnings_drift ---------------------------------------
    #: Event-day reaction magnitude percentile against the symbol's own
    #: trailing |ROC-20| distribution -- a 3% move is a repricing for a
    #: sleepy utility and noise for a volatile small-cap.
    drift_reaction_percentile: float = 0.55


class BacktestConfig(BaseModel):
    """Backtest engine behaviour and validation gates."""

    initial_capital_usd: float = 100_000.0
    #: Signals are computed on bar close and may only execute from the next bar.
    execution_delay_bars: int = 1
    #: Which price the next-bar order references.
    entry_reference: Literal["next_open", "next_open_limit", "stop_trigger"] = "next_open_limit"
    allow_shorts: bool = True
    #: Minimum completed trades before a strategy is treated as validated.
    min_trades_for_validation: int = 30
    #: Warn when a win/loss ratio rests on fewer than this many trades.
    min_trades_for_confidence: int = 50
    walk_forward_train_days: int = 504
    walk_forward_test_days: int = 126
    walk_forward_step_days: int = 126
    #: Fraction of the sample reserved as a final untouched out-of-sample block.
    holdout_fraction: float = 0.25
    risk_free_rate_annual: float = 0.04
    trading_days_per_year: int = 252
    #: Force-close any position still open at the end of the test window so it
    #: is classified as a win or a loss rather than being quietly dropped.
    force_close_open_positions: bool = True
    include_delisted: bool = True
    random_seed: int = 20240101


class NotificationConfig(BaseModel):
    enabled: bool = False
    channels: list[Literal["windows", "email", "webhook"]] = Field(default_factory=list)
    webhook_url_credential: str = "notify_webhook_url"
    email_to: list[str] = Field(default_factory=list)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user_credential: str = "smtp_user"
    smtp_password_credential: str = "smtp_password"
    smtp_starttls: bool = True
    #: Suppress a repeat of the same (event, symbol) inside this window.
    cooldown_minutes: int = 120
    max_per_hour: int = 20
    events: list[str] = Field(
        default_factory=lambda: [
            "signal_approaching_entry",
            "entry_triggered",
            "stop_hit",
            "target_hit",
            "earnings_approaching",
            "sentiment_reversal",
            "data_source_failure",
            "risk_limit_breach",
        ]
    )


class SchedulerConfig(BaseModel):
    enabled: bool = False
    timezone: str = "America/New_York"
    #: Cron-ish schedules; the CLI can also run any job once, on demand.
    market_data_refresh_cron: str = "30 16 * * mon-fri"
    social_refresh_cron: str = "0 */4 * * *"
    scan_cron: str = "45 16 * * mon-fri"
    paper_mark_cron: str = "5 16 * * mon-fri"
    misfire_grace_time_s: int = 3600


class TradingModeConfig(BaseModel):
    """Live trading is off by default and requires two explicit opt-ins."""

    mode: Literal["backtest", "paper", "live"] = "paper"
    #: Must be set to True in config *and* confirmed interactively before any
    #: broker adapter is permitted to transmit an order.
    live_trading_authorised: bool = False
    broker: str | None = None
    broker_credential: str | None = None
    #: Immediate halt: blocks all new orders in paper and live modes.
    kill_switch_engaged: bool = False

    @model_validator(mode="after")
    def _live_requires_authorisation(self) -> TradingModeConfig:
        if self.mode == "live" and not self.live_trading_authorised:
            raise ValueError(
                "mode='live' requires live_trading_authorised=true and a configured broker. "
                "Live trading is refused until you explicitly authorise it."
            )
        if self.mode == "live" and not self.broker:
            raise ValueError("mode='live' requires a configured broker adapter")
        return self


class UIConfig(BaseModel):
    display_timezone: str = "America/New_York"
    theme: Literal["dark", "light"] = "dark"
    chart_lookback_days: int = 180
    table_page_size: int = 50
    port: int = 8501


# --------------------------------------------------------------------------
# Root configuration
# --------------------------------------------------------------------------


class AppConfig(BaseSettings):
    """Root configuration object passed explicitly through the application."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
    )

    profile: str = "default"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    earnings: EarningsConfig = Field(default_factory=EarningsConfig)
    tipranks: TipRanksConfig = Field(default_factory=TipRanksConfig)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    x: XConfig = Field(default_factory=XConfig)
    stocktwits: StocktwitsConfig = Field(default_factory=StocktwitsConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    costs: CostModelConfig = Field(default_factory=CostModelConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    calibration: StrategyCalibrationConfig = Field(default_factory=StrategyCalibrationConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    trading: TradingModeConfig = Field(default_factory=TradingModeConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    # --- construction -----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        """Load configuration from TOML plus environment overrides.

        Args:
            path: Explicit config file. Defaults to ``$CLAUDETRADE_CONFIG`` and
                then ``<app_dir>/config.toml``. A missing file is not an error;
                built-in defaults run fully offline.
        """
        candidate: Path | None = None
        if path is not None:
            candidate = Path(path).expanduser()
        elif os.environ.get(f"{ENV_PREFIX}CONFIG"):
            candidate = Path(os.environ[f"{ENV_PREFIX}CONFIG"]).expanduser()
        else:
            default = default_app_dir() / "config.toml"
            if default.exists():
                candidate = default

        file_data: dict[str, Any] = {}
        if candidate is not None:
            if not candidate.exists():
                raise FileNotFoundError(f"configuration file not found: {candidate}")
            with candidate.open("rb") as fh:
                file_data = tomllib.load(fh)
            file_data.pop("secrets", None)  # defensive: never honour secrets in TOML

        return cls(**file_data)

    # --- derived ----------------------------------------------------------

    def public_dict(self) -> dict[str, Any]:
        """Configuration with no secret-bearing fields, safe to persist or log.

        Credential *names* are retained (they are lookup keys, not secrets);
        credential *values* are never held on this object in the first place.
        """
        return self.model_dump(mode="json", exclude={"paths"})

    @property
    def config_hash(self) -> str:
        """Digest of the effective configuration, stamped onto every artefact."""
        return content_hash(self.public_dict())

    def database_url(self) -> str:
        return self.database.effective_url(self.paths)

    def describe_enabled_sources(self) -> dict[str, bool]:
        """Which optional data sources are switched on, for status display."""
        return {
            "market_data": True,
            "earnings": True,
            "reddit": self.reddit.enabled,
            "x": self.x.enabled,
            "stocktwits": self.stocktwits.enabled,
            "news": self.news.enabled,
            "ai": self.ai.provider != "null",
            "notifications": self.notifications.enabled,
            "scheduler": self.scheduler.enabled,
        }


_CACHED: AppConfig | None = None


def get_config(path: str | Path | None = None, *, reload: bool = False) -> AppConfig:
    """Process-wide configuration accessor.

    Prefer passing ``AppConfig`` explicitly into components (it keeps them
    testable); this helper exists for entry points -- CLI, UI, scheduler.
    """
    global _CACHED
    if _CACHED is None or reload or path is not None:
        _CACHED = AppConfig.load(path)
    return _CACHED


def reset_config_cache() -> None:
    """Clear the cached configuration (used by tests)."""
    global _CACHED
    _CACHED = None
