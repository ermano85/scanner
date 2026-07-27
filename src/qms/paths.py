"""Canonical on-disk layout. Every module resolves paths through here, never by literal."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
OUT_DIR = REPO_ROOT / "out"

RAW_DIR = DATA_DIR / "raw"
BARS_DIR = DATA_DIR / "bars"
UNIVERSE_DIR = DATA_DIR / "universe"
EARNINGS_DIR = DATA_DIR / "earnings"
ACTIONS_DIR = DATA_DIR / "actions"
FEATURES_DIR = DATA_DIR / "features"

BARS_FILE = BARS_DIR / "bars.parquet"
UNIVERSE_FILE = UNIVERSE_DIR / "universe.parquet"
EARNINGS_FILE = EARNINGS_DIR / "earnings.parquet"
ACTIONS_FILE = ACTIONS_DIR / "actions.parquet"
FEATURES_FILE = FEATURES_DIR / "features.parquet"


def raw_batch_dir(kind: str, run_date: dt.date) -> Path:
    """Landing zone for as-fetched vendor payloads, partitioned by kind and run date."""
    return RAW_DIR / kind / f"date={run_date.isoformat()}"


def scan_out_dir(as_of: dt.date) -> Path:
    return OUT_DIR / as_of.isoformat()


def ensure_dirs() -> None:
    for path in (
        RAW_DIR,
        BARS_DIR,
        UNIVERSE_DIR,
        EARNINGS_DIR,
        ACTIONS_DIR,
        FEATURES_DIR,
        OUT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
