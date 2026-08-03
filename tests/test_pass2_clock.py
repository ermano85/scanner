"""Session clock and timezone handling.

The operator is in Estonia and the market is in New York, and the two change their clocks
on different dates. For about three weeks a year the gap is 6 or 8 hours rather than 7, so
any code that treats the offset as a constant is wrong twice a year in a way that shifts
every session boundary.
"""

from __future__ import annotations

import datetime as dt

import pytest

from qms import calendar as mcal
from qms.pass2 import clock as clockmod


def _at(text: str) -> clockmod.SessionClock:
    moment, forced = clockmod.resolve_now(text)
    return clockmod.build(moment, forced=forced)


def _gap_hours(session_clock: clockmod.SessionClock) -> float:
    local = session_clock.in_operator_tz().utcoffset()
    east = session_clock.in_exchange_tz().utcoffset()
    return (local - east).total_seconds() / 3600


@pytest.mark.parametrize(
    "moment, expected_gap",
    [
        # US on DST from the 2nd Sunday in March, the EU only from the last Sunday.
        ("2026-03-12T10:00", 6),
        ("2026-07-30T10:00", 7),
        # EU leaves DST on the last Sunday in October, the US a week later.
        ("2026-10-28T10:00", 6),
        ("2026-11-02T10:00", 7),
    ],
)
def test_the_tallinn_to_new_york_gap_is_not_assumed_constant(moment, expected_gap):
    assert _gap_hours(_at(moment)) == expected_gap


def test_minutes_since_open_counts_from_the_actual_bell():
    assert _at("2026-07-30T09:30").minutes_since_open == 0
    assert _at("2026-07-30T10:00").minutes_since_open == 30
    assert _at("2026-07-30T09:29").minutes_since_open is None


def test_a_half_day_is_reported_with_its_early_close():
    """The day after Thanksgiving closes at 13:00 ET."""
    session_clock = _at("2026-11-27T12:30")
    assert session_clock.is_open
    assert session_clock.half_day
    assert "HALF-DAY" in session_clock.describe_state()
    assert "13:00" in session_clock.describe_state()

    # And 13:30 that day is already after the close, unlike a normal session.
    assert not _at("2026-11-27T13:30").is_open
    assert _at("2026-07-30T13:30").is_open


@pytest.mark.parametrize("holiday", ["2026-12-25T12:00", "2026-01-19T12:00", "2026-01-01T12:00"])
def test_market_holidays_are_flagged(holiday):
    session_clock = _at(holiday)
    assert session_clock.state == clockmod.STATE_HOLIDAY
    assert not session_clock.is_open


def test_pre_market_refers_to_todays_session_not_yesterdays():
    """The reference session must match the session the intraday payload describes."""
    session_clock = _at("2026-07-30T08:00")
    assert session_clock.state == clockmod.STATE_PRE
    assert session_clock.reference_session == dt.date(2026, 7, 30)


def test_at_accepts_a_bare_time_as_us_eastern():
    moment, forced = clockmod.resolve_now("16:05", reference=dt.datetime(2026, 7, 30, 12, tzinfo=dt.UTC))
    assert forced
    assert moment.astimezone(mcal.EXCHANGE_TZ).strftime("%H:%M") == "16:05"


def test_at_respects_an_explicit_offset():
    moment, _ = clockmod.resolve_now("2026-07-30T17:00:00+03:00")
    assert moment == dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)


def test_at_rejects_nonsense():
    with pytest.raises(ValueError):
        clockmod.resolve_now("half past four")


def test_naive_datetimes_are_refused_at_the_boundary():
    """A naive datetime read as UTC would shift every session comparison by hours."""
    with pytest.raises(ValueError):
        clockmod.build(dt.datetime(2026, 7, 30, 10, 0))
    with pytest.raises(ValueError):
        mcal.minutes_since_open(dt.datetime(2026, 7, 30, 10, 0))


def test_session_bells_come_from_the_exchange_calendar():
    opened = mcal.session_open(dt.date(2026, 7, 30)).astimezone(mcal.EXCHANGE_TZ)
    closed = mcal.session_close(dt.date(2026, 7, 30)).astimezone(mcal.EXCHANGE_TZ)
    assert (opened.hour, opened.minute) == (9, 30)
    assert (closed.hour, closed.minute) == (16, 0)

    half = mcal.session_close(dt.date(2026, 11, 27)).astimezone(mcal.EXCHANGE_TZ)
    assert (half.hour, half.minute) == (13, 0)
    assert mcal.is_half_day(dt.date(2026, 11, 27))
    assert not mcal.is_half_day(dt.date(2026, 7, 30))
