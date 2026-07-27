"""Provider protocol and the canonical schemas every provider must return.

The point of this indirection is that two of the four v1 data sources are unofficial
endpoints that can change or vanish. Swapping to a paid vendor should be one new module
implementing this protocol plus a config line — not a refactor. Everything downstream of
ingest knows only these schemas.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

import polars as pl

# Bars are stored SPLIT-ADJUSTED and NOT dividend-adjusted; see docs/DATA.md.
# `adjclose` (split + dividend) is carried for provenance and is unused by v1 features.
BARS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "adjclose": pl.Float64,
}

UNIVERSE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "name": pl.Utf8,
    "exchange": pl.Utf8,
    "is_etf": pl.Boolean,
    "test_issue": pl.Boolean,
    "financial_status": pl.Utf8,
}

# `when` is the session-relative timing of the release, which decides the last safe
# session in the blackout gate: "bmo" (before market open), "amc" (after market close),
# or "unknown".
EARNINGS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "earnings_date": pl.Date,
    "when": pl.Utf8,
    "eps_forecast": pl.Float64,
    "market_cap": pl.Float64,
    "fiscal_quarter_ending": pl.Utf8,
}

ACTIONS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "action": pl.Utf8,  # "split" | "dividend"
    "numerator": pl.Float64,
    "denominator": pl.Float64,
    "amount": pl.Float64,
}


def empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def conform(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Force a frame into a canonical schema — column set, order and dtypes.

    Called on every provider's output so a vendor quirk (a missing column, an int where a
    float belongs) fails here rather than three layers downstream.
    """
    missing = set(schema) - set(df.columns)
    if missing:
        raise ValueError(f"provider frame is missing required column(s): {sorted(missing)}")
    return df.select([pl.col(name).cast(dtype) for name, dtype in schema.items()])


class ProviderError(RuntimeError):
    """A provider failed in a way the caller should surface, not swallow."""


@runtime_checkable
class Provider(Protocol):
    """What any data vendor must supply for the scanner to run."""

    name: str

    def universe(self) -> pl.DataFrame:
        """Tradeable symbols with venue and instrument-type flags. UNIVERSE_SCHEMA."""
        ...

    def bars(self, symbols: list[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        """Split-adjusted daily OHLCV over [start, end]. BARS_SCHEMA."""
        ...

    def actions(self, symbols: list[str], start: dt.date, end: dt.date) -> pl.DataFrame:
        """Splits and dividends. ACTIONS_SCHEMA. Used to explain price jumps."""
        ...

    def earnings(self, start: dt.date, end: dt.date) -> pl.DataFrame:
        """Earnings dates over [start, end]. EARNINGS_SCHEMA."""
        ...
