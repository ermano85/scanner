"""Daily bars and corporate actions from Yahoo's chart endpoint.

UNOFFICIAL ENDPOINT. It has no contract and can change without notice; see the README.
It is used directly rather than through `yfinance` because this job needs precise control
over concurrency, backoff and resume, and because a thin client over a response shape we
have verified is easier to debug than a library that reshapes it.

Verified 2026-07-27 on NVDA across its 2024-06-10 10:1 split: bars dated 2024-06-06 carry
close=121.00 against a real screen price that day of ~$1,210, and volume is restated to
match. So `indicators.quote` is **split-adjusted and not dividend-adjusted** — exactly the
policy in docs/DATA.md, with no post-processing. `indicators.adjclose` is the
split+dividend series and is stored but unused by v1 features.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import polars as pl

from qms.ingest.base import ACTIONS_SCHEMA, BARS_SCHEMA, ProviderError, conform, empty
from qms.ingest.http import HttpClient, HttpError
from qms.ingest.universe import to_vendor_symbol

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo timestamps daily bars at the session open in exchange-local time. For US equities
# that is 09:30 ET, which never crosses midnight UTC in either DST regime — but we
# convert explicitly rather than relying on that.
EXCHANGE_TZ = ZoneInfo("America/New_York")


def _bar_date(epoch_seconds: int) -> dt.date:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=EXCHANGE_TZ).date()


def fetch_symbol_chart(
    client: HttpClient,
    symbol: str,
    start: dt.date,
    end: dt.date,
) -> dict:
    """Raw chart payload for one symbol over [start, end]."""
    # period2 is exclusive-ish and interpreted in exchange time; pad by a day so the end
    # session is always included.
    params = {
        "period1": int(dt.datetime.combine(start, dt.time.min, tzinfo=EXCHANGE_TZ).timestamp()),
        "period2": int(
            dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=EXCHANGE_TZ).timestamp()
        ),
        "interval": "1d",
        "events": "div,split",
        "includeAdjustedClose": "true",
    }
    payload = client.get_json(CHART_URL.format(symbol=to_vendor_symbol(symbol)), params)

    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        code = (chart["error"] or {}).get("code", "")
        raise HttpError(
            f"{symbol}: {chart['error']}",
            permanent=code in {"Not Found", "Bad Request"},
        )
    results = chart.get("result")
    if not results:
        raise ProviderError(f"{symbol}: chart response carried no result")
    return results[0]


def parse_bars(symbol: str, result: dict, max_date: dt.date) -> pl.DataFrame:
    """Chart payload -> BARS_SCHEMA frame.

    `max_date` must be the last *completed* session. Yahoo happily returns a live,
    partially-formed bar for the current session; ingesting it would mean the same date
    holds different values before and after the close, which silently corrupts both the
    feature store and any future backtest.
    """
    timestamps = result.get("timestamp") or []
    quote_blocks = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote_blocks[0] if quote_blocks else {}
    adj_blocks = (result.get("indicators") or {}).get("adjclose") or []
    adjclose = (adj_blocks[0] if adj_blocks else {}).get("adjclose") or [None] * len(timestamps)

    if not timestamps:
        return empty(BARS_SCHEMA)

    frame = pl.DataFrame(
        {
            "symbol": [symbol] * len(timestamps),
            "date": [_bar_date(ts) for ts in timestamps],
            "open": quote.get("open") or [None] * len(timestamps),
            "high": quote.get("high") or [None] * len(timestamps),
            "low": quote.get("low") or [None] * len(timestamps),
            "close": quote.get("close") or [None] * len(timestamps),
            "volume": quote.get("volume") or [None] * len(timestamps),
            "adjclose": adjclose,
        },
        schema_overrides={
            "date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "adjclose": pl.Float64,
        },
    )

    frame = (
        frame
        # Halted sessions come back as an all-null row. A null OHLC is not a bar.
        .filter(
            pl.col("close").is_not_null()
            & pl.col("open").is_not_null()
            & pl.col("high").is_not_null()
            & pl.col("low").is_not_null()
        )
        .filter(pl.col("date") <= max_date)
        # Yahoo occasionally repeats the final timestamp when a live bar and the settled
        # bar for the same session both appear; last wins.
        .unique(subset=["symbol", "date"], keep="last", maintain_order=True)
        .sort("date")
    )
    return conform(frame, BARS_SCHEMA)


def parse_actions(symbol: str, result: dict) -> pl.DataFrame:
    """Chart payload -> ACTIONS_SCHEMA frame.

    Splits are what matter: the data-quality gate uses them to decide whether a large
    overnight price jump is a corporate action or a bad tick.
    """
    events = result.get("events") or {}
    records: list[dict] = []

    for raw in (events.get("splits") or {}).values():
        records.append(
            {
                "symbol": symbol,
                "date": _bar_date(int(raw["date"])),
                "action": "split",
                "numerator": float(raw.get("numerator") or 0.0),
                "denominator": float(raw.get("denominator") or 0.0),
                "amount": None,
            }
        )
    for raw in (events.get("dividends") or {}).values():
        records.append(
            {
                "symbol": symbol,
                "date": _bar_date(int(raw["date"])),
                "action": "dividend",
                "numerator": None,
                "denominator": None,
                "amount": float(raw.get("amount") or 0.0),
            }
        )

    if not records:
        return empty(ACTIONS_SCHEMA)

    frame = pl.DataFrame(
        records,
        schema_overrides={
            "date": pl.Date,
            "numerator": pl.Float64,
            "denominator": pl.Float64,
            "amount": pl.Float64,
        },
    )
    return conform(frame.sort("date"), ACTIONS_SCHEMA)
