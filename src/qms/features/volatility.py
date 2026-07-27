"""ADR% and ATR. Both [DOC]. Spec §3.1 and §3.2.

These two are not interchangeable and the difference is the reason both exist:

* **ADR% excludes gaps by construction** — it only ever looks inside a session's own
  high/low. It is the "how much does this thing move on a normal day" measure, and it is
  what makes a $20 biotech and a $400 software name comparable in one ranking.
* **ATR includes gaps** — true range takes the previous close into account. It is the
  risk measure, which is why the stop and extension arithmetic in §5 uses ATR and not ADR.

Every rule that names one of them means that one specifically.
"""

from __future__ import annotations

import polars as pl

from qms.config import ScanConfig
from qms.features.registry import feature


def _register_adr(period: int) -> None:
    @feature(
        f"adr_pct_{period}",
        provenance="DOC",
        warmup=period,
        description=f"Average daily range %, {period} sessions (mean of H/L ratios)",
    )
    def _adr(cfg: ScanConfig, _period: int = period) -> pl.Expr:
        """100 * (mean(high/low) - 1) over the window.

        NOT mean(high)/mean(low) - 1. The ThinkOrSwim script circulating in the source
        doc computes the latter, which is a different and price-level-biased quantity:
        the ratio of means weights high-priced sessions more heavily, so it drifts with
        the price level over the window instead of measuring typical daily range.
        """
        return (
            pl.when(pl.col("low") > 0)
            .then(pl.col("high") / pl.col("low"))
            .otherwise(None)
            .rolling_mean(window_size=_period, min_samples=_period)
            .sub(1.0)
            .mul(100.0)
        )


# Spec §3.1: implement the 14-period variant too; the doc notes the lookback isn't
# critical. Both are registered so the causality suite covers both.
for _period in (14, 20):
    _register_adr(_period)


def true_range() -> pl.Expr:
    """max(H-L, |H-C_prev|, |L-C_prev|).

    On the first bar there is no previous close, so true range is just H-L rather than
    null — otherwise the recursive ATR below would never start.
    """
    prev_close = pl.col("close").shift(1)
    high_low = pl.col("high") - pl.col("low")
    return (
        pl.when(prev_close.is_null())
        .then(high_low)
        .otherwise(
            pl.max_horizontal(
                high_low,
                (pl.col("high") - prev_close).abs(),
                (pl.col("low") - prev_close).abs(),
            )
        )
    )


@feature(
    "true_range",
    provenance="DOC",
    warmup=1,
    description="True range, includes gaps",
)
def _true_range(_cfg: ScanConfig) -> pl.Expr:
    return true_range()


def adr_pct(period: int) -> pl.Expr:
    """Standalone ADR% expression, exposed so tests can pin an arbitrary window."""
    return (
        pl.when(pl.col("low") > 0)
        .then(pl.col("high") / pl.col("low"))
        .otherwise(None)
        .rolling_mean(window_size=period, min_samples=period)
        .sub(1.0)
        .mul(100.0)
    )


def wilder_atr(period: int) -> pl.Expr:
    """Wilder-smoothed ATR: an EWM with alpha = 1/period and adjust=False.

    Causality note: this is recursive with unbounded memory, so its value at bar `i`
    depends on every bar from the start of the series. That is fine and is *not* a
    lookahead — it only ever reads backwards. It does mean the causality suite must
    truncate the **tail** of a series and never the head: a head-truncated series has a
    legitimately different warm-up and would disagree for correct code.
    """
    return (
        true_range()
        .ewm_mean(alpha=1.0 / period, adjust=False, min_samples=period)
    )


@feature(
    "atr_14",
    provenance="DOC",
    warmup=lambda cfg: cfg.features.atr.period,
    description="Average true range, Wilder smoothing (includes gaps)",
)
def _atr_primary(cfg: ScanConfig) -> pl.Expr:
    return wilder_atr(cfg.features.atr.period)


# The [EXT] contraction metric in consolidation.py is a ratio of a fast to a slow ATR.
# Registered here beside the primary ATR so there is exactly one Wilder implementation.
@feature(
    "atr_fast",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.atr_fast,
    description="Short-window ATR, denominator-free input to the contraction ratio",
)
def _atr_fast(cfg: ScanConfig) -> pl.Expr:
    return wilder_atr(cfg.features.consolidation.atr_fast)


@feature(
    "atr_slow",
    provenance="EXT",
    warmup=lambda cfg: cfg.features.consolidation.atr_slow,
    description="Long-window ATR, input to the contraction ratio",
)
def _atr_slow(cfg: ScanConfig) -> pl.Expr:
    return wilder_atr(cfg.features.consolidation.atr_slow)
