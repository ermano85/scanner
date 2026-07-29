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
from qms.ingest.gapfill import run_gapfill
from qms.ingest.http import HttpClient
from qms.ingest.run import run_ingest
from qms.ingest.store import read_parquet_or_empty
from qms.quality import active_universe, check_quality, enforce
from qms.report.build import build_report
from qms.rules.scan_a import run_scan_a


def run_nightly(
    as_of_date: dt.date | None = None,
    skip_ingest: bool = False,
    skip_gapfill: bool = False,
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

    # Repair before features are built, so the quality gate below judges the data the scan
    # will actually use. Isolated in its own try: the fallback source is unofficial too,
    # and a failure here must leave the primary store intact rather than aborting a run
    # whose data may well be fine.
    if not skip_gapfill:
        try:
            run_gapfill(cfg, universe_cfg, expected_session, client=HttpClient())
        except Exception as exc:  # noqa: BLE001 — reported, then the gate decides
            print(f"[nightly] gap-fill failed ({exc}); continuing to the quality gate")

    build_feature_store(cfg=cfg, rebuild=True)

    bars = read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA)
    issues = check_quality(
        bars=bars,
        universe=read_parquet_or_empty(paths.UNIVERSE_FILE, UNIVERSE_SCHEMA),
        actions=read_parquet_or_empty(paths.ACTIONS_FILE, ACTIONS_SCHEMA),
        cfg=cfg,
        expected_session=expected_session,
        # Same population the gap-fill repairs; see gapfill_floor_dollar_vol.
        active=active_universe(bars, universe_cfg.gapfill_floor_dollar_vol),
    )
    enforce(issues, allow_stale=allow_stale)

    result = run_scan_a(as_of_date=as_of, cfg=cfg, echo=True)
    return build_report(as_of_date=as_of, cfg=cfg, result=result)
