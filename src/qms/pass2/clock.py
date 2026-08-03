"""The session clock: what time is it, in both places, and where are we in the session.

The operator is in Estonia and the market is in New York. Those two zones change their
clocks on *different dates* — the US springs forward on the second Sunday in March and
the EU on the last Sunday in March, and in autumn the US falls back a week after the EU.
So for roughly three weeks a year the offset is 6 or 8 hours rather than the usual 7.

The defence is to never compute an offset at all. Internally everything is a tz-aware UTC
instant; local times exist only at the moment of rendering, produced by `zoneinfo`, which
knows the rules. The opening and closing bells come from `exchange_calendars`, so a
half-day is not a special case — it is just a session whose close is 13:00.

`--at` exists so this is testable without waiting for 16:30 Tallinn time on a weekday.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from qms import calendar as mcal

OPERATOR_TZ = ZoneInfo("Europe/Tallinn")
EXCHANGE_TZ = mcal.EXCHANGE_TZ

STATE_OPEN = "OPEN"
STATE_PRE = "PRE-MARKET"
STATE_POST = "AFTER HOURS"
STATE_CLOSED = "CLOSED"
STATE_HOLIDAY = "HOLIDAY"
STATE_WEEKEND = "WEEKEND"


@dataclass(frozen=True)
class SessionClock:
    """Where `now` sits relative to the US market calendar."""

    now: dt.datetime  # tz-aware UTC
    state: str
    session_date: dt.date | None  # the session in progress, if any
    reference_session: dt.date  # the session whose bars are "today's", open or not
    minutes_since_open: float | None
    session_open: dt.datetime | None
    session_close: dt.datetime | None
    half_day: bool
    forced: bool

    @property
    def is_open(self) -> bool:
        return self.state == STATE_OPEN

    def in_operator_tz(self) -> dt.datetime:
        return self.now.astimezone(OPERATOR_TZ)

    def in_exchange_tz(self) -> dt.datetime:
        return self.now.astimezone(EXCHANGE_TZ)

    def describe_state(self) -> str:
        """The header's market-state string, including the early-close warning."""
        # ASCII only, deliberately: this text is copied out of a Windows console and
        # pasted into a chat window, and cp1252 turns an em-dash into a question mark.
        if self.state == STATE_OPEN and self.half_day and self.session_close is not None:
            closes = self.session_close.astimezone(EXCHANGE_TZ).strftime("%H:%M")
            return f"{STATE_OPEN} - HALF-DAY, early close {closes} ET"
        if self.state == STATE_HOLIDAY:
            return f"{STATE_HOLIDAY} - US market closed"
        return self.state


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def resolve_now(at: str | None, *, reference: dt.datetime | None = None) -> tuple[dt.datetime, bool]:
    """Turn `--at` into a tz-aware UTC instant. Returns (moment, was_forced).

    Accepted forms, in the order they are tried:

      HH:MM              — that wall-clock time **in US Eastern**, on the reference date
      YYYY-MM-DDTHH:MM   — same, with an explicit date
      ...any ISO with an explicit offset — taken at face value

    Eastern is the default zone because every other time in this tool is a market time,
    and `--at 16:05` in the spec plainly means five past the New York close. An operator
    who means their own clock can pass a full ISO timestamp with an offset.
    """
    base = reference or now_utc()
    if at is None:
        return base, False

    text = at.strip()
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is not None:
            return parsed.astimezone(dt.UTC), True
        # Naive but complete: interpret in Eastern.
        return parsed.replace(tzinfo=EXCHANGE_TZ).astimezone(dt.UTC), True

    # Bare HH:MM, applied to the reference date as seen in Eastern.
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            clock = dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        day = base.astimezone(EXCHANGE_TZ).date()
        stamped = dt.datetime.combine(day, clock, tzinfo=EXCHANGE_TZ)
        return stamped.astimezone(dt.UTC), True

    raise ValueError(
        f"could not read --at {at!r}; expected HH:MM (US Eastern), "
        "YYYY-MM-DDTHH:MM, or a full ISO timestamp with an offset"
    )


def build(now: dt.datetime, *, forced: bool = False) -> SessionClock:
    """Classify `now` against the exchange calendar."""
    if now.tzinfo is None:
        raise ValueError("clock.build requires a timezone-aware datetime")
    now = now.astimezone(dt.UTC)

    et_date = now.astimezone(EXCHANGE_TZ).date()
    session = mcal.current_session(now)

    if session is not None:
        opened = mcal.session_open(session)
        closes = mcal.session_close(session)
        return SessionClock(
            now=now,
            state=STATE_OPEN,
            session_date=session,
            reference_session=session,
            minutes_since_open=(now - opened).total_seconds() / 60.0,
            session_open=opened,
            session_close=closes,
            half_day=mcal.is_half_day(session),
            forced=forced,
        )

    # Not open. Distinguish "today is a session, we are outside its hours" from
    # "today is not a session at all" — the operator needs to know which.
    if mcal.is_session(et_date):
        opened = mcal.session_open(et_date)
        closes = mcal.session_close(et_date)
        state = STATE_PRE if now < opened else STATE_POST
        # `reference_session` is the session this run is *about*, which in pre-market is
        # today's upcoming one, not yesterday's finished one. It has to match the session
        # the intraday payload describes, or the check that the vendor's regular-hours
        # window agrees with the exchange calendar fires on a one-day offset every morning
        # and trains the operator to ignore it. Prior-session OHLCV is a separate question,
        # answered by stepping back from here in `daily.py`.
        return SessionClock(
            now=now,
            state=state,
            session_date=None,
            reference_session=et_date,
            minutes_since_open=None,
            session_open=opened,
            session_close=closes,
            half_day=mcal.is_half_day(et_date),
            forced=forced,
        )

    state = STATE_WEEKEND if et_date.weekday() >= 5 else STATE_HOLIDAY
    return SessionClock(
        now=now,
        state=state,
        session_date=None,
        reference_session=mcal.session_or_previous(et_date),
        minutes_since_open=None,
        session_open=None,
        session_close=None,
        half_day=False,
        forced=forced,
    )


def format_both(moment: dt.datetime) -> str:
    """`14:35:02 EEST (Tallinn) / 07:35:02 EDT (New York)` — both zones, named, never an offset."""
    local = moment.astimezone(OPERATOR_TZ)
    east = moment.astimezone(EXCHANGE_TZ)
    return (
        f"{local.strftime('%Y-%m-%d %H:%M:%S')} {local.tzname()} (Tallinn) / "
        f"{east.strftime('%H:%M:%S')} {east.tzname()} (New York)"
    )


def et_time(moment: dt.datetime) -> str:
    """`10:34:00 EDT` — for stamping an individual measurement."""
    east = moment.astimezone(EXCHANGE_TZ)
    return f"{east.strftime('%H:%M:%S')} {east.tzname()}"
