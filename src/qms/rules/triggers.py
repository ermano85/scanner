"""Trigger tags — why this name is on today's list. Spec §4.2.

Tags explain a candidate; they do not filter it. A survivor with no tag still appears, it
just ranks last, because "passes every gate but is nowhere near an entry" is information
worth seeing rather than hiding.

`AT_*MA` is [DOC] — the source takes entries at the 10/20/50. `AT_PIVOT` is a reasonable
reading of breakout entry, but the specific proximity threshold is [EXT]. Both live here
rather than in `gates.py` precisely because neither is allowed to remove a row.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig

TAG_SEPARATOR = " "


def add_trigger_tags(frame: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Attach one boolean column per trigger, plus a joined `triggers` string."""
    triggers = cfg.scan_a.triggers
    tag_columns: list[str] = []

    for period in triggers.ma_periods:
        column = f"trigger_at_{period}ma"
        frame = frame.with_columns(
            (pl.col(f"dist_to_sma_{period}_adr").abs() <= triggers.near_ma_adr)
            .fill_null(False)
            .alias(column)
        )
        tag_columns.append(column)

    # Near the high of any consolidation bucket counts: a 5-day flag and a 40-day base are
    # different setups and the scan should surface both.
    pivot_conditions = []
    for bucket in triggers.pivot_buckets:
        pivot_conditions.append(
            (
                (pl.col(f"pivot_high_{bucket}") - pl.col("close"))
                / pl.col("close")
                * pl.lit(100.0)
                <= triggers.near_pivot_pct
            ).fill_null(False)
        )
    combined = pivot_conditions[0]
    for condition in pivot_conditions[1:]:
        combined = combined | condition
    frame = frame.with_columns(combined.alias("trigger_at_pivot"))
    tag_columns.append("trigger_at_pivot")

    label = {f"trigger_at_{p}ma": f"AT_{p}MA" for p in triggers.ma_periods}
    label["trigger_at_pivot"] = "AT_PIVOT"

    tag_expr = pl.concat_str(
        [
            pl.when(pl.col(column)).then(pl.lit(label[column])).otherwise(pl.lit(""))
            for column in tag_columns
        ],
        separator=TAG_SEPARATOR,
    )

    return frame.with_columns(
        tag_expr.str.strip_chars().str.replace_all(r"\s+", TAG_SEPARATOR).alias("triggers"),
        pl.sum_horizontal([pl.col(c).cast(pl.Int32) for c in tag_columns]).alias("trigger_count"),
    )


def trigger_columns(cfg: ScanConfig) -> list[str]:
    return [f"trigger_at_{p}ma" for p in cfg.scan_a.triggers.ma_periods] + ["trigger_at_pivot"]
