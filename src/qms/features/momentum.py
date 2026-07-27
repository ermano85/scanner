"""Trailing-day gainers. [DOC], spec §3.3.

Exact lookbacks from the doc's TC2000 formulas, in **trading days** rather than calendar
days — which is why they are 21/63/126 and not 30/90/180.

The doc gives no threshold for these: he scans for the biggest gainers and works down the
list. So these are raw returns here, and the *ranking* layer converts them to
cross-sectional percentiles fresh on each scan date. A fixed "+50%" bar would produce
400 candidates in a bull market and zero in a chop.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.features.registry import feature


def _register_gain(name: str) -> None:
    @feature(
        name,
        provenance="DOC",
        warmup=lambda cfg, _n=name: cfg.features.momentum.as_dict()[_n],
        description=f"Trailing return over the {name} lookback, percent",
    )
    def _gain(cfg: ScanConfig, _n: str = name) -> pl.Expr:
        lookback = cfg.features.momentum.as_dict()[_n]
        prior = pl.col("close").shift(lookback)
        return (
            pl.when(prior > 0)
            .then(pl.col("close") / prior - 1.0)
            .otherwise(None)
            .mul(100.0)
        )


for _name in ("gain_1m", "gain_3m", "gain_6m"):
    _register_gain(_name)
