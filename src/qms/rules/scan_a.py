"""Scan A — the breakout watchlist. Spec §4.

Runs after the close; the output is tomorrow's watchlist.

**`as_of_date` is the session the watchlist is FOR**, and the feature store is filtered to
bars *strictly before* it. So the Monday-evening run passes Tuesday and legitimately sees
Monday's close. Stating this precisely matters more than it looks: the off-by-one here is
exactly what silently invalidates a backtest built on top of this engine later, and by
then it is undetectable by inspection.

Pipeline order is deliberate:

    latest cross-section
      -> liquidity gates      (price, $volume, ADR)
      -> momentum percentiles (ranked within the SURVIVORS of the above, not the world)
      -> momentum gate
      -> trend gates          (MA stack k-of-m, above the 50)
      -> earnings blackout
      -> trigger tags
      -> score and rank
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import polars as pl

from qms import paths
from qms.calendar import (
    last_completed_session,
    next_session,
    shift_sessions,
    trading_days_between,
)
from qms.config import ScanConfig, load_scan_config
from qms.features.build import load_feature_store
from qms.ingest.base import EARNINGS_SCHEMA
from qms.ingest.store import read_parquet_or_empty
from qms.quality import effective_latest_session
from qms.rules import gates, rank, triggers
from qms.sizing.calculator import add_sizing


@dataclass
class ScanResult:
    as_of_date: dt.date
    data_date: dt.date | None
    candidates: pl.DataFrame
    universe_size: int
    after_liquidity: int
    rejections: dict[str, int]
    staleness_sessions: int
    dropped_stale_symbols: int = 0

    @property
    def is_stale(self) -> bool:
        return self.staleness_sessions > 0


def resolve_as_of(as_of_date: dt.date | None) -> dt.date:
    """Default `as_of_date` to the next session after the last completed one.

    Running this in the evening produces tomorrow's watchlist, which is what the scan is
    for. Passing an explicit date is what makes re-running a historical scan possible.
    """
    if as_of_date is not None:
        return as_of_date
    return next_session(last_completed_session())


def latest_cross_section(
    features: pl.DataFrame,
    cfg: ScanConfig | None = None,
) -> tuple[pl.DataFrame, dt.date | None, int]:
    """One row per symbol, all drawn from the same session.

    Returns `(frame, reference_date, dropped)`.

    Two things are going on, both learned from real data rather than theory:

    **The vendor's trailing edge is ragged.** On 2026-07-24, 42 of 11,574 symbols had a
    bar for a session missing for everyone else. Ranking those 42 — carrying one extra
    day of return — against the rest of the market on the prior close is a quiet,
    systematic distortion of every cross-sectional percentile. So the reference date is
    the newest *well-covered* session, and later bars are ignored.

    **Symbols go quiet.** A halted or barely-traded name keeps its last bar forever, and
    without a freshness check it sits in the cross-section with months-old prices and can
    pass every gate. Anything whose most recent bar is more than
    `quality.max_bar_age_sessions` behind the reference is dropped.
    """
    if features.is_empty():
        return features, None, 0

    cfg = cfg or load_scan_config()
    reference = effective_latest_session(features, cfg.quality.min_universe_coverage)
    if reference is None:
        reference = features["date"].max()

    trimmed = features.filter(pl.col("date") <= reference)
    latest = trimmed.sort(["symbol", "date"]).group_by("symbol", maintain_order=True).tail(1)

    oldest_allowed = shift_sessions(reference, -cfg.quality.max_bar_age_sessions)
    fresh = latest.filter(pl.col("date") >= oldest_allowed)
    return fresh, reference, latest.height - fresh.height


def run_scan_a(
    as_of_date: dt.date | None = None,
    cfg: ScanConfig | None = None,
    features: pl.DataFrame | None = None,
    earnings: pl.DataFrame | None = None,
    echo: bool = False,
) -> ScanResult:
    cfg = cfg or load_scan_config()
    as_of = resolve_as_of(as_of_date)

    if features is None:
        features = load_feature_store(as_of_date=as_of)
    else:
        features = features.filter(pl.col("date") < as_of)

    if earnings is None:
        earnings = read_parquet_or_empty(paths.EARNINGS_FILE, EARNINGS_SCHEMA)

    latest, data_date, dropped_stale = latest_cross_section(features, cfg)
    universe_size = latest.height

    # How far behind the scan date the reference session actually is. Non-zero means the
    # vendor is missing sessions — see docs/DATA.md, where a real whole-market hole is
    # recorded. The -1 is because as_of is the session the watchlist is FOR, so the
    # newest bar is expected to be exactly one session behind it.
    staleness = 0
    if data_date is not None:
        staleness = max(0, trading_days_between(data_date, as_of) - 1)

    # 1. Liquidity first, so percentiles are computed over a tradeable population.
    latest = gates.apply_liquidity_gates(latest, cfg)
    # Counted on the pre-filter frame: measuring them afterwards would report zero for
    # every liquidity gate, since only survivors remain by then.
    rejections = gates.rejection_summary(latest)
    liquid = gates.survivors(latest)
    after_liquidity = liquid.height

    # 2. Momentum percentiles within that population, fresh for this scan date.
    liquid = rank.add_momentum_percentiles(liquid)
    liquid = gates.apply_momentum_gate(liquid, cfg)

    # 3. Remaining [DOC] gates.
    liquid = gates.apply_trend_gates(liquid, cfg)
    liquid = gates.attach_earnings(liquid, earnings, cfg, as_of)

    rejections.update(
        {
            gate: count
            for gate, count in gates.rejection_summary(liquid).items()
            if gate not in rejections
        }
    )
    passing = gates.survivors(liquid)

    # 4. Explain, rank, and size the survivors.
    passing = triggers.add_trigger_tags(passing, cfg)
    passing = rank.add_score(passing, cfg)
    candidates = rank.rank_candidates(passing, cfg)
    candidates = add_sizing(candidates, cfg)

    result = ScanResult(
        as_of_date=as_of,
        data_date=data_date,
        candidates=candidates,
        universe_size=universe_size,
        after_liquidity=after_liquidity,
        rejections=rejections,
        staleness_sessions=staleness,
        dropped_stale_symbols=dropped_stale,
    )

    if echo:
        # Presentation lives in the output layer, not here — spec §1 keeps the four
        # layers strictly separated, and console formatting constants are exactly the
        # kind of thing that should never appear in rule code.
        from qms.report.console import echo_scan_result

        echo_scan_result(result, cfg)
    return result
