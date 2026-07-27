"""Console rendering of a scan result.

Lives in the output layer rather than the rules layer: presentation constants have no
business in rule code, and keeping them apart is what lets `tests/test_no_literals.py`
police the rules package without a pile of exemptions.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.rules.scan_a import ScanResult

TABLE_WIDTH_CHARS = 200
FLOAT_PRECISION = 2

SUMMARY_COLUMNS = (
    "rank",
    "symbol",
    "close",
    "atr_14",
    "gain_1m",
    "gain_3m",
    "gain_6m",
    "momentum_pctile",
    "triggers",
    "days_to_earnings",
    "shares",
    "binding_cap",
    "score",
)


def echo_scan_result(result: ScanResult, cfg: ScanConfig) -> None:
    print(f"[scan] as_of {result.as_of_date} (the watchlist is FOR this session)")
    print(f"[scan] newest bar {result.data_date}")
    if result.is_stale:
        print(
            f"[scan] WARNING: data is {result.staleness_sessions} session(s) stale — the "
            "vendor is missing bars. See docs/DATA.md."
        )
    if result.dropped_stale_symbols:
        print(
            f"[scan] dropped {result.dropped_stale_symbols} symbol(s) whose newest bar is "
            "too far behind the reference session"
        )
    print(f"[scan] universe {result.universe_size} -> {result.after_liquidity} after liquidity")
    print(f"[scan] rejected by gate: {result.rejections}")
    print(f"[scan] {result.candidates.height} candidates")

    if result.candidates.is_empty():
        print("[scan] nothing survived — check the gate rejections above")
        return

    adr_column = f"adr_pct_{cfg.features.adr.primary}"
    wanted = [*SUMMARY_COLUMNS[:3], adr_column, *SUMMARY_COLUMNS[3:]]
    available = [c for c in wanted if c in result.candidates.columns]

    with pl.Config(
        tbl_rows=cfg.scan_a.ranking.max_candidates,
        tbl_width_chars=TABLE_WIDTH_CHARS,
        float_precision=FLOAT_PRECISION,
    ):
        print(result.candidates.select(available))
