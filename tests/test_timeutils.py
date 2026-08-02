"""Exchange-calendar session-key semantics (QA handoff v3, F24).

``current_trading_session`` exists because ``utc_now().date()`` is the wrong
session key for a US-equity scanner: from Friday 20:00 ET onward the UTC date
is already Saturday. These tests pin the ET-calendar behaviour at the exact
instants QA reproduced the bug at, plus the weekend/holiday roll-backs.
"""

from __future__ import annotations

import datetime as dt

from claudetrade.utils.timeutils import (
    current_trading_session,
    is_trading_day,
    market_holidays,
)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


def test_friday_evening_et_is_fridays_session_despite_saturday_utc() -> None:
    # QA's exact repro instant: 2026-07-31 22:40 ET == 2026-08-01 02:40 UTC.
    assert current_trading_session(_utc(2026, 8, 1, 2, 40)) == dt.date(2026, 7, 31)


def test_weekend_resolves_to_the_preceding_friday() -> None:
    saturday_noon_et = _utc(2026, 8, 1, 16, 0)
    sunday_noon_et = _utc(2026, 8, 2, 16, 0)
    assert current_trading_session(saturday_noon_et) == dt.date(2026, 7, 31)
    assert current_trading_session(sunday_noon_et) == dt.date(2026, 7, 31)


def test_regular_trading_day_is_its_own_session_all_day_et() -> None:
    # Monday 2026-08-03, pre-market through after-hours (ET).
    for hour_utc in (11, 15, 21):  # 07:00, 11:00, 17:00 ET (EDT = UTC-4)
        assert current_trading_session(_utc(2026, 8, 3, hour_utc)) == dt.date(2026, 8, 3)


def test_holiday_monday_resolves_to_the_preceding_friday() -> None:
    labor_day = dt.date(2026, 9, 7)  # first Monday of September 2026
    assert labor_day in market_holidays(2026)
    assert not is_trading_day(labor_day)
    assert current_trading_session(_utc(2026, 9, 7, 16, 0)) == dt.date(2026, 9, 4)


class TestSessionForInstant:
    """Which session a post's information belongs to.

    The after-hours case is why sentiment never accumulated: refreshes run
    after the close, so most gathered posts were dated after the last
    session's close, fell outside every session window, and were discarded
    as "look-ahead violations" they never were.
    """

    def test_during_the_session_belongs_to_that_session(self) -> None:
        from claudetrade.utils.timeutils import session_for_instant

        # Friday 2026-07-31, 11:00 ET.
        assert session_for_instant(_utc(2026, 7, 31, 15, 0)) == dt.date(2026, 7, 31)

    def test_at_the_close_still_belongs_to_that_session(self) -> None:
        from claudetrade.utils.timeutils import session_close_utc, session_for_instant

        friday = dt.date(2026, 7, 31)
        assert session_for_instant(session_close_utc(friday)) == friday

    def test_after_the_close_belongs_to_the_next_session(self) -> None:
        """A Friday-evening post is early information about Monday -- not
        late information about Friday, and not a violation."""
        from claudetrade.utils.timeutils import session_for_instant

        # Friday 2026-07-31, 19:00 ET (the owner's refresh ran at 22:13 ET).
        assert session_for_instant(_utc(2026, 8, 1, 2, 0)) == dt.date(2026, 8, 3)

    def test_weekend_posts_belong_to_monday(self) -> None:
        from claudetrade.utils.timeutils import session_for_instant

        saturday_noon_et = _utc(2026, 8, 1, 16, 0)
        sunday_noon_et = _utc(2026, 8, 2, 16, 0)
        assert session_for_instant(saturday_noon_et) == dt.date(2026, 8, 3)
        assert session_for_instant(sunday_noon_et) == dt.date(2026, 8, 3)

    def test_pre_market_belongs_to_that_same_session(self) -> None:
        """07:00 ET Monday is before Monday's close, so it is Monday's."""
        from claudetrade.utils.timeutils import session_for_instant

        assert session_for_instant(_utc(2026, 8, 3, 11, 0)) == dt.date(2026, 8, 3)

    def test_the_result_is_always_a_tradable_session_at_or_after_the_post(self) -> None:
        from claudetrade.utils.timeutils import session_close_utc, session_for_instant

        moment = _utc(2026, 8, 1, 2, 0)
        for _ in range(40):
            session = session_for_instant(moment)
            assert is_trading_day(session)
            # Never look-ahead: the session's close is at or after the post.
            assert moment <= session_close_utc(session)
            moment += dt.timedelta(hours=11)


def test_result_is_never_a_weekend_or_holiday() -> None:
    moment = _utc(2026, 1, 1, 12, 0)  # New Year's Day, itself a holiday
    for _ in range(45):
        session = current_trading_session(moment)
        assert is_trading_day(session)
        assert session <= moment.astimezone(dt.timezone(dt.timedelta(hours=-4))).date()
        moment += dt.timedelta(hours=13)  # sweep odd offsets across ~3.5 weeks
