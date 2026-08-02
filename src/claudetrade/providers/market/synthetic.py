"""Fully-offline, deterministic synthetic market data generator.

**THIS DATA IS FABRICATED.** Every symbol, price series, corporate action and
reference field produced by this module is a procedurally generated fiction
seeded from a fixed integer. Nothing here is fitted to, derived from, or
intended to resemble any specific real security's actual historical prices.
Backtest results computed against this provider say **nothing** about how any
strategy would have performed in real markets -- its only purpose is to let the
rest of ClaudeTrade (universe construction, signal generation, risk controls,
the backtest engine, the UI) run and be exercised end-to-end with zero API keys
and zero network access, on data whose generating process is fully known so
engine correctness can be validated against it.

Design summary (see class docstring for the exact recipe):

* A shared market-wide return factor with a sticky two-state volatility regime
  and a slow-moving drift regime (bull/neutral/bear), so quiet and turbulent
  eras alternate the way real cycles do.
* Eleven GICS-style sector factors, each partially correlated with the market
  factor.
* Per-name beta to the market and sector factors, idiosyncratic Student-t
  ("fat tail") noise, mean-reverting volume with spikes on big moves, and
  occasional earnings-day gaps.
* Ordinary equities are delisted at roughly 2.2% a year across the history --
  most (not all) after a sustained decline, which is the realistic failure
  mode that makes an *unbiased* backtest possible: a universe that quietly
  drops its losers before you can test against them is the single most common
  way a paper strategy is fooled.
* A handful of forward/reverse splits, with ``adj_close`` kept continuous and
  ``close`` left as the raw (jumping) print, exactly as a real vendor feed
  would present them.

Everything is generated once per seed and cached in memory (module-level, so
repeated construction of the provider within one process is instant).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import threading
from dataclasses import dataclass, field

import numpy as np

from claudetrade.config import MarketDataConfig
from claudetrade.domain import Bar, CorporateAction, SecurityInfo
from claudetrade.providers.base import MarketDataProvider, ProviderStatus
from claudetrade.utils.timeutils import trading_day_range

log = logging.getLogger(__name__)

#: Generous, fixed (not "today"-relative) history window. Fixed so that the
#: generated series is identical regardless of what day the app happens to run
#: on -- tying it to `utc_now()` would silently break the "deterministic given
#: a seed" contract every single day.
HISTORY_START = dt.date(2015, 1, 2)
HISTORY_END = dt.date(2030, 12, 31)

_DEFAULT_SEED = 1337

_GICS_SECTORS: tuple[str, ...] = (
    "Technology",
    "Health Care",
    "Financials",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Industrials",
    "Materials",
    "Utilities",
    "Real Estate",
    "Communication Services",
)

# --------------------------------------------------------------------------
# Fictional name generation
# --------------------------------------------------------------------------

_PREFIXES: tuple[str, ...] = (
    "North", "South", "East", "West", "Blue", "Crimson", "Golden", "Silver",
    "Iron", "Cedar", "Maple", "Summit", "Harbor", "Vertex", "Meridian",
    "Falcon", "Atlas", "Bright", "Clearwater", "Stonebridge", "Ridgeline",
    "Lighthouse", "Cobalt", "Amber", "Granite", "Willow", "Foxglove", "Ember",
    "Ironwood", "Nimbus", "Quartz", "Highland", "Driftwood", "Cascade",
    "Bramble", "Slate", "Marigold", "Frontier", "Anchor", "Beacon", "Sable",
    "Hollow", "Redwood", "Copperfield", "Windermere", "Saltmarsh", "Palisade",
)

#: sector -> (suffix word choices, fictional industry label per suffix)
_SECTOR_WORDS: dict[str, tuple[str, ...]] = {
    "Technology": ("Semiconductor", "Systems", "Software", "Networks", "Robotics", "Cyber", "Cloud",
                   "Data", "Photonics", "Quantum"),
    "Health Care": ("Biosciences", "Therapeutics", "Pharma", "Diagnostics", "Health", "Medical",
                    "Genomics", "Labs"),
    "Financials": ("Financial", "Capital", "Trust", "Bancorp", "Holdings", "Credit", "Group"),
    "Consumer Discretionary": ("Retail", "Apparel", "Motors", "Leisure", "Hospitality", "Brands"),
    "Consumer Staples": ("Foods", "Beverages", "Grocers", "Provisions", "Consumer"),
    "Energy": ("Energy", "Petroleum", "Resources", "Power", "Fuels"),
    "Industrials": ("Industrial", "Manufacturing", "Aerospace", "Logistics", "Engineering"),
    "Materials": ("Materials", "Mining", "Chemicals", "Minerals"),
    "Utilities": ("Utilities", "Electric", "Water", "Gas"),
    "Real Estate": ("Properties", "Realty", "REIT", "Estates"),
    "Communication Services": ("Media", "Communications", "Broadcasting", "Telecom", "Streaming"),
}

_CORP_SUFFIXES: tuple[str, ...] = ("Corp", "Inc", "Holdings", "Group", "Co")

_EXCHANGES: tuple[str, ...] = ("NASDAQ", "NYSE", "NYSE ARCA")

#: 3 fictional leveraged/inverse products, deliberately not real tickers.
_LEVERAGED_SPECS: tuple[dict, ...] = (
    {"symbol": "TRP3X", "name": "TriPoint Bull 3X Daily ETF (synthetic)", "leverage": 3.0},
    {"symbol": "TRN3X", "name": "TriPoint Bear 3X Daily ETF (synthetic)", "leverage": -3.0},
    {"symbol": "DBL2X", "name": "DoubleArc Inverse 2X Daily ETF (synthetic)", "leverage": -2.0},
)


def _stable_seed(*parts: object) -> int:
    """Deterministic 32-bit seed derived from arbitrary hashable parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass(slots=True)
class _SymbolSpec:
    """Internal generation parameters for one symbol. Not part of the public API."""

    symbol: str
    sector: str
    is_etf: bool = False
    is_leveraged: bool = False
    beta_market: float = 1.0
    beta_sector: float = 0.0
    idio_vol: float = 0.02
    drift: float = 0.0
    listed_date: dt.date = HISTORY_START
    delisted_date: dt.date | None = None
    delisting_is_decline: bool = True
    splits: list[tuple[dt.date, float]] = field(default_factory=list)
    initial_price: float = 50.0
    shares_outstanding: float = 5.0e7


@dataclass(slots=True)
class _GeneratedSeries:
    bars: list[Bar]
    corporate_actions: list[CorporateAction]
    security: SecurityInfo


class SyntheticMarketProvider(MarketDataProvider):
    """Deterministic, fully offline market data generator. See module docstring.

    Everything is generated eagerly at construction time (fast: ~120 names x a
    few thousand trading days is a trivial amount of vectorised numpy work) so
    that reference fields such as ``market_cap_usd`` can reflect the actually
    generated final price rather than a placeholder. Results for a given seed
    are cached at module scope, so re-constructing the provider (e.g. once per
    CLI invocation) does not redo the work.
    """

    name = "synthetic"

    def __init__(self, config: MarketDataConfig | None = None, *, seed: int = _DEFAULT_SEED):
        self._config = config or MarketDataConfig()
        self._seed = seed
        self._calls = 0
        cache_key = (seed, tuple(sorted(self._config.sector_etfs.items())), self._config.benchmark_symbol)
        with _CACHE_LOCK:
            cached = _UNIVERSE_CACHE.get(cache_key)
            if cached is None:
                cached = _generate_universe(self._config, seed)
                _UNIVERSE_CACHE[cache_key] = cached
        self._series: dict[str, _GeneratedSeries] = cached

    # -- MarketDataProvider protocol ---------------------------------------

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=True,
            configured=True,
            message=(
                "SYNTHETIC/FABRICATED offline generator; ticker names and prices "
                "are not real market data"
            ),
            supports_point_in_time=True,
            supports_delisted=True,
            rate_limit_per_minute=None,
            calls_made=self._calls,
            licence_note=(
                "SYNTHETIC DATA -- fabricated for engine validation only. This is not real "
                "market data, is not derived from any real security, and results computed "
                "against it say nothing about real-market performance. Safe to redistribute; "
                "unsuitable as evidence of strategy edge."
            ),
            capabilities={
                "intraday": True,
                "corporate_actions": True,
                "point_in_time_universe": True,
                "synthetic_data": True,
            },
        )

    def get_daily_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        *,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        self._calls += 1
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            series = self._series.get(symbol)
            if series is None:
                out[symbol] = []
                continue
            bars = [b for b in series.bars if start <= b.session <= end]
            if not adjusted:
                bars = [
                    Bar(
                        symbol=b.symbol, session=b.session, open=b.open, high=b.high,
                        low=b.low, close=b.close, volume=b.volume, adj_close=None, source=b.source,
                    )
                    for b in bars
                ]
            out[symbol] = bars
        return out

    def get_intraday_bars(
        self,
        symbols: list[str],
        start: dt.datetime,
        end: dt.datetime,
        *,
        interval_minutes: int = 5,
    ) -> dict[str, list[Bar]]:
        """Synthesise an intraday path from each daily bar.

        Limitation shared with the rest of the codebase: ``Bar`` carries only a
        ``session`` date, not a timestamp, so intraday bars for one session are
        distinguishable only by their position in the returned list (assumed to
        be in chronological order at the requested interval starting at the
        regular-session open). Callers needing true intraday timestamps must
        derive them from list position and ``interval_minutes``.
        """
        self._calls += 1
        start_date, end_date = start.date(), end.date()
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            series = self._series.get(symbol)
            if series is None:
                out[symbol] = []
                continue
            intraday: list[Bar] = []
            for bar in series.bars:
                if not (start_date <= bar.session <= end_date):
                    continue
                intraday.extend(
                    _intraday_from_daily(bar, interval_minutes, seed_key=f"{self._seed}:{symbol}:{bar.session}")
                )
            out[symbol] = intraday
        return out

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        return {s: self._series[s].security for s in symbols if s in self._series}

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[CorporateAction]]:
        out: dict[str, list[CorporateAction]] = {}
        for symbol in symbols:
            series = self._series.get(symbol)
            if series is None:
                out[symbol] = []
                continue
            out[symbol] = [ca for ca in series.corporate_actions if start <= ca.session <= end]
        return out

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        infos = [s.security for s in self._series.values()]
        if as_of is None:
            return infos
        # `is_active_on` is exactly the point-in-time rule we need: excludes
        # not-yet-listed names and already-delisted names, while still
        # including a name that is *going to* be delisted later -- that is
        # what keeps this free of survivorship bias.
        return [info for info in infos if info.is_active_on(as_of)]


_UNIVERSE_CACHE: dict[tuple, dict[str, _GeneratedSeries]] = {}
_CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Universe construction
# --------------------------------------------------------------------------


def _make_symbol(rng: np.random.Generator, prefix: str, word: str, used: set[str]) -> str:
    base = (prefix[:2] + word[:2]).upper()
    candidate = base
    suffix_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    attempt = 0
    while candidate in used:
        candidate = base + suffix_letters[int(rng.integers(0, 26))]
        attempt += 1
        if attempt > 50:  # pragma: no cover - defensive; word lists are large enough
            candidate = f"{base}{len(used)}"
            break
    used.add(candidate)
    return candidate


def _generate_universe(config: MarketDataConfig, seed: int) -> dict[str, _GeneratedSeries]:
    rng = np.random.default_rng(seed)
    sessions = trading_day_range(HISTORY_START, HISTORY_END)
    n_days = len(sessions)

    market_returns, _regime = _generate_market_factor(rng, n_days)
    sector_returns = {
        sector: _generate_sector_factor(rng, n_days, market_returns) for sector in _GICS_SECTORS
    }

    specs: list[_SymbolSpec] = []
    used_symbols: set[str] = set()

    # -- benchmark + sector ETFs: real-world tickers (needed because the rest
    # of the app looks these up by name from config), synthetic prices behind them.
    used_symbols.add(config.benchmark_symbol)
    specs.append(
        _SymbolSpec(
            symbol=config.benchmark_symbol,
            sector="Broad Market",
            is_etf=True,
            beta_market=1.0,
            beta_sector=0.0,
            idio_vol=0.003,
            # No explicit drift: beta_market=1.0 against a market factor that
            # already carries the long-run drift target would otherwise
            # double-count it.
            drift=0.0,
            initial_price=250.0,
            shares_outstanding=9.0e8,
        )
    )
    for sector, etf_symbol in config.sector_etfs.items():
        used_symbols.add(etf_symbol)
        specs.append(
            _SymbolSpec(
                symbol=etf_symbol,
                sector=sector,
                is_etf=True,
                beta_market=0.25,
                beta_sector=1.0,
                idio_vol=0.005,
                drift=0.0,  # factor exposures already carry the drift; see SPY note above
                initial_price=float(rng.uniform(40, 140)),
                shares_outstanding=float(rng.uniform(5e7, 3e8)),
            )
        )

    for lev in _LEVERAGED_SPECS:
        used_symbols.add(lev["symbol"])
        specs.append(
            _SymbolSpec(
                symbol=lev["symbol"],
                sector="Broad Market",
                is_etf=True,
                is_leveraged=True,
                beta_market=lev["leverage"],
                beta_sector=0.0,
                idio_vol=0.01,
                drift=0.0,
                initial_price=float(rng.uniform(15, 60)),
                shares_outstanding=float(rng.uniform(1e7, 8e7)),
            )
        )

    # -- ordinary equities, spread across sectors --------------------------
    n_stocks = 105
    base_per_sector, remainder = divmod(n_stocks, len(_GICS_SECTORS))
    # Delisting rate. US equities historically leave the exchange at roughly
    # 2-4% a year once mergers, acquisitions and failures are counted together.
    # Because these are spread uniformly across ~16 years of history, the
    # cumulative fraction has to be large for any *individual* backtest window
    # to contain a realistic number of failures: at 12% total, a two-year window
    # averaged fewer than two delistings and the delisting code path was
    # effectively never exercised.
    _ANNUAL_DELISTING_RATE = 0.022
    _history_years = max(1.0, (HISTORY_END - HISTORY_START).days / 365.25)
    n_delisted_target = round(n_stocks * min(0.60, _ANNUAL_DELISTING_RATE * _history_years))
    delisted_so_far = 0
    stock_index = 0

    for sector_i, sector in enumerate(_GICS_SECTORS):
        count = base_per_sector + (1 if sector_i < remainder else 0)
        words = _SECTOR_WORDS[sector]
        for _ in range(count):
            prefix = _PREFIXES[int(rng.integers(0, len(_PREFIXES)))]
            word = words[int(rng.integers(0, len(words)))]
            symbol = _make_symbol(rng, prefix, word, used_symbols)

            listed_date = HISTORY_START
            if rng.uniform() < 0.10:  # ~10% IPO after the start of history
                offset_days = int(rng.integers(60, len(sessions) - 400))
                listed_date = sessions[offset_days]

            delisted_date: dt.date | None = None
            delisting_is_decline = True
            # Spread remaining delistings roughly evenly instead of front-loading them.
            remaining_stocks = n_stocks - stock_index
            remaining_delistings = n_delisted_target - delisted_so_far
            if remaining_delistings > 0 and rng.uniform() < (remaining_delistings / max(1, remaining_stocks)):
                listed_idx = sessions.index(listed_date)
                earliest = max(listed_idx + 250, int(n_days * 0.15))
                latest = int(n_days * 0.95)
                if latest > earliest:
                    delist_idx = int(rng.integers(earliest, latest))
                    delisted_date = sessions[delist_idx]
                    delisting_is_decline = rng.uniform() < 0.80
                    delisted_so_far += 1

            splits: list[tuple[dt.date, float]] = []
            if rng.uniform() < 0.06:
                split_idx = int(rng.integers(int(n_days * 0.2), int(n_days * 0.8)))
                ratio = float(rng.choice([1.5, 2.0, 2.0, 3.0, 0.2]))
                splits.append((sessions[split_idx], ratio))

            specs.append(
                _SymbolSpec(
                    symbol=symbol,
                    sector=sector,
                    is_etf=False,
                    beta_market=float(np.clip(rng.normal(1.0, 0.3), 0.2, 2.0)),
                    beta_sector=float(np.clip(rng.normal(0.65, 0.22), 0.0, 1.2)),
                    idio_vol=float(rng.uniform(0.015, 0.04)),
                    # Idiosyncratic growth premium/discount on top of market+sector
                    # exposure; kept modest so that compounding over up to 16 years
                    # of history stays within a plausible (if generous) range even
                    # for the luckiest/unluckiest few names in a 105-stock universe.
                    drift=float(np.clip(rng.normal(0.00010, 0.00018), -0.0005, 0.0006)),
                    listed_date=listed_date,
                    delisted_date=delisted_date,
                    delisting_is_decline=delisting_is_decline,
                    splits=splits,
                    initial_price=float(math.exp(rng.uniform(math.log(8), math.log(300)))),
                    shares_outstanding=float(math.exp(rng.uniform(math.log(2e7), math.log(2e9)))),
                )
            )
            stock_index += 1

    result: dict[str, _GeneratedSeries] = {}
    for spec in specs:
        result[spec.symbol] = _generate_symbol_series(
            spec, sessions, market_returns, sector_returns.get(spec.sector), seed
        )
    return result


def _generate_market_factor(rng: np.random.Generator, n_days: int) -> tuple[np.ndarray, np.ndarray]:
    """Shared market log-return factor with a sticky vol regime and a
    mean-reverting drift regime.

    The drift follows a slow Ornstein-Uhlenbeck process around a long-run
    target rather than an unconstrained multi-state Markov chain: bull/bear
    *eras* still emerge (the process wanders for months to years above or
    below the target before reverting), but the reversion is what keeps a
    16-year fixed-seed simulation from occasionally compounding into an
    absurd multi-decade bull run purely from small-sample regime-occupancy
    luck -- which is exactly what an unconstrained 3-state chain did during
    development and is why this is not implemented as one.
    """
    target_drift = 0.00025  # long-run daily drift, ~6.5%/year
    kappa = 0.01  # reversion speed of the drift process itself
    eta = 0.00004  # drift-of-drift noise scale

    # Vol regime: 2 states (quiet / volatile), sticky (mean duration ~70 days).
    vol_state = np.zeros(n_days, dtype=np.int64)
    state = 0
    p_switch_vol = 1.0 / 70.0

    drift = np.zeros(n_days)
    d = target_drift
    drift_noise = rng.normal(0.0, 1.0, size=n_days)
    vol_switches = rng.uniform(size=n_days)
    for t in range(n_days):
        if vol_switches[t] < p_switch_vol:
            state = int(rng.integers(0, 2))
        vol_state[t] = state
        d = d + kappa * (target_drift - d) + eta * drift_noise[t]
        drift[t] = d

    vol_by_state = np.array([0.007, 0.020])  # quiet, volatile daily sigma
    sigmas = vol_by_state[vol_state]
    # Fat tails via Student-t (df=4), scaled to unit variance before applying sigma.
    t_noise = rng.standard_t(df=4, size=n_days) / math.sqrt(2.0)
    returns = drift + sigmas * t_noise
    return returns, vol_state


def _generate_sector_factor(
    rng: np.random.Generator, n_days: int, market_returns: np.ndarray
) -> np.ndarray:
    """Sector-wide return partially correlated with the market factor."""
    idio = rng.normal(0.0, 0.009, size=n_days)
    sector_beta = float(rng.uniform(0.4, 0.8))
    return sector_beta * market_returns + idio


def _generate_symbol_series(
    spec: _SymbolSpec,
    sessions: list[dt.date],
    market_returns: np.ndarray,
    sector_returns: np.ndarray | None,
    seed: int,
) -> _GeneratedSeries:
    rng = np.random.default_rng(_stable_seed(seed, spec.symbol))
    n_days = len(sessions)

    listed_idx = sessions.index(spec.listed_date) if spec.listed_date in sessions else 0
    delisted_idx = sessions.index(spec.delisted_date) if spec.delisted_date in sessions else None
    last_idx = (delisted_idx - 1) if delisted_idx is not None else (n_days - 1)

    idio = rng.standard_t(df=5, size=n_days) / math.sqrt(1.666) * spec.idio_vol

    sector_component = sector_returns if sector_returns is not None else np.zeros(n_days)
    log_returns = spec.drift + spec.beta_market * market_returns + spec.beta_sector * sector_component + idio

    # Occasional earnings-day gaps: a handful of dates per year, deterministic
    # per symbol, each an extra return shock independent of the daily process.
    earnings_days = _quarterly_offsets(rng, n_days)
    gap_shocks = rng.standard_t(df=3, size=len(earnings_days)) * 0.035
    for day_idx, shock in zip(earnings_days, gap_shocks, strict=True):
        if 0 <= day_idx < n_days:
            log_returns[day_idx] += shock

    # Distress leg into a decline-delisting: extra negative drift for the
    # trailing ~250 sessions, plus a final sharp drop on the delisting print.
    if delisted_idx is not None and spec.delisting_is_decline:
        window_start = max(listed_idx, delisted_idx - 250)
        log_returns[window_start:delisted_idx] -= 0.0045
        if delisted_idx - 1 >= 0:
            log_returns[delisted_idx - 1] -= 0.35
    elif delisted_idx is not None:
        # Acquired-at-a-premium delisting: one large positive jump near the end.
        if delisted_idx - 2 >= 0:
            log_returns[delisted_idx - 2] += 0.28

    log_price = np.zeros(n_days)
    log_price[listed_idx] = math.log(spec.initial_price)
    for i in range(listed_idx + 1, last_idx + 1):
        log_price[i] = log_price[i - 1] + log_returns[i]
    adj_close = np.exp(log_price)

    # Split adjustment: raw close = adj_close * (product of split ratios still
    # ahead of that date). See module docstring for the derivation.
    cumulative_ratio = np.ones(n_days)
    for split_date, ratio in spec.splits:
        split_idx = sessions.index(split_date)
        cumulative_ratio[:split_idx] *= ratio
    raw_close = adj_close * cumulative_ratio

    # Volume: mean-reverting around a per-symbol baseline, with spikes on big
    # moves (proxying for higher participation around news/gaps).
    baseline_volume = math.exp(rng.uniform(math.log(2e4), math.log(6e6)))
    volume = np.zeros(n_days)
    vol_state = baseline_volume
    move_size = np.abs(log_returns)
    for i in range(listed_idx, last_idx + 1):
        reversion = 0.15 * (baseline_volume - vol_state)
        spike = baseline_volume * min(4.0, move_size[i] * 18.0)
        noise = vol_state * float(rng.normal(0.0, 0.12))
        vol_state = max(baseline_volume * 0.05, vol_state + reversion + spike * 0.3 + noise)
        volume[i] = vol_state
    raw_volume = volume * cumulative_ratio  # more shares outstanding -> proportionally more volume

    # OHLC from the close path: a plausible daily range around each close,
    # widened on big-move days, with open anchored near the prior close.
    bars: list[Bar] = []
    prev_close = raw_close[listed_idx]
    for i in range(listed_idx, last_idx + 1):
        close = max(0.01, float(raw_close[i]))
        day_range_pct = 0.006 + min(0.06, abs(float(log_returns[i])) * 1.4) + float(rng.uniform(0, 0.004))
        rng_open_gap = float(rng.normal(0.0, 0.003))
        open_ = max(0.01, prev_close * (1.0 + rng_open_gap))
        hi_extra = close * day_range_pct * float(rng.uniform(0.3, 1.0))
        lo_extra = close * day_range_pct * float(rng.uniform(0.3, 1.0))
        high = max(open_, close) + hi_extra
        low = max(0.01, min(open_, close) - lo_extra)
        bars.append(
            Bar(
                symbol=spec.symbol,
                session=sessions[i],
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=round(float(raw_volume[i]), 1),
                adj_close=round(float(adj_close[i]), 6),
                source="synthetic",
            )
        )
        prev_close = close

    corporate_actions: list[CorporateAction] = []
    for split_date, ratio in spec.splits:
        corporate_actions.append(
            CorporateAction(
                symbol=spec.symbol, session=split_date, kind="split", ratio=ratio,
                detail=f"synthetic {'forward' if ratio >= 1 else 'reverse'} split, ratio={ratio}",
            )
        )
    if spec.delisted_date is not None:
        reason = "sustained decline" if spec.delisting_is_decline else "acquisition"
        corporate_actions.append(
            CorporateAction(
                symbol=spec.symbol, session=spec.delisted_date, kind="delisting",
                detail=f"synthetic delisting ({reason})",
            )
        )

    last_close = bars[-1].close if bars else spec.initial_price
    # `market_cap_usd` is a purely informational reference field (used by
    # liquidity/size filters elsewhere); a lucky multi-decade compounding run
    # can occasionally put the raw price*shares product at an economically
    # absurd figure (bigger than any real company). Clip it to the largest
    # plausible real-world market cap rather than let one outlier undermine
    # every size-based filter downstream -- the price series itself is left
    # untouched, only this display/filter figure is bounded.
    market_cap = min(last_close * spec.shares_outstanding, 3.5e12)
    security = SecurityInfo(
        symbol=spec.symbol,
        name=_display_name(spec),
        exchange=_EXCHANGES[int(rng.integers(0, len(_EXCHANGES)))] if not spec.is_etf else "NYSE ARCA",
        sector=spec.sector,
        industry=f"{spec.sector} (synthetic)",
        market_cap_usd=round(market_cap, 2),
        shares_outstanding=spec.shares_outstanding,
        is_etf=spec.is_etf,
        is_leveraged_or_inverse=spec.is_leveraged,
        listed_date=spec.listed_date,
        delisted_date=spec.delisted_date,
    )
    return _GeneratedSeries(bars=bars, corporate_actions=corporate_actions, security=security)


def _display_name(spec: _SymbolSpec) -> str:
    if spec.is_leveraged:
        return next(lev["name"] for lev in _LEVERAGED_SPECS if lev["symbol"] == spec.symbol)
    if spec.is_etf:
        return f"Synthetic {spec.sector} Sector Fund ({spec.symbol}, fictional data)"
    # Deterministic-looking company name from the symbol's own letters, purely
    # cosmetic -- the actual generation used a prefix/word/suffix draw already
    # baked into `symbol`; we don't retain those parts, so re-derive a label.
    return f"{spec.symbol} Fictional {spec.sector} Company"


def _quarterly_offsets(rng: np.random.Generator, n_days: int) -> list[int]:
    """~4 evenly-spaced but jittered indices per year, for earnings-like gaps."""
    step = 63  # trading days in a quarter, approximately
    offset = int(rng.integers(0, step))
    out = []
    idx = offset
    while idx < n_days:
        out.append(idx + int(rng.integers(-3, 4)))
        idx += step
    return out


def _intraday_from_daily(bar: Bar, interval_minutes: int, *, seed_key: str) -> list[Bar]:
    """Plausible intraday bridge from a daily OHLCV bar.

    Not real intraday data -- there is none to synthesise from since the daily
    process is the only thing generated -- but a Brownian bridge from open to
    close, clipped to the day's [low, high], is a reasonable stand-in for
    testing intraday-aware code paths.
    """
    n = max(1, 390 // max(1, interval_minutes))
    rng = np.random.default_rng(_stable_seed(seed_key))
    o, h, low_, c = bar.open, bar.high, bar.low, bar.close
    log_o, log_c = math.log(max(o, 1e-6)), math.log(max(c, 1e-6))
    log_h, log_l = math.log(max(h, 1e-6)), math.log(max(low_, 1e-6))

    incr = rng.normal(0.0, 1.0, size=n)
    walk = np.cumsum(incr)
    bridge = walk - (np.arange(1, n + 1) / n) * walk[-1]  # forces bridge[-1] == 0
    bridge = np.concatenate(([0.0], bridge))  # bridge[0] == 0 too

    sigma = max(1e-6, (log_h - log_l) / 4.0)
    path = log_o + (log_c - log_o) * (np.arange(n + 1) / n) + sigma * bridge
    path = np.clip(path, log_l, log_h)
    path[0], path[-1] = log_o, log_c
    prices = np.exp(path)

    # U-shaped participation: more volume near the open and close.
    x = np.linspace(-1, 1, n)
    weights = 1.0 + 0.8 * np.exp(-2.0 * (x + 1) ** 2) + 0.8 * np.exp(-2.0 * (x - 1) ** 2)
    weights *= rng.lognormal(0.0, 0.15, size=n)
    weights /= weights.sum()
    volumes = bar.volume * weights

    out: list[Bar] = []
    for i in range(n):
        seg_open, seg_close = float(prices[i]), float(prices[i + 1])
        wiggle = abs(seg_close - seg_open) * float(rng.uniform(0.1, 0.5)) + 1e-6
        seg_high = max(seg_open, seg_close) + wiggle
        seg_low = max(0.01, min(seg_open, seg_close) - wiggle)
        out.append(
            Bar(
                symbol=bar.symbol,
                session=bar.session,
                open=round(seg_open, 4),
                high=round(seg_high, 4),
                low=round(seg_low, 4),
                close=round(seg_close, 4),
                volume=round(float(volumes[i]), 1),
                adj_close=None,
                source="synthetic-intraday",
            )
        )
    return out
