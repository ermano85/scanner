"""Consolidation quality. **Every feature in this module is [EXT].** Spec §3.7.

The source material specifies none of this. These metrics are the implementer's guess at
"what does a tight base look like numerically", they are unvalidated, and they are to be
treated with suspicion.

**They may contribute to the ranking score. They may never act as a filter in v1.**

That rule is enforced structurally rather than by convention: everything here registers
with `provenance="EXT"`, `rules/gates.py` is built from `doc_features()` only, and
`tests/test_ext_quarantine.py` fails if any EXT feature name appears in the gate module.

Computed at three duration buckets because a high-tight flag and a multi-month base are
different animals and one parameter set will not catch both.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.features.registry import feature


@feature(
    "bar_index",
    provenance="EXT",
    warmup=0,
    description="Per-symbol sequential bar number; plumbing for the rolling regression",
)
def _bar_index(_cfg: ScanConfig) -> pl.Expr:
    return pl.int_range(pl.len(), dtype=pl.Int64)


def _register_bucket(bucket: int) -> None:
    @feature(
        f"pivot_high_{bucket}",
        provenance="EXT",
        warmup=bucket,
        description=f"Highest high over the trailing {bucket} sessions",
    )
    def _pivot_high(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        return pl.col("high").rolling_max(window_size=_n, min_samples=_n)

    @feature(
        f"pivot_low_{bucket}",
        provenance="EXT",
        warmup=bucket,
        description=f"Lowest low over the trailing {bucket} sessions",
    )
    def _pivot_low(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        return pl.col("low").rolling_min(window_size=_n, min_samples=_n)

    @feature(
        f"tightness_{bucket}",
        provenance="EXT",
        warmup=bucket,
        deps=(f"pivot_high_{bucket}", f"pivot_low_{bucket}"),
        description=f"Range of the trailing {bucket} sessions as a % of its low",
    )
    def _tightness(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        low = pl.col(f"pivot_low_{_n}")
        return (
            pl.when(low > 0)
            .then((pl.col(f"pivot_high_{_n}") - low) / low)
            .otherwise(None)
            .mul(100.0)
        )

    @feature(
        f"tightness_adr_{bucket}",
        provenance="EXT",
        warmup=lambda cfg, _n=bucket: max(_n, cfg.features.adr.primary),
        deps=(f"tightness_{bucket}",),
        description=f"{bucket}-session range measured in average-days rather than percent",
    )
    def _tightness_adr(cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        adr = pl.col(f"adr_pct_{cfg.features.adr.primary}")
        return pl.when(adr > 0).then(pl.col(f"tightness_{_n}") / adr).otherwise(None)

    @feature(
        f"depth_from_high_{bucket}",
        provenance="EXT",
        warmup=bucket,
        deps=(f"pivot_high_{bucket}",),
        description=f"How far below the {bucket}-session high the close sits, percent",
    )
    def _depth(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        high = pl.col(f"pivot_high_{_n}")
        return (
            pl.when(high > 0)
            .then((high - pl.col("close")) / high)
            .otherwise(None)
            .mul(100.0)
        )

    @feature(
        f"low_slope_{bucket}",
        provenance="EXT",
        warmup=bucket,
        deps=("bar_index",),
        description=f"OLS slope of the low over {bucket} sessions; >= 0 suggests higher lows",
    )
    def _low_slope(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        """Closed-form rolling OLS slope.

        slope = (n*Sxy - Sx*Sy) / (n*Sxx - Sx^2)

        Using the global bar index as x rather than a window-local 0..n-1 ramp. A
        regression slope is invariant to translating x, so the result is identical, and
        it keeps every term an ordinary unweighted `rolling_sum` — no weighted windows
        and no `rolling_map`, both of which are easy to get subtly wrong and slow over
        13,000 symbols.
        """
        x = pl.col("bar_index").cast(pl.Float64)
        y = pl.col("low")
        window = {"window_size": _n, "min_samples": _n}
        sx = x.rolling_sum(**window)
        sy = y.rolling_sum(**window)
        sxy = (x * y).rolling_sum(**window)
        sxx = (x * x).rolling_sum(**window)
        denominator = _n * sxx - sx * sx
        return (
            pl.when(denominator != 0)
            .then((_n * sxy - sx * sy) / denominator)
            .otherwise(None)
        )


    @feature(
        f"low_slope_pct_{bucket}",
        provenance="EXT",
        warmup=bucket,
        deps=(f"low_slope_{bucket}",),
        description=f"{bucket}-session low slope as a % of price, per session",
    )
    def _low_slope_pct(_cfg: ScanConfig, _n: int = bucket) -> pl.Expr:
        """Scale-free version of the slope, for cross-sectional ranking.

        The raw OLS slope is in price units per session, so a $400 name mechanically
        outranks a $20 name with an identical-looking base. Dividing by price makes the
        two comparable, which is the whole point of ranking them against each other.
        """
        return (
            pl.when(pl.col("close") > 0)
            .then(pl.col(f"low_slope_{_n}") / pl.col("close"))
            .otherwise(None)
            .mul(100.0)
        )


for _bucket in (5, 15, 40):
    _register_bucket(_bucket)


@feature(
    "contraction",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.atr_slow,
    deps=("atr_fast", "atr_slow"),
    description="Fast ATR over slow ATR; below ~0.8 suggests volatility compression",
)
def _contraction(_cfg: ScanConfig) -> pl.Expr:
    slow = pl.col("atr_slow")
    return pl.when(slow > 0).then(pl.col("atr_fast") / slow).otherwise(None)


@feature(
    "vol_dryup",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.vol_slow,
    deps=("avg_vol_fast", "avg_vol_slow"),
    description="Fast over slow average volume; below ~0.8 suggests supply exhaustion",
)
def _vol_dryup(_cfg: ScanConfig) -> pl.Expr:
    slow = pl.col("avg_vol_slow")
    return pl.when(slow > 0).then(pl.col("avg_vol_fast") / slow).otherwise(None)
