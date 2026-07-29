"""Deterministic synthetic earnings generator.

Quarterly cadence per symbol derived from a stable hash of the symbol name.
For backtest integrity, `as_of` is set to a realistic announcement date
(~21 days before confirmed dates, with wider uncertainty for estimates),
preventing earnings-date look-ahead bias when used in historical queries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging

from claudetrade.domain import EarningsEvent, EarningsSession
from claudetrade.providers.base import EarningsProvider, ProviderStatus

log = logging.getLogger(__name__)

#: Maximum time delta from report date for confirmed announcements (days).
CONFIRMED_AS_OF_DAYS = 21

#: Maximum time delta from report date for estimated announcements (days).
ESTIMATED_AS_OF_DAYS = 45


def _stable_hash(symbol: str, year: int, quarter: int) -> int:
    """Deterministic seed derived from symbol and fiscal period."""
    msg = f"{symbol}:{year}:{quarter}".encode()
    return int(hashlib.sha256(msg).digest()[:4].hex(), 16)


def _generate_quarterly_dates(
    symbol: str, start_year: int, end_year: int
) -> list[tuple[dt.date, bool]]:
    """Generate quarterly report dates for a symbol, deterministically.

    Returns:
        List of (report_date, is_confirmed) tuples. Confirmed status depends on
        whether the date is in the past (confirmed) or future (unconfirmed).
    """
    dates: list[tuple[dt.date, bool]] = []
    today = dt.datetime.now(tz=dt.UTC).date()

    for year in range(start_year, end_year + 1):
        for quarter in range(1, 5):
            h = _stable_hash(symbol, year, quarter)
            # Deterministic month within fiscal quarter (0=Q1, 1=Q2, 2=Q3, 3=Q4).
            q_start_month = (quarter - 1) * 3 + 1
            month_offset = (h % 60) // 20  # 0, 1, or 2 months into quarter
            report_month = q_start_month + month_offset

            # Day of month, with deterministic bias toward mid-month.
            day_seed = (h >> 8) % 28
            report_day = min(28, 10 + day_seed)

            try:
                report_date = dt.date(year, report_month, report_day)
            except ValueError:
                # Rare: Feb 30 or similar. Back off to last day of month.
                if report_month == 2:
                    report_date = dt.date(year, 2, 28)
                else:
                    report_date = dt.date(year, report_month, 28)

            # Confirmed if in the past (beyond the estimated window).
            confirmed = report_date <= today
            dates.append((report_date, confirmed))

    return dates


class SyntheticEarningsProvider(EarningsProvider):
    """Deterministic earnings calendar for backtesting without look-ahead bias."""

    name = "synthetic"

    def __init__(self) -> None:
        self._calls = 0

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            kind="earnings",
            available=True,
            configured=True,
            message="synthetic offline generator; always available",
            supports_point_in_time=True,
            rate_limit_per_minute=None,
            calls_made=self._calls,
            licence_note=(
                "SYNTHETIC DATA -- fabricated for engine validation only. "
                "Quarterly dates and surprises are deterministic per symbol."
            ),
            capabilities={
                "point_in_time": True,
                "historical_surprises": True,
            },
        )

    def get_upcoming_earnings(
        self, symbols: list[str], *, through: dt.date | None = None
    ) -> dict[str, list[EarningsEvent]]:
        """Scheduled reports not yet reported (unconfirmed estimates).

        Args:
            symbols: List of ticker symbols.
            through: Latest date to include. Defaults to today.

        Returns:
            Dict mapping symbol to list of EarningsEvent, sorted by date.
        """
        self._calls += 1
        if through is None:
            through = dt.datetime.now(tz=dt.UTC).date()

        out: dict[str, list[EarningsEvent]] = {}
        for symbol in symbols:
            dates = _generate_quarterly_dates(symbol, through.year - 1, through.year + 2)
            events: list[EarningsEvent] = []
            for report_date, is_confirmed in dates:
                if report_date <= through or is_confirmed:
                    continue
                h = _stable_hash(symbol, report_date.year, (report_date.month - 1) // 3 + 1)

                session = (
                    EarningsSession.BEFORE_OPEN if (h & 1) == 0 else
                    EarningsSession.AFTER_CLOSE
                )

                # For unconfirmed (future) dates, as_of is in the future too
                # (makes no sense, but signals "not yet announced").
                # Most realistic: estimate is released ~45 days before.
                as_of_days_before = ESTIMATED_AS_OF_DAYS
                as_of = dt.datetime.combine(
                    report_date - dt.timedelta(days=as_of_days_before),
                    dt.time(8, 0),
                ).replace(tzinfo=dt.UTC)

                event = EarningsEvent(
                    symbol=symbol,
                    report_date=report_date,
                    session=session,
                    confirmed=False,
                    eps_estimate=None,
                    eps_actual=None,
                    source="synthetic",
                    as_of=as_of,
                )
                events.append(event)

            out[symbol] = sorted(events, key=lambda e: e.report_date)

        return out

    def get_historical_earnings(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[EarningsEvent]]:
        """Past reports including actuals and surprise percentages.

        Args:
            symbols: List of ticker symbols.
            start: Earliest report date (inclusive).
            end: Latest report date (inclusive).

        Returns:
            Dict mapping symbol to list of EarningsEvent, sorted by date.
            Each event includes confirmed dates, eps_actual, and surprise_pct.
        """
        self._calls += 1
        out: dict[str, list[EarningsEvent]] = {}

        for symbol in symbols:
            dates = _generate_quarterly_dates(symbol, start.year - 1, end.year + 1)
            events: list[EarningsEvent] = []

            for report_date, _is_confirmed in dates:
                if not (start <= report_date <= end):
                    continue

                h = _stable_hash(symbol, report_date.year, (report_date.month - 1) // 3 + 1)

                session = (
                    EarningsSession.BEFORE_OPEN if (h & 1) == 0 else
                    EarningsSession.AFTER_CLOSE
                )

                # Past earnings are confirmed if they're in the past.
                confirmed = report_date <= dt.datetime.now(tz=dt.UTC).date()

                # Generate deterministic EPS and surprise from hash.
                eps_estimate = 1.0 + (h & 255) / 256.0 * 3.0
                eps_actual = eps_estimate * (1.0 + (((h >> 8) & 127) - 64) / 256.0)
                surprise_pct = (eps_actual - eps_estimate) / abs(eps_estimate)
                surprise_pct *= 100.0

                # For confirmed reports, as_of is ~21 days before the report
                # (when earnings were typically announced/estimated).
                # For estimates, use ~45 days.
                as_of_days_before = (
                    CONFIRMED_AS_OF_DAYS if confirmed else ESTIMATED_AS_OF_DAYS
                )

                as_of = dt.datetime.combine(
                    report_date - dt.timedelta(days=as_of_days_before),
                    dt.time(8, 0),
                ).replace(tzinfo=dt.UTC)

                event = EarningsEvent(
                    symbol=symbol,
                    report_date=report_date,
                    session=session,
                    confirmed=confirmed,
                    eps_estimate=round(eps_estimate, 2),
                    eps_actual=round(eps_actual, 2) if confirmed else None,
                    surprise_pct=round(surprise_pct, 1) if confirmed else None,
                    source="synthetic",
                    as_of=as_of,
                )
                events.append(event)

            out[symbol] = sorted(events, key=lambda e: e.report_date)

        return out
