"""Cross-sectional percentiles and the ranking score. Spec §3.3 and §4.3.

**Why percentiles rather than raw values.** The doc gives no momentum threshold — he scans
for the biggest gainers and works down the list. A fixed "+50%" bar produces 400
candidates in a bull market and zero in a chop, so the cut has to be relative to the day's
own cross-section.

**Why every score component is percentile-ranked.** The raw metrics are on wildly
different scales: `tightness_adr` runs 2-10, `contraction` sits near 1.0, `depth_from_high`
runs 0-30. Multiplying raw values by weights would make the score a near-pure function of
whichever metric happens to have the widest numeric range, and the configured weights
would be close to meaningless. Ranking each to [0, 1] first makes a weight mean what it
looks like it means.

Per spec §4.3 these weights are **not** to be tuned before several hundred output charts
have been reviewed. There is currently no way to evaluate them.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig

MOMENTUM_COLUMNS = ("gain_1m", "gain_3m", "gain_6m")


def _percentile(column: str) -> pl.Expr:
    """Fraction of the non-null cross-section at or below each value, in [0, 1]."""
    return pl.col(column).rank(method="average") / pl.col(column).is_not_null().sum()


def add_momentum_percentiles(frame: pl.DataFrame) -> pl.DataFrame:
    """Percentile-rank each momentum lookback, then take the best of the three.

    The max is taken over the *percentile ranks*, not over the raw gains: a +40% six-month
    move and a +40% one-month move are not the same achievement, and comparing them in raw
    percent would systematically favour the longest lookback.
    """
    frame = frame.with_columns(
        [_percentile(column).alias(f"{column}_pctile") for column in MOMENTUM_COLUMNS]
    )
    return frame.with_columns(
        pl.max_horizontal([pl.col(f"{c}_pctile") for c in MOMENTUM_COLUMNS]).alias(
            "momentum_pctile"
        )
    )


def add_score(frame: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Weighted ranking score over percentile-normalised components.

    Every `[EXT]` consolidation metric enters here and nowhere else — this module ranks,
    it never filters.
    """
    if frame.is_empty():
        return frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("score"))

    ranking = cfg.scan_a.ranking
    weights = ranking.weights
    bucket = ranking.score_bucket
    neutral = ranking.neutral_percentile
    trigger_total = len(cfg.scan_a.triggers.ma_periods) + 1  # the MAs plus AT_PIVOT

    # (raw column, weight). Sign lives entirely in the configured weight.
    components: list[tuple[pl.Expr, float]] = [
        (pl.col("momentum_pctile"), weights.momentum_pctile),
        (pl.col("trigger_count") / trigger_total, weights.trigger_bonus),
        (_percentile(f"tightness_adr_{bucket}"), weights.tightness_adr),
        (_percentile("contraction"), weights.contraction),
        (_percentile("vol_dryup"), weights.vol_dryup),
        (_percentile(f"low_slope_pct_{bucket}"), weights.low_slope),
        (_percentile(f"depth_from_high_{bucket}"), weights.depth_from_high),
    ]

    # A missing [EXT] metric must not silently zero a candidate's score, so it scores at
    # the neutral percentile instead. This matters most for freshly listed names, where
    # the long consolidation windows have not filled.
    score = None
    for expr, weight in components:
        term = expr.fill_null(neutral) * weight
        score = term if score is None else score + term

    return frame.with_columns(score.alias("score"))


def rank_candidates(frame: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Sort by score and cut to the configured maximum."""
    if frame.is_empty():
        return frame.with_columns(pl.lit(None, dtype=pl.UInt32).alias("rank"))
    ordered = frame.sort("score", descending=True, nulls_last=True)
    ordered = ordered.with_columns(
        (pl.int_range(pl.len(), dtype=pl.UInt32) + 1).alias("rank")
    )
    return ordered.head(cfg.scan_a.ranking.max_candidates)
