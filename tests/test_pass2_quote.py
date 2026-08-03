"""The session low: the number the stop is derived from.

Fixtures are built here rather than pasted, following the convention in the rest of the
suite. Building them is also what makes the important case testable at all: on the real
CBRL session of 2026-07-30 the pre-market low (56.00) sat *above* the regular-hours low
(55.25), so live data cannot demonstrate that pre-market exclusion works. The first test
below constructs the dangerous arrangement deliberately.
"""

from __future__ import annotations

import datetime as dt

import pytest

from qms import calendar as mcal
from qms.config import load_scan_config
from qms.pass2 import clock as clockmod
from qms.pass2 import quote as quotemod

SESSION = dt.date(2026, 7, 30)


@pytest.fixture(scope="module")
def cfg():
    return load_scan_config()


def _epoch(hour: int, minute: int, day: dt.date = SESSION) -> int:
    return int(dt.datetime.combine(day, dt.time(hour, minute), tzinfo=mcal.EXCHANGE_TZ).timestamp())


def _payload(bars, *, vendor_low=None, market_time=None, day=SESSION):
    """A chart payload shaped like Yahoo's, from (hour, minute, low, high) tuples."""
    start, end = _epoch(9, 30, day), _epoch(16, 0, day)
    timestamps = [_epoch(h, m, day) for h, m, _, _ in bars]
    lows = [low for _, _, low, _ in bars]
    highs = [high for _, _, _, high in bars]
    rth = [b for b in bars if start <= _epoch(b[0], b[1], day) < end]
    return {
        "meta": {
            "regularMarketPrice": 57.08,
            "regularMarketTime": market_time if market_time is not None else _epoch(11, 0, day),
            "regularMarketDayLow": vendor_low
            if vendor_low is not None
            else (min(b[2] for b in rth) if rth else None),
            "tradingPeriods": {"regular": [[{"start": start, "end": end}]]},
        },
        "timestamp": timestamps,
        "indicators": {"quote": [{"low": lows, "high": highs}]},
    }


def _at(text: str):
    moment, forced = clockmod.resolve_now(text)
    return clockmod.build(moment, forced=forced)


def test_premarket_low_below_the_session_low_is_excluded(cfg):
    """The corruption case: a pre-market print lower than anything in regular hours.

    If this leaked through, the stop would be set off a price the session never traded at.
    """
    payload = _payload(
        [
            (7, 15, 50.00, 50.50),  # pre-market, and the lowest print of the day
            (9, 30, 56.00, 56.40),
            (10, 34, 55.25, 55.60),
            (10, 45, 55.80, 56.10),
        ]
    )
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T11:00"), cfg)

    assert result.session_low.value == pytest.approx(55.25)
    assert result.session_low.ok
    # And the excluded print is shown, so the operator can see the filter ran.
    assert result.premarket_low_excluded.value == pytest.approx(50.00)
    assert "EXCLUDED" in result.premarket_low_excluded.note


def test_the_bar_on_the_opening_bell_is_included_and_the_one_before_is_not(cfg):
    """A 1-minute bar is stamped at its open, so 09:30 is in the session and 09:29 is not."""
    payload = _payload(
        [
            (9, 29, 40.00, 40.10),  # one minute early
            (9, 30, 56.00, 56.40),  # the opening bar
            (10, 0, 56.50, 56.90),
        ]
    )
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T11:00"), cfg)

    assert result.session_low.value == pytest.approx(56.00)
    assert result.premarket_low_excluded.value == pytest.approx(40.00)


def test_low_is_truncated_at_the_forced_moment(cfg):
    """--at is a real time machine: a low set after that moment must not be visible."""
    payload = _payload(
        [
            (9, 30, 56.00, 56.40),
            (10, 0, 55.90, 56.10),
            (11, 30, 50.00, 55.00),  # after the --at moment
        ]
    )
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T10:30"), cfg)

    assert result.session_low.value == pytest.approx(55.90)


def test_disagreement_with_the_vendor_day_low_is_reported_not_resolved(cfg):
    payload = _payload(
        [(9, 30, 56.00, 56.40), (10, 34, 55.25, 55.60)],
        vendor_low=54.10,  # a value the bars do not support
    )
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T16:30"), cfg)

    assert quotemod.FLAG_LOW_MISMATCH in result.flags
    # Both numbers survive; neither is silently chosen.
    assert result.session_low.value == pytest.approx(55.25)
    assert result.crosscheck.value == pytest.approx(54.10)
    assert "DISAGREES" in result.crosscheck.note


def test_agreement_with_the_vendor_day_low_is_recorded(cfg):
    payload = _payload([(9, 30, 56.00, 56.40), (10, 34, 55.25, 55.60)])
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T16:30"), cfg)

    assert quotemod.FLAG_LOW_MISMATCH not in result.flags
    assert result.crosscheck.value == pytest.approx(55.25)
    assert "agrees" in result.crosscheck.note


def test_a_previous_close_is_never_presented_as_a_live_price(cfg):
    """Observed on the real endpoint with the market shut: a stale price in a live field."""
    payload = _payload(
        [(9, 30, 56.00, 56.40)],
        market_time=_epoch(16, 0, dt.date(2026, 7, 29)),  # yesterday's close
    )
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T09:00"), cfg)

    assert result.is_live is False
    assert quotemod.FLAG_STALE in result.flags
    assert "PREVIOUS CLOSE" in result.current_price.note
    # And no session low is invented for a session that has not begun.
    assert not result.session_low.ok


def test_no_session_low_before_the_opening_bell(cfg):
    payload = _payload([(7, 15, 50.00, 50.50), (9, 0, 51.00, 51.20)])
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T09:15"), cfg)

    assert not result.session_low.ok
    assert "not started" in result.session_low.reason


def test_a_payload_for_another_session_is_refused(cfg):
    """The 1-minute endpoint only serves the current session; say so rather than imply
    the requested session had no trading."""
    payload = _payload([(9, 30, 56.00, 56.40)], day=dt.date(2026, 7, 31))
    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T11:00"), cfg)

    assert quotemod.FLAG_PAYLOAD_DATE in result.flags
    assert not result.session_low.ok
    assert "only serves the current session" in result.session_low.reason


def test_bars_without_trades_are_skipped_not_zero_filled(cfg):
    """Yahoo returns nulls for minutes with no prints. A null must never become a 0.0 low."""
    payload = _payload([(9, 30, 56.00, 56.40), (9, 31, 55.90, 56.00)])
    payload["indicators"]["quote"][0]["low"] = [56.00, None]
    payload["indicators"]["quote"][0]["high"] = [56.40, None]
    payload["meta"]["regularMarketDayLow"] = 56.00

    result = quotemod.build_quote("TEST", payload, _at("2026-07-30T11:00"), cfg)

    assert result.session_low.value == pytest.approx(56.00)
