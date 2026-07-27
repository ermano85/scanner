"""Ingest orchestration: universe -> bars -> earnings, idempotent and resumable.

Fetch strategy (spec §2, plan §4). 13,000 symbols is too many to refetch nightly, so:

* **backfill** — every symbol, `data.backfill_years` of history. One-time.
* **nightly** — only the *active* set (20d dollar volume above the universe floor), over
  a `data.nightly_lookback_days` window rather than a single bar, so a missed night
  self-heals and late vendor restatements propagate.
* **full refresh** — every symbol, short window. Weekly, to catch new listings and names
  that have grown into liquidity.

The active floor sits an order of magnitude below the scan's dollar-volume gate
specifically so a name can become eligible between full refreshes.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms import paths
from qms.calendar import last_completed_session, shift_sessions
from qms.config import ScanConfig, UniverseConfig, load_scan_config, load_universe_config
from qms.ingest import store
from qms.ingest.base import (
    ACTIONS_SCHEMA,
    BARS_SCHEMA,
    EARNINGS_SCHEMA,
    UNIVERSE_SCHEMA,
)
from qms.ingest.http import HttpClient, HttpError
from qms.ingest.nasdaq_earnings import fetch_earnings_range
from qms.ingest.universe import apply_universe_filters, fetch_universe
from qms.ingest.yahoo import fetch_symbol_chart, parse_actions, parse_bars

BATCH_SYMBOLS = 250
# How far ahead of the scan date the earnings calendar is pulled. Comfortably wider than
# any sane blackout so the gate never runs out of forward data.
EARNINGS_FORWARD_SESSIONS = 15
EARNINGS_BACKWARD_SESSIONS = 5


def run_ingest(
    backfill: bool = False,
    full_universe: bool = False,
    symbols: list[str] | None = None,
    scan_cfg: ScanConfig | None = None,
    universe_cfg: UniverseConfig | None = None,
    client: HttpClient | None = None,
) -> None:
    scan_cfg = scan_cfg or load_scan_config()
    universe_cfg = universe_cfg or load_universe_config()
    client = client or HttpClient()
    paths.ensure_dirs()

    as_of = last_completed_session()
    print(f"[ingest] last completed session: {as_of}")

    universe = ingest_universe(client, universe_cfg)
    print(f"[ingest] universe: {universe.height} symbols after filters")

    targets = symbols or select_fetch_targets(
        universe, universe_cfg, backfill=backfill, full_universe=full_universe
    )
    print(f"[ingest] fetching bars for {len(targets)} symbols")

    if backfill:
        start = as_of - dt.timedelta(days=int(scan_cfg.data.backfill_years * 366))
    else:
        start = as_of - dt.timedelta(days=scan_cfg.data.nightly_lookback_days)

    ingest_bars(client, targets, start, as_of)
    ingest_earnings(client, as_of)


# ------------------------------------------------------------------------- universe


def ingest_universe(client: HttpClient, cfg: UniverseConfig) -> pl.DataFrame:
    raw = fetch_universe(client)
    filtered = apply_universe_filters(raw, cfg)
    store.write_parquet_atomic(filtered, paths.UNIVERSE_FILE)
    return filtered


def select_fetch_targets(
    universe: pl.DataFrame,
    cfg: UniverseConfig,
    backfill: bool,
    full_universe: bool,
) -> list[str]:
    """Which symbols to fetch tonight."""
    all_symbols = universe["symbol"].to_list()
    if backfill or full_universe:
        return all_symbols

    bars = store.read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA)
    if bars.is_empty():
        # Nothing stored yet, so there is no active set to narrow to.
        return all_symbols

    active = active_symbols(bars, cfg.active_universe_floor_dollar_vol)
    known = set(all_symbols)
    # Newly listed names have no history and would never enter the active set on their
    # own, so always include symbols we hold no bars for.
    unseen = known - set(bars["symbol"].unique().to_list())
    return sorted((active & known) | unseen)


def active_symbols(bars: pl.DataFrame, floor_dollar_vol: float, window: int = 20) -> set[str]:
    """Symbols whose recent average dollar volume clears the refetch floor."""
    recent = (
        bars.sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .tail(window)
        .with_columns((pl.col("close") * pl.col("volume")).alias("dollar_vol"))
        .group_by("symbol")
        .agg(pl.col("dollar_vol").mean().alias("avg_dollar_vol"))
        .filter(pl.col("avg_dollar_vol") >= floor_dollar_vol)
    )
    return set(recent["symbol"].to_list())


# ----------------------------------------------------------------------------- bars


def ingest_bars(
    client: HttpClient,
    symbols: list[str],
    start: dt.date,
    end: dt.date,
) -> None:
    params = {"start": start.isoformat(), "end": end.isoformat()}
    bars_dir = paths.raw_batch_dir("bars", end)
    actions_dir = paths.raw_batch_dir("actions", end)
    manifest = store.Manifest.load_or_create("bars", end, bars_dir, params)
    actions_manifest = store.Manifest.load_or_create("actions", end, actions_dir, params)

    pending = manifest.pending(symbols)
    if not pending:
        print("[ingest] bars already complete for this run; compacting only")
    else:
        print(f"[ingest] {len(pending)} symbols pending ({len(manifest.completed)} already done)")
        _fetch_bars_batched(client, pending, start, end, manifest, actions_manifest)

    before, after = store.compact(bars_dir, paths.BARS_FILE, BARS_SCHEMA, ["symbol", "date"])
    print(f"[ingest] bars store: {before} -> {after} rows")
    a_before, a_after = store.compact(
        actions_dir, paths.ACTIONS_FILE, ACTIONS_SCHEMA, ["symbol", "date", "action"]
    )
    print(f"[ingest] actions store: {a_before} -> {a_after} rows")

    if manifest.permanent_failures:
        print(f"[ingest] {len(manifest.permanent_failures)} symbols permanently unavailable")
    if manifest.transient_failures:
        print(f"[ingest] {len(manifest.transient_failures)} transient failures — re-run to retry")


def _fetch_bars_batched(
    client: HttpClient,
    pending: list[str],
    start: dt.date,
    end: dt.date,
    manifest: store.Manifest,
    actions_manifest: store.Manifest,
) -> None:
    bar_rows: list[pl.DataFrame] = []
    action_rows: list[pl.DataFrame] = []
    done = 0

    def on_error(symbol: str, exc: Exception) -> None:
        permanent = isinstance(exc, HttpError) and exc.permanent
        manifest.record_failure(symbol, str(exc)[:200], permanent)

    def fetch(symbol: str) -> tuple[pl.DataFrame, pl.DataFrame]:
        result = fetch_symbol_chart(client, symbol, start, end)
        return parse_bars(symbol, result, end), parse_actions(symbol, result)

    for symbol, (bars, actions) in client.map(fetch, pending, on_error=on_error):
        if not bars.is_empty():
            bar_rows.append(bars)
        if not actions.is_empty():
            action_rows.append(actions)
        manifest.record_success(symbol)
        done += 1

        if done % BATCH_SYMBOLS == 0:
            _flush(bar_rows, manifest)
            _flush(action_rows, actions_manifest)
            manifest.save()
            actions_manifest.save()
            print(f"[ingest]   {done}/{len(pending)} symbols")

    _flush(bar_rows, manifest)
    _flush(action_rows, actions_manifest)
    manifest.save()
    actions_manifest.save()


def _flush(frames: list[pl.DataFrame], manifest: store.Manifest) -> None:
    """Write the accumulated frames as one batch and reset the buffer.

    Flushing on a fixed symbol count is what bounds the loss from an interrupted run: at
    most one batch of work is discarded, and the manifest is saved immediately after so
    the resume set is accurate.
    """
    if not frames:
        return
    store.write_parquet_atomic(
        pl.concat(frames, how="vertical_relaxed"), manifest.next_batch_path()
    )
    frames.clear()


# ------------------------------------------------------------------------- earnings


def ingest_earnings(client: HttpClient, as_of: dt.date) -> None:
    start = shift_sessions(as_of, -EARNINGS_BACKWARD_SESSIONS)
    end = shift_sessions(as_of, EARNINGS_FORWARD_SESSIONS)

    failures: list[str] = []
    frame = fetch_earnings_range(
        client, start, end, on_error=lambda day, exc: failures.append(f"{day}: {exc}")
    )

    batch_dir = paths.raw_batch_dir("earnings", as_of)
    manifest = store.Manifest.load_or_create(
        "earnings", as_of, batch_dir, {"start": start.isoformat(), "end": end.isoformat()}
    )
    if not frame.is_empty():
        store.write_parquet_atomic(frame, manifest.next_batch_path())
    manifest.save()

    before, after = store.compact(
        batch_dir, paths.EARNINGS_FILE, EARNINGS_SCHEMA, ["symbol", "earnings_date"]
    )
    print(f"[ingest] earnings {start}..{end}: {frame.height} rows fetched, store {before} -> {after}")
    if failures:
        print(f"[ingest] earnings: {len(failures)} date(s) failed — {failures[0]}")


__all__ = [
    "run_ingest",
    "ingest_universe",
    "ingest_bars",
    "ingest_earnings",
    "select_fetch_targets",
    "active_symbols",
    "UNIVERSE_SCHEMA",
]
