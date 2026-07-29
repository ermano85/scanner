"""Earnings calendar from Nasdaq's public calendar endpoint.

UNOFFICIAL ENDPOINT. One request per calendar date, ~150-370 rows each (probed
2026-07-27 across five dates, forward and historical). The blackout gate only needs the
forward window, so a nightly run costs about fifteen requests.

The `time` field matters and is not cosmetic. A company reporting *after* the close on
day D is tradeable on the morning of D; one reporting *before* the open on D is not. The
blackout gate consumes `when` to decide the last safe session.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms.calendar import sessions_in_range
from qms.ingest.base import EARNINGS_SCHEMA, conform, empty
from qms.ingest.http import HttpClient

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

# Nasdaq's own vocabulary for release timing.
_WHEN_BEFORE_OPEN = "bmo"
_WHEN_AFTER_CLOSE = "amc"
_WHEN_UNKNOWN = "unknown"

_TIME_MAP = {
    "time-pre-market": _WHEN_BEFORE_OPEN,
    "time-after-hours": _WHEN_AFTER_CLOSE,
    "time-not-supplied": _WHEN_UNKNOWN,
}


def clean_number(raw: str | None) -> float | None:
    """Nasdaq formats numbers as '$3.23', '($0.14)', '$673,938,031,023' or ''.

    Public because the historical-bars endpoint in `nasdaq_bars` serves the same dialect —
    prices with a leading '$' on equities but not on ETFs, and volumes with thousands
    separators. One parser, one set of edge cases.
    """
    if not raw:
        return None
    text = raw.strip().replace("$", "").replace(",", "")
    if not text or text in {"N/A", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def fetch_earnings_for_date(client: HttpClient, day: dt.date) -> pl.DataFrame:
    payload = client.get_json(EARNINGS_URL, {"date": day.isoformat()})
    rows = ((payload or {}).get("data") or {}).get("rows") or []

    records = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        records.append(
            {
                "symbol": symbol,
                "earnings_date": day,
                "when": _TIME_MAP.get(row.get("time") or "", _WHEN_UNKNOWN),
                "eps_forecast": clean_number(row.get("epsForecast")),
                "market_cap": clean_number(row.get("marketCap")),
                "fiscal_quarter_ending": (row.get("fiscalQuarterEnding") or None),
            }
        )

    if not records:
        return empty(EARNINGS_SCHEMA)

    frame = pl.DataFrame(
        records,
        schema_overrides={
            "earnings_date": pl.Date,
            "eps_forecast": pl.Float64,
            "market_cap": pl.Float64,
            "fiscal_quarter_ending": pl.Utf8,
        },
    )
    return conform(frame, EARNINGS_SCHEMA)


def fetch_earnings_range(
    client: HttpClient,
    start: dt.date,
    end: dt.date,
    on_error=None,
) -> pl.DataFrame:
    """Earnings across every *session* in [start, end].

    Non-sessions are skipped: companies do not schedule releases for days the market is
    shut, and asking costs a request each.
    """
    days = sessions_in_range(start, end)
    if not days:
        return empty(EARNINGS_SCHEMA)

    frames: list[pl.DataFrame] = []
    for _day, frame in client.map(
        lambda d: fetch_earnings_for_date(client, d),
        days,
        on_error=on_error,
    ):
        if not frame.is_empty():
            frames.append(frame)

    if not frames:
        return empty(EARNINGS_SCHEMA)

    return (
        pl.concat(frames)
        .unique(subset=["symbol", "earnings_date"], keep="first")
        .sort(["symbol", "earnings_date"])
    )
