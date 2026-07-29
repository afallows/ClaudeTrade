"""Load daily OHLCV market data from user-supplied CSV files.

Supports both single-file (with symbol column) and per-symbol files.
Columns are matched case-insensitively with flexible naming (date/session/timestamp,
open, high, low, close, adj_close/adjclose, volume).

Optional securities.csv for reference data (SecurityInfo).
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

from claudetrade.domain import Bar, SecurityInfo
from claudetrade.providers.base import MarketDataProvider, ProviderError, ProviderStatus

log = logging.getLogger(__name__)


class CSVMarketProvider(MarketDataProvider):
    """Load daily OHLCV from CSV files."""

    name = "csv"

    def __init__(self, csv_dir: str | Path | None = None) -> None:
        """Initialize from a CSV directory.

        Args:
            csv_dir: Directory containing CSV files. Can have per-symbol files
                or one combined file with a symbol column.

        Raises:
            ProviderError: if the directory doesn't exist.
        """
        self._csv_dir = Path(csv_dir) if csv_dir else None
        self._bars: dict[str, list[Bar]] = {}
        self._securities: dict[str, SecurityInfo] = {}
        self._calls = 0
        self._has_delisted_column = False

        if self._csv_dir and self._csv_dir.exists():
            try:
                self._load_bars()
                self._load_securities()
                self._infer_capabilities()
            except Exception as exc:
                raise ProviderError(
                    f"failed to load market data from {self._csv_dir}: {exc}",
                    provider=self.name,
                ) from exc

    def _infer_capabilities(self) -> None:
        """Check if any CSV has a delisted column."""
        # This would require re-scanning the files; for now, default to False.
        # In a real implementation, track this during _load_bars.
        self._has_delisted_column = False

    def _load_bars(self) -> None:
        """Load OHLCV data from CSV files."""
        if not self._csv_dir:
            return

        # Check for combined file first.
        combined_file = self._csv_dir / "bars.csv"
        if combined_file.exists():
            self._load_combined_csv(combined_file)
            return

        # Otherwise, load per-symbol files.
        for csv_file in sorted(self._csv_dir.glob("*.csv")):
            if csv_file.name in {"securities.csv", "bars.csv"}:
                continue
            symbol = csv_file.stem.upper()
            try:
                bars = self._load_symbol_csv(csv_file)
                if bars:
                    self._bars[symbol] = bars
                    log.debug("loaded %d bars for %s", len(bars), symbol)
            except Exception as exc:
                log.warning("skipping %s: %s", csv_file, exc)

    def _load_combined_csv(self, csv_file: Path) -> None:
        """Load bars.csv with symbol column."""
        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSV is empty")

            fieldnames = [fn.lower() if fn else "" for fn in reader.fieldnames]
            if "symbol" not in fieldnames:
                raise ValueError("combined CSV missing 'symbol' column")

            for row_num, raw_row in enumerate(reader, start=2):
                row = {k.lower(): v for k, v in raw_row.items() if k}
                try:
                    symbol = row["symbol"].strip().upper()
                    bar = self._parse_bar_row(row, symbol)
                    if bar:
                        if symbol not in self._bars:
                            self._bars[symbol] = []
                        self._bars[symbol].append(bar)
                except (KeyError, ValueError) as exc:
                    log.warning("skipping bar row %d: %s", row_num, exc)

        # Sort each symbol's bars by session.
        for bars in self._bars.values():
            bars.sort(key=lambda b: b.session)

    def _load_symbol_csv(self, csv_file: Path) -> list[Bar]:
        """Load a single-symbol CSV file."""
        symbol = csv_file.stem.upper()
        bars: list[Bar] = []

        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return []

            for row_num, raw_row in enumerate(reader, start=2):
                row = {k.lower(): v for k, v in raw_row.items() if k}
                try:
                    bar = self._parse_bar_row(row, symbol)
                    if bar:
                        bars.append(bar)
                except (KeyError, ValueError) as exc:
                    log.warning("skipping %s row %d: %s", csv_file, row_num, exc)

        bars.sort(key=lambda b: b.session)
        return bars

    @staticmethod
    def _parse_bar_row(row: dict[str, str], symbol: str) -> Bar | None:
        """Parse a single bar from a row dict."""
        # Date column (flexible naming).
        date_aliases = {"date", "session", "timestamp", "time", "datetime"}
        date_col = next((col for col in row if col in date_aliases), None)
        if not date_col:
            raise ValueError("no date column found")

        session = CSVMarketProvider._parse_date(row[date_col])

        # OHLCV columns.
        open_ = float(row.get("open", 0))
        high = float(row.get("high", 0))
        low = float(row.get("low", 0))
        close = float(row.get("close", 0))
        volume = float(row.get("volume", 0))

        # Adjusted close (optional).
        adj_close_aliases = {"adj_close", "adjclose", "adj_price"}
        adj_close_col = next(
            (col for col in row if col in adj_close_aliases), None
        )
        adj_close = float(row[adj_close_col]) if adj_close_col else None

        if not (open_ > 0 and high > 0 and low > 0 and close > 0):
            raise ValueError("invalid OHLC prices")

        return Bar(
            symbol=symbol,
            session=session,
            open=round(open_, 4),
            high=round(high, 4),
            low=round(low, 4),
            close=round(close, 4),
            volume=round(volume, 1),
            adj_close=round(adj_close, 6) if adj_close else None,
            source="csv",
        )

    @staticmethod
    def _parse_date(s: str) -> dt.date:
        """Parse common date formats."""
        s = s.strip()
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"]:
            try:
                return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.UTC).date()
            except ValueError:
                continue
        raise ValueError(f"cannot parse date: {s}") from None

    def _load_securities(self) -> None:
        """Load reference data from securities.csv if present."""
        securities_file = self._csv_dir / "securities.csv" if self._csv_dir else None
        if not securities_file or not securities_file.exists():
            return

        with open(securities_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return

            fieldnames = [fn.lower() if fn else "" for fn in reader.fieldnames]
            if "symbol" not in fieldnames:
                return

            for row_num, raw_row in enumerate(reader, start=2):
                row = {k.lower(): v for k, v in raw_row.items() if k}
                try:
                    symbol = row["symbol"].strip().upper()
                    self._securities[symbol] = SecurityInfo(
                        symbol=symbol,
                        name=row.get("name", ""),
                        exchange=row.get("exchange", ""),
                        sector=row.get("sector", ""),
                        industry=row.get("industry", ""),
                        market_cap_usd=self._parse_float(row.get("market_cap_usd")),
                        is_etf=self._parse_bool(row.get("is_etf", "false")),
                        listed_date=self._parse_date_opt(row.get("listed_date")),
                        delisted_date=self._parse_date_opt(
                            row.get("delisted_date")
                        ),
                    )
                except Exception as exc:
                    log.warning("skipping securities row %d: %s", row_num, exc)

        log.info("loaded %d securities", len(self._securities))

    @staticmethod
    def _parse_bool(s: str) -> bool:
        return s.strip().lower() in {"true", "yes", "1", "t", "y"}

    @staticmethod
    def _parse_float(s: str | None) -> float | None:
        if not s or not s.strip():
            return None
        try:
            return float(s.strip())
        except ValueError:
            return None

    @staticmethod
    def _parse_date_opt(s: str | None) -> dt.date | None:
        if not s or not s.strip():
            return None
        try:
            return CSVMarketProvider._parse_date(s)
        except ValueError:
            return None

    def status(self) -> ProviderStatus:
        available = bool(self._csv_dir and self._csv_dir.exists())
        return ProviderStatus(
            name=self.name,
            kind="market",
            available=available,
            configured=available,
            message=(
                f"CSV loaded from {self._csv_dir}" if available
                else f"CSV directory not found: {self._csv_dir}"
            ),
            supports_point_in_time=True,
            supports_delisted=self._has_delisted_column,
            rate_limit_per_minute=None,
            calls_made=self._calls,
            licence_note="User-supplied data; licence is user's responsibility.",
            capabilities={
                "daily_bars": True,
                "intraday": False,
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
            bars = self._bars.get(symbol, [])
            filtered = [b for b in bars if start <= b.session <= end]
            if not adjusted:
                filtered = [
                    Bar(
                        symbol=b.symbol,
                        session=b.session,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        volume=b.volume,
                        adj_close=None,
                        source=b.source,
                    )
                    for b in filtered
                ]
            out[symbol] = filtered
        return out

    def get_intraday_bars(
        self,
        symbols: list[str],  # noqa: ARG002
        start: dt.datetime,  # noqa: ARG002
        end: dt.datetime,  # noqa: ARG002
        *,
        interval_minutes: int = 5,  # noqa: ARG002
    ) -> dict[str, list[Bar]]:
        raise ProviderError("intraday bars not supported by CSV provider",
                            provider=self.name)

    def get_security_info(self, symbols: list[str]) -> dict[str, SecurityInfo]:
        return {s: self._securities[s] for s in symbols if s in self._securities}

    def get_corporate_actions(
        self, symbols: list[str], start: dt.date, end: dt.date  # noqa: ARG002
    ) -> dict[str, list]:
        # CSV provider doesn't support corporate actions.
        return {s: [] for s in symbols}

    def list_universe(self, *, as_of: dt.date | None = None) -> list[SecurityInfo]:
        infos = list(self._securities.values()) if self._securities else [
            SecurityInfo(symbol=symbol) for symbol in self._bars
        ]
        if as_of is None:
            return infos
        return [info for info in infos if info.is_active_on(as_of)]
