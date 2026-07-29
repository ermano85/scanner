"""Scan A hard gates. Every gate here is [DOC]. Spec §4.1.

**No [EXT] feature may be referenced in this module.** Unvalidated extrapolations may
contribute to ranking and may never filter (spec §3.7). `tests/test_ext_quarantine.py`
enforces that by AST-scanning this file, including string literals, so building a column
name dynamically will not sneak past it.

**No numeric literals.** Every threshold arrives from `config/scan.yaml`.
`tests/test_no_literals.py` enforces that the same way.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms.calendar import trading_days_between
from qms.config import ScanConfig

# Reason codes attached to each rejected symbol, so a disappointing scan can be explained
# rather than just being short.
GATE_PRICE = "price"
GATE_DOLLAR_VOL = "dollar_vol"
GATE_ADR = "adr"
GATE_MA_STACK = "ma_stack"
GATE_ABOVE_50 = "above_50"
GATE_EARNINGS = "earnings"
GATE_MOMENTUM = "momentum"
GATE_SECTOR = "sector"

# Tag for a symbol the earnings feed simply has no row for.
EARNINGS_UNKNOWN = "EARNINGS_UNKNOWN"
# Tag for a symbol with no SEC industry classification — ETFs, most foreign issuers.
SIC_UNKNOWN = "SIC_UNKNOWN"


def apply_liquidity_gates(latest: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Price, dollar volume and ADR — the gates that define a tradeable universe.

    Applied *before* momentum percentiles are computed. Ranking against the full 13,000
    would dilute the percentile with names that can never pass a liquidity screen anyway,
    which would make "top 10%" mean something different every night depending on how much
    illiquid junk happened to be listed.
    """
    gates = cfg.scan_a.gates
    adr_column = f"adr_pct_{cfg.features.adr.primary}"
    return latest.with_columns(
        (pl.col("close") >= gates.min_price).alias(f"pass_{GATE_PRICE}"),
        (pl.col("avg_dollar_vol_20") >= gates.min_dollar_vol).alias(f"pass_{GATE_DOLLAR_VOL}"),
        (pl.col(adr_column) >= gates.min_adr).alias(f"pass_{GATE_ADR}"),
    )


def apply_trend_gates(latest: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """MA stack and above-the-50.

    `ma_stack_ok` is already the k-of-m tolerance from spec §4.1 — computed in the feature
    layer rather than here so the causality suite covers it.
    """
    return latest.with_columns(
        pl.col("ma_stack_ok").fill_null(False).alias(f"pass_{GATE_MA_STACK}"),
        (
            pl.col("above_sma_50").fill_null(False)
            if cfg.scan_a.gates.above_50
            else pl.lit(True)
        ).alias(f"pass_{GATE_ABOVE_50}"),
    )


def attach_earnings(
    latest: pl.DataFrame,
    earnings: pl.DataFrame,
    cfg: ScanConfig,
    as_of_date: dt.date,
) -> pl.DataFrame:
    """Attach the next earnings date and apply the blackout.

    Two decisions worth stating plainly, because both are judgment calls the spec leaves
    open and both change what comes out of the scan:

    **Missing earnings dates pass.** A symbol the calendar has no row for is tagged
    `EARNINGS_UNKNOWN` and survives. Hard-failing on absent data would silently delete a
    large slice of the universe whenever the free feed has a bad night — a failure mode
    invisible in the output, which is the worst kind. The report shows the tag so the
    human can see exactly which names are unverified.

    **`bmo` gets one extra day of blackout.** A company reporting before the open on day E
    gaps before you can act on E, so the last genuinely unexposed session is a day earlier
    than for a company reporting after the close. `unknown` — which is roughly half of all
    rows — is treated as `amc`, the less restrictive reading, rather than assuming the
    worst for half the universe. The report prints the raw timing so the judgment stays
    with the reader.
    """
    blackout_days = cfg.scan_a.gates.earnings_blackout_days

    if earnings.is_empty():
        return latest.with_columns(
            pl.lit(None, dtype=pl.Date).alias("next_earnings_date"),
            pl.lit(None, dtype=pl.Utf8).alias("earnings_when"),
            pl.lit(None, dtype=pl.Int32).alias("days_to_earnings"),
            pl.lit(True).alias(f"pass_{GATE_EARNINGS}"),
            pl.lit(True).alias("earnings_unknown"),
        )

    # Only forward-looking releases matter; a report that already happened is not a risk.
    upcoming = (
        earnings.filter(pl.col("earnings_date") >= as_of_date)
        .sort(["symbol", "earnings_date"])
        .group_by("symbol", maintain_order=True)
        .first()
        .select(
            "symbol",
            pl.col("earnings_date").alias("next_earnings_date"),
            pl.col("when").alias("earnings_when"),
        )
    )

    joined = latest.join(upcoming, on="symbol", how="left")

    # Trading-day distance, computed once per distinct date rather than per row.
    distinct_dates = [
        d for d in joined["next_earnings_date"].unique().to_list() if d is not None
    ]
    distance = {d: trading_days_between(as_of_date, d) for d in distinct_dates}

    return joined.with_columns(
        pl.col("next_earnings_date")
        .replace_strict(distance, default=None, return_dtype=pl.Int32)
        .alias("days_to_earnings")
    ).with_columns(
        pl.col("next_earnings_date").is_null().alias("earnings_unknown"),
    ).with_columns(
        pl.when(pl.col("next_earnings_date").is_null())
        .then(True)
        .otherwise(
            pl.when(pl.col("earnings_when") == pl.lit("bmo"))
            .then(pl.col("days_to_earnings") - pl.lit(1) > blackout_days)
            .otherwise(pl.col("days_to_earnings") > blackout_days)
        )
        .alias(f"pass_{GATE_EARNINGS}")
    )


def attach_sector(
    latest: pl.DataFrame,
    sic: pl.DataFrame,
    excluded_sic: list[int],
) -> pl.DataFrame:
    """Attach SEC industry classification and exclude unwanted sectors.

    **This is an operator preference, not a rule from the source material.** It is not
    tagged `[DOC]`: nothing in the Laws of Swing doc says avoid pharma. It is here because
    the operator judged clinical-stage binaries unpredictable, and it belongs in config so
    that judgment stays visible and reversible.

    **Symbols with no classification pass**, tagged `SIC_UNKNOWN` — the same reasoning as
    a missing earnings date. ETFs have no meaningful SIC and most foreign issuers file no
    US registration, so failing them closed would silently delete a large, arbitrary slice
    of the universe.
    """
    if sic.is_empty():
        return latest.with_columns(
            pl.lit(None, dtype=pl.Int32).alias("sic"),
            pl.lit(None, dtype=pl.Utf8).alias("sic_description"),
            pl.lit(True).alias("sic_unknown"),
            pl.lit(True).alias(f"pass_{GATE_SECTOR}"),
        )

    joined = latest.join(
        sic.select("symbol", "sic", "sic_description"), on="symbol", how="left"
    )
    return joined.with_columns(
        pl.col("sic").is_null().alias("sic_unknown"),
        (pl.col("sic").is_null() | ~pl.col("sic").is_in(excluded_sic)).alias(
            f"pass_{GATE_SECTOR}"
        ),
    )


def apply_momentum_gate(latest: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Top-decile momentum on the best of the three lookbacks.

    `momentum_pctile` is produced by `rank.add_momentum_percentiles`, which takes the max
    of the three *percentile ranks* rather than the percentile of the max raw gain — a
    six-month move and a one-month move are not comparable in raw percent.
    """
    return latest.with_columns(
        (pl.col("momentum_pctile") >= cfg.scan_a.gates.momentum_pctile)
        .fill_null(False)
        .alias(f"pass_{GATE_MOMENTUM}")
    )


def gate_columns(frame: pl.DataFrame) -> list[str]:
    return [column for column in frame.columns if column.startswith("pass_")]


def survivors(frame: pl.DataFrame) -> pl.DataFrame:
    """Rows passing every gate present on the frame."""
    columns = gate_columns(frame)
    if not columns:
        return frame
    keep = pl.lit(True)
    for column in columns:
        keep = keep & pl.col(column).fill_null(False)
    return frame.filter(keep)


def rejection_summary(frame: pl.DataFrame) -> dict[str, int]:
    """How many symbols each gate removed, for explaining a thin scan."""
    return {
        column.removeprefix("pass_"): int((~frame[column].fill_null(False)).sum())
        for column in gate_columns(frame)
    }
