"""Monitor mode: open positions and their alerts.

The case that drives the design is the live `journal/positions.csv`, whose `current_stop`
column currently reads `NONE - CANCELLED BY HAND 2026-07-29`. A parser calling `float()`
on that crashes; one coercing it to 0.0 reports a position comfortably above its stop,
which is worse than crashing.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.ingest.base import BARS_SCHEMA
from qms.pass2 import positions as posmod
from qms.pass2.model import Value

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 14, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_scan_config()


def _row(**overrides) -> dict:
    row = {
        "symbol": "TEST",
        "entry_date": "2026-07-29",
        "entry_price": "56.30",
        "shares": "41",
        "initial_stop": "55.45",
        "current_stop": "55.45",
        "risk_dollars": "34.85",
        "partial_taken": "no",
        "thesis": "test position",
    }
    row.update(overrides)
    return row


def _bars(closes: list[float], symbol: str = "TEST") -> pl.DataFrame:
    start = dt.date(2026, 5, 1)
    rows = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000_000.0,
                "adjclose": close,
            }
        )
        day += dt.timedelta(days=1)
    return pl.DataFrame(rows, schema=BARS_SCHEMA)


def _price(value: float) -> Value:
    return Value.fetched(value, source="test", as_of=NOW)


def _evaluate(cfg, row, price=56.50, *, bars=None, earnings_days=None, is_live=True):
    return posmod.evaluate(
        row,
        cfg,
        today=TODAY,
        price=_price(price),
        is_live=is_live,
        bars=bars if bars is not None else _bars([50.0] * 20),
        earnings_days=earnings_days,
    )


def _tags(report) -> set[str]:
    return {a.tag for a in report.alerts}


def test_a_non_numeric_current_stop_raises_the_unprotected_alert(cfg):
    report = _evaluate(cfg, _row(current_stop="NONE - CANCELLED BY HAND 2026-07-29"))

    assert not report.current_stop.ok
    assert "not a price" in report.current_stop.reason
    assert posmod.ALERT_NO_STOP[0] in _tags(report)
    # It must not be mistaken for a breach, which would misdescribe the problem.
    assert posmod.ALERT_STOP_BREACHED[0] not in _tags(report)


def test_an_empty_current_stop_also_raises_it(cfg):
    report = _evaluate(cfg, _row(current_stop=""))
    assert posmod.ALERT_NO_STOP[0] in _tags(report)


def test_the_real_positions_file_parses(cfg):
    """The shipped journal is the fixture that matters most.

    Asserts that whatever is in the file today parses cleanly, not that anything is held.
    Flat is a normal and frequent state in this strategy — most days are no-action days —
    and a test that fails on an empty book would fail for a correct reason, which trains
    you to ignore it.
    """
    from pathlib import Path

    rows, failures = posmod.read_positions(Path("journal/positions.csv"))
    assert failures == []

    for row in rows:
        report = _evaluate(cfg, row, bars=_bars([50.0] * 20, symbol=row["symbol"]))
        assert report.symbol


def test_a_price_below_the_stop_is_a_breach_with_dollars_and_r(cfg):
    report = _evaluate(cfg, _row(), price=55.00)

    assert posmod.ALERT_STOP_BREACHED[0] in _tags(report)
    breach = next(a for a in report.alerts if a.tag == posmod.ALERT_STOP_BREACHED[0])
    assert breach.critical
    assert breach.rank == 0
    assert "0.45" in breach.detail  # 55.45 - 55.00
    assert "R" in breach.detail


def test_a_price_above_the_stop_is_not_a_breach(cfg):
    assert posmod.ALERT_STOP_BREACHED[0] not in _tags(_evaluate(cfg, _row(), price=56.50))


def test_pnl_in_dollars_and_r(cfg):
    report = _evaluate(cfg, _row(), price=57.30)
    # (57.30 - 56.30) * 41 = 41.00, over 34.85 of risk.
    assert report.unrealized_dollars.value == pytest.approx(41.00)
    assert report.unrealized_r.value == pytest.approx(41.00 / 34.85)


def test_values_from_a_non_live_price_say_so(cfg):
    report = _evaluate(cfg, _row(), price=57.30, is_live=False)
    assert "non-live price" in report.unrealized_dollars.note


def test_concentration_fires_above_the_configured_cap(cfg):
    ceiling = cfg.sizing.max_account_concentration * cfg.sizing.account
    over = _evaluate(cfg, _row(shares="41"), price=(ceiling / 41) + 1)
    assert posmod.ALERT_CONCENTRATION[0] in _tags(over)

    under = _evaluate(cfg, _row(shares="41"), price=(ceiling / 41) - 1)
    assert posmod.ALERT_CONCENTRATION[0] not in _tags(under)


def test_the_partial_window_alert_respects_the_configured_range(cfg):
    low, high = cfg.pass2.partial_window_days
    # 2026-07-24 is a Friday; 2026-07-31 is the Friday after, so five sessions.
    inside = _evaluate(cfg, _row(entry_date="2026-07-24"))
    assert low <= inside.days_held.value <= high
    assert posmod.ALERT_PARTIAL[0] in _tags(inside)

    # A partial already taken silences it.
    taken = _evaluate(cfg, _row(entry_date="2026-07-24", partial_taken="2026-07-29 33%"))
    assert posmod.ALERT_PARTIAL[0] not in _tags(taken)

    # And so does being outside the window.
    fresh = _evaluate(cfg, _row(entry_date="2026-07-30"))
    assert posmod.ALERT_PARTIAL[0] not in _tags(fresh)


def test_days_held_counts_trading_days_not_calendar_days(cfg):
    report = _evaluate(cfg, _row(entry_date="2026-07-24"))
    assert report.days_held.value == 5  # seven calendar days, five sessions


def test_earnings_soon_uses_the_configured_horizon(cfg):
    inside = _evaluate(cfg, _row(), earnings_days=cfg.pass2.earnings_soon_days)
    assert posmod.ALERT_EARNINGS[0] in _tags(inside)

    outside = _evaluate(cfg, _row(), earnings_days=cfg.pass2.earnings_soon_days + 1)
    assert posmod.ALERT_EARNINGS[0] not in _tags(outside)


def test_below_the_trail_average_is_judged_on_a_close(cfg):
    """A close below, never an intraday touch."""
    falling = _bars([60.0] * 15 + [50.0])
    report = _evaluate(cfg, _row(), price=59.0, bars=falling)
    assert report.below_sma_on_close.value is True
    assert posmod.ALERT_BELOW_MA[0] in _tags(report)
    assert "A close, not an intraday touch" in report.below_sma_on_close.formula

    rising = _bars([50.0] * 15 + [60.0])
    # Price is far below the average intraday, but the last close was above it.
    calm = _evaluate(cfg, _row(), price=10.0, bars=rising)
    assert calm.below_sma_on_close.value is False
    assert posmod.ALERT_BELOW_MA[0] not in _tags(calm)


def test_alert_ranks_put_the_criticals_first(cfg):
    report = _evaluate(
        cfg,
        _row(current_stop="NONE", entry_date="2026-07-24", shares="1000"),
        price=55.00,
        bars=_bars([60.0] * 15 + [50.0]),
        earnings_days=1,
    )
    ranks = [a.rank for a in sorted(report.alerts, key=lambda a: a.rank)]
    assert ranks == sorted(ranks)
    assert report.alerts and min(a.rank for a in report.alerts) <= 1
    assert any(a.critical for a in report.alerts)


def test_a_missing_file_is_reported_not_raised(cfg):
    from pathlib import Path

    rows, failures = posmod.read_positions(Path("does-not-exist.csv"))
    assert rows == []
    assert failures and "not found" in failures[0].detail


def test_missing_columns_are_reported(cfg, tmp_path):
    path = tmp_path / "positions.csv"
    path.write_text("symbol,entry_price\nTEST,10.0\n", encoding="utf-8")
    _rows, failures = posmod.read_positions(path)
    assert failures and "missing column" in failures[0].detail
