"""Daily bars from Nasdaq's historical quote endpoint — the gap-fill source.

UNOFFICIAL ENDPOINT, like the earnings calendar it sits beside.

**Why this exists.** Yahoo's chart endpoint intermittently serves an all-null row for a
completed session. Observed 2026-07-24: every symbol tested — AAPL, MSFT, NVDA, SPY, KO,
JNJ — returned `close: null`, under both `range=5d` and `range=1mo`, while 42 of 11,574
symbols carried a real bar. That ragged edge made a Monday-evening report show Thursday's
close. Nasdaq has the session (AAPL closed $333.02 on 47.5M shares), so the market data
exists and the hole is Yahoo's alone.

**Why it is not the primary source.** Roughly 2.4 s per request against Yahoo's ~0.3 s, and
one request per symbol either way. Fine for repairing a few sessions across the active
universe; hopeless as a 12,000-symbol bulk loader.

Verified 2026-07-27:

* Full OHLCV, and 2+ years of history (519 rows back to 2024-07-01).
* ETFs supported via `assetclass=etf`.
* Split-adjusted, matching the policy in docs/DATA.md.
* Agrees with Yahoo **exactly** on overlapping sessions (AAPL 07/23 = 321.66 from both).
  Volume differs only by Yahoo rounding to the nearest 100: 40,840,800 vs 40,840,780.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms.ingest.base import BARS_SCHEMA, ProviderError, conform, empty, valid_bars
from qms.ingest.http import HttpClient
from qms.ingest.nasdaq_earnings import clean_number
from qms.ingest.universe import to_vendor_symbol

HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"

ASSET_CLASS_STOCKS = "stocks"
ASSET_CLASS_ETF = "etf"

# Nasdaq renders dates as MM/DD/YYYY, not ISO.
_DATE_FORMAT = "%m/%d/%Y"

# The endpoint caps rows per response; ask for more than any sane gap window needs.
_ROW_LIMIT = 9999

# A single-day request returns ZERO rows — `fromdate=2026-07-24&todate=2026-07-24` yields
# an empty table even though the session exists and a wider range returns it. Found the
# expensive way: a full gap-fill pass over 5,470 symbols fetched nothing at all, because
# repairing exactly one session is precisely the case that trips this.
#
# Every request is therefore widened to span at least this many calendar days. It costs
# nothing (one request either way), and the caller filters to the sessions it wanted.
_MIN_SPAN_DAYS = 7


def _parse_date(raw: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(raw.strip(), _DATE_FORMAT).date()
    except (ValueError, AttributeError):
        return None


def parse_bars(symbol: str, payload: dict, max_date: dt.date) -> pl.DataFrame:
    """Nasdaq historical payload -> BARS_SCHEMA.

    `adjclose` is left **null**: this endpoint does not publish a dividend-adjusted close,
    and inventing one by copying `close` would assert something we did not receive. No v1
    feature reads the column — it exists for provenance — so a null is both honest and
    harmless.
    """
    rows = (((payload or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    if not rows:
        return empty(BARS_SCHEMA)

    records = []
    for row in rows:
        day = _parse_date(row.get("date", ""))
        if day is None or day > max_date:
            continue
        records.append(
            {
                "symbol": symbol,
                "date": day,
                "open": clean_number(row.get("open")),
                "high": clean_number(row.get("high")),
                "low": clean_number(row.get("low")),
                "close": clean_number(row.get("close")),
                # Some thinly traded ETFs come back with no volume at all — VFLO, VTEC and
                # VTES on 2026-07-24. `valid_bars` drops those rather than storing a bar
                # whose dollar volume cannot be computed.
                "volume": clean_number(row.get("volume")),
                "adjclose": None,
            }
        )

    if not records:
        return empty(BARS_SCHEMA)

    frame = pl.DataFrame(
        records,
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
    frame = valid_bars(frame).unique(subset=["symbol", "date"], keep="last").sort("date")
    return conform(frame, BARS_SCHEMA)


def fetch_symbol_bars(
    client: HttpClient,
    symbol: str,
    start: dt.date,
    end: dt.date,
    is_etf: bool = False,
) -> pl.DataFrame:
    """Bars for one symbol over [start, end].

    `assetclass` must match the instrument or the endpoint returns nothing, so a miss on
    the expected class retries with the other one rather than reporting a phantom gap —
    the universe's ETF flag comes from the NASDAQ Trader directory and is not infallible.
    """
    request_start = min(start, end - dt.timedelta(days=_MIN_SPAN_DAYS))
    params = {
        "assetclass": ASSET_CLASS_ETF if is_etf else ASSET_CLASS_STOCKS,
        "fromdate": request_start.isoformat(),
        "todate": end.isoformat(),
        "limit": _ROW_LIMIT,
    }
    url = HISTORICAL_URL.format(symbol=to_vendor_symbol(symbol))

    payload = client.get_json(url, params)
    bars = parse_bars(symbol, payload, max_date=end)
    if not bars.is_empty():
        return bars

    params["assetclass"] = ASSET_CLASS_STOCKS if is_etf else ASSET_CLASS_ETF
    return parse_bars(symbol, client.get_json(url, params), max_date=end)


def fetch_bars(
    client: HttpClient,
    symbols: list[str],
    start: dt.date,
    end: dt.date,
    etf_flags: dict[str, bool] | None = None,
    on_error=None,
) -> pl.DataFrame:
    """Bars for many symbols. One request each, bounded by the client's concurrency."""
    if not symbols:
        return empty(BARS_SCHEMA)
    flags = etf_flags or {}

    frames: list[pl.DataFrame] = []
    for _symbol, frame in client.map(
        lambda s: fetch_symbol_bars(client, s, start, end, flags.get(s, False)),
        symbols,
        on_error=on_error,
    ):
        if not frame.is_empty():
            frames.append(frame)

    if not frames:
        return empty(BARS_SCHEMA)
    return pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])


class NasdaqBarsProvider:
    """Adapter satisfying the bars half of the `Provider` protocol."""

    name = "nasdaq"

    def __init__(self, client: HttpClient, etf_flags: dict[str, bool] | None = None):
        self.client = client
        self.etf_flags = etf_flags or {}

    def bars(self, symbols: list[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        return fetch_bars(self.client, symbols, start, end, self.etf_flags)

    def universe(self) -> pl.DataFrame:
        raise ProviderError("nasdaq bars provider does not supply a universe")

    def actions(self, symbols: list[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        raise ProviderError("nasdaq bars provider does not supply corporate actions")

    def earnings(self, start: dt.date, end: dt.date) -> pl.DataFrame:
        raise ProviderError("use ingest.nasdaq_earnings for the earnings calendar")
