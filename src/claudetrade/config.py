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
    #: Skip symbols whose stored bars already reach the window's last trading
    #: session. On a per-symbol provider the fetch cost is one rate-limited
    #: call PER SYMBOL (``yahoo_rate_limit_per_minute``, 120/min), so a
    #: refresh that re-requests an already-current universe costs ~20 minutes
    #: for 2,400 symbols and changes nothing. Only the tail is skipped;
    #: interior gaps are ``claudetrade db backfill``'s job. Set to ``false``
    #: (or run ``claudetrade refresh --full``) to force a complete sweep.
    incremental_prices: bool = True
    request_timeout_s: float = 20.0
    rate_limit_per_minute: int = 60
    #: Yahoo's undocumented chart endpoint gets its own bucket, separate from
    #: the field above (which stooq -- an opt-in-only fallback -- still reads).
    #: 120/min is still conservative for a single-symbol-per-call, keyless,
    #: undocumented endpoint, but was the second-biggest driver (after
    #: TipRanks' own rate limit -- see ``TipRanksConfig.rate_limit_per_minute``)
    #: of the owner's first live refresh taking 80+ minutes for ~2,400 symbols.
    yahoo_rate_limit_per_minute: int = 120
    #: Worker threads used for the per-symbol fetch loops in
    #: ``TipRanksProvider``/``YahooMarketProvider`` (market caps, security
    #: info, earnings, and the last-resort bars cascade). Each provider's own
    #: ``RateLimiter`` is shared across every worker thread -- see
    #: ``providers.base.RateLimiter`` and ``providers.base.parallel_map`` --
    #: so raising this overlaps request *latency* across symbols; it does not
    #: raise the enforced calls/minute ceiling, which is what
    #: ``rate_limit_per_minute``/``yahoo_rate_limit_per_minute`` control.
    #: 12 gives enough in-flight requests to actually saturate the 300/min
    #: tipranks budget at ~1-2 s per response; the limiter, not this, is the
    #: throughput ceiling.
    max_workers: int = 12
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

    #: Raised from the original conservative default of 30/min (ADR-0008
    #: Decision 1's launch posture) to 60/min after the owner confirmed their
    #: own brokerage app calls this same public eToro widget endpoint freely
    #: at that kind of cadence with no observed pushback -- it is still a
    #: fraction of what a browser tab idly refreshing a watchlist would issue,
    #: and remains fully self-imposed and operator-configurable, not a vendor
    #: published ceiling. This was the single biggest driver of the owner's
    #: first live refresh taking 80+ minutes for ~2,400 symbols (roughly
    #: symbol_count / rate_limit_per_minute at that rate); see also
    #: ``MarketDataConfig.max_workers``, which overlaps request latency across
    #: symbols but does not itself raise this ceiling.
    #:
    #: Raised 60 -> 300 at the owner's explicit direction ("we should be able
    #: to hit tipranks way harder and faster"): ~2,400 symbols now pace out
    #: at roughly 8 minutes instead of 40. Still self-imposed; if TipRanks
    #: ever pushes back the adapter's 429/403 handling backs off and fails
    #: closed rather than hammering on.
    rate_limit_per_minute: int = 300
    request_timeout_s: float = 20.0
    #: Cached ``overview`` responses are reused until this many trading
    #: sessions have elapsed since they were fetched (see
    #: ``utils.timeutils.trading_days_between``), so a scan universe of
    #: thousands of symbols does not re-fetch earnings/caps/refdata on every
    #: refresh within the same trading day -- only once a new session begins.
    #: Stored under ``paths.cache_dir/tipranks/``.
    cache_ttl_trading_days: int = 1
    #: A confirmed "TipRanks has no data for this symbol" result (a clean
    #: HTTP 404 from BOTH ``dataForTicker`` and the ``historicalprices``
    #: fallback probe -- see ``TipRanksProvider._resolve``) is cached under
    #: this much longer TTL instead of ``cache_ttl_trading_days``. Without
    #: this, a genuinely delisted/renamed name (the owner's log showed ANSS,
    #: JNPR, FLT, SQ, K, WBA, HES, DFS, PARA and others) gets re-probed on
    #: EVERY refresh forever, paying a full round trip for a symbol that will
    #: never resolve. 30 trading days means a name that later regains
    #: coverage (a re-listing, a data-vendor backfill) is picked up within
    #: that window at worst -- an accepted trade-off, not an oversight. Also
    #: applied to the "prices_only" state (a real, still-listed security with
    #: no analyst/overview coverage -- typically a closed-end fund -- that
    #: only ``historicalprices`` can serve): that gap is no more likely to
    #: close from one day to the next than a fully unknown ticker is.
    unknown_ticker_ttl_days: int = 30
    #: PRIMARY market-data batching path (owner directive, confirmed by live
    #: probes): ``marketsv3.tipranks.com/api/quotes/GetQuotes?tickers=A,B,C``
    #: (the CIBC-app endpoint, not the widget) returns a real-time quote
    #: snapshot -- current-session OHLCV plus caps -- for every ticker in one
    #: HTTP call, confirmed envelope shape ``{"quotes": [...], "errors":
    #: [...], "metadata": {...}}`` (see
    #: ``providers.market.tipranks._parse_getquotes_envelope`` and the module
    #: docstring's GetQuotes section). ``TipRanksProvider.get_market_caps``
    #: tries this for the WHOLE requested symbol list first -- turning what
    #: used to be one ``dataForTicker`` call per symbol (~2,400 calls for a
    #: full universe refresh) into roughly a dozen batch calls -- before
    #: falling back to the pre-existing per-symbol ``dataForTicker`` path for
    #: anything GetQuotes did not cover. Was ``False`` (an off-by-default,
    #: Canadian-only, UNVERIFIED optimisation) before this confirmation;
    #: flipped to ``True`` now that GetQuotes is the primary path, not an
    #: opt-in extra. Market-cap coverage never depends on this succeeding
    #: regardless -- ``dataForTicker`` remains the fallback for every symbol,
    #: US and Canadian alike, and any GetQuotes failure (bad shape, network
    #: error, a chunk failing outright) is caught and logged, degrading to
    #: that pre-existing per-symbol path with no user-visible effect beyond
    #: the wasted batch call(s).
    use_getquotes_batch: bool = True
    #: Symbols per GetQuotes HTTP call. 200 is conservative-but-batched: the
    #: owner's own brokerage integration batches far more heavily than this
    #: without issue, but this stays deliberately smaller and fully
    #: operator-configurable rather than matching that ceiling exactly, per
    #: this module's self-imposed-limits posture (ADR-0008 Decision 1).
    getquotes_batch_size: int = 200


class PolygonConfig(BaseModel):
    """Polygon.io grouped-daily EOD bars (QA handoff v3 F23 fix).

    One HTTP call returns the ENTIRE US equity market's OHLCV for a trading
    date, which inverts the per-symbol cost model that made whole-universe
    refreshes take minutes and left the scanner starved of history -- see
    ``providers.market.polygon.PolygonProvider``. Recommended primary for an
    operator willing to create a (free) API key:

        [market_data]
        provider = "polygon"
        fallbacks = ["tipranks", "yahoo", "csv"]

    **Enabled-by-key**: with no key resolvable the provider reports itself
    unconfigured and every bars call degrades cleanly to the fallbacks, so
    this configuration is safe even before a key exists. The default
    ``market_data.provider`` stays ``"tipranks"`` for zero-key installs.
    """

    #: Direct config-file key slot. Supported because a free-tier key is
    #: low-stakes, but DISCOURAGED: prefer the ``POLYGON_API_KEY`` env var or
    #: ``claudetrade secrets set polygon_api_key`` -- config.toml is meant to
    #: be shareable. A value here is redacted from ``AppConfig.public_dict``
    #: (so it can never reach the config hash, logs, or persisted run
    #: metadata) -- the one deliberate exception to "credential values are
    #: never held on the config object", see ``public_dict``.
    api_key: str = ""
    #: Credential-store name (``claudetrade.secrets``) checked after the
    #: plain ``POLYGON_API_KEY`` env var and before ``api_key`` above.
    api_key_credential: str = "polygon_api_key"
    #: API origin, overridable without a code change. The vendor has been
    #: reported rebranding (polygon.io -> massive.com); vendors normally keep
    #: an existing API hostname working across a rebrand, but this package
    #: cannot verify that from an egress-blocked build, and a hard-coded host
    #: would make a domain move a code change for every operator. Point this
    #: at whatever origin the current documentation gives if the default ever
    #: stops resolving. Path shape is assumed unchanged.
    base_url: str = "https://api.polygon.io"
    #: The documented free tier is ~5 requests/minute; the provider paces
    #: itself under this and honours a 429's Retry-After regardless. A paid
    #: tier lifts this -- raise to match your plan.
    rate_limit_per_minute: int = 5
    #: A grouped response is ~10k rows (~2-3 MB); allow more transfer time
    #: than the per-symbol providers' 20s default.
    request_timeout_s: float = 30.0


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

    #: ``provider = "reddit"`` (the default, mirroring
    #: ``MarketDataConfig.provider = "tipranks"``) points at the live OAuth/
    #: cookie-session adapter; with no credentials configured it disables
    #: itself cleanly (``NotConfiguredError``, logged, pipeline continues) --
    #: never silently fabricating sentiment. A real refresh log showed the
    #: previous ``"synthetic"`` default storing tens of thousands of
    #: fabricated posts on an install the owner believed was fully live (the
    #: same footgun ``market_data.provider`` used to have before it defaulted
    #: to ``"tipranks"``). Set ``provider = "synthetic"`` explicitly for an
    #: offline/demo install with no credentials and no network.
    enabled: bool = True
    provider: str = "reddit"
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
    #: Optional second cookie for cookie-session mode. Reddit's own web
    #: frontend sends BOTH ``reddit_session`` and ``token_v2`` (an HttpOnly
    #: OAuth JWT cookie -- invisible to ``document.cookie``, only visible via
    #: DevTools' Application/Storage -> Cookies panel, never the Console).
    #: CONFIRMED (owner-validated 2026-07-31): ``reddit_session`` alone,
    #: correctly attached, is sufficient for a 200 from a non-browser client
    #: -- this is an optional extra, not a requirement. ``reddit_session`` is
    #: long-lived (weeks+); ``token_v2`` is short-lived (hours, not weeks),
    #: so try ``reddit_session`` alone first and only add this if that stops
    #: working. When this does not resolve, cookie-session mode behaves
    #: exactly as before (``Cookie: reddit_session=<v>`` only) -- see
    #: ``RedditProvider._cookie_header``.
    token_v2_credential: str = "reddit_token_v2"
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
            # Highest-volume hype venue; strategies A (sentiment breakout)
            # and D (hype-failure short) need visibility into it.
            "wallstreetbets",
            # Half the universe is TSX-listed; US-centric subreddits barely
            # mention Canadian names.
            "CanadianInvestor",
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

    **Auto-enable (owner directive, 2026-07-31)**: mirroring
    ``RedditConfig.enabled``, both ``enabled`` and ``session_enabled`` now
    default to ``True`` -- "use if credentialed", not "on unconditionally".
    Nothing changes for an operator with no X credentials configured at all:
    both live paths still resolve nothing, ``XProvider.__init__`` still
    raises ``NotConfiguredError``, and ``get_social_providers`` still catches
    that and continues without X, exactly as it did when ``enabled`` was
    ``False`` by default. What changes is that the *moment* the owner's own
    ``x_auth_token``/``x_ct0`` cookies (or a bearer token) resolve from the
    secrets store, the source activates on the next refresh with no
    additional flag to flip -- the same self-selecting posture
    ``RedditProvider`` already has for its cookie-session mode. Both fields
    remain explicit, operator-settable disable knobs (``enabled = false``
    turns the whole source off regardless of credentials; ``session_enabled
    = false`` keeps the official-API path while refusing the ToS-risking
    cookie-session path even if cookies are configured).
    """

    #: ``provider = "x"`` (the default, mirroring ``RedditConfig.provider =
    #: "reddit"``) points at the live API/cookie-session adapter; with no
    #: credentials configured it disables itself cleanly
    #: (``NotConfiguredError``, logged, pipeline continues) -- never silently
    #: fabricating sentiment. The previous ``"synthetic"`` default had the
    #: exact footgun RedditConfig documents: combined with ``enabled = True``
    #: it wrote seeded fake posts into a live install's aggregates (QA found
    #: the fabricated ticker BLSH carrying engagement-weighted sentiment while
    #: every real ticker read 0.0). Set ``provider = "synthetic"`` explicitly
    #: for an offline/demo install, and run ``claudetrade db purge-synthetic``
    #: to clear fabricated rows an old default left behind.
    enabled: bool = True
    provider: str = "x"
    bearer_credential: str = "x_bearer_token"
    query_terms: list[str] = Field(default_factory=list)
    max_results_per_query: int = 100
    lookback_hours: int = 48
    rate_limit_per_minute: int = 15
    request_timeout_s: float = 20.0
    store_author_names: bool = False

    # --- Cookie-session mode (ADR-0008 Decision 1; owner-accepted risk) -----
    #: Auto-enabled (owner directive, 2026-07-31): consulted whenever
    #: ``bearer_credential`` does not resolve -- the official API path is
    #: always preferred when available -- AND the two session cookies below
    #: resolve from the secrets store. Set to ``False`` to keep the official
    #: API path (if configured) while refusing to ever attempt the
    #: ToS-risking cookie-session path, no matter what is stored under
    #: ``auth_token_credential``/``ct0_credential``.
    session_enabled: bool = True
    #: Exported from the browser's devtools -> Application/Storage -> Cookies
    #: for x.com, after logging in as the owner: ``auth_token`` and ``ct0``
    #: (the CSRF token cookie). See docs/api-providers.md for the exact
    #: click-path. Stored via the normal credential store, never in config.
    auth_token_credential: str = "x_auth_token"
    ct0_credential: str = "x_ct0"
    #: The opaque query ID in x.com's internal GraphQL search URL
    #: (``/i/api/graphql/<THIS>/SearchTimeline``). X rotates it without notice
    #: and it is account-independent, so it lives here rather than in the
    #: secrets store -- it is a moving endpoint detail, not a credential.
    #:
    #: Empty by default and *deliberately not shipped with a value*: a real
    #: one can only come from the operator's own browser capture (devtools ->
    #: Network, filter "graphql", search any cashtag while logged in, then
    #: copy the path segment before ``/SearchTimeline``). Session mode fails
    #: closed with an explicit message until this is set -- it does not
    #: silently issue doomed requests, and it does not blame the cookies.
    session_query_id: str = ""
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
    credential used or bypassed, no paywall touched.

    Reads ``api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json``, which is
    Stocktwits' own documented, unauthenticated basic-read endpoint -- open
    to anyone, which is why a logged-out browser tab is served HTTP 200 JSON
    from it.

    **Browser-TLS impersonation (owner directive, 2026-07-31; ADR-0008
    Decision 1 Amendment 1).** That same endpoint answers HTTP 403 to a plain
    Python client from the same machine and IP. The confirmed cause is
    Cloudflare bot management gating on the TLS ClientHello fingerprint
    (JA3): a stdlib-``ssl``-backed client presents a handshake no browser
    produces. The provider therefore issues its GET through ``curl_cffi``'s
    browser impersonation (``impersonate`` below), reproducing the
    handshake of the browser the endpoint already serves. This is a scoped,
    owner-authorised exception for this one keyless source; it does not
    extend to credentialed sources (X, Reddit), and every other fail-closed
    guarantee still holds -- conservative human-scale rates with jitter, a
    hard per-cycle symbol cap, no CAPTCHA solving, no proxy rotation, no
    Cloudflare cookie harvesting, and ``SourceBlockedError`` (cycle over, no
    retry loop) if the edge blocks anyway. Impersonation makes a block
    unlikely, not impossible.

    ``curl_cffi`` is an **optional** dependency, lazy-imported at request
    time. Without it the whole application still runs; this source simply
    reports itself unavailable with an install hint.

    On by default (owner directive): the vendor's published unauthenticated
    budget (200 requests/hour) is respected by ``rate_limit_per_minute`` and
    ``max_symbols_per_cycle`` rather than by leaving the source switched off.
    """

    enabled: bool = True
    provider: str = "stocktwits"
    #: curl_cffi browser-impersonation profile supplying the TLS/JA3
    #: fingerprint (and the matching User-Agent + client hints). ``"chrome"``
    #: is curl_cffi's alias for its current newest Chrome build, so it tracks
    #: library upgrades instead of pinning a version that goes stale; explicit
    #: profiles (``"chrome142"``, ``"safari180"``, ``"firefox135"``, ...) are
    #: accepted too -- see curl_cffi's ``BrowserType`` for the full list.
    #: Worth changing only if the edge starts blocking the default profile.
    impersonate: str = "chrome"
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
    #: while accepting a real browser tab. The User-Agent (and matching
    #: ``sec-ch-ua*`` client hints) now come from the ``impersonate`` profile
    #: itself, so they cannot disagree with the TLS fingerprint on the wire --
    #: a mismatch that would be a bot signal in its own right. See
    #: ``providers.social.stocktwits._http_get``/``_browser_headers``.
    user_agent: str = "windows:claudetrade:0.1.0 (research; contact configured by operator)"
    store_author_names: bool = False


class ApeWisdomConfig(BaseModel):
    """ApeWisdom aggregate mention counts for Reddit and 4chan.

    A deliberately different *kind* of source from every other entry in this
    section. Reddit/X/Stocktwits/news providers return individual posts that
    this application then resolves tickers from and classifies. ApeWisdom
    publishes the finished tally instead: per ticker, how many times a
    community mentioned it and how many upvotes those mentions drew, plus
    the same numbers 24h earlier. Free, keyless, and documented at
    ``apewisdom.io/api``.

    Two properties make it worth having despite carrying no post text:

    * **Corpus reach.** It counts whole communities (all of r/wallstreetbets,
      r/stocks, 4chan's /biz/) continuously. This application's own Reddit
      and X fetches are narrow, rate-limited windows by comparison, and QA
      found Stocktwits configured with an empty watchlist feeding nothing.
    * **Tickers, pre-resolved.** Rows arrive as symbols, so nothing here goes
      through the common-word entity resolution that kept minting ``AS``,
      ``YOU`` and ``DAY`` from ordinary English (QA F25). This source
      structurally cannot produce that class of junk.

    What it explicitly does **not** carry is direction: a mention count says
    people are talking, never what they are saying. These observations feed
    the attention axis only and are stored under their own ``source`` labels
    -- they never contribute to ``raw_sentiment``, ``bull_bear_ratio``, or
    the combined ``"all"`` aggregate that strategies score against. See
    ``domain.SymbolAttention`` and ``providers.social.apewisdom``.
    """

    enabled: bool = True
    provider: str = "apewisdom"
    base_url: str = "https://apewisdom.io/api/v1.0"
    #: Which communities to pull, in ApeWisdom's own filter vocabulary.
    #: ``all-stocks`` is its combined equity-subreddit tally (wallstreetbets,
    #: stocks, investing, options, ...); ``4chan`` is the /biz/ board. Both
    #: default on because the two populations differ enough that one is a
    #: poor proxy for the other. Crypto-only filters are deliberately absent
    #: -- this application screens US equities.
    filters: list[str] = Field(default_factory=lambda: ["all-stocks", "4chan"])
    #: Pages to walk per filter (100 tickers per page). Two pages covers the
    #: names with enough chatter to matter; the long tail is single-digit
    #: mention counts that ``min_mentions`` would discard anyway.
    max_pages_per_filter: int = 2
    #: Rows below this many mentions are dropped before storage. One or two
    #: mentions is indistinguishable from noise and would otherwise dominate
    #: the row count without informing anything.
    min_mentions: int = 5
    rate_limit_per_minute: int = 30
    request_timeout_s: float = 20.0
    user_agent: str = "claudetrade/0.1 (research; contact configured by operator)"


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
    """Optional LLM sentiment assistance -- an ensemble ADJUNCT, never the
    decision-maker.

    The system is fully functional with ``provider = "none"`` (the default):
    sentiment falls back entirely to the deterministic RULES-based ensemble
    (``sentiment.classifiers.RuleSentimentClassifier``), which remains the
    MANDATORY floor whether or not AI is configured -- see
    ``sentiment.ai_classifier``'s module docstring. AI is strictly opt-in
    (owner directive, 2026-07-31: "configuration for Claude or ChatGPT, user
    prompted to set one up at setup" -- see ``scripts/setup.ps1``'s
    end-of-run prompt and ``docs/ai-setup.md``); AI output can never relax a
    risk control and a malformed/failed AI response always degrades to the
    rule classifier, never raises into the pipeline.

    **Provider choice and cost**: ``model`` is empty by default, which each
    provider adapter resolves to its own sensible default (see
    ``providers.ai.anthropic_provider.AnthropicProvider`` and
    ``providers.ai.openai_provider.OpenAIProvider``) -- set it explicitly to
    override. For Anthropic, the default is ``"claude-opus-5"``
    ($5/$25 per MTok input/output); ``"claude-haiku-4-5"`` ($1/$5 per MTok)
    is the economical choice for high-volume PER-POST classification at a
    real quality/cost tradeoff -- the owner picks based on post volume and
    budget, this module does not choose for them. For OpenAI, check current
    model names/pricing at platform.openai.com before relying on the default
    here (OpenAI's lineup and pricing move faster than this comment).
    """

    provider: Literal["anthropic", "openai", "none"] = "none"
    #: Empty means "use the provider adapter's own default" -- see the class
    #: docstring. Set explicitly to pin a specific model.
    model: str = ""
    #: Credential name for the Anthropic API key (see ``claudetrade.secrets``).
    #: Consulted only when ``provider == "anthropic"``.
    anthropic_api_key_credential: str = "anthropic_api_key"
    #: Credential name for the OpenAI API key. Consulted only when
    #: ``provider == "openai"``.
    openai_api_key_credential: str = "openai_api_key"
    #: Non-Anthropic-default base URL override (Anthropic-compatible proxy,
    #: self-hosted gateway, etc.). ``None`` uses each SDK's own default.
    base_url: str | None = None
    max_output_tokens: int = 1024
    #: Reserved, currently unused by either shipped adapter: NOT sent to
    #: Anthropic (temperature/top_p/top_k are removed on current Claude
    #: models -- Opus 5/Sonnet 5 return 400 -- see
    #: ``providers.ai.anthropic_provider``), and deliberately not sent to
    #: OpenAI either -- reasoning-tier OpenAI models reject non-default
    #: sampling parameters the same way current Claude models do, and this
    #: field's default model is reasoning-tier. Kept on the config for a
    #: future non-reasoning-model path; not wired into either request today.
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
    #: Per-1M-token prices used only for local cost accounting; defaults are
    #: Claude Opus 5's published rate. Update to match the configured model's
    #: current pricing -- e.g. claude-haiku-4-5 is $1.00/$5.00 per MTok, not
    #: $5.00/$25.00. Check platform.openai.com for current OpenAI pricing.
    input_cost_per_mtok_usd: float = 5.0
    output_cost_per_mtok_usd: float = 25.0

    @property
    def api_key_credential(self) -> str:
        """Credential name for the currently-selected provider.

        Convenience accessor (not a model field, so it never round-trips
        through ``model_dump``/TOML) for callers that just want "the one
        relevant AI credential name" without branching on ``provider``
        themselves -- e.g. ``cli.py``'s ``probe``/``secrets list`` commands.
        Defaults to the Anthropic credential name when ``provider ==
        "none"``, matching this class's Anthropic-first historical default.
        """
        if self.provider == "openai":
            return self.openai_api_key_credential
        return self.anthropic_api_key_credential


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
    #:
    #: Lowered $1B -> $500M at the owner's direction (2026-07-31) to widen the
    #: net to mid-caps. Safe because this is only *eligibility*: the thin-name
    #: guardrails (``FilterConfig.min_avg_dollar_volume_usd``, ``min_price``,
    #: ``exclude_penny_stocks``, ``min_atr_pct``) live at the filter/scoring
    #: layer and are unchanged, so illiquid 500M-1B names are still vetoed there.
    min_market_cap_usd: float = 500_000_000.0
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

    #: Kept equal to ``SentimentConfig.min_unique_authors_for_signal``: this
    #: is the HARD veto layer for the same underlying adequacy question that
    #: layer already answers softly, and a stricter value here silently
    #: hard-failed samples the sentiment module itself considered adequate.
    min_unique_authors: int = 4
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
    #: Hard ceiling (calendar days) on how far back the PERSISTED post
    #: history may be re-read when a refresh rebuilds its rolling baseline
    #: (``sentiment.store.load_stored_posts``, driven from
    #: ``Pipeline.refresh``). Sized for a 120-day analysis window plus
    #: buffer, so a symbol's own normal is measured over quarters rather
    #: than over whatever one fetch happened to return.
    #:
    #: NOT the same thing as ``lookback_days`` above, and conflating the two
    #: is a trap worth naming: ``lookback_days`` is how far back the
    #: PROVIDERS are asked to fetch (every entry point passes it as
    #: ``social_lookback_hours = lookback_days * 24``) and is bounded by
    #: what Reddit/X/news will actually serve, ~72 hours in practice. This
    #: one bounds how much of what we ALREADY STORED is read back, which is
    #: limited only by disk. Raising ``lookback_days`` to get a longer
    #: baseline would just make every refresh beg providers for history they
    #: do not have.
    history_window_days: int = 180
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
    #:
    #: ``fast_window_days`` is the *recent* window. For MENTION growth (see
    #: ``sentiment.aggregation._mention_growth``) the baseline it is compared
    #: against is the ``slow_window_days - fast_window_days`` stretch
    #: IMMEDIATELY PRECEDING it, so the two windows never overlap and the
    #: total span stays ``slow_window_days``. They used to overlap -- the slow
    #: bucket contained every fast post -- which made the "growth" reading an
    #: artifact of the window ratio rather than a measurement: a first-ever
    #: burst of 2 posts and a burst of 200 both scored exactly
    #: ``slow_days/fast_days - 1``.
    fast_window_days: int = 2
    slow_window_days: int = 10
    #: Additive (Laplace-style) smoothing applied to BOTH mention rates, in
    #: posts per covered session, before they are divided. This is what stops
    #: a ratio built on a handful of posts reading as loudly as the same ratio
    #: on a real sample: at a prior of 0.5, one post per session becoming
    #: three reads as +133% instead of the raw +200%, while 10 -> 30 posts per
    #: session -- the identical ratio, on a sample large enough to mean
    #: something -- still reads +190%. Set to 0.0 to compare the raw rates
    #: (not recommended: small samples then dominate every percentile rank
    #: downstream).
    mention_growth_prior_per_session: float = 0.5
    #: Total mentions (recent + baseline window) below which mention growth is
    #: reported as 0.0 -- "not measurable", not "flat". A gate rather than a
    #: smaller prior because below a handful of posts the *sign* is noise too,
    #: and ``mention_acceleration`` is percentile-ranked by strategies a/c
    #: where a noisy sign is worse than an absent reading.
    min_mentions_for_growth: int = 5
    #: Above this share of posts from one author/community, flag concentration.
    source_concentration_alert: float = 0.40
    duplicate_ratio_alert: float = 0.35
    use_ai_classifier: bool = True
    ai_sample_per_symbol: int = 20
    #: Fetch every social provider (Reddit, news RSS, X, Stocktwits, ...)
    #: concurrently with the market-data phases of a refresh (securities /
    #: prices / earnings) rather than strictly after them. Social sources hit
    #: completely different hosts than the market-data provider (TipRanks/
    #: Yahoo), so there is no reason the ~minutes-long social fetch should sit
    #: behind the ~8-minute market pass instead of overlapping it. Only the
    #: NETWORK FETCH moves earlier -- persistence (posts, mentions, daily
    #: sentiment aggregates) still happens on the main refresh thread, after
    #: the securities phase has committed (mention resolution depends on the
    #: alias table ``ingest_securities`` writes), exactly as it does today.
    #: See ``data.ingest.DataIngestor.run_full_refresh``. Set False to
    #: restore the fully sequential order (securities -> prices -> earnings
    #: -> social fetch -> sentiment persist) -- useful for tests/debugging
    #: where a strict, single-threaded ordering is easier to reason about.
    fetch_concurrently: bool = True
    #: How long ``run_full_refresh`` waits, once the market-data phases are
    #: done, for a still-running background social fetch before giving up on
    #: it for this refresh. A social fetch is minutes at most in practice --
    #: this is deliberately generous so it is essentially never hit in
    #: normal operation, not a tuning knob. On timeout the refresh proceeds
    #: with zero posts from the abandoned fetch (a warning is logged); the
    #: background thread is a daemon and is not killed, but its result is
    #: discarded rather than raced with the main thread -- the same ground
    #: gets covered on the next refresh.
    fetch_join_timeout_s: float = 300.0


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
            # The breakout with no confirming social sample. It is a separate
            # strategy rather than a branch inside ``sentiment_breakout``
            # because it is a different thesis with a different edge, and the
            # two are mutually exclusive by construction -- see
            # ``strategies.f_volume_breakout``. Disable it to run a
            # sentiment-only screen without also losing the confirmed
            # breakouts.
            "volume_breakout",
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
    #: Share of the ATTENTION component taken from aggregator sources
    #: (ApeWisdom's ``apewisdom:<community>`` rows) rather than from this
    #: application's own post counts. Aggregators watch whole communities
    #: continuously where the local fetches are narrow, rate-limited windows
    #: into the same populations, so their mention volume is the better
    #: attention input -- but only after each source is ranked against its
    #: OWN history (see ``signals.scoring._attention_score``); the two count
    #: scales differ by ~100x and must never be summed. Applies to the
    #: attention axis ONLY: an aggregator row carries no polarity, no authors
    #: and no text, so it can never reach ``reddit_sentiment``,
    #: ``x_sentiment``, ``manipulation_risk`` or ``data_confidence``. Set to
    #: 0.0 to score attention from local post counts alone.
    attention_aggregator_weight: float = 0.45
    #: Observations of an aggregator source's own history (including today's)
    #: required before its reading is used at all. Below this the percentile
    #: rank that normalises it has no distribution to rank against, and using
    #: the raw ratio instead would mix a wide-corpus scale into a
    #: narrow-corpus one -- the exact swamping this normalisation exists to
    #: prevent. Under-covered sources are simply absent from the axis.
    attention_min_history_sessions: int = 10


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
    #: Minimum CURRENT polarity for ``sentiment_breakout`` to fire at all --
    #: not a scored component but a hard precondition, because a strategy
    #: named "sentiment breakout" recommending a name with no social sample,
    #: or with negative sentiment, is mislabelled rather than merely
    #: degraded. Applied to the raw decayed mean AND to the
    #: manipulation-resistant one-vote-per-author mean, so a single prolific
    #: poster cannot supply the confirmation on his own. Deliberately just
    #: above zero: this asks for "positive, not merely not-negative", and the
    #: strength of the reading is what the scored components measure.
    breakout_min_positive_sentiment: float = 0.05

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
    """Background scheduling.

    Two unrelated things live here, and only the second one is wired up:

    * ``enabled`` + the ``*_cron`` fields describe the never-built APScheduler
      job runner (see ``docs/known-limitations.md``). They remain inert, and
      default off, so nothing that already reads them changes meaning.
    * ``social_collection_*`` drives :mod:`claudetrade.scheduler` -- the
      in-process hourly social/attention collector the web API server starts
      from its lifespan. It is ON by default, unlike the cron block, because
      the data it collects **cannot be backfilled**: Reddit ``/new`` paging,
      X recent-search and ApeWisdom's rolling 24h snapshot only ever serve
      roughly the last few days, so the 120-session baseline this
      application's premise needs can only be accumulated forward. An hour
      the app was open but not collecting is an hour permanently missing
      from that baseline, which is a far worse default than the modest
      request volume of collecting it.
    """

    enabled: bool = False
    timezone: str = "America/New_York"
    #: Cron-ish schedules; the CLI can also run any job once, on demand.
    market_data_refresh_cron: str = "30 16 * * mon-fri"
    social_refresh_cron: str = "0 */4 * * *"
    scan_cron: str = "45 16 * * mon-fri"
    paper_mark_cron: str = "5 16 * * mon-fri"
    misfire_grace_time_s: int = 3600

    # --- in-app hourly social/attention collection -------------------------
    #: Collect social posts + aggregator attention on a timer for as long as
    #: the web API server is running. Set false to opt out entirely (the
    #: operator then owns keeping history alive via `claudetrade sentiment
    #: collect` or an OS timer -- there is no other way to recover the gap).
    social_collection_enabled: bool = True
    #: Cadence. Hourly is the default because ApeWisdom's snapshot is a
    #: rolling 24h window with no history endpoint, so sampling it once a day
    #: throws away 23/24 of what it could have told us.
    social_collection_interval_minutes: int = 60
    #: Random 0..N seconds added to every wait. Without it, every restart
    #: (and every install) fires on the same wall-clock boundary, which both
    #: synchronises load against the upstream APIs and makes two ClaudeTrade
    #: processes on one machine collide on the refresh lock every single hour
    #: instead of drifting apart.
    social_collection_jitter_seconds: int = 300
    #: How far back each collection asks the social providers to look. Wider
    #: than the cadence ON PURPOSE: a tick skipped (lock held) or missed (the
    #: machine was asleep) is then recovered by the next one rather than lost,
    #: and 72h still covers the current session's aggregation window across a
    #: weekend gap (Friday close -> Monday). It also matches what the Reddit
    #: and news providers are already configured to look back, so it does not
    #: push any provider past its own window; page caps bound the cost.
    social_collection_lookback_hours: int = 72
    #: Optional quieter overnight cadence, in minutes; 0 (the default) means
    #: "same cadence around the clock". Social flows 24/7 -- the ApeWisdom
    #: snapshot moves overnight and Asian-hours chatter is real -- so slowing
    #: down at night costs real samples; it is offered, not recommended.
    social_collection_overnight_interval_minutes: int = 0
    #: Half-open [start, end) hour range, in ``timezone``, that counts as
    #: overnight when the field above is non-zero. Wraps past midnight.
    social_collection_overnight_start_hour: int = 22
    social_collection_overnight_end_hour: int = 4
    #: Ceiling on the exponential back-off applied after consecutive failed
    #: collections. Bounded so a source that was down for a night is retried
    #: within a few hours rather than effectively never again.
    social_collection_max_backoff_minutes: int = 240


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


class McpConfig(BaseModel):
    """MCP stdio server behaviour (``claudetrade mcp``).

    Exists because of a QA-observed production lockup (handoff v3, F26): the
    ``mcp`` package runs *sync* tool functions directly on the server's event
    loop thread, so one tool call that stalls -- e.g. reads slowed by a
    concurrent CLI data refresh writing to the same SQLite file -- froze the
    entire server, including the transport's message reader, and every
    subsequent call appeared dead. Tool bodies therefore run on a worker
    thread with a hard deadline (see ``mcp_server.build_server``); on expiry
    the client gets a structured ``timed_out`` payload instead of silence.
    """

    #: Deadline for ordinary (read-mostly) tools. Generous next to the
    #: sub-second normal case, but small enough that a wedged database never
    #: makes the server *appear* dead -- the QA acceptance is "MCP reads
    #: never hang", not "reads are always fast".
    tool_timeout_seconds: float = 30.0
    #: Separate, larger deadline for ``run_scan`` only: a full-universe scan
    #: is a legitimate multi-minute compute-and-write job, and killing it at
    #: the read deadline would make the tool useless on a real installation.
    scan_timeout_seconds: float = 300.0
    #: Master switch for ``submit_research_revision``. False makes the tool
    #: refuse with a structured ``{"accepted": false, ...}`` payload rather
    #: than raise -- an operator who wants this installation strictly
    #: read-only from MCP (no append to the research ledger at all) can say
    #: so without disabling every other tool.
    research_writes_enabled: bool = True
    #: Hard cap on the magnitude of any single component-score adjustment an
    #: MCP research revision may apply, in the same 0-100 units the
    #: component itself is scored in. Bounded so that "web research nudges
    #: the ranking" cannot become "web research repaints the score" --
    #: ``signals.research.ResearchLedger.append_research_revision`` clamps
    #: every submitted delta to +/- this value before it is stored.
    max_component_adjustment: float = 20.0

    @field_validator("tool_timeout_seconds", "scan_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout must be positive")
        return v

    @field_validator("max_component_adjustment")
    @classmethod
    def _bounded_adjustment_cap(cls, v: float) -> float:
        if not (0 < v <= 50):
            raise ValueError("max_component_adjustment must be > 0 and <= 50")
        return v


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
    polygon: PolygonConfig = Field(default_factory=PolygonConfig)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    x: XConfig = Field(default_factory=XConfig)
    stocktwits: StocktwitsConfig = Field(default_factory=StocktwitsConfig)
    apewisdom: ApeWisdomConfig = Field(default_factory=ApeWisdomConfig)
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
    mcp: McpConfig = Field(default_factory=McpConfig)

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
        credential *values* are never held on this object in the first place
        -- with one deliberate exception: ``PolygonConfig.api_key`` accepts a
        direct value for low-friction free-tier setup (see that field's
        docstring), so it is redacted here. Redaction also keeps
        ``config_hash`` independent of the key's value, which is correct: a
        credential is not a strategy-relevant parameter.
        """
        data = self.model_dump(mode="json", exclude={"paths"})
        if data.get("polygon", {}).get("api_key"):
            data["polygon"]["api_key"] = "***"
        return data

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
            "ai": self.ai.provider != "none",
            "notifications": self.notifications.enabled,
            "scheduler": self.scheduler.enabled,
            # Listed separately from "scheduler" because it is the only
            # scheduling that is actually implemented -- see SchedulerConfig.
            "hourly_social_collection": self.scheduler.social_collection_enabled,
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
