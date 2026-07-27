"""Position sizing. All [DOC]. Spec §5.

Arguably the most useful output of the whole tool, and the part a screener normally
ignores. Four independent caps are computed and the smallest wins — but the number that
actually matters is **which one bound**. A name where liquidity binds far below the
risk-based size is a name you cannot trade at your account size, and knowing that before
the open is worth a lot.

**These are pre-open estimates.** A nightly scan has no intraday low, so `low_of_day` is
the previous session's low and `entry` is its close. The real numbers must be recomputed
intraday against the session's actual low. The report labels them accordingly rather than
presenting them as final.

**One cap is inert at the shipped config values.** With `max_pct_of_avg_vol: 0.01` and
`dollar_vol_multiple: 200`, the dollar-volume cap works out to exactly *half* the
share-liquidity cap whenever the two inputs are internally consistent — the price cancels,
leaving 0.005 * avg_vol against 0.01 * avg_vol. So the 1%-of-average-volume rule can never
be the binding constraint. Both rules are [DOC] so neither is dropped, but do not expect
to ever see `liquidity` reported as the binding cap unless one of those two config values
changes. `tests/test_sizing.py` pins the relationship so the consequence stays visible.

No numeric literals: every constant comes from `config/scan.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from qms.config import ScanConfig

CAP_RISK = "risk"
CAP_LIQUIDITY = "liquidity"
CAP_DOLLAR_VOL = "dollar_vol"
CAP_CONCENTRATION = "concentration"

CAP_COLUMNS = {
    CAP_RISK: "shares_risk",
    CAP_LIQUIDITY: "shares_liquidity",
    CAP_DOLLAR_VOL: "shares_dollarvol",
    CAP_CONCENTRATION: "shares_concentration",
}


@dataclass(frozen=True)
class SizingInputs:
    """Single-candidate inputs, for the unit tests and the worked example in the doc."""

    entry_price: float
    low_of_day: float
    atr: float
    avg_vol_20: float
    avg_dollar_vol_20: float


def size_one(inputs: SizingInputs, cfg: ScanConfig) -> dict:
    """Scalar version of the vectorised calculation below. Same arithmetic, same caps."""
    sizing = cfg.sizing
    risk_dollars = sizing.account * sizing.risk_pct
    stop_price = inputs.low_of_day * sizing.stop_buffer
    risk_share = inputs.entry_price - stop_price

    shares = {
        CAP_RISK: (risk_dollars / risk_share) if risk_share > 0 else 0.0,
        CAP_LIQUIDITY: sizing.max_pct_of_avg_vol * inputs.avg_vol_20,
        CAP_DOLLAR_VOL: (inputs.avg_dollar_vol_20 / sizing.dollar_vol_multiple)
        / inputs.entry_price,
        CAP_CONCENTRATION: (sizing.max_account_concentration * sizing.account)
        / inputs.entry_price,
    }
    binding = min(shares, key=lambda key: shares[key])
    final = shares[binding]

    return {
        "stop_price": stop_price,
        "risk_per_share": risk_share,
        "risk_dollars": risk_dollars,
        "shares": final,
        "binding_cap": binding,
        "position_dollars": final * inputs.entry_price,
        "actual_risk_dollars": final * risk_share,
        "max_entry": inputs.low_of_day + inputs.atr * sizing.max_entry_atr_multiple,
        "preferred_entry_low": inputs.low_of_day + inputs.atr * sizing.preferred_entry_atr_low,
        "preferred_entry_high": inputs.low_of_day + inputs.atr * sizing.preferred_entry_atr_high,
        "stop_exceeds_atr": risk_share > inputs.atr,
        **{CAP_COLUMNS[cap]: value for cap, value in shares.items()},
    }


def add_sizing(frame: pl.DataFrame, cfg: ScanConfig) -> pl.DataFrame:
    """Vectorised sizing over a candidate frame.

    Expects `close`, `low`, `atr_14`, `avg_vol_20`, `avg_dollar_vol_20`.
    """
    if frame.is_empty():
        return frame

    sizing = cfg.sizing
    entry = pl.col("close")
    risk_dollars = sizing.account * sizing.risk_pct

    frame = frame.with_columns(
        (pl.col("low") * sizing.stop_buffer).alias("stop_price"),
    ).with_columns(
        (entry - pl.col("stop_price")).alias("risk_per_share"),
    )

    frame = frame.with_columns(
        pl.when(pl.col("risk_per_share") > 0)
        .then(risk_dollars / pl.col("risk_per_share"))
        .otherwise(None)
        .alias(CAP_COLUMNS[CAP_RISK]),
        (sizing.max_pct_of_avg_vol * pl.col("avg_vol_20")).alias(CAP_COLUMNS[CAP_LIQUIDITY]),
        ((pl.col("avg_dollar_vol_20") / sizing.dollar_vol_multiple) / entry).alias(
            CAP_COLUMNS[CAP_DOLLAR_VOL]
        ),
        ((sizing.max_account_concentration * sizing.account) / entry).alias(
            CAP_COLUMNS[CAP_CONCENTRATION]
        ),
    )

    cap_names = list(CAP_COLUMNS)
    cap_columns = [CAP_COLUMNS[name] for name in cap_names]

    frame = frame.with_columns(
        pl.min_horizontal([pl.col(c) for c in cap_columns]).alias("shares"),
    )

    # Which cap bound: the first whose value equals the minimum.
    binding = pl.lit(None, dtype=pl.Utf8)
    for name in reversed(cap_names):
        binding = (
            pl.when(pl.col(CAP_COLUMNS[name]) == pl.col("shares"))
            .then(pl.lit(name))
            .otherwise(binding)
        )

    return frame.with_columns(
        binding.alias("binding_cap"),
        (pl.col("shares") * entry).alias("position_dollars"),
        (pl.col("shares") * pl.col("risk_per_share")).alias("actual_risk_dollars"),
        pl.lit(risk_dollars).alias("risk_dollars"),
        (pl.col("low") + pl.col("atr_14") * sizing.max_entry_atr_multiple).alias("max_entry"),
        (pl.col("low") + pl.col("atr_14") * sizing.preferred_entry_atr_low).alias(
            "preferred_entry_low"
        ),
        (pl.col("low") + pl.col("atr_14") * sizing.preferred_entry_atr_high).alias(
            "preferred_entry_high"
        ),
    ).with_columns(
        # Spec §5.1: the doc says skip it if the day's move already exceeds the ATR.
        # Computed on a daily close this is an approximation of an intrinsically intraday
        # test, so it is a FLAG and never a filter.
        (entry > pl.col("max_entry")).alias("extended"),
        (pl.col("risk_per_share") > pl.col("atr_14")).alias("stop_exceeds_atr"),
    )
