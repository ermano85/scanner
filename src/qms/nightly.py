"""The whole pipeline in one command: ingest -> features -> quality gate -> scan -> report.

The quality gate sits deliberately *between* the feature build and the scan. Running it
earlier would miss problems that only appear once features are computed; running it later
would mean the report has already been written by the time anyone finds out the data was
three days old.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from qms import paths
from qms.calendar import last_completed_session, next_session
from qms.config import load_scan_config, load_universe_config
from qms.features.build import build_feature_store
from qms.ingest.base import ACTIONS_SCHEMA, BARS_SCHEMA, UNIVERSE_SCHEMA
from qms.ingest.http import HttpClient
from qms.ingest.run import run_ingest
from qms.ingest.store import read_parquet_or_empty
from qms.quality import check_quality, enforce
from qms.report.build import build_report
from qms.rules.scan_a import run_scan_a


def run_nightly(
    as_of_date: dt.date | None = None,
    skip_ingest: bool = False,
    full_universe: bool = False,
    allow_stale: bool = False,
) -> Path:
    cfg = load_scan_config()
    universe_cfg = load_universe_config()

    expected_session = last_completed_session()
    as_of = as_of_date or next_session(expected_session)
    print(f"[nightly] last completed session {expected_session}; watchlist for {as_of}")

    if not skip_ingest:
        run_ingest(
            full_universe=full_universe,
            scan_cfg=cfg,
            universe_cfg=universe_cfg,
            client=HttpClient(),
        )
    else:
        print("[nightly] skipping ingest")

    build_feature_store(cfg=cfg)

    issues = check_quality(
        bars=read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA),
        universe=read_parquet_or_empty(paths.UNIVERSE_FILE, UNIVERSE_SCHEMA),
        actions=read_parquet_or_empty(paths.ACTIONS_FILE, ACTIONS_SCHEMA),
        cfg=cfg,
        expected_session=expected_session,
    )
    enforce(issues, allow_stale=allow_stale)

    result = run_scan_a(as_of_date=as_of, cfg=cfg, echo=True)
    return build_report(as_of_date=as_of, cfg=cfg, result=result)
