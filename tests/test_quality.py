"""The data-quality gate.

This is the module that decides whether the scanner runs or refuses, so its failure modes
matter more than most. The staleness case is not hypothetical: a whole-market missing
session was observed on 2026-07-24 and is recorded in docs/DATA.md.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.ingest.base import ACTIONS_SCHEMA, UNIVERSE_SCHEMA, empty
from qms.quality import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    DataQualityError,
    check_quality,
    enforce,
)

CFG = load_scan_config()
EXPECTED = dt.date(2026, 7, 24)


def _bars(
    symbols: int = 600,
    sessions: tuple[dt.date, ...] = (dt.date(2026, 7, 23), dt.date(2026, 7, 24)),
    close: float = 100.0,
) -> pl.DataFrame:
    rows = []
    for index in range(symbols):
        for day in sessions:
            rows.append(
                {
                    "symbol": f"S{index:04d}",
                    "date": day,
                    "open": close,
                    "high": close * 1.02,
                    "low": close * 0.98,
                    "close": close,
                    "volume": 1_000_000.0,
                    "adjclose": close,
                }
            )
    return pl.DataFrame(rows, schema_overrides={"date": pl.Date})


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_clean_data_passes():
    issues = check_quality(
        _bars(), empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED
    )
    assert issues == [], f"unexpected issues: {[str(i) for i in issues]}"


def test_empty_store_is_an_error():
    issues = check_quality(
        pl.DataFrame(schema=_bars().schema),
        empty(UNIVERSE_SCHEMA),
        empty(ACTIONS_SCHEMA),
        CFG,
        EXPECTED,
    )
    assert _codes(issues) == {"empty_store"}


def test_stale_data_is_caught():
    """The observed 2026-07-24 vendor hole: newest bar is a session behind."""
    bars = _bars(sessions=(dt.date(2026, 7, 22), dt.date(2026, 7, 23)))
    issues = check_quality(bars, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)
    assert "stale_data" in _codes(issues)
    stale = next(i for i in issues if i.code == "stale_data")
    assert stale.severity == SEVERITY_ERROR
    assert "2026-07-23" in stale.message and "2026-07-24" in stale.message


def test_thin_universe_is_caught():
    issues = check_quality(
        _bars(symbols=10), empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED
    )
    assert "thin_universe" in _codes(issues)


def test_a_ragged_trailing_edge_is_reported_as_staleness_not_freshness():
    """The exact shape of the real 2026-07-24 failure.

    A handful of symbols carry a bar for a session that is missing for everyone else.
    Keying on max(date) would call this data fresh. The newest *well-covered* session is
    the day before, so the correct diagnosis is stale data plus a ragged edge.
    """
    full = _bars(symbols=600)
    trimmed = pl.concat(
        [
            full.filter(pl.col("date") == dt.date(2026, 7, 23)),
            full.filter(pl.col("date") == dt.date(2026, 7, 24)).head(42),
        ]
    )
    issues = check_quality(trimmed, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)

    codes = _codes(issues)
    assert "stale_data" in codes, "42 stray bars must not mask a missing session"
    assert "ragged_edge" in codes
    stale = next(i for i in issues if i.code == "stale_data")
    assert "2026-07-23" in stale.message


def test_no_well_covered_session_at_all_is_an_error():
    """Enough symbols overall, but every individual session is half-populated.

    Split so neither date reaches the coverage bar — the shape of an ingest that died
    partway through and was resumed against a different date.
    """
    full = _bars(symbols=600)
    sparse = pl.concat(
        [
            full.filter(pl.col("date") == dt.date(2026, 7, 23)).head(300),
            full.filter(pl.col("date") == dt.date(2026, 7, 24)).tail(300),
        ]
    )
    assert sparse["symbol"].n_unique() == 600, "fixture must clear the min_symbols gate"

    issues = check_quality(sparse, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)
    assert "thin_coverage" in _codes(issues)
    assert "thin_universe" not in _codes(issues)


def test_effective_latest_session_picks_the_covered_day():
    from qms.quality import effective_latest_session

    full = _bars(symbols=600)
    trimmed = pl.concat(
        [
            full.filter(pl.col("date") == dt.date(2026, 7, 23)),
            full.filter(pl.col("date") == dt.date(2026, 7, 24)).head(42),
        ]
    )
    assert effective_latest_session(trimmed, 0.80) == dt.date(2026, 7, 23)
    assert effective_latest_session(full, 0.80) == dt.date(2026, 7, 24)
    assert effective_latest_session(full.clear(), 0.80) is None


def test_null_ohlcv_reaching_the_store_is_an_error():
    bars = _bars().with_columns(
        pl.when(pl.int_range(pl.len()) == 0).then(None).otherwise(pl.col("close")).alias("close")
    )
    issues = check_quality(bars, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)
    assert "null_ohlcv" in _codes(issues)


def test_impossible_bars_are_caught():
    """low <= close <= high is not negotiable; a violation means bad parsing."""
    bars = _bars().with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(9999.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    issues = check_quality(bars, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)
    assert "impossible_bars" in _codes(issues)


def _bars_with_jump(affected: int, symbols: int = 600) -> pl.DataFrame:
    bars = _bars(symbols=symbols)
    jumped = {f"S{i:04d}" for i in range(affected)}
    return bars.with_columns(
        pl.when(
            pl.col("symbol").is_in(list(jumped)) & (pl.col("date") == dt.date(2026, 7, 24))
        )
        .then(pl.col("close") * 3.0)
        .otherwise(pl.col("close"))
        .alias("close")
    ).with_columns(
        pl.max_horizontal("high", "close").alias("high"),
        pl.min_horizontal("low", "close").alias("low"),
    )


def test_widespread_unexplained_jumps_are_an_error():
    """A missed split adjustment looks exactly like this and would rank as a big gainer."""
    issues = check_quality(
        _bars_with_jump(affected=200), empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED
    )
    jump = next(i for i in issues if i.code == "unexplained_jump")
    assert jump.severity == SEVERITY_ERROR


def test_a_few_jumps_are_only_a_warning():
    """Real biotech binaries and squeezes do move this much; do not block on a handful."""
    issues = check_quality(
        _bars_with_jump(affected=3), empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED
    )
    jump = next(i for i in issues if i.code == "unexplained_jump")
    assert jump.severity == SEVERITY_WARNING


def test_a_jump_explained_by_a_split_is_not_reported():
    bars = _bars_with_jump(affected=200)
    actions = pl.DataFrame(
        {
            "symbol": [f"S{i:04d}" for i in range(200)],
            "date": [dt.date(2026, 7, 24)] * 200,
            "action": ["split"] * 200,
            "numerator": [1.0] * 200,
            "denominator": [3.0] * 200,
            "amount": [None] * 200,
        },
        schema_overrides={"date": pl.Date, "amount": pl.Float64},
    )
    issues = check_quality(bars, empty(UNIVERSE_SCHEMA), actions, CFG, EXPECTED)
    assert "unexplained_jump" not in _codes(issues)


# ------------------------------------------------------------------------ enforcement


def test_enforce_raises_on_an_error():
    issues = check_quality(
        _bars(sessions=(dt.date(2026, 7, 21), dt.date(2026, 7, 22))),
        empty(UNIVERSE_SCHEMA),
        empty(ACTIONS_SCHEMA),
        CFG,
        EXPECTED,
    )
    with pytest.raises(DataQualityError, match="stale_data"):
        enforce(issues)


def test_allow_stale_downgrades_only_staleness():
    """The escape hatch must not become a blanket override."""
    stale_only = check_quality(
        _bars(sessions=(dt.date(2026, 7, 21), dt.date(2026, 7, 22))),
        empty(UNIVERSE_SCHEMA),
        empty(ACTIONS_SCHEMA),
        CFG,
        EXPECTED,
    )
    enforce(stale_only, allow_stale=True)  # must not raise

    with_other_error = check_quality(
        _bars(symbols=5, sessions=(dt.date(2026, 7, 21),)),
        empty(UNIVERSE_SCHEMA),
        empty(ACTIONS_SCHEMA),
        CFG,
        EXPECTED,
    )
    with pytest.raises(DataQualityError):
        enforce(with_other_error, allow_stale=True)


def test_enforce_tolerates_warnings():
    issues = check_quality(
        _bars_with_jump(affected=3), empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED
    )
    enforce(issues)  # must not raise


def test_errors_are_reported_before_warnings():
    bars = _bars_with_jump(affected=3, symbols=600).filter(
        pl.col("date") <= dt.date(2026, 7, 22)
    )
    issues = check_quality(bars, empty(UNIVERSE_SCHEMA), empty(ACTIONS_SCHEMA), CFG, EXPECTED)
    severities = [i.severity for i in issues]
    assert severities == sorted(severities, key=lambda s: s != SEVERITY_ERROR)
