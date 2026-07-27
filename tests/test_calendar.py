"""Trading-day arithmetic. The earnings blackout gate depends on every one of these."""

from __future__ import annotations

import datetime as dt

from qms import calendar as cal


def test_weekend_is_not_a_session():
    assert not cal.is_session(dt.date(2026, 7, 25))  # Saturday
    assert not cal.is_session(dt.date(2026, 7, 26))  # Sunday
    assert cal.is_session(dt.date(2026, 7, 24))  # Friday


def test_holiday_is_not_a_session():
    assert not cal.is_session(dt.date(2026, 1, 1))  # New Year's Day
    assert not cal.is_session(dt.date(2026, 7, 3))  # Independence Day observed


def test_friday_to_monday_is_one_trading_day_not_three():
    """The whole reason this module exists rather than using timedelta."""
    friday = dt.date(2026, 7, 24)
    monday = dt.date(2026, 7, 27)
    assert (monday - friday).days == 3
    assert cal.trading_days_between(friday, monday) == 1


def test_blackout_window_spans_a_holiday():
    """Three trading days before 2026-07-07 reaches back past the 4th of July closure."""
    earnings = dt.date(2026, 7, 7)
    start = cal.shift_sessions(earnings, -3)
    assert cal.trading_days_between(start, earnings) == 3
    # Crosses both a weekend and the observed holiday, so it is more than 3 calendar days.
    assert (earnings - start).days > 3


def test_same_session_is_zero():
    day = dt.date(2026, 7, 27)
    assert cal.trading_days_between(day, day) == 0


def test_reverse_direction_is_negative():
    a = dt.date(2026, 7, 20)
    b = dt.date(2026, 7, 24)
    assert cal.trading_days_between(a, b) == -cal.trading_days_between(b, a)


def test_next_and_previous_are_strict():
    monday = dt.date(2026, 7, 27)
    assert cal.next_session(monday) == dt.date(2026, 7, 28)
    assert cal.previous_session(monday) == dt.date(2026, 7, 24)


def test_session_or_previous_snaps_weekend_backwards():
    assert cal.session_or_previous(dt.date(2026, 7, 25)) == dt.date(2026, 7, 24)
    assert cal.session_or_next(dt.date(2026, 7, 25)) == dt.date(2026, 7, 27)


def test_shift_sessions_round_trips():
    start = dt.date(2026, 7, 27)
    assert cal.shift_sessions(cal.shift_sessions(start, 5), -5) == start


def test_sessions_in_range_excludes_non_sessions():
    days = cal.sessions_in_range(dt.date(2026, 7, 20), dt.date(2026, 7, 26))
    assert days == [
        dt.date(2026, 7, 20),
        dt.date(2026, 7, 21),
        dt.date(2026, 7, 22),
        dt.date(2026, 7, 23),
        dt.date(2026, 7, 24),
    ]
