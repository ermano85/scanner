"""Universe from the NASDAQ Trader symbol directory.

Official, free, no key, updated each trading day. Two pipe-delimited files with
*different column names for the same thing*, which is the only reason this module is
longer than ten lines.

Probed 2026-07-27: 5,567 Nasdaq rows + 7,495 other rows, 13,029 non-test symbols,
of which 5,559 are ETFs.
"""

from __future__ import annotations

import re

import polars as pl

from qms.config import UniverseConfig
from qms.ingest.base import UNIVERSE_SCHEMA, ProviderError, conform
from qms.ingest.http import HttpClient

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# Both files end with a "File Creation Time" trailer that is not a data row.
_TRAILER_PREFIX = "File Creation Time"
_NASDAQ_EXCHANGE_CODE = "Q"


def _parse_pipe_file(text: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(_TRAILER_PREFIX)]
    if not lines:
        raise ProviderError("symbol directory file was empty")
    header = [h.strip() for h in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        cells = line.split("|")
        if len(cells) != len(header):
            continue
        rows.append({key: value.strip() for key, value in zip(header, cells, strict=True)})
    return rows


def fetch_universe(client: HttpClient) -> pl.DataFrame:
    """Raw universe, before any config filtering. UNIVERSE_SCHEMA."""
    nasdaq_rows = _parse_pipe_file(client.get(NASDAQ_LISTED_URL).text)
    other_rows = _parse_pipe_file(client.get(OTHER_LISTED_URL).text)

    records = []
    for row in nasdaq_rows:
        records.append(
            {
                "symbol": row["Symbol"],
                "name": row["Security Name"],
                "exchange": _NASDAQ_EXCHANGE_CODE,
                "is_etf": row.get("ETF") == "Y",
                "test_issue": row.get("Test Issue") == "Y",
                "financial_status": row.get("Financial Status") or None,
            }
        )
    for row in other_rows:
        # otherlisted uses "ACT Symbol" for the CQS symbol and carries a separate
        # "NASDAQ Symbol" column; the ACT symbol is the one quoted elsewhere.
        records.append(
            {
                "symbol": row["ACT Symbol"],
                "name": row["Security Name"],
                "exchange": row.get("Exchange") or None,
                "is_etf": row.get("ETF") == "Y",
                "test_issue": row.get("Test Issue") == "Y",
                "financial_status": None,
            }
        )

    if not records:
        raise ProviderError("symbol directory produced no rows")

    frame = pl.DataFrame(records, schema_overrides={"financial_status": pl.Utf8})
    # A symbol can appear on both files (dually-quoted); keep the first occurrence.
    frame = frame.unique(subset=["symbol"], keep="first", maintain_order=True)
    return conform(frame, UNIVERSE_SCHEMA)


def apply_universe_filters(frame: pl.DataFrame, cfg: UniverseConfig) -> pl.DataFrame:
    """Reduce the raw directory to the symbols this scanner will ever consider."""
    out = frame

    if cfg.exclude_test_issues:
        out = out.filter(~pl.col("test_issue"))

    enabled = cfg.enabled_exchanges()
    out = out.filter(pl.col("exchange").is_in(list(enabled)))

    if not cfg.include_etfs:
        out = out.filter(~pl.col("is_etf"))

    if cfg.exclude_deficient:
        out = out.filter(
            pl.col("financial_status").is_null() | (pl.col("financial_status") == "N")
        )

    if cfg.exclude_symbols:
        out = out.filter(~pl.col("symbol").is_in(cfg.exclude_symbols))

    # Warrants, units and rights carry a dotted suffix (AAC.U, ACHR.W, AIIA.R). Verified
    # 2026-07-27: 168 dotted symbols across the two files and zero dashed ones, so the
    # dot is the only separator to match. Deliberately NOT applying a bare 5th-letter
    # heuristic — that eats real tickers.
    if cfg.exclude_suffixes:
        suffix_pattern = "|".join(re.escape(s) for s in cfg.exclude_suffixes)
        out = out.filter(~pl.col("symbol").str.contains(rf"\.({suffix_pattern})$"))

    for pattern in cfg.exclude_name_patterns:
        out = out.filter(~pl.col("name").str.contains(f"(?i){pattern}"))

    return out


def to_vendor_symbol(symbol: str) -> str:
    """Directory symbols use '.' for share classes; Yahoo uses '-' (BRK.A -> BRK-A).

    One-way by design. Directory symbols never contain '-', but Yahoo's namespace does
    for other reasons, so inverting this by string replacement would be lossy. Callers
    carry the canonical symbol alongside and label rows with that.
    """
    return symbol.replace(".", "-")
