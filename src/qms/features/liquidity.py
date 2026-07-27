"""Liquidity. [DOC], spec §3.6.

Both measures are needed for different jobs: the share-count cap in position sizing is a
fraction of *share* volume, while the tradeability gate and the dollar-volume cap are in
*dollars*.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.features.registry import feature


@feature(
    "avg_vol_20",
    provenance="DOC",
    warmup=lambda cfg: cfg.features.liquidity.avg_vol_window,
    description="Average share volume over the liquidity window",
)
def _avg_vol(cfg: ScanConfig) -> pl.Expr:
    window = cfg.features.liquidity.avg_vol_window
    return pl.col("volume").rolling_mean(window_size=window, min_samples=window)


@feature(
    "avg_dollar_vol_20",
    provenance="DOC",
    warmup=lambda cfg: cfg.features.liquidity.avg_dollar_vol_window,
    description="Average close*volume over the liquidity window",
)
def _avg_dollar_vol(cfg: ScanConfig) -> pl.Expr:
    window = cfg.features.liquidity.avg_dollar_vol_window
    return (pl.col("close") * pl.col("volume")).rolling_mean(
        window_size=window, min_samples=window
    )


@feature(
    "avg_vol_fast",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.vol_fast,
    description="Short-window average volume, numerator of the volume dry-up ratio",
)
def _avg_vol_fast(cfg: ScanConfig) -> pl.Expr:
    window = cfg.features.consolidation.vol_fast
    return pl.col("volume").rolling_mean(window_size=window, min_samples=window)


@feature(
    "avg_vol_slow",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.vol_slow,
    description="Long-window average volume, denominator of the volume dry-up ratio",
)
def _avg_vol_slow(cfg: ScanConfig) -> pl.Expr:
    window = cfg.features.consolidation.vol_slow
    return pl.col("volume").rolling_mean(window_size=window, min_samples=window)
