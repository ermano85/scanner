"""Ingest parsing and resume logic. No network: every input here is a literal fixture.

The Yahoo payloads below are trimmed copies of real responses captured 2026-07-27,
including the all-null 2026-07-24 session that the live feed actually served.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import UniverseConfig
from qms.ingest import store
from qms.ingest.base import BARS_SCHEMA, conform, empty
from qms.ingest.nasdaq_earnings import clean_number, fetch_earnings_for_date
from qms.ingest.universe import apply_universe_filters, to_vendor_symbol
from qms.ingest.yahoo import parse_actions, parse_bars

# 13:30Z == 09:30 America/New_York, which is how Yahoo stamps a daily bar.
TS_0721 = 1784640600
TS_0722 = 1784727000
TS_0723 = 1784813400
TS_0724 = 1784899800
TS_0727 = 1785159000


def _chart(timestamps, closes, *, splits=None, dividends=None):
    n = len(timestamps)
    return {
        "timestamp": list(timestamps),
        "indicators": {
            "quote": [
                {
                    "open": [None if c is None else c - 1 for c in closes],
                    "high": [None if c is None else c + 2 for c in closes],
                    "low": [None if c is None else c - 2 for c in closes],
                    "close": list(closes),
                    "volume": [None if c is None else 1_000_000 for c in closes],
                }
            ],
            "adjclose": [{"adjclose": list(closes)}],
        },
        "events": {"splits": splits or {}, "dividends": dividends or {}},
    }


# ------------------------------------------------------------------------ bar parsing


def test_null_session_is_dropped_not_interpolated():
    """The real 2026-07-24 hole: a null-OHLC row is not a bar."""
    result = _chart([TS_0722, TS_0723, TS_0724], [325.89, 321.66, None])
    bars = parse_bars("AAPL", result, max_date=dt.date(2026, 7, 24))
    assert bars["date"].to_list() == [dt.date(2026, 7, 22), dt.date(2026, 7, 23)]
    assert bars["close"].null_count() == 0


def test_live_bar_after_last_completed_session_is_dropped():
    """Ingesting a partially-formed bar would make the same date mutate intraday."""
    result = _chart([TS_0723, TS_0727], [321.66, 336.83])
    bars = parse_bars("AAPL", result, max_date=dt.date(2026, 7, 24))
    assert bars["date"].to_list() == [dt.date(2026, 7, 23)]


def test_timestamps_map_to_exchange_local_dates():
    """13:30Z is 09:30 in New York — the bar belongs to that ET date, not the UTC one."""
    bars = parse_bars("AAPL", _chart([TS_0721], [327.74]), max_date=dt.date(2026, 7, 24))
    assert bars["date"].to_list() == [dt.date(2026, 7, 21)]


def test_duplicate_session_keeps_last():
    result = _chart([TS_0723, TS_0723], [321.66, 322.00])
    bars = parse_bars("AAPL", result, max_date=dt.date(2026, 7, 24))
    assert bars.height == 1
    assert bars["close"].to_list() == [322.00]


def test_empty_payload_yields_empty_frame_with_schema():
    bars = parse_bars("NOPE", _chart([], []), max_date=dt.date(2026, 7, 24))
    assert bars.is_empty()
    assert bars.columns == list(BARS_SCHEMA)


def test_bars_conform_to_schema_exactly():
    bars = parse_bars("AAPL", _chart([TS_0723], [321.66]), max_date=dt.date(2026, 7, 24))
    assert bars.columns == list(BARS_SCHEMA)
    assert bars.schema["volume"] == pl.Float64
    assert bars.schema["date"] == pl.Date


# --------------------------------------------------------------------- action parsing


def test_split_event_is_parsed():
    result = _chart(
        [TS_0723],
        [321.66],
        splits={"1718026200": {"date": 1718026200, "numerator": 10, "denominator": 1}},
    )
    actions = parse_actions("NVDA", result)
    split = actions.filter(pl.col("action") == "split")
    assert split.height == 1
    assert split["numerator"].to_list() == [10.0]
    assert split["denominator"].to_list() == [1.0]


def test_dividend_event_is_parsed():
    result = _chart([TS_0723], [321.66], dividends={"1780579800": {"amount": 0.25}})
    # Dividend records carry no date key in this trimmed fixture shape, so build one that
    # matches the real payload, which always includes it.
    result["events"]["dividends"] = {"1780579800": {"amount": 0.25, "date": 1780579800}}
    actions = parse_actions("NVDA", result)
    dividend = actions.filter(pl.col("action") == "dividend")
    assert dividend.height == 1
    assert dividend["amount"].to_list() == [0.25]


def test_no_events_yields_empty_actions():
    assert parse_actions("AAPL", _chart([TS_0723], [321.66])).is_empty()


# ------------------------------------------------------------------- universe filters


def _universe_cfg(**overrides) -> UniverseConfig:
    base = {
        "exchanges": {"Q": True, "N": True, "A": True, "P": True, "Z": True, "V": False},
        "include_etfs": True,
        "exclude_test_issues": True,
        "exclude_deficient": False,
        "exclude_suffixes": ["W", "U", "R"],
        "exclude_name_patterns": [r"\bLeveraged\b"],
        "exclude_symbols": ["BADSYM"],
        "exclude_sic": [2834],
        "active_universe_floor_dollar_vol": 2_000_000.0,
        "gapfill_floor_dollar_vol": 10_000_000.0,
    }
    base.update(overrides)
    return UniverseConfig.model_validate(base)


def _universe_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAPL", "ATEST", "AAC.U", "ACHR.W", "AGM.A", "LEVR", "BADSYM", "IEXX"],
            "name": [
                "Apple Inc.",
                "Nasdaq Test Issue",
                "Ares Acquisition - Units",
                "Archer - Warrants",
                "Federal Agricultural Class A",
                "Direxion Leveraged Fund",
                "Some Junk",
                "IEX Listed Thing",
            ],
            "exchange": ["Q", "Q", "N", "N", "N", "P", "Q", "V"],
            "is_etf": [False, False, False, False, False, True, False, False],
            "test_issue": [False, True, False, False, False, False, False, False],
            "financial_status": [None, None, None, None, None, None, None, None],
        },
        schema_overrides={"financial_status": pl.Utf8},
    )


def test_universe_filters_drop_the_right_rows():
    kept = apply_universe_filters(_universe_frame(), _universe_cfg())["symbol"].to_list()
    assert kept == ["AAPL", "AGM.A"]


def test_share_class_suffix_survives_but_units_and_warrants_do_not():
    """AGM.A is a share class and tradeable; AAC.U and ACHR.W are not the common stock."""
    kept = apply_universe_filters(_universe_frame(), _universe_cfg())["symbol"].to_list()
    assert "AGM.A" in kept
    assert "AAC.U" not in kept
    assert "ACHR.W" not in kept


def test_etfs_can_be_excluded_wholesale():
    cfg = _universe_cfg(include_etfs=False, exclude_name_patterns=[])
    kept = apply_universe_filters(_universe_frame(), cfg)["symbol"].to_list()
    assert "LEVR" not in kept


def test_vendor_symbol_mapping():
    assert to_vendor_symbol("BRK.A") == "BRK-A"
    assert to_vendor_symbol("AAPL") == "AAPL"


# ------------------------------------------------------------------- earnings parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$3.23", 3.23),
        ("($0.14)", -0.14),
        ("$673,938,031,023", 673_938_031_023.0),
        ("", None),
        (None, None),
        ("N/A", None),
        ("--", None),
    ],
)
def test_nasdaq_number_cleaning(raw, expected):
    assert clean_number(raw) == expected


class _StubClient:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url, params=None):
        return self.payload


def test_earnings_row_mapping():
    payload = {
        "data": {
            "rows": [
                {
                    "symbol": "V",
                    "time": "time-after-hours",
                    "epsForecast": "$3.23",
                    "marketCap": "$673,938,031,023",
                    "fiscalQuarterEnding": "Jun/2026",
                },
                {
                    "symbol": "aapl",
                    "time": "time-pre-market",
                    "epsForecast": "",
                    "marketCap": "",
                    "fiscalQuarterEnding": "",
                },
                {"symbol": "", "time": "time-not-supplied"},
            ]
        }
    }
    frame = fetch_earnings_for_date(_StubClient(payload), dt.date(2026, 7, 28))
    assert frame.height == 2, "blank symbols must be dropped"
    assert frame["symbol"].to_list() == ["V", "AAPL"], "symbols are upper-cased"
    assert frame["when"].to_list() == ["amc", "bmo"]
    assert frame["market_cap"].to_list() == [673_938_031_023.0, None]


def test_unmapped_time_becomes_unknown():
    payload = {"data": {"rows": [{"symbol": "X", "time": "something-new"}]}}
    frame = fetch_earnings_for_date(_StubClient(payload), dt.date(2026, 7, 28))
    assert frame["when"].to_list() == ["unknown"]


# ------------------------------------------------------- manifest, resume, compaction


def test_manifest_resume_skips_completed_and_dead_symbols(tmp_path):
    manifest = store.Manifest.load_or_create("bars", dt.date(2026, 7, 24), tmp_path, {"a": 1})
    manifest.record_success("AAPL")
    manifest.record_failure("DEAD", "404", permanent=True)
    manifest.record_failure("FLAKY", "503", permanent=False)
    manifest.save()

    reloaded = store.Manifest.load_or_create("bars", dt.date(2026, 7, 24), tmp_path, {"a": 1})
    pending = reloaded.pending(["AAPL", "DEAD", "FLAKY", "NEW"])
    assert pending == ["FLAKY", "NEW"], "completed and permanently-dead are skipped, transient retried"


def test_manifest_with_different_params_does_not_resume(tmp_path):
    """A different date range under the same run date is a different job."""
    manifest = store.Manifest.load_or_create("bars", dt.date(2026, 7, 24), tmp_path, {"start": "a"})
    manifest.record_success("AAPL")
    manifest.save()

    other = store.Manifest.load_or_create("bars", dt.date(2026, 7, 24), tmp_path, {"start": "b"})
    assert other.pending(["AAPL"]) == ["AAPL"]


def test_success_clears_a_prior_failure(tmp_path):
    manifest = store.Manifest.load_or_create("bars", dt.date(2026, 7, 24), tmp_path, {})
    manifest.record_failure("X", "503", permanent=False)
    manifest.record_success("X")
    assert "X" not in manifest.transient_failures
    assert manifest.pending(["X"]) == []


def _bar_row(symbol: str, day: dt.date, close: float) -> pl.DataFrame:
    return conform(
        pl.DataFrame(
            {
                "symbol": [symbol],
                "date": [day],
                "open": [close],
                "high": [close],
                "low": [close],
                "close": [close],
                "volume": [1.0],
                "adjclose": [close],
            },
            schema_overrides={"date": pl.Date},
        ),
        BARS_SCHEMA,
    )


def test_compaction_is_idempotent(tmp_path):
    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"
    _bar_row("AAPL", dt.date(2026, 7, 23), 321.66).write_parquet(batch_dir / "batch_0000.parquet")

    first = store.compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])
    payload_after_first = target.read_bytes()
    second = store.compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])

    assert first == (0, 1)
    assert second == (1, 1)
    assert target.read_bytes() == payload_after_first, "re-compaction must be a byte no-op"


def test_compaction_lets_a_restatement_win(tmp_path):
    """A vendor correcting a bar must overwrite, not duplicate."""
    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"

    _bar_row("AAPL", dt.date(2026, 7, 23), 321.66).write_parquet(batch_dir / "batch_0000.parquet")
    store.compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])

    for stale in batch_dir.glob("batch_*.parquet"):
        stale.unlink()
    _bar_row("AAPL", dt.date(2026, 7, 23), 999.99).write_parquet(batch_dir / "batch_0001.parquet")
    store.compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])

    result = pl.read_parquet(target)
    assert result.height == 1
    assert result["close"].to_list() == [999.99]


def test_compact_with_no_batches_leaves_target_alone(tmp_path):
    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"
    assert store.compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"]) == (0, 0)
    assert not target.exists()


def test_conform_rejects_a_missing_column():
    with pytest.raises(ValueError, match="missing required column"):
        conform(pl.DataFrame({"symbol": ["A"]}), BARS_SCHEMA)


def test_empty_helper_has_full_schema():
    assert empty(BARS_SCHEMA).columns == list(BARS_SCHEMA)
