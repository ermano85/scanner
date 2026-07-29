"""TradingView watchlist export.

Format per TradingView's own documentation: a `.txt` file of comma-separated,
exchange-prefixed symbols, imported via *Upload list…* on the watchlist menu. Their
1,000-symbol cap is irrelevant here — the scan never emits more than a few dozen.

A `###` section header is written first so the import lands in a labelled group. That
syntax is community-derived and is **not** in TradingView's documentation, so the payload
below it is the plain documented form: if the header is ignored, the symbols still import.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

from qms import paths
from qms.ingest.base import UNIVERSE_SCHEMA
from qms.ingest.store import read_parquet_or_empty

# NASDAQ Trader venue codes -> TradingView exchange prefixes.
#
# P (NYSE Arca) and Z (Cboe BZX) list mostly ETFs, and TradingView files US ETFs under
# AMEX regardless of the primary venue, so both map there rather than to a literal
# translation of the listing exchange.
EXCHANGE_PREFIX = {
    "Q": "NASDAQ",
    "N": "NYSE",
    "A": "AMEX",
    "P": "AMEX",
    "Z": "AMEX",
}
DEFAULT_PREFIX = "NASDAQ"

SECTION_PREFIX = "###"


def _exchange_map() -> dict[str, str]:
    universe = read_parquet_or_empty(paths.UNIVERSE_FILE, UNIVERSE_SCHEMA)
    if universe.is_empty():
        return {}
    return dict(zip(universe["symbol"].to_list(), universe["exchange"].to_list(), strict=True))


def to_tradingview_symbol(symbol: str, exchange: str | None) -> str:
    """`AGM.A` on NYSE -> `NYSE:AGM.A`.

    TradingView uses a dot for share classes, same as the NASDAQ Trader directory, so the
    symbol passes through unchanged — unlike the Yahoo mapping, which needs a dash.
    """
    return f"{EXCHANGE_PREFIX.get(exchange or '', DEFAULT_PREFIX)}:{symbol}"


def render_watchlist(
    symbols: list[str],
    as_of: dt.date,
    exchanges: dict[str, str] | None = None,
) -> str:
    exchanges = exchanges if exchanges is not None else _exchange_map()
    mapped = [to_tradingview_symbol(s, exchanges.get(s)) for s in symbols]
    return f"{SECTION_PREFIX}Qullamaggie {as_of}\n{','.join(mapped)}\n"


def write_watchlist(
    candidates: pl.DataFrame,
    as_of: dt.date,
    out_dir: Path,
    exchanges: dict[str, str] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = candidates["symbol"].to_list() if not candidates.is_empty() else []
    path = out_dir / "tradingview.txt"
    path.write_text(render_watchlist(symbols, as_of, exchanges), encoding="utf-8")
    print(f"[tradingview] {len(symbols)} symbol(s) -> {path}")
    return path
