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
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from claudetrade.config import AppConfig
from claudetrade.db.models import Security
from claudetrade.db.session import Database
from claudetrade.domain import SecurityInfo
from claudetrade.logging_setup import get_logger

log = get_logger(__name__)


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
        if self.db is None:
            log.warning("universe source is 'database' but no database handle was supplied")
            return []
        with self.db.read_session() as session:
            rows = session.execute(select(Security)).scalars().all()
            return [
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

    def _load_csv(self) -> list[SecurityInfo]:
        path = self.config.universe.csv_path
        if path is None or not Path(path).exists():
            raise FileNotFoundError(
                f"universe.source is 'csv' but universe.csv_path is missing or unreadable: {path}"
            )
        out: list[SecurityInfo] = []
        with Path(path).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                normalised = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                symbol = normalised.get("symbol") or normalised.get("ticker")
                if not symbol:
                    continue
                out.append(
                    SecurityInfo(
                        symbol=symbol.upper(),
                        name=normalised.get("name", ""),
                        exchange=normalised.get("exchange", ""),
                        sector=normalised.get("sector", ""),
                        industry=normalised.get("industry", ""),
                        market_cap_usd=_maybe_float(normalised.get("market_cap")),
                        is_etf=_maybe_bool(normalised.get("is_etf")),
                        is_leveraged_or_inverse=_maybe_bool(normalised.get("is_leveraged")),
                        listed_date=_maybe_date(normalised.get("listed_date")),
                        delisted_date=_maybe_date(normalised.get("delisted_date")),
                    )
                )
        return out

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
