"""Causality: no feature may read a bar at index > i.

Spec §1 calls this the difference between a backtest that means something and one that
doesn't. It is checked two complementary ways, and both enumerate the feature **registry**
rather than a hand-maintained list — so a feature added tomorrow is covered tonight, and
forgetting to test one is impossible.

**(a) Tail-truncation equivalence.** `build(series[:t+1])` must agree with `build(series)`
at row `t`, for every feature and several `t`.

    Truncation removes the TAIL, never the head. Wilder's ATR is recursive with unbounded
    memory, so its value at bar t legitimately depends on every bar before it — a
    head-truncated series has a different warm-up and would disagree for perfectly correct
    code. Getting this backwards produces a test that fails on correct code, which then
    gets "fixed" by loosening the tolerance, and the real lookahead sails straight
    through. This is the single easiest way to render the whole suite worthless.

**(b) Future perturbation.** Corrupt every bar after index i, recompute, and assert rows
0..i are unchanged. This catches leaks (a) can miss: full-history normalisation, centred
windows, a `.max()` over an unbounded frame, a `bfill`.
"""

from __future__ import annotations

import datetime as dt
import math

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.features.registry import all_features, build_features, feature_warmup

CFG = load_scan_config()
FEATURE_NAMES = sorted(all_features())

# Longest warm-up in the registry is the 200 SMA, so fixtures must comfortably exceed it
# or most features are null everywhere and the test proves nothing.
SERIES_LENGTH = 400
TRUNCATION_POINTS = (250, 300, 349, 399)


def _synthetic_bars(symbol: str = "TEST", n: int = SERIES_LENGTH, seed: int = 7) -> pl.DataFrame:
    """A deterministic pseudo-random walk with gaps, wide days and a volume cycle.

    Deliberately not smooth: gaps exercise the ATR/ADR divergence, and the volume cycle
    keeps the dry-up ratio away from a constant.
    """
    closes, highs, lows, opens, volumes = [], [], [], [], []
    price = 50.0
    state = seed
    for i in range(n):
        state = (1103515245 * state + 12345) % (2**31)
        shock = (state / 2**31 - 0.5) * 0.06
        gap = 0.03 if i % 37 == 0 else 0.0
        open_price = price * (1 + gap)
        price = max(1.0, open_price * (1 + shock))
        spread = abs(shock) + 0.01
        high = max(open_price, price) * (1 + spread)
        low = min(open_price, price) * (1 - spread)
        opens.append(open_price)
        highs.append(high)
        lows.append(low)
        closes.append(price)
        volumes.append(1_000_000.0 * (1.0 + 0.5 * math.sin(i / 11.0)))

    start = dt.date(2024, 1, 1)
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": [start + dt.timedelta(days=i) for i in range(n)],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "adjclose": closes,
        },
        schema_overrides={"date": pl.Date},
    )


def _values_equal(full, truncated, feature_name: str) -> bool:
    if full is None or truncated is None:
        return full is None and truncated is None
    if isinstance(full, bool) or isinstance(truncated, bool):
        return full == truncated
    if math.isnan(full) and math.isnan(truncated):
        return True
    # Recursive (EWM-based) features accumulate float error differently depending on how
    # many rows polars processes in one pass, so they get a relative tolerance. Pure
    # window features must match exactly.
    if feature_name in {"atr_14", "atr_fast", "atr_slow", "contraction"}:
        return math.isclose(full, truncated, rel_tol=1e-12, abs_tol=1e-12)
    return full == truncated


@pytest.fixture(scope="module")
def full_features() -> pl.DataFrame:
    return build_features(_synthetic_bars(), CFG)


@pytest.fixture(scope="module")
def bars() -> pl.DataFrame:
    return _synthetic_bars()


def test_registry_is_not_empty():
    """A silently empty registry would make every test below vacuously pass."""
    assert len(FEATURE_NAMES) > 20, f"only {len(FEATURE_NAMES)} features registered"


@pytest.mark.parametrize("truncate_at", TRUNCATION_POINTS)
def test_tail_truncation_equivalence(bars, full_features, truncate_at):
    """Computing on history up to t must give the same answer as computing on everything."""
    truncated = build_features(bars.head(truncate_at + 1), CFG)
    assert truncated.height == truncate_at + 1

    full_row = full_features.row(truncate_at, named=True)
    trunc_row = truncated.row(truncate_at, named=True)

    mismatches = [
        f"{name}: full={full_row[name]!r} truncated={trunc_row[name]!r}"
        for name in FEATURE_NAMES
        if not _values_equal(full_row[name], trunc_row[name], name)
    ]
    assert not mismatches, (
        f"{len(mismatches)} feature(s) changed when future bars were removed — "
        f"these read forward:\n  " + "\n  ".join(mismatches)
    )


@pytest.mark.parametrize("cut", (200, 260, 330))
def test_future_perturbation_leaves_the_past_untouched(bars, full_features, cut):
    """Corrupt everything after `cut`; rows 0..cut must be bit-identical.

    Catches what truncation cannot: a feature that normalises over the whole series, or
    reads a global max, still produces *a* value under truncation — but it changes here.
    """
    corrupted = bars.with_columns(
        [
            pl.when(pl.int_range(pl.len()) > cut)
            .then(pl.col(column) * 7.5 + 13.0)
            .otherwise(pl.col(column))
            .alias(column)
            for column in ("open", "high", "low", "close", "volume")
        ]
    )
    recomputed = build_features(corrupted, CFG)

    original_past = full_features.head(cut + 1).select(FEATURE_NAMES)
    recomputed_past = recomputed.head(cut + 1).select(FEATURE_NAMES)

    differing = [
        name
        for name in FEATURE_NAMES
        if not original_past[name].equals(recomputed_past[name], null_equal=True)
    ]
    assert not differing, (
        f"feature(s) {differing} changed at rows <= {cut} when only LATER bars were "
        "corrupted — that is a lookahead leak"
    )


def test_every_feature_is_actually_exercised(full_features):
    """Guards against a feature that is null everywhere, which would pass vacuously."""
    all_null = [
        name for name in FEATURE_NAMES if full_features[name].null_count() == full_features.height
    ]
    assert not all_null, f"feature(s) produced no values at all: {all_null}"


def test_warmup_is_respected_no_backfill(full_features):
    """A feature must emit null before it has enough history — never a back-filled value.

    `bfill` is explicitly banned by spec §1. It is also the most seductive bug here,
    because it makes the output look complete.
    """
    warmups = feature_warmup(CFG)
    offenders = []
    for name in FEATURE_NAMES:
        warmup = warmups[name]
        if warmup <= 1:
            continue
        # The value one bar before the window is satisfied must not exist.
        if full_features[name][warmup - 2] is not None:
            offenders.append(f"{name} (warmup={warmup}) has a value at row {warmup - 2}")
    assert not offenders, "features produced values before their window filled:\n  " + "\n  ".join(
        offenders
    )


def test_multi_symbol_panel_does_not_bleed_across_symbols():
    """`.over("symbol")` must isolate groups; a rolling window that spans the boundary
    between two tickers would be a silent, catastrophic leak."""
    solo = build_features(_synthetic_bars("AAA", seed=7), CFG)
    panel = build_features(
        pl.concat([_synthetic_bars("AAA", seed=7), _synthetic_bars("BBB", seed=99)]),
        CFG,
    ).filter(pl.col("symbol") == "AAA")

    differing = [
        name for name in FEATURE_NAMES if not solo[name].equals(panel[name], null_equal=True)
    ]
    assert not differing, f"feature(s) {differing} differ when another symbol is present"


def test_symbol_order_does_not_matter():
    """Results must not depend on the order symbols happen to arrive in."""
    a = _synthetic_bars("AAA", seed=7)
    b = _synthetic_bars("BBB", seed=99)
    forward = build_features(pl.concat([a, b]), CFG).filter(pl.col("symbol") == "BBB")
    reverse = build_features(pl.concat([b, a]), CFG).filter(pl.col("symbol") == "BBB")

    differing = [
        name for name in FEATURE_NAMES if not forward[name].equals(reverse[name], null_equal=True)
    ]
    assert not differing, f"feature(s) {differing} depend on input symbol ordering"


def test_unsorted_input_is_handled():
    """The builder sorts; a caller handing over shuffled rows must still get it right."""
    ordered = _synthetic_bars("AAA", seed=7)
    shuffled = ordered.sample(fraction=1.0, shuffle=True, seed=3)

    expected = build_features(ordered, CFG)
    actual = build_features(shuffled, CFG)

    differing = [
        name for name in FEATURE_NAMES if not expected[name].equals(actual[name], null_equal=True)
    ]
    assert not differing, f"feature(s) {differing} depend on input row ordering"
