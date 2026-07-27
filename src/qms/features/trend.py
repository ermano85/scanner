"""Moving averages and distance-to-MA. Spec §3.4 and §3.5.

The SMAs are [DOC] — 10/20/50 are load-bearing (entries are taken at them, and the trail
uses the 10-day) and the 200 is optional context. Note the trailing stop is a **simple**
10-day average, not an EMA.

`dist_in_adr` is the useful one. Expressing "how far is this from its 20-day average" in
ADR units rather than percent is what makes a $20 biotech and a $400 software name
comparable in a single ranking: 3% is a routine day for one and a large move for the
other. The normalization is [EXT]; the underlying idea that entries happen at the
10/20/50 is [DOC].
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.features.registry import feature


def _register_sma(period: int) -> None:
    @feature(
        f"sma_{period}",
        provenance="DOC",
        warmup=period,
        description=f"Simple moving average of close, {period} sessions",
    )
    def _sma(_cfg: ScanConfig, _p: int = period) -> pl.Expr:
        return pl.col("close").rolling_mean(window_size=_p, min_samples=_p)

    @feature(
        f"dist_to_sma_{period}_pct",
        provenance="DOC",
        warmup=period,
        deps=(f"sma_{period}",),
        description=f"(close - sma_{period}) / close, percent",
    )
    def _dist_pct(_cfg: ScanConfig, _p: int = period) -> pl.Expr:
        return (
            pl.when(pl.col("close") > 0)
            .then((pl.col("close") - pl.col(f"sma_{_p}")) / pl.col("close"))
            .otherwise(None)
            .mul(100.0)
        )

    @feature(
        f"dist_to_sma_{period}_adr",
        provenance="DOC",
        warmup=lambda cfg, _p=period: max(_p, cfg.features.adr.primary),
        deps=(f"dist_to_sma_{period}_pct",),
        description=f"Distance to the {period} SMA measured in average-days",
    )
    def _dist_adr(cfg: ScanConfig, _p: int = period) -> pl.Expr:
        adr = pl.col(f"adr_pct_{cfg.features.adr.primary}")
        return (
            pl.when(adr > 0)
            .then(pl.col(f"dist_to_sma_{_p}_pct") / adr)
            .otherwise(None)
        )


# Registered for every SMA period the config asks for. The config validator already
# guarantees the rule layer only references periods that appear here.
for _period in (10, 20, 50, 200):
    _register_sma(_period)


@feature(
    "ma_stack_ok",
    provenance="DOC",
    # The slow SMA must fill (slow bars, first value at index slow-1) *before* the k-of-m
    # window can start accumulating, so the total is slow + m - 1, not slow + m. The
    # off-by-one matters: it is the difference between the gate being computable on the
    # 29th bar of a new listing and silently waiting for the 30th.
    warmup=lambda cfg: cfg.scan_a.ma_stack.slow + cfg.scan_a.ma_stack.m - 1,
    deps=("sma_10", "sma_20"),
    description="fast SMA above slow SMA on at least k of the last m sessions",
)
def _ma_stack_ok(cfg: ScanConfig) -> pl.Expr:
    """[DOC] with tolerance, spec §4.1.

    The doc calls "10-day above 20-day" a general guideline and says brief undercuts are
    acceptable. Implemented as k-of-m rather than a same-day boolean, because a strict
    same-day test drops exactly the pullback setups the scan exists to find — a name that
    dipped under its 20 for two sessions and reclaimed it is the textbook entry, not a
    disqualification.
    """
    stack = cfg.scan_a.ma_stack
    above = (pl.col(f"sma_{stack.fast}") > pl.col(f"sma_{stack.slow}")).cast(pl.Int32)
    return above.rolling_sum(window_size=stack.m, min_samples=stack.m) >= stack.k


@feature(
    "above_sma_50",
    provenance="DOC",
    warmup=50,
    deps=("sma_50",),
    description="Close above the 50-day SMA",
)
def _above_50(_cfg: ScanConfig) -> pl.Expr:
    return pl.col("close") > pl.col("sma_50")
