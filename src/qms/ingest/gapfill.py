"""Repair under-covered sessions in the bar store from the fallback source.

Kept as a separate step from the primary ingest on purpose: a failure here must leave the
Yahoo-sourced store exactly as it was, not half-rewritten. It reads the store, works out
which recent trading sessions are missing or thin, refetches only those, and hands the rows
to the same `store.compact` the primary path uses — so Nasdaq rows supersede Yahoo nulls,
a re-run is a no-op, and nothing about the causality guarantees changes.

Scope is deliberately narrow. Only the **active** universe is repaired: a symbol below the
refetch floor can never clear the scan's dollar-volume gate, so paying 2.4 s a request to
fix its history buys nothing.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms import paths
from qms.calendar import sessions_in_range, shift_sessions
from qms.config import ScanConfig, UniverseConfig
from qms.ingest import store
from qms.ingest.base import BARS_SCHEMA, UNIVERSE_SCHEMA, valid_bars
from qms.ingest.http import HttpClient, HttpConfig, HttpError
from qms.ingest.nasdaq_bars import fetch_bars
from qms.quality import active_universe

# How far back to look for holes. Wider than a long weekend, narrow enough that a routine
# nightly run does not re-examine months of settled history.
GAP_LOOKBACK_SESSIONS = 8

# The fallback source has no bulk endpoint — one request per symbol at ~3 s under load —
# so throughput here is entirely a function of concurrency. Six workers took 45-60 minutes
# over the full active set and returned zero 429s across more than an hour, so there is
# clear headroom. Sixteen brings a realistic repair to a few minutes while still sitting
# well inside what a public website's own API serves.
GAPFILL_HTTP = HttpConfig(max_workers=16, min_interval_s=0.03)

# Measured, not guessed: 2,611 symbols repaired in 84 s at the settings above. Latency per
# request is far better at sixteen workers than at six (~0.5 s vs ~3.3 s) because
# connection reuse dominates, which is why the naive six-worker extrapolation predicted an
# hour for work that takes minutes.
SECONDS_PER_SYMBOL = 0.033


def find_gap_sessions(
    bars: pl.DataFrame,
    cfg: ScanConfig,
    expected_session: dt.date,
    active: set[str] | None = None,
    lookback: int = GAP_LOOKBACK_SESSIONS,
) -> list[dt.date]:
    """Trading sessions in the recent window whose coverage is below the threshold.

    Scoped to the same `active` population the quality gate measures and this module
    repairs. Keeping the three in step is what makes the repair actually satisfy the gate
    — measure across all 11,574 symbols while repairing 3,000 of them and coverage sticks
    at 26% forever.
    """
    if bars.is_empty():
        return []

    scoped = bars.filter(pl.col("symbol").is_in(list(active))) if active else bars
    total_symbols = scoped["symbol"].n_unique()
    if not total_symbols:
        return []

    # Never look back past the store's own earliest bar. A session that predates the data
    # is not a hole to repair, it is just history we do not hold — and treating it as a
    # gap would send the repair path chasing sessions the primary ingest never wanted.
    window_start = max(shift_sessions(expected_session, -lookback), scoped["date"].min())
    candidates = sessions_in_range(window_start, expected_session)
    if not candidates:
        return []

    counts = (
        scoped.filter(pl.col("date").is_in(candidates))
        .group_by("date")
        .agg(pl.col("symbol").n_unique().alias("symbols"))
    )
    present = dict(zip(counts["date"].to_list(), counts["symbols"].to_list(), strict=True))

    floor = cfg.quality.min_universe_coverage * total_symbols
    return [day for day in candidates if present.get(day, 0) < floor]


def symbols_missing_sessions(
    bars: pl.DataFrame,
    population: set[str],
    gaps: list[dt.date],
) -> set[str]:
    """Of `population`, those lacking a bar for at least one session in `gaps`.

    Without this the repair refetches the entire population every time, including the
    symbols that already have the session — which is most of them once a run has been
    interrupted and restarted. At ~3 s per request that is the difference between a
    resumable few minutes and a fixed hour.
    """
    if not population or not gaps:
        return set()

    have = (
        bars.filter(pl.col("symbol").is_in(list(population)) & pl.col("date").is_in(gaps))
        .group_by("symbol")
        .agg(pl.col("date").n_unique().alias("sessions"))
        .filter(pl.col("sessions") == len(gaps))
    )
    return population - set(have["symbol"].to_list())


def _etf_flags() -> dict[str, bool]:
    universe = store.read_parquet_or_empty(paths.UNIVERSE_FILE, UNIVERSE_SCHEMA)
    if universe.is_empty():
        return {}
    return dict(zip(universe["symbol"].to_list(), universe["is_etf"].to_list(), strict=True))


def run_gapfill(
    cfg: ScanConfig,
    universe_cfg: UniverseConfig,
    expected_session: dt.date,
    client: HttpClient | None = None,
    symbols: list[str] | None = None,
) -> int:
    """Detect and repair thin sessions. Returns the number of rows written."""
    client = client or HttpClient(config=GAPFILL_HTTP)
    bars = store.read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA)

    if bars.is_empty():
        print("[gapfill] bar store is empty; nothing to repair")
        return 0

    # The gapfill floor, not the refetch floor. Repair scope and measurement scope must
    # match, and this source is far too slow to sweep the whole active universe.
    active = active_universe(bars, universe_cfg.gapfill_floor_dollar_vol)
    gaps = find_gap_sessions(bars, cfg, expected_session, active)
    if not gaps:
        print("[gapfill] no under-covered sessions in the recent window")
        return 0

    targets = symbols or sorted(symbols_missing_sessions(bars, active, gaps))
    if not targets:
        print("[gapfill] every tracked symbol already has those sessions")
        return 0

    start, end = min(gaps), max(gaps)
    print(
        f"[gapfill] {len(gaps)} thin session(s) {start}..{end}; "
        f"{len(targets)} of {len(active)} tracked symbols need repair "
        f"(~{max(1, round(len(targets) * SECONDS_PER_SYMBOL / 60))} min)"
    )

    failures: list[str] = []

    def on_error(symbol: str, exc: Exception) -> None:
        if not isinstance(exc, HttpError) or not exc.permanent:
            failures.append(symbol)

    fetched = fetch_bars(client, targets, start, end, _etf_flags(), on_error=on_error)

    # Only the sessions we set out to repair. Nasdaq happily returns the whole range, and
    # letting well-covered days through would overwrite good Yahoo bars — including their
    # adjclose, which this source does not provide — for no benefit.
    repaired = fetched.filter(pl.col("date").is_in(gaps))
    if repaired.is_empty():
        print("[gapfill] fallback source returned nothing for those sessions")
        return 0

    batch_dir = paths.raw_batch_dir("bars_gapfill", expected_session)
    manifest = store.Manifest.load_or_create(
        "bars_gapfill",
        expected_session,
        batch_dir,
        {"start": start.isoformat(), "end": end.isoformat(), "sessions": len(gaps)},
    )
    store.write_parquet_atomic(repaired, manifest.next_batch_path())
    for symbol in repaired["symbol"].unique().to_list():
        manifest.record_success(symbol)
    manifest.save()

    before, after = store.compact(
        batch_dir, paths.BARS_FILE, BARS_SCHEMA, ["symbol", "date"], validate=valid_bars
    )
    print(
        f"[gapfill] recovered {repaired.height} bars for "
        f"{repaired['symbol'].n_unique()} symbols; store {before} -> {after} rows"
    )
    if failures:
        print(f"[gapfill] {len(failures)} symbol(s) failed transiently — re-run to retry")
    return repaired.height
