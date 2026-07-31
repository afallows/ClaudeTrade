"""Timezone-aware time handling.

Rules enforced across the codebase:

* Everything stored or computed internally is **timezone-aware UTC**.
* Naive datetimes entering the system are rejected, not silently localised --
  silent localisation is a classic source of look-ahead bias (a bar stamped
  ``16:00`` interpreted as UTC is 11 hours early in Sydney).
* Display conversion happens only at the presentation edge.

The exchange calendar here is a *simplified* US equity calendar: weekends plus
the standard NYSE holiday rules.  It is deliberately conservative and is
documented as an approximation in ``docs/known-limitations.md``.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = dt.UTC
MARKET_TZ = ZoneInfo("America/New_York")

#: US regular trading hours in exchange-local time.
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(16, 0)


def utc_now() -> dt.datetime:
    """Current instant as timezone-aware UTC."""
    return dt.datetime.now(tz=UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as UTC, rejecting naive datetimes.

    Raises:
        ValueError: if ``value`` carries no tzinfo.
    """
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime rejected: all timestamps must be timezone-aware "
            f"(got {value!r}). Attach tzinfo at the ingestion boundary."
        )
    return value.astimezone(UTC)


def to_display(value: dt.datetime, tz_name: str = "America/New_York") -> dt.datetime:
    """Convert a UTC instant to the operator's display timezone."""
    return ensure_utc(value).astimezone(ZoneInfo(tz_name))


def session_close_utc(session: dt.date) -> dt.datetime:
    """UTC instant of the regular-session close for a given trading date."""
    local = dt.datetime.combine(session, MARKET_CLOSE, tzinfo=MARKET_TZ)
    return local.astimezone(UTC)


def session_open_utc(session: dt.date) -> dt.datetime:
    """UTC instant of the regular-session open for a given trading date."""
    local = dt.datetime.combine(session, MARKET_OPEN, tzinfo=MARKET_TZ)
    return local.astimezone(UTC)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The ``n``-th ``weekday`` (Mon=0) of a month; ``n=-1`` means last."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    if month == 12:
        last = dt.date(year, 12, 31)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - dt.timedelta(days=offset)


def _observed(day: dt.date) -> dt.date:
    """Apply the US 'observed holiday' shift (Sat -> Fri, Sun -> Mon)."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian computus -- needed for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month, day = divmod(h + lo - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


@lru_cache(maxsize=64)
def market_holidays(year: int) -> frozenset[dt.date]:
    """Approximate NYSE full-day closures for ``year``.

    Ad-hoc closures (national days of mourning, weather) are *not* modelled;
    treat this as a filter for obviously non-tradable dates rather than an
    authoritative calendar.
    """
    days = {
        _observed(dt.date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(dt.date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:  # Juneteenth became an exchange holiday in 2022
        days.add(_observed(dt.date(year, 6, 19)))
    return frozenset(days)


def is_trading_day(day: dt.date) -> bool:
    """True when ``day`` is a weekday and not an approximate exchange holiday."""
    return day.weekday() < 5 and day not in market_holidays(day.year)


def next_trading_day(day: dt.date, *, skip: int = 1) -> dt.date:
    """The ``skip``-th trading day strictly after ``day``."""
    cur = day
    remaining = skip
    while remaining > 0:
        cur += dt.timedelta(days=1)
        if is_trading_day(cur):
            remaining -= 1
    return cur


def previous_trading_day(day: dt.date, *, skip: int = 1) -> dt.date:
    """The ``skip``-th trading day strictly before ``day``."""
    cur = day
    remaining = skip
    while remaining > 0:
        cur -= dt.timedelta(days=1)
        if is_trading_day(cur):
            remaining -= 1
    return cur


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """Count trading days in the half-open interval ``(start, end]``."""
    if end <= start:
        return 0
    count = 0
    cur = start
    while cur < end:
        cur += dt.timedelta(days=1)
        if is_trading_day(cur):
            count += 1
    return count


def trading_day_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """All trading days in the closed interval ``[start, end]``."""
    out: list[dt.date] = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out
