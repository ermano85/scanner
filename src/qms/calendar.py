"""Trading-day arithmetic over the NYSE calendar.

The earnings blackout gate in spec §4.1 counts *trading* days, not calendar days. Over a
3-day blackout the difference is not cosmetic: a Friday close is one trading day from the
following Monday but three calendar days, and a holiday week shifts it again. Every
date-difference in this codebase goes through here.

Note that ``exchange_calendars``' own ``next_session``/``previous_session`` raise on any
input that is not itself a session, so the helpers below snap first and step second.
"""

from __future__ import annotations

import datetime as dt
import functools

import exchange_calendars as xcals
import pandas as pd

CALENDAR_NAME = "XNYS"


@functools.lru_cache(maxsize=1)
def _calendar() -> xcals.ExchangeCalendar:
    # Bounded well before any data we hold and far enough forward for the earnings
    # calendar's horizon.
    return xcals.get_calendar(CALENDAR_NAME, start="1999-01-01")


def _ts(day: dt.date) -> pd.Timestamp:
    return pd.Timestamp(day)


def is_session(day: dt.date) -> bool:
    return bool(_calendar().is_session(_ts(day)))


def sessions_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """All trading sessions in [start, end], inclusive of both ends."""
    if start > end:
        return []
    return [s.date() for s in _calendar().sessions_in_range(_ts(start), _ts(end))]


def session_or_previous(day: dt.date) -> dt.date:
    """`day` if it is a session, else the most recent session before it."""
    return _calendar().date_to_session(_ts(day), direction="previous").date()


def session_or_next(day: dt.date) -> dt.date:
    """`day` if it is a session, else the first session after it."""
    return _calendar().date_to_session(_ts(day), direction="next").date()


def previous_session(day: dt.date) -> dt.date:
    """The last session **strictly before** `day`, whether or not `day` is a session."""
    if is_session(day):
        return _calendar().previous_session(_ts(day)).date()
    return session_or_previous(day)


def next_session(day: dt.date) -> dt.date:
    """The first session **strictly after** `day`, whether or not `day` is a session."""
    if is_session(day):
        return _calendar().next_session(_ts(day)).date()
    return session_or_next(day)


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """Count of sessions strictly after `start`, up to and including `end`.

    Returns 0 for the same session and a negative count when `end` precedes `start`.
    This is the "how many trading days until earnings" primitive: if earnings are the
    very next session, the answer is 1.
    """
    if start == end:
        return 0
    if end < start:
        return -trading_days_between(end, start)
    first = next_session(start)
    if first > end:
        return 0
    return len(sessions_in_range(first, end))


def last_completed_session(now: dt.datetime | None = None) -> dt.date:
    """The most recent session whose closing bell has already rung.

    The nightly job must never ingest a partially-formed bar. If this runs at 11:00 ET the
    exchange is mid-session, and today's high/low/close are provisional — writing them
    would poison both the feature store and any future backtest, because a re-run after
    the close would silently produce different history for the same date.
    """
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    cal = _calendar()
    candidate = session_or_previous(moment.date())
    # A handful of iterations at most: long weekends and holiday runs are short.
    while pd.Timestamp(cal.session_close(_ts(candidate))).to_pydatetime() > moment:
        candidate = previous_session(candidate)
    return candidate


def shift_sessions(day: dt.date, n: int) -> dt.date:
    """Move `n` sessions forward (n > 0) or backward (n < 0) from `day`.

    `n == 0` snaps a non-session backwards to the last completed session.
    """
    if n == 0:
        return session_or_previous(day)
    step = next_session if n > 0 else previous_session
    current = day
    for _ in range(abs(n)):
        current = step(current)
    return current
