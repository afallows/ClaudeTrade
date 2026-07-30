"""Universe selection.

Decides which securities are eligible to be scanned on a given session.

The one rule that matters for research integrity is **point-in-time
membership**: ``for_session`` returns the names that were listed and trading on
that date, including names that were later delisted. Filtering the universe by
today's listing status is the textbook way to manufacture survivorship bias --
a backtest run only over companies that still exist has quietly excluded every
company that failed.

Liquidity and price filters are applied here only where they can be evaluated
point-in-time. Filters that depend on computed features (average dollar volume,
volatility) are applied later, in ``signals.scoring.apply_hard_gates``, where
the feature values for that session are available.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.db.models import Security
from claudetrade.db.session import Database
from claudetrade.domain import SecurityInfo
from claudetrade.logging_setup import get_logger

log = get_logger(__name__)

#: Packaged seed universes shipped inside the package (see module docstring
#: below `load_packaged_universe`). Adding a new packaged file only requires a
#: new entry here plus the CSV under ``data/universes/``.
PACKAGED_UNIVERSE_DIR = Path(__file__).parent / "universes"
PACKAGED_UNIVERSE_FILES: dict[str, str] = {
    "us_default": "us_default.csv",
    "ca_default": "ca_default.csv",
}

#: Rough, honestly-approximate dollar figure for each seed file's
#: ``market_cap_bucket`` label. These are NOT live market capitalisations --
#: the packaged CSVs are hand-curated seed lists that go stale over time, so a
#: precise figure would be false precision. The bucket only needs to be right
#: enough to clear (or legitimately fail) ``FilterConfig.min_market_cap_usd``.
MARKET_CAP_BUCKET_USD: dict[str, float] = {
    "mega": 250_000_000_000.0,
    "large": 50_000_000_000.0,
    "mid": 5_000_000_000.0,
    "small": 1_000_000_000.0,
}


@dataclass(slots=True)
class UniverseReport:
    """What was included, what was dropped, and why."""

    session: dt.date
    included: list[SecurityInfo] = field(default_factory=list)
    excluded: dict[str, list[str]] = field(default_factory=dict)

    def exclude(self, symbol: str, reason: str) -> None:
        self.excluded.setdefault(reason, []).append(symbol)

    @property
    def symbols(self) -> list[str]:
        return [s.symbol for s in self.included]

    def summary(self) -> dict[str, int]:
        return {"included": len(self.included), **{k: len(v) for k, v in self.excluded.items()}}


class UniverseSelector:
    """Builds the scannable universe from the configured source."""

    def __init__(self, config: AppConfig, db: Database | None = None):
        self.config = config
        self.db = db

    # --- sources ----------------------------------------------------------

    def load_all(self) -> list[SecurityInfo]:
        """Every known security, before any filtering.

        Delisted names are deliberately included; ``for_session`` decides
        whether each was tradable on the date being evaluated.
        """
        source = self.config.universe.source
        if source == "csv":
            return self._load_csv()
        if source == "static":
            return [SecurityInfo(symbol=s) for s in self.config.universe.static_symbols]
        return self._load_database()

    def _load_database(self) -> list[SecurityInfo]:
        """Securities stored from a previous refresh, seeded from the packaged
        default universes when nothing has been stored yet.

        A brand-new install has an empty ``securities`` table until the first
        ``claudetrade refresh`` completes. Rather than reporting an empty
        universe in that window, this seeds from the packaged CSVs configured
        in ``UniverseConfig.packaged_universes`` (default: the US and Canadian
        seed lists shipped with the package) -- which is also exactly what lets
        ``claudetrade refresh`` itself have something to fetch on a fresh
        install pointed at a real provider (see
        ``StooqMarketProvider.list_universe``). Once the database has rows,
        those take precedence; any packaged symbol not yet stored is still
        appended, so switching on real data does not silently hide names that
        have not been refreshed yet.
        """
        db_rows: list[SecurityInfo] = []
        if self.db is None:
            log.warning("universe source is 'database' but no database handle was supplied")
        else:
            with self.db.read_session() as session:
                rows = session.execute(select(Security)).scalars().all()
                db_rows = [
                    SecurityInfo(
                        symbol=r.symbol,
                        name=r.name,
                        exchange=r.exchange,
                        sector=r.sector,
                        industry=r.industry,
                        market_cap_usd=r.market_cap_usd,
                        shares_outstanding=r.shares_outstanding,
                        is_etf=r.is_etf,
                        is_leveraged_or_inverse=r.is_leveraged_or_inverse,
                        listed_date=r.listed_date,
                        delisted_date=r.delisted_date,
                    )
                    for r in rows
                ]

        names = self.config.universe.packaged_universes
        if not names:
            return db_rows

        packaged = load_packaged_universe(names)
        if not db_rows:
            if packaged:
                log.info(
                    "no securities stored yet; seeding universe from packaged default(s) "
                    "%s (%d symbols) -- run 'claudetrade refresh' to pull real price history",
                    list(names), len(packaged),
                )
            return packaged

        known = {s.symbol for s in db_rows}
        return db_rows + [s for s in packaged if s.symbol not in known]

    def _load_csv(self) -> list[SecurityInfo]:
        path = self.config.universe.csv_path
        if path is None or not Path(path).exists():
            raise FileNotFoundError(
                f"universe.source is 'csv' but universe.csv_path is missing or unreadable: {path}"
            )
        return _read_universe_csv(Path(path))

    # --- selection --------------------------------------------------------

    def for_session(
        self,
        session: dt.date,
        *,
        securities: list[SecurityInfo] | None = None,
    ) -> UniverseReport:
        """Point-in-time universe for one session.

        Args:
            session: The decision date.
            securities: Pre-loaded reference data; loaded from the source when
                omitted.

        Returns:
            A ``UniverseReport`` listing inclusions and the reason for every
            exclusion.
        """
        universe = securities if securities is not None else self.load_all()
        cfg = self.config.universe
        filters = self.config.filters
        report = UniverseReport(session=session)

        for security in universe:
            # Point-in-time listing status. This is the survivorship guard.
            if not security.is_active_on(session):
                reason = (
                    "delisted_before_session"
                    if security.delisted_date and session >= security.delisted_date
                    else "not_yet_listed"
                )
                report.exclude(security.symbol, reason)
                continue

            if security.exchange and security.exchange not in cfg.permitted_exchanges:
                report.exclude(security.symbol, "exchange_not_permitted")
                continue
            if security.is_etf and not cfg.include_etfs:
                report.exclude(security.symbol, "etf_excluded")
                continue
            if filters.exclude_leveraged_inverse_etfs and security.is_leveraged_or_inverse:
                report.exclude(security.symbol, "leveraged_or_inverse")
                continue
            if (
                filters.exclude_binary_event_sectors
                and security.industry in filters.binary_event_sectors
            ):
                report.exclude(security.symbol, "binary_event_sector")
                continue
            # Market cap is slow-moving; the value stored is the latest known.
            # Treated as advisory here and re-checked point-in-time in scoring.
            if (
                security.market_cap_usd is not None
                and security.market_cap_usd < filters.min_market_cap_usd
            ):
                report.exclude(security.symbol, "below_min_market_cap")
                continue

            report.included.append(security)

        if len(report.included) > cfg.max_symbols:
            # Deterministic truncation: largest first, so a truncated universe
            # is at least a stable and explicable subset.
            report.included.sort(key=lambda s: -(s.market_cap_usd or 0.0))
            dropped = report.included[cfg.max_symbols :]
            report.included = report.included[: cfg.max_symbols]
            for security in dropped:
                report.exclude(security.symbol, "exceeded_max_symbols")

        log.info(
            "universe for %s: %d symbols (%s)",
            session,
            len(report.included),
            ", ".join(f"{k}={len(v)}" for k, v in sorted(report.excluded.items())) or "no exclusions",
        )
        return report

    def delisted_between(self, start: dt.date, end: dt.date) -> list[SecurityInfo]:
        """Names that died inside a window -- used to verify a backtest is unbiased."""
        return [
            s
            for s in self.load_all()
            if s.delisted_date is not None and start <= s.delisted_date <= end
        ]

    def survivorship_check(self, start: dt.date, end: dt.date) -> dict[str, object]:
        """Report whether the universe contains failed companies.

        A backtest whose universe has zero delistings over a multi-year window
        is almost certainly survivorship-biased, and the report says so
        explicitly rather than leaving the operator to infer it.
        """
        universe = self.load_all()
        dead = [s for s in universe if s.delisted_date and start <= s.delisted_date <= end]
        years = max((end - start).days / 365.25, 0.01)
        rate = len(dead) / max(len(universe), 1) / years
        biased = len(dead) == 0 and years >= 1.0
        return {
            "universe_size": len(universe),
            "delisted_in_window": len(dead),
            "annual_delisting_rate": round(rate, 4),
            "likely_survivorship_biased": biased,
            "note": (
                "No delisted securities are present over a multi-year window. Results from this "
                "universe overstate achievable performance and must not be treated as unbiased."
                if biased
                else "Universe includes delisted securities."
            ),
        }


def _security_from_row(normalised: dict[str, str]) -> SecurityInfo | None:
    """Build a ``SecurityInfo`` from a normalised (lower-cased, stripped) CSV row.

    Shared by ``UniverseSelector._load_csv`` (an arbitrary user-supplied file)
    and ``load_packaged_universe`` (the CSVs shipped under ``data/universes/``),
    so both accept the same column set. ``country`` is accepted but not stored
    on ``SecurityInfo`` -- it has no dedicated field there and is already
    implied by ``exchange``; it exists in the packaged files purely for a human
    editing them to read at a glance.
    """
    symbol = normalised.get("symbol") or normalised.get("ticker")
    if not symbol:
        return None
    market_cap = _maybe_float(normalised.get("market_cap"))
    if market_cap is None:
        bucket = normalised.get("market_cap_bucket", "").strip().lower()
        market_cap = MARKET_CAP_BUCKET_USD.get(bucket)
    return SecurityInfo(
        symbol=symbol.upper(),
        name=normalised.get("name", ""),
        exchange=normalised.get("exchange", "").upper(),
        sector=normalised.get("sector", ""),
        industry=normalised.get("industry", ""),
        market_cap_usd=market_cap,
        is_etf=_maybe_bool(normalised.get("is_etf")),
        is_leveraged_or_inverse=_maybe_bool(normalised.get("is_leveraged")),
        listed_date=_maybe_date(normalised.get("listed_date")),
        delisted_date=_maybe_date(normalised.get("delisted_date")),
    )


def _read_universe_csv(path: Path) -> list[SecurityInfo]:
    """Parse one universe CSV file into ``SecurityInfo`` rows.

    Lines starting with ``#`` are treated as comments and skipped before the
    header is read -- the packaged files lead with a generation-date and
    caveat banner in that form (see ``load_packaged_universe``).
    """
    out: list[SecurityInfo] = []
    with path.open(newline="", encoding="utf-8") as fh:
        lines = [line for line in fh if not line.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        normalised = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        info = _security_from_row(normalised)
        if info is not None:
            out.append(info)
    return out


def load_packaged_universe(
    names: Sequence[str] = ("us_default", "ca_default"),
) -> list[SecurityInfo]:
    """Load one or more of the seed universes packaged with claudetrade.

    These are hand-curated CSVs under ``src/claudetrade/data/universes/`` --
    roughly the S&P 500 plus liquid mid-caps (``us_default``) and the TSX 60
    plus other liquid TSX names (``ca_default``). They are seed lists an
    operator can edit, not a live index feed: constituents drift over time and
    the files will go stale. They exist so that a fresh install pointed at a
    real market-data provider (``market_data.provider = "stooq"``) has
    hundreds of US and Canadian symbols to scan on the first
    ``claudetrade refresh`` instead of an empty universe.

    Stooq's free endpoint has no bulk reference-data listing of its own (see
    ``providers.market.stooq``), so ``StooqMarketProvider.list_universe`` calls
    this same function -- it is the seed both the universe selector and the
    stooq adapter draw from.

    Unknown names are skipped with a warning rather than raising, so a typo in
    configuration degrades to "fewer symbols" rather than an unhandled error.
    Symbols are de-duplicated across files, first file wins.
    """
    out: list[SecurityInfo] = []
    seen: set[str] = set()
    for name in names:
        filename = PACKAGED_UNIVERSE_FILES.get(name)
        if filename is None:
            log.warning(
                "unknown packaged universe %r; available: %s",
                name, sorted(PACKAGED_UNIVERSE_FILES),
            )
            continue
        path = PACKAGED_UNIVERSE_DIR / filename
        if not path.exists():
            log.warning("packaged universe file missing: %s", path)
            continue
        for info in _read_universe_csv(path):
            if info.symbol in seen:
                continue
            seen.add(info.symbol)
            out.append(info)
    return out


def _maybe_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _maybe_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def _maybe_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            # Date-only field: there is no time or zone to preserve.
            return dt.datetime.strptime(value, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None
