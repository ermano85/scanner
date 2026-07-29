"""The nightly pipeline wiring.

Ingest is always stubbed out — these tests must never touch the network. What is being
checked is the ordering and the gating: that the quality gate runs after the feature build
and before the scan, and that it can actually stop the pipeline.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.quality import DataQualityError


def _bars(symbol: str, n: int = 260, drift: float = 0.005, end: dt.date = dt.date(2026, 7, 24)):
    closes = []
    price = 100.0
    for _ in range(n):
        price *= 1.0 + drift
        closes.append(price)

    dates: list[dt.date] = []
    cursor = end
    while len(dates) < n:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= dt.timedelta(days=1)
    dates.reverse()

    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": dates,
            "open": closes,
            "high": [c * 1.03 for c in closes],
            "low": [c * 0.97 for c in closes],
            "close": closes,
            "volume": [5_000_000.0] * n,
            "adjclose": closes,
        },
        schema_overrides={"date": pl.Date},
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard stop on outbound HTTP from this module.

    The gap-fill runs inside `run_nightly`, so without this the suite quietly starts
    hitting Nasdaq for every fixture session it thinks is missing — which is both slow
    and a test that depends on the internet being up.
    """
    from qms.ingest import http

    def _blocked(*_args, **_kwargs):
        raise AssertionError("tests must not make network calls")

    monkeypatch.setattr(http.HttpClient, "get", _blocked)
    monkeypatch.setattr(http.HttpClient, "get_json", _blocked)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point every store at a temp dir and pre-populate the bar store."""
    from qms import paths

    for name, value in {
        "DATA_DIR": tmp_path,
        "BARS_DIR": tmp_path / "bars",
        "UNIVERSE_DIR": tmp_path / "universe",
        "EARNINGS_DIR": tmp_path / "earnings",
        "ACTIONS_DIR": tmp_path / "actions",
        "FEATURES_DIR": tmp_path / "features",
        "RAW_DIR": tmp_path / "raw",
        "OUT_DIR": tmp_path / "out",
        "BARS_FILE": tmp_path / "bars" / "bars.parquet",
        "UNIVERSE_FILE": tmp_path / "universe" / "universe.parquet",
        "EARNINGS_FILE": tmp_path / "earnings" / "earnings.parquet",
        "ACTIONS_FILE": tmp_path / "actions" / "actions.parquet",
        "FEATURES_FILE": tmp_path / "features" / "features.parquet",
    }.items():
        monkeypatch.setattr(paths, name, value)
    monkeypatch.setattr(paths, "scan_out_dir", lambda d: tmp_path / "out" / d.isoformat())
    paths.ensure_dirs()

    # Enough symbols to clear quality.min_symbols.
    bars = pl.concat([_bars(f"S{i:04d}", drift=0.001 + i % 7 * 0.001) for i in range(600)])
    bars.write_parquet(paths.BARS_FILE)
    return paths


def test_nightly_runs_end_to_end_without_network(wired, monkeypatch):
    from qms import nightly

    called = {"ingest": False, "gapfill": False}

    def _no_ingest(**_kwargs):
        called["ingest"] = True

    def _no_gapfill(*_args, **_kwargs):
        called["gapfill"] = True
        return 0

    monkeypatch.setattr(nightly, "run_ingest", _no_ingest)
    monkeypatch.setattr(nightly, "run_gapfill", _no_gapfill)
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 24))

    html = nightly.run_nightly()

    assert called["ingest"], "the nightly must ingest unless told not to"
    assert called["gapfill"], "the nightly must attempt a gap-fill unless told not to"
    assert html.exists()
    assert (html.parent / "ranked.csv").exists()


def test_gapfill_failure_does_not_abort_the_run(wired, monkeypatch):
    """The fallback source is unofficial too. Its failure must not lose the whole scan.

    The quality gate is what decides whether the resulting data is usable — a gap-fill
    error is a reason to check, not a reason to produce nothing.
    """
    from qms import nightly

    def _boom(*_args, **_kwargs):
        raise RuntimeError("nasdaq unreachable")

    monkeypatch.setattr(nightly, "run_ingest", lambda **_k: None)
    monkeypatch.setattr(nightly, "run_gapfill", _boom)
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 24))

    assert nightly.run_nightly(skip_ingest=True).exists()


def test_skip_gapfill_really_skips(wired, monkeypatch):
    from qms import nightly

    def _boom(*_args, **_kwargs):
        raise AssertionError("gap-fill must not run when --skip-gapfill is passed")

    monkeypatch.setattr(nightly, "run_ingest", lambda **_k: None)
    monkeypatch.setattr(nightly, "run_gapfill", _boom)
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 24))

    assert nightly.run_nightly(skip_ingest=True, skip_gapfill=True).exists()


def test_skip_ingest_really_skips(wired, monkeypatch):
    from qms import nightly

    def _boom(**_kwargs):
        raise AssertionError("ingest must not run when --skip-ingest is passed")

    monkeypatch.setattr(nightly, "run_ingest", _boom)
    monkeypatch.setattr(nightly, "run_gapfill", lambda *_a, **_k: 0)
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 24))

    assert nightly.run_nightly(skip_ingest=True).exists()


def test_quality_gate_stops_the_pipeline_before_a_report_is_written(wired, monkeypatch):
    """The point of the gate: no report at all, rather than a normal-looking stale one."""
    from qms import nightly

    monkeypatch.setattr(nightly, "run_ingest", lambda **_k: None)
    monkeypatch.setattr(nightly, "run_gapfill", lambda *_a, **_k: 0)
    # Claim a later session than any bar we hold, so the store is stale.
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 31))

    with pytest.raises(DataQualityError, match="stale_data"):
        nightly.run_nightly(skip_ingest=True)

    assert not list((wired.OUT_DIR).rglob("index.html")), "no report may be written"


def test_allow_stale_lets_the_pipeline_through_and_marks_the_report(wired, monkeypatch):
    from qms import nightly

    monkeypatch.setattr(nightly, "run_ingest", lambda **_k: None)
    monkeypatch.setattr(nightly, "run_gapfill", lambda *_a, **_k: 0)
    monkeypatch.setattr(nightly, "last_completed_session", lambda: dt.date(2026, 7, 31))

    html = nightly.run_nightly(skip_ingest=True, allow_stale=True)
    assert html.exists()
    assert "stale" in html.read_text(encoding="utf-8").lower()
