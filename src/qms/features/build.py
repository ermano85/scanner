"""Build the feature store from the bar store.

Spec §1: features are computed once and **written back to disk**, never recomputed during
rule tuning. That separation is what lets thresholds be changed and the scan re-run in
seconds instead of minutes, which in turn is what makes it practical to actually look at
several hundred charts before touching a weight.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms import paths
from qms.config import ScanConfig, load_scan_config
from qms.features.registry import all_features, build_features as compute_features
from qms.ingest.base import BARS_SCHEMA
from qms.ingest.store import read_parquet_or_empty, write_parquet_atomic

# Rows kept per symbol in the stored feature frame. The scan only ever reads the latest
# row, but charts need six months and the causality cross-check needs to re-run a scan
# for an earlier date, so a year of slack is cheap insurance.
RETAIN_ROWS_PER_SYMBOL = 320


def build_feature_store(
    rebuild: bool = False,
    cfg: ScanConfig | None = None,
    retain_rows: int = RETAIN_ROWS_PER_SYMBOL,
) -> pl.DataFrame:
    """Recompute the feature store from the bar store.

    `rebuild=False` skips the work when the stored features are already newer than the
    bars they derive from. There is no incremental path on purpose: a partial feature
    update is how a store ends up holding rows computed under two different configs,
    which is exactly the kind of silent inconsistency the causality suite cannot catch.
    Recomputing everything takes seconds and is always correct.
    """
    cfg = cfg or load_scan_config()
    paths.ensure_dirs()

    bars = read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA)
    if bars.is_empty():
        raise RuntimeError("bar store is empty — run `qms ingest --backfill` first")

    if not rebuild and _features_are_current():
        print("[features] store is newer than the bars; pass --rebuild to force")
        return pl.read_parquet(paths.FEATURES_FILE)

    print(f"[features] {bars.height} bars across {bars['symbol'].n_unique()} symbols")

    features = compute_features(bars, cfg)

    # Trim the *head* of each symbol's history, never the tail. Dropping trailing rows
    # would silently change what "latest" means; dropping leading rows only discards
    # already-consumed warm-up.
    if retain_rows:
        features = (
            features.sort(["symbol", "date"]).group_by("symbol", maintain_order=True).tail(retain_rows)
        )

    write_parquet_atomic(features, paths.FEATURES_FILE)
    feature_names = sorted(all_features())
    print(f"[features] wrote {features.height} rows x {len(feature_names)} features")
    _report_coverage(features, feature_names)
    return features


def _features_are_current() -> bool:
    if not paths.FEATURES_FILE.exists() or not paths.BARS_FILE.exists():
        return False
    return paths.FEATURES_FILE.stat().st_mtime >= paths.BARS_FILE.stat().st_mtime


def _report_coverage(features: pl.DataFrame, names: list[str]) -> None:
    """Print how much of the latest cross-section each feature actually covers.

    A feature that is null for most symbols on the newest bar is usually a warm-up
    problem — not enough history ingested — and it will quietly empty a gate.
    """
    latest = features.group_by("symbol").tail(1)
    if latest.is_empty():
        return
    thin = [
        (name, latest[name].null_count() / latest.height)
        for name in names
        if latest[name].null_count() / latest.height > 0.5
    ]
    if thin:
        print(f"[features] WARNING: {len(thin)} feature(s) null for >50% of symbols on the latest bar")
        for name, share in sorted(thin, key=lambda kv: -kv[1])[:10]:
            print(f"[features]   {name}: {share:.0%} null")


def load_feature_store(as_of_date: dt.date | None = None) -> pl.DataFrame:
    """Read the feature store, optionally filtered to bars strictly before `as_of_date`.

    `as_of_date` is the session the watchlist is **for**. Bars strictly before it are
    therefore exactly the information available when the scan runs — the Monday-evening
    run passes Tuesday and legitimately sees Monday's close. Spec §1.
    """
    if not paths.FEATURES_FILE.exists():
        raise RuntimeError("feature store not found — run `qms features --rebuild` first")
    features = pl.read_parquet(paths.FEATURES_FILE)
    if as_of_date is not None:
        features = features.filter(pl.col("date") < as_of_date)
    return features


# Kept for the CLI's import path.
build_features = build_feature_store
