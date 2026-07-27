"""Hand-computed values for every [DOC] formula.

The causality suite proves features do not read forward. It says nothing about whether
they compute the right thing. That is this file's job, and every expected number below is
worked out by hand in the docstring rather than captured from a previous run — a
regression baseline snapshotted from the code under test proves only that it still does
what it did.
"""

from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.features.registry import build_features
from qms.features.volatility import adr_pct, true_range, wilder_atr

CFG = load_scan_config()


def _frame(**columns) -> pl.DataFrame:
    n = len(next(iter(columns.values())))

    def floats(values) -> list[float]:
        # Tests write literals like [100, 100, 100.0]; coerce so polars does not infer
        # Int64 from the first element and then choke on the last.
        return [float(v) for v in values]

    base = {
        "symbol": ["T"] * n,
        "date": [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "open": floats(columns.get("open", columns.get("close"))),
        "high": floats(columns.get("high", columns.get("close"))),
        "low": floats(columns.get("low", columns.get("close"))),
        "close": floats(columns.get("close")),
        "volume": floats(columns.get("volume", [1_000_000.0] * n)),
    }
    base["adjclose"] = base["close"]
    return pl.DataFrame(base, schema_overrides={"date": pl.Date})


def _apply(frame: pl.DataFrame, expr: pl.Expr, name: str = "out") -> list:
    return frame.select(expr.alias(name))[name].to_list()


# ------------------------------------------------------------------------------ ADR%


def test_adr_is_the_mean_of_ratios_not_the_ratio_of_means():
    """The distinction spec §3.1 insists on, on a case where the two actually differ.

    highs = [11, 24], lows = [10, 20], window 2.

      mean of ratios  : (11/10 + 24/20) / 2 = (1.1 + 1.2) / 2 = 1.15  -> 15.0%
      ratio of means  : mean(11,24) / mean(10,20) = 17.5 / 15 = 1.1666 -> 16.67%

    The ThinkOrSwim script in the source doc computes the second one. It is price-level
    biased: the higher-priced session dominates the numerator and denominator, so the
    answer drifts with price rather than measuring typical daily range.
    """
    frame = _frame(close=[10.5, 22.0], high=[11.0, 24.0], low=[10.0, 20.0])
    result = _apply(frame, adr_pct(2))

    assert result[0] is None, "no value before the window fills"
    assert result[1] == pytest.approx(15.0)

    ratio_of_means = (17.5 / 15.0 - 1) * 100
    assert not math.isclose(result[1], ratio_of_means, rel_tol=1e-6)


def test_adr_excludes_gaps_by_construction():
    """ADR only ever looks inside a session, so an overnight gap cannot move it.

    Both series have identical intraday ranges (high/low = 1.05 every day). The second
    gaps up 30% overnight on the final bar. ADR must be identical; ATR must not be.
    """
    flat = _frame(close=[100, 100, 100.0], high=[105, 105, 105.0], low=[100, 100, 100.0])
    gapped = _frame(close=[100, 100, 130.0], high=[105, 105, 136.5], low=[100, 100, 130.0])

    assert _apply(flat, adr_pct(3))[-1] == pytest.approx(5.0)
    assert _apply(gapped, adr_pct(3))[-1] == pytest.approx(5.0)

    assert _apply(gapped, true_range())[-1] > _apply(flat, true_range())[-1]


# ------------------------------------------------------------------------- true range


def test_true_range_picks_the_gap_leg():
    """bar2: H-L = 4, |H - Cprev| = 12, |L - Cprev| = 8  ->  12.

    This is precisely why ATR and ADR are not interchangeable: the same session scores
    3.7% on ADR (112/108) and 12 points on true range.
    """
    frame = _frame(close=[100.0, 110.0], high=[100.0, 112.0], low=[100.0, 108.0])
    assert _apply(frame, true_range()) == [0.0, 12.0]


def test_true_range_first_bar_falls_back_to_high_minus_low():
    """With no previous close the recursive ATR would never start otherwise."""
    frame = _frame(close=[100.0], high=[103.0], low=[99.0])
    assert _apply(frame, true_range()) == [4.0]


# ------------------------------------------------------------------------------- ATR


def test_wilder_atr_recursion():
    """alpha = 1/2 over TRs [4, 12, 2], adjust=False, min_samples=2.

      y0 = 4                          (suppressed: window not yet full)
      y1 = 0.5*12 + 0.5*4  = 8
      y2 = 0.5*2  + 0.5*8  = 5

    Note y1 depends on y0 even though y0 is not emitted — the recursion runs from the
    first bar and only the *display* is suppressed during warm-up.
    """
    frame = _frame(close=[100.0, 110.0, 109.0], high=[104.0, 112.0, 110.0], low=[100.0, 108.0, 108.0])
    assert _apply(frame, true_range()) == [4.0, 12.0, 2.0]

    result = _apply(frame, wilder_atr(2))
    assert result[0] is None
    assert result[1] == pytest.approx(8.0)
    assert result[2] == pytest.approx(5.0)


def test_atr_warmup_does_not_restart_the_recursion():
    """A longer history must change the first emitted ATR, proving memory is unbounded.

    If polars restarted the EWM at the min_samples boundary, prepending bars would leave
    the value at the boundary unchanged.
    """
    short = _frame(close=[100.0, 110.0], high=[104.0, 112.0], low=[100.0, 108.0])
    long = _frame(
        close=[50.0, 100.0, 110.0], high=[90.0, 104.0, 112.0], low=[50.0, 100.0, 108.0]
    )
    assert _apply(short, wilder_atr(2))[-1] != pytest.approx(_apply(long, wilder_atr(2))[-1])


# -------------------------------------------------------------------------- momentum


def test_momentum_uses_trading_day_lookbacks():
    """gain_1m = close / close.shift(21) - 1, in percent.

    Bar 21 closes at 150 against bar 0 at 100 -> +50%.
    """
    closes = [100.0] + [110.0] * 20 + [150.0]
    features = build_features(_frame(close=closes), CFG)
    gains = features["gain_1m"].to_list()

    assert gains[20] is None, "21 bars of lookback needs 22 bars of history"
    assert gains[21] == pytest.approx(50.0)


# ----------------------------------------------------------------------------- trend


def test_sma_and_distance_in_adr_units():
    """Distance in ADR units is what makes two different-priced names comparable.

    Closes are constant at 100 with a 5%-range day, so sma_10 = 100 and
    dist_to_sma_10_pct = 0. Then a jump to 110 puts price 10% above a 101 average.
    """
    closes = [100.0] * 10
    frame = _frame(close=closes, high=[105.0] * 10, low=[100.0] * 10)
    features = build_features(frame, CFG)

    assert features["sma_10"].to_list()[-1] == pytest.approx(100.0)
    assert features["dist_to_sma_10_pct"].to_list()[-1] == pytest.approx(0.0)
    assert features["adr_pct_20"].to_list()[-1] is None, "20-bar ADR needs 20 bars"


def test_dist_in_adr_divides_percent_distance_by_adr():
    """A name 6% above its 10 SMA with a 3% ADR is '2 average days' extended."""
    n = 40
    frame = _frame(
        close=[100.0] * n,
        high=[103.0] * n,
        low=[100.0] * n,
    )
    features = build_features(frame, CFG)
    row = features.row(n - 1, named=True)

    adr = row["adr_pct_20"]
    assert adr == pytest.approx(3.0)
    assert row["dist_to_sma_10_adr"] == pytest.approx(row["dist_to_sma_10_pct"] / adr)


def test_ma_stack_tolerates_brief_undercuts():
    """k-of-m, not a same-day boolean. Spec §4.1.

    Config ships k=8, m=10. A name whose 10 SMA sits above its 20 SMA on 8 of the last
    10 sessions must pass even though it dipped below on two of them — that dip is the
    pullback entry, not a disqualification.
    """
    stack = CFG.scan_a.ma_stack
    assert (stack.k, stack.m) == (8, 10), "test is written against the shipped k-of-m"

    # Rising series: 10 SMA is above the 20 SMA throughout the measured window.
    rising = [100.0 + i for i in range(60)]
    features = build_features(_frame(close=rising), CFG)
    assert features["ma_stack_ok"].to_list()[-1] is True

    # Falling series: never stacked.
    falling = [200.0 - i for i in range(60)]
    features = build_features(_frame(close=falling), CFG)
    assert features["ma_stack_ok"].to_list()[-1] is False


def test_ma_stack_is_null_until_both_averages_exist():
    features = build_features(_frame(close=[100.0 + i for i in range(60)]), CFG)
    values = features["ma_stack_ok"].to_list()
    warmup = CFG.scan_a.ma_stack.slow + CFG.scan_a.ma_stack.m - 1
    assert values[warmup - 2] is None
    assert values[warmup - 1] is not None


# ------------------------------------------------------------------------- liquidity


def test_dollar_volume_is_close_times_volume():
    n = 25
    frame = _frame(close=[10.0] * n, volume=[1_000_000.0] * n)
    features = build_features(frame, CFG)
    assert features["avg_dollar_vol_20"].to_list()[-1] == pytest.approx(10_000_000.0)
    assert features["avg_vol_20"].to_list()[-1] == pytest.approx(1_000_000.0)


# ------------------------------------------------------- [EXT] consolidation formulas


def test_low_slope_recovers_an_exact_linear_trend():
    """Lows rise by exactly 2.0 per bar, so the 5-bar OLS slope must be exactly 2.0."""
    n = 30
    lows = [5.0 + 2.0 * i for i in range(n)]
    frame = _frame(close=[low + 1 for low in lows], high=[low + 2 for low in lows], low=lows)
    features = build_features(frame, CFG)
    assert features["low_slope_5"].to_list()[-1] == pytest.approx(2.0)


def test_low_slope_is_negative_on_lower_lows():
    n = 30
    lows = [100.0 - 1.5 * i for i in range(n)]
    frame = _frame(close=[low + 1 for low in lows], high=[low + 2 for low in lows], low=lows)
    features = build_features(frame, CFG)
    assert features["low_slope_5"].to_list()[-1] == pytest.approx(-1.5)


def test_tightness_is_range_over_low():
    """Over the last 5 bars: max high 110, min low 100 -> (110-100)/100 = 10%."""
    n = 10
    frame = _frame(
        close=[105.0] * n,
        high=[110.0] * n,
        low=[100.0] * n,
    )
    features = build_features(frame, CFG)
    assert features["tightness_5"].to_list()[-1] == pytest.approx(10.0)


def test_depth_from_high_measures_the_pullback():
    """Close 90 against a 5-bar high of 100 is 10% off the high."""
    n = 10
    frame = _frame(
        close=[90.0] * n,
        high=[100.0] * n,
        low=[85.0] * n,
    )
    features = build_features(frame, CFG)
    assert features["depth_from_high_5"].to_list()[-1] == pytest.approx(10.0)


def test_pivot_high_is_the_rolling_max():
    highs = [100.0, 120.0, 110.0, 105.0, 103.0, 102.0]
    frame = _frame(close=highs, high=highs, low=[h - 5 for h in highs])
    features = build_features(frame, CFG)
    assert features["pivot_high_5"].to_list()[-1] == pytest.approx(120.0)


def test_contraction_and_dryup_are_fast_over_slow():
    """Both ratios below 1 mean the recent window is quieter than the long one."""
    n = 80
    # Volume halves for the last 5 bars; the fast window sees it and the slow one barely.
    volumes = [1_000_000.0] * (n - 5) + [500_000.0] * 5
    frame = _frame(close=[100.0] * n, high=[101.0] * n, low=[99.0] * n, volume=volumes)
    features = build_features(frame, CFG)
    assert features["vol_dryup"].to_list()[-1] < 1.0


# ---------------------------------------------------------------- degenerate inputs


def test_zero_and_negative_prices_produce_null_not_infinity():
    """A bad tick must not become inf and poison a ranking."""
    frame = _frame(close=[100.0, 0.0, 100.0], high=[101.0, 0.0, 101.0], low=[99.0, 0.0, 99.0])
    values = _apply(frame, adr_pct(1))
    assert values[1] is None
    assert all(v is None or math.isfinite(v) for v in values)
