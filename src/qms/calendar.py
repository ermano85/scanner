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
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

CALENDAR_NAME = "XNYS"
EXCHANGE_TZ = ZoneInfo("America/New_York")


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


# --------------------------------------------------------------------- intraday times
#
# Everything above deals in whole sessions, which is all the nightly scan ever needed.
# Pass 2 runs *during* a session, so it needs the bells themselves. These come from the
# exchange calendar rather than a hardcoded 09:30/16:00 for one reason: a half-day is not
# a special case here, it is simply a session whose `session_close` is 13:00. Hardcoding
# the close would silently mis-state "minutes since open" on exactly the low-liquidity
# days where an entry is most fragile.


REGULAR_CLOSE_ET = dt.time(16, 0)


def session_open(day: dt.date) -> dt.datetime:
    """Opening bell for `day` as a tz-aware UTC datetime. Raises if `day` is not a session."""
    return pd.Timestamp(_calendar().session_open(_ts(day))).to_pydatetime()


def session_close(day: dt.date) -> dt.datetime:
    """Closing bell for `day` as a tz-aware UTC datetime. Raises if `day` is not a session."""
    return pd.Timestamp(_calendar().session_close(_ts(day))).to_pydatetime()


def is_half_day(day: dt.date) -> bool:
    """True when `day` is a session that closes early (1pm ET the day after Thanksgiving, etc.)."""
    if not is_session(day):
        return False
    close_et = session_close(day).astimezone(EXCHANGE_TZ).time()
    return close_et < REGULAR_CLOSE_ET


def current_session(now: dt.datetime) -> dt.date | None:
    """The session in progress at `now`, or None if the market is not open.

    "In progress" means between the bells inclusive of the open and exclusive of the
    close. The ET date is what selects the candidate session, not the UTC date — after
    19:00 ET those differ, and using UTC would look up tomorrow's session.
    """
    moment = _require_aware(now)
    day = moment.astimezone(EXCHANGE_TZ).date()
    if not is_session(day):
        return None
    if session_open(day) <= moment < session_close(day):
        return day
    return None


def minutes_since_open(now: dt.datetime) -> float | None:
    """Minutes elapsed since the opening bell, or None when no session is in progress."""
    day = current_session(now)
    if day is None:
        return None
    return (_require_aware(now) - session_open(day)).total_seconds() / 60.0


def _require_aware(moment: dt.datetime) -> dt.datetime:
    """Reject naive datetimes at the boundary.

    A naive datetime here is not a small bug: it would be interpreted as UTC, shifting
    every session comparison by 3-11 hours depending on the season and the operator's
    location, and the resulting session low would be wrong without looking wrong.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"naive datetime is ambiguous here: {moment!r}; attach a timezone")
    return moment


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
