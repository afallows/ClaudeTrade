"""Load earnings events from a user-supplied CSV file.

CSV schema: symbol, report_date, session, confirmed, eps_estimate, eps_actual,
as_of (optional, datetime or date). Columns are case-insensitive.

For backtest integrity, `as_of` should reflect when the date/estimate was
*known*, not when it was scraped.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

from claudetrade.domain import EarningsEvent, EarningsSession
from claudetrade.providers.base import EarningsProvider, ProviderError, ProviderStatus

log = logging.getLogger(__name__)


class CSVEarningsProvider(EarningsProvider):
    """Load earnings calendar from a CSV file."""

    name = "csv"

    def __init__(self, csv_path: str | Path | None = None) -> None:
        """Initialize from a CSV file path.

        Args:
            csv_path: Path to the CSV file.

        Raises:
            ProviderError: if the file cannot be read or is malformed.
        """
        self._csv_path = Path(csv_path) if csv_path else None
        self._events: dict[str, list[EarningsEvent]] = {}
        self._calls = 0

        if self._csv_path and self._csv_path.exists():
            try:
                self._load_csv()
            except Exception as exc:
                raise ProviderError(
                    f"failed to load earnings CSV from {self._csv_path}: {exc}",
                    provider=self.name,
                ) from exc

    def _load_csv(self) -> None:
        """Parse the CSV file."""
        if not self._csv_path or not self._csv_path.exists():
            return

        with open(self._csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSV file is empty")

            # Normalize column names to lowercase.
            fieldnames = [fn.lower() if fn else "" for fn in reader.fieldnames]
            expected = {"symbol", "report_date", "session"}

            if not expected.issubset(set(fieldnames)):
                raise ValueError(
                    f"CSV missing required columns {expected - set(fieldnames)}. "
                    f"Found: {fieldnames}"
                )

            for row_num, raw_row in enumerate(reader, start=2):
                # Normalize keys to lowercase.
                row = {k.lower(): v for k, v in raw_row.items() if k}
                try:
                    symbol = row["symbol"].strip().upper()
                    report_date = self._parse_date(row["report_date"])
                    session_str = row["session"].strip().upper()

                    # Map session string to enum.
                    session_map = {
                        "BMO": EarningsSession.BEFORE_OPEN,
                        "BEFORE_OPEN": EarningsSession.BEFORE_OPEN,
                        "AMC": EarningsSession.AFTER_CLOSE,
                        "AFTER_CLOSE": EarningsSession.AFTER_CLOSE,
                        "DURING": EarningsSession.DURING,
                        "UNKNOWN": EarningsSession.UNKNOWN,
                    }
                    session = session_map.get(session_str, EarningsSession.UNKNOWN)

                    confirmed = self._parse_bool(row.get("confirmed", "False"))

                    eps_estimate = self._parse_float(row.get("eps_estimate"))
                    eps_actual = self._parse_float(row.get("eps_actual"))
                    surprise_pct = self._parse_float(row.get("surprise_pct"))

                    as_of_str = row.get("as_of", "")
                    as_of = self._parse_datetime(as_of_str) if as_of_str else None

                    event = EarningsEvent(
                        symbol=symbol,
                        report_date=report_date,
                        session=session,
                        confirmed=confirmed,
                        eps_estimate=eps_estimate,
                        eps_actual=eps_actual,
                        surprise_pct=surprise_pct,
                        source="csv",
                        as_of=as_of,
                    )

                    if symbol not in self._events:
                        self._events[symbol] = []
                    self._events[symbol].append(event)

                except (KeyError, ValueError) as exc:
                    log.warning(
                        "skipping malformed earnings row %d: %s",
                        row_num, exc
                    )
                    continue

        # Sort each symbol's events by report_date.
        for events in self._events.values():
            events.sort(key=lambda e: e.report_date)

        log.info(
            "loaded %d earnings events for %d symbols from %s",
            sum(len(e) for e in self._events.values()),
            len(self._events),
            self._csv_path,
        )

    @staticmethod
    def _parse_date(s: str) -> dt.date:
        """Parse YYYY-MM-DD or other common formats."""
        s = s.strip()
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"]:
            try:
                return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.UTC).date()
            except ValueError:
                continue
        raise ValueError(f"cannot parse date: {s}") from None

    @staticmethod
    def _parse_datetime(s: str) -> dt.datetime:
        """Parse ISO-format datetime with optional timezone."""
        s = s.strip()
        # Try ISO format with timezone.
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            pass
        # Try ISO format, assume UTC if no timezone.
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
            return parsed
        except ValueError as exc:
            raise ValueError(f"cannot parse datetime: {s}") from exc

    @staticmethod
    def _parse_float(s: str | None) -> float | None:
        """Parse a float, returning None if empty or invalid."""
        if not s or not s.strip():
            return None
        try:
            return float(s.strip())
        except ValueError as exc:
            raise ValueError(f"invalid float: {s}") from exc

    @staticmethod
    def _parse_bool(s: str) -> bool:
        """Parse a boolean string."""
        return s.strip().lower() in {"true", "yes", "1", "t", "y"}

    def status(self) -> ProviderStatus:
        available = bool(self._csv_path and self._csv_path.exists())
        return ProviderStatus(
            name=self.name,
            kind="earnings",
            available=available,
            configured=available,
            message=(
                f"CSV loaded from {self._csv_path}" if available
                else f"CSV file not found: {self._csv_path}"
            ),
            supports_point_in_time=True,
            rate_limit_per_minute=None,
            calls_made=self._calls,
            licence_note="User-supplied data; licence is user's responsibility.",
            capabilities={"point_in_time": True},
        )

    def get_upcoming_earnings(
        self, symbols: list[str], *, through: dt.date | None = None
    ) -> dict[str, list[EarningsEvent]]:
        """Return unconfirmed (estimated) earnings up to `through`."""
        self._calls += 1
        if through is None:
            through = dt.datetime.now(tz=dt.UTC).date()

        out: dict[str, list[EarningsEvent]] = {}
        for symbol in symbols:
            events = self._events.get(symbol, [])
            out[symbol] = [
                e for e in events
                if not e.confirmed and e.report_date <= through
            ]
        return out

    def get_historical_earnings(
        self, symbols: list[str], start: dt.date, end: dt.date
    ) -> dict[str, list[EarningsEvent]]:
        """Return confirmed earnings in the date range."""
        self._calls += 1
        out: dict[str, list[EarningsEvent]] = {}
        for symbol in symbols:
            events = self._events.get(symbol, [])
            out[symbol] = [
                e for e in events
                if e.confirmed and start <= e.report_date <= end
            ]
        return out
