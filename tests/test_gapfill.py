"""Nasdaq bar parsing and the gap-fill repair path.

Payloads below are trimmed copies of real responses captured 2026-07-27, including the
exact 2026-07-24 session Yahoo served as all-null. No network: everything is a literal.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import load_scan_config, load_universe_config
from qms.ingest.base import BARS_SCHEMA, conform
from qms.ingest.gapfill import find_gap_sessions
from qms.ingest.nasdaq_bars import parse_bars
from qms.ingest.store import compact
from qms.quality import active_universe, effective_latest_session

CFG = load_scan_config()
UNIVERSE_CFG = load_universe_config()


def _payload(rows: list[dict]) -> dict:
    return {"data": {"tradesTable": {"rows": rows}}}


# Real AAPL rows, verbatim formatting.
AAPL_ROWS = [
    {"date": "07/27/2026", "close": "$336.91", "volume": "49,604,300", "open": "$334.10", "high": "$338.14", "low": "$333.50"},
    {"date": "07/24/2026", "close": "$333.02", "volume": "47,489,420", "open": "$321.79", "high": "$334.37", "low": "$321.62"},
    {"date": "07/23/2026", "close": "$321.66", "volume": "40,840,780", "open": "$323.50", "high": "$324.10", "low": "$320.05"},
]

# Real SPY rows — note ETFs omit the leading '$'.
SPY_ROWS = [
    {"date": "07/24/2026", "close": "738.93", "volume": "44,781,980", "open": "738.10", "high": "740.22", "low": "737.05"},
]


# ------------------------------------------------------------------------ parsing


def test_parses_the_session_yahoo_served_as_null():
    """The whole reason this source exists."""
    bars = parse_bars("AAPL", _payload(AAPL_ROWS), max_date=dt.date(2026, 7, 24))
    row = bars.filter(pl.col("date") == dt.date(2026, 7, 24)).row(0, named=True)

    assert row["close"] == pytest.approx(333.02)
    assert row["open"] == pytest.approx(321.79)
    assert row["high"] == pytest.approx(334.37)
    assert row["low"] == pytest.approx(321.62)
    assert row["volume"] == pytest.approx(47_489_420)


def test_dollar_signs_and_thousands_separators_are_stripped():
    bars = parse_bars("AAPL", _payload(AAPL_ROWS), max_date=dt.date(2026, 7, 27))
    assert bars["close"].to_list() == pytest.approx([321.66, 333.02, 336.91])


def test_etf_rows_without_a_dollar_sign_parse_identically():
    bars = parse_bars("SPY", _payload(SPY_ROWS), max_date=dt.date(2026, 7, 24))
    assert bars["close"].to_list() == pytest.approx([738.93])


def test_dates_are_month_day_year_not_iso():
    """07/24/2026 is 24 July. Reading it as ISO would silently shift every bar."""
    bars = parse_bars("AAPL", _payload(AAPL_ROWS), max_date=dt.date(2026, 7, 27))
    assert bars["date"].to_list() == [
        dt.date(2026, 7, 23),
        dt.date(2026, 7, 24),
        dt.date(2026, 7, 27),
    ]


def test_bars_after_max_date_are_dropped():
    """Same live-bar guard as the primary parser."""
    bars = parse_bars("AAPL", _payload(AAPL_ROWS), max_date=dt.date(2026, 7, 24))
    assert dt.date(2026, 7, 27) not in bars["date"].to_list()


def test_adjclose_is_null_not_a_copy_of_close():
    """This endpoint publishes no dividend-adjusted close; inventing one would assert
    something we did not receive."""
    bars = parse_bars("AAPL", _payload(AAPL_ROWS), max_date=dt.date(2026, 7, 27))
    assert bars["adjclose"].null_count() == bars.height


def test_incomplete_row_is_dropped():
    rows = [{"date": "07/24/2026", "close": "$1.00", "volume": "100"}]  # no OHL
    assert parse_bars("X", _payload(rows), max_date=dt.date(2026, 7, 24)).is_empty()


def test_unparseable_date_is_dropped():
    rows = [{"date": "not-a-date", "close": "$1", "open": "$1", "high": "$1", "low": "$1", "volume": "1"}]
    assert parse_bars("X", _payload(rows), max_date=dt.date(2026, 7, 24)).is_empty()


def test_empty_payload_yields_schema_conformant_frame():
    bars = parse_bars("X", {"data": {"tradesTable": {"rows": []}}}, max_date=dt.date(2026, 7, 24))
    assert bars.is_empty()
    assert bars.columns == list(BARS_SCHEMA)


# ------------------------------------------------------- request window construction


class _RecordingClient:
    """Captures the params of every request instead of making one."""

    def __init__(self, rows=None):
        self.calls: list[dict] = []
        self.rows = rows if rows is not None else AAPL_ROWS

    def get_json(self, url, params=None):
        self.calls.append(dict(params or {}))
        return _payload(self.rows)


def test_single_session_request_is_widened():
    """Regression, found the expensive way.

    `fromdate=2026-07-24&todate=2026-07-24` returns an empty table from Nasdaq even though
    the session exists — verified live. Repairing exactly one session is the *normal* case
    for this module, so an un-widened request made a full 5,470-symbol pass fetch nothing
    at all while looking like it was working.
    """
    from qms.ingest.nasdaq_bars import fetch_symbol_bars

    client = _RecordingClient()
    day = dt.date(2026, 7, 24)
    fetch_symbol_bars(client, "AAPL", day, day)

    sent = client.calls[0]
    assert sent["todate"] == "2026-07-24"
    assert sent["fromdate"] < sent["todate"], "a single-day span returns nothing"


def test_a_wide_request_is_left_alone():
    from qms.ingest.nasdaq_bars import fetch_symbol_bars

    client = _RecordingClient()
    fetch_symbol_bars(client, "AAPL", dt.date(2026, 1, 5), dt.date(2026, 7, 24))
    assert client.calls[0]["fromdate"] == "2026-01-05"


def test_asset_class_follows_the_etf_flag():
    from qms.ingest.nasdaq_bars import fetch_symbol_bars

    equity = _RecordingClient()
    fetch_symbol_bars(equity, "AAPL", dt.date(2026, 7, 24), dt.date(2026, 7, 24), is_etf=False)
    assert equity.calls[0]["assetclass"] == "stocks"

    etf = _RecordingClient(rows=SPY_ROWS)
    fetch_symbol_bars(etf, "SPY", dt.date(2026, 7, 24), dt.date(2026, 7, 24), is_etf=True)
    assert etf.calls[0]["assetclass"] == "etf"


def test_empty_result_retries_with_the_other_asset_class():
    """The directory's ETF flag is not infallible; a miss must not look like a data gap."""
    from qms.ingest.nasdaq_bars import fetch_symbol_bars

    client = _RecordingClient(rows=[])
    fetch_symbol_bars(client, "WEIRD", dt.date(2026, 7, 24), dt.date(2026, 7, 24), is_etf=False)

    assert len(client.calls) == 2
    assert [c["assetclass"] for c in client.calls] == ["stocks", "etf"]


def test_successful_first_call_does_not_retry():
    from qms.ingest.nasdaq_bars import fetch_symbol_bars

    client = _RecordingClient()
    fetch_symbol_bars(client, "AAPL", dt.date(2026, 7, 24), dt.date(2026, 7, 24))
    assert len(client.calls) == 1


# ------------------------------------------------------------------ bar validation


def test_row_without_volume_is_dropped():
    """Real case: VFLO, VTEC and VTES came back with no volume on 2026-07-24. Without
    volume there is no dollar volume, and dollar volume is a hard gate."""
    rows = [
        {"date": "07/24/2026", "close": "$98.54", "open": "$98.40", "high": "$98.56",
         "low": "$98.38", "volume": ""},
    ]
    assert parse_bars("VTEC", _payload(rows), max_date=dt.date(2026, 7, 24)).is_empty()


def test_zeroed_prices_are_dropped():
    """Real case: Yahoo served BGFI with open=high=low=0 beside a close of 25.13 on
    2026-07-28. A zero price would make ADR infinite and the stop nonsense."""
    from qms.ingest.base import BARS_SCHEMA as _S
    from qms.ingest.base import valid_bars

    frame = pl.DataFrame(
        {
            "symbol": ["BGFI", "OK"],
            "date": [dt.date(2026, 7, 28)] * 2,
            "open": [0.0, 10.0], "high": [0.0, 11.0], "low": [0.0, 9.0],
            "close": [25.13, 10.5], "volume": [0.0, 100.0], "adjclose": [25.13, 10.5],
        },
        schema_overrides={"date": pl.Date},
    ).select(list(_S))

    assert valid_bars(frame)["symbol"].to_list() == ["OK"]


def test_high_below_low_is_dropped():
    from qms.ingest.base import BARS_SCHEMA as _S
    from qms.ingest.base import valid_bars

    frame = pl.DataFrame(
        {
            "symbol": ["BAD"], "date": [dt.date(2026, 7, 28)],
            "open": [10.0], "high": [9.0], "low": [11.0],
            "close": [10.0], "volume": [100.0], "adjclose": [10.0],
        },
        schema_overrides={"date": pl.Date},
    ).select(list(_S))
    assert valid_bars(frame).is_empty()


def test_zero_volume_is_kept():
    """A halted or untraded session legitimately has no volume but real prices."""
    from qms.ingest.base import BARS_SCHEMA as _S
    from qms.ingest.base import valid_bars

    frame = pl.DataFrame(
        {
            "symbol": ["HALT"], "date": [dt.date(2026, 7, 28)],
            "open": [10.0], "high": [10.0], "low": [10.0],
            "close": [10.0], "volume": [0.0], "adjclose": [10.0],
        },
        schema_overrides={"date": pl.Date},
    ).select(list(_S))
    assert valid_bars(frame).height == 1


def test_compaction_cleans_rows_already_in_the_store(tmp_path):
    """Validation rules are usually written after something bad is already stored, so
    `compact` applies them to the whole store rather than only to incoming rows."""
    from qms.ingest.base import valid_bars

    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"

    dirty = pl.concat([_bar("GOOD", dt.date(2026, 7, 23), 10.0), _bar("BAD", dt.date(2026, 7, 23), 10.0)])
    dirty = dirty.with_columns(
        pl.when(pl.col("symbol") == "BAD").then(pl.lit(0.0)).otherwise(pl.col("low")).alias("low"),
        pl.when(pl.col("symbol") == "BAD").then(pl.lit(0.0)).otherwise(pl.col("high")).alias("high"),
    )
    dirty.write_parquet(target)

    compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"], validate=valid_bars)
    assert pl.read_parquet(target)["symbol"].to_list() == ["GOOD"]


# ------------------------------------------------------------- gap-fill precedence


def _bar(symbol: str, day: dt.date, close: float, adjclose: float | None = None) -> pl.DataFrame:
    return conform(
        pl.DataFrame(
            {
                "symbol": [symbol],
                "date": [day],
                "open": [close],
                "high": [close],
                "low": [close],
                "close": [close],
                "volume": [1_000_000.0],
                "adjclose": [adjclose],
            },
            schema_overrides={"date": pl.Date, "adjclose": pl.Float64},
        ),
        BARS_SCHEMA,
    )


def test_gapfill_row_lands_where_the_primary_had_nothing(tmp_path):
    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"

    _bar("AAPL", dt.date(2026, 7, 23), 321.66, 321.66).write_parquet(target)
    _bar("AAPL", dt.date(2026, 7, 24), 333.02).write_parquet(batch_dir / "batch_0000.parquet")
    compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])

    result = pl.read_parquet(target).sort("date")
    assert result.height == 2
    assert result["close"].to_list() == pytest.approx([321.66, 333.02])
    # The pre-existing Yahoo bar keeps its adjclose; the repaired one has none.
    assert result["adjclose"].to_list()[0] == pytest.approx(321.66)
    assert result["adjclose"].to_list()[1] is None


def test_a_later_primary_fetch_supersedes_the_repair(tmp_path):
    """When Yahoo backfills the hole it becomes authoritative again, adjclose included."""
    batch_dir = tmp_path / "raw"
    batch_dir.mkdir()
    target = tmp_path / "bars.parquet"

    _bar("AAPL", dt.date(2026, 7, 24), 333.02).write_parquet(target)
    _bar("AAPL", dt.date(2026, 7, 24), 333.02, 332.80).write_parquet(
        batch_dir / "batch_0000.parquet"
    )
    compact(batch_dir, target, BARS_SCHEMA, ["symbol", "date"])

    result = pl.read_parquet(target)
    assert result.height == 1
    assert result["adjclose"].to_list() == pytest.approx([332.80])


# ------------------------------------------------------------- gap detection scope


def _store(symbols: int, sessions: list[dt.date], missing_on: dict[dt.date, int] | None = None):
    """Bars for `symbols` names, optionally dropping the first N on a given session."""
    missing_on = missing_on or {}
    rows = []
    for i in range(symbols):
        for day in sessions:
            if i < missing_on.get(day, 0):
                continue
            rows.append(
                {
                    "symbol": f"S{i:04d}",
                    "date": day,
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                    "volume": 1_000_000.0, "adjclose": 100.0,
                }
            )
    return pl.DataFrame(rows, schema_overrides={"date": pl.Date})


SESSIONS = [dt.date(2026, 7, 22), dt.date(2026, 7, 23), dt.date(2026, 7, 24)]


def test_thin_session_is_detected_as_a_gap():
    bars = _store(600, SESSIONS, missing_on={dt.date(2026, 7, 24): 580})
    gaps = find_gap_sessions(bars, CFG, dt.date(2026, 7, 24))
    assert gaps == [dt.date(2026, 7, 24)]


def test_fully_covered_sessions_are_not_gaps():
    assert find_gap_sessions(_store(600, SESSIONS), CFG, dt.date(2026, 7, 24)) == []


def test_gap_detection_and_coverage_share_a_population():
    """Repair scope must equal measurement scope, or the gate can never be satisfied.

    Repairing only the active names while measuring coverage across every stored symbol
    pins coverage at the active share of the universe — here 25% — no matter how complete
    the repair is. Both must be scoped the same way.
    """
    bars = _store(600, SESSIONS, missing_on={dt.date(2026, 7, 24): 450})
    # The 150 symbols that DO have 07/24 are exactly the ones we would have repaired.
    active = {f"S{i:04d}" for i in range(450, 600)}

    assert find_gap_sessions(bars, CFG, dt.date(2026, 7, 24)) == [dt.date(2026, 7, 24)]
    assert find_gap_sessions(bars, CFG, dt.date(2026, 7, 24), active) == []
    assert effective_latest_session(bars, 0.80) == dt.date(2026, 7, 23)
    assert effective_latest_session(bars, 0.80, active) == dt.date(2026, 7, 24)


def test_only_symbols_actually_missing_a_session_are_refetched():
    """The difference between a resumable few minutes and a fixed hour.

    At ~3 s per request, refetching the whole population when most of it already has the
    session is the single biggest cost in this path — and it makes an interrupted run
    start from scratch instead of picking up where it stopped.
    """
    from qms.ingest.gapfill import symbols_missing_sessions

    bars = _store(600, SESSIONS, missing_on={dt.date(2026, 7, 24): 450})
    population = {f"S{i:04d}" for i in range(600)}
    gaps = [dt.date(2026, 7, 24)]

    missing = symbols_missing_sessions(bars, population, gaps)
    assert len(missing) == 450
    assert "S0000" in missing, "has no 07/24 bar"
    assert "S0599" not in missing, "already repaired"


def test_nothing_missing_means_nothing_to_fetch():
    from qms.ingest.gapfill import symbols_missing_sessions

    bars = _store(600, SESSIONS)
    population = {f"S{i:04d}" for i in range(600)}
    assert symbols_missing_sessions(bars, population, [dt.date(2026, 7, 24)]) == set()


def test_a_symbol_missing_only_one_of_several_gaps_is_refetched():
    """One request covers the whole range, so a partial miss still needs the symbol."""
    from qms.ingest.gapfill import symbols_missing_sessions

    bars = _store(10, SESSIONS, missing_on={dt.date(2026, 7, 24): 3})
    population = {f"S{i:04d}" for i in range(10)}
    gaps = [dt.date(2026, 7, 23), dt.date(2026, 7, 24)]
    assert len(symbols_missing_sessions(bars, population, gaps)) == 3


def test_empty_store_reports_no_gaps():
    assert find_gap_sessions(_store(0, SESSIONS), CFG, dt.date(2026, 7, 24)) == []


def test_active_universe_applies_the_dollar_volume_floor():
    bars = pl.concat([_store(3, SESSIONS)]).with_columns(
        pl.when(pl.col("symbol") == "S0000")
        .then(pl.lit(1.0))
        .otherwise(pl.col("volume"))
        .alias("volume")
    )
    active = active_universe(bars, UNIVERSE_CFG.active_universe_floor_dollar_vol)
    assert "S0000" not in active
    assert {"S0001", "S0002"} <= active
