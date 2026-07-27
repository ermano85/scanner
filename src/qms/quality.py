"""Data-quality gate.

Two of the four v1 feeds are unofficial endpoints with no contract, and a whole-market
missing session has already been observed in the wild (see docs/DATA.md, 2026-07-24). The
purpose of this module is to make that kind of failure **loud**.

A scanner that quietly ranks last week's data is worse than one that refuses to run: the
output looks completely normal, and there is nothing in a candidate list that says "these
prices are three days old".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import polars as pl

from qms.calendar import shift_sessions, trading_days_between
from qms.config import ScanConfig

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


class DataQualityError(RuntimeError):
    def __init__(self, issues: list[QualityIssue]):
        self.issues = issues
        super().__init__(
            "data-quality gate failed:\n  " + "\n  ".join(str(i) for i in issues)
        )


def effective_latest_session(bars: pl.DataFrame, min_coverage: float) -> dt.date | None:
    """The most recent session where enough of the universe actually has a bar.

    The vendor's trailing edge is ragged: a handful of symbols routinely carry a bar for a
    session that is missing for everyone else. Any logic keyed on `max(date)` is therefore
    wrong in two ways at once — it declares stale data fresh, and it builds a
    cross-section that mixes two different sessions. This is the honest reference date.
    """
    if bars.is_empty():
        return None
    total = bars["symbol"].n_unique()
    if not total:
        return None

    per_date = (
        bars.group_by("date")
        .agg(pl.col("symbol").n_unique().alias("symbols"))
        .filter(pl.col("symbols") >= min_coverage * total)
    )
    if per_date.is_empty():
        return None
    return per_date["date"].max()


def check_quality(
    bars: pl.DataFrame,
    universe: pl.DataFrame,
    actions: pl.DataFrame,
    cfg: ScanConfig,
    expected_session: dt.date,
) -> list[QualityIssue]:
    """Every issue found, worst first. Empty list means the data is fit to scan."""
    issues: list[QualityIssue] = []
    quality = cfg.quality

    if bars.is_empty():
        return [QualityIssue("empty_store", SEVERITY_ERROR, "the bar store has no rows")]

    symbol_count = bars["symbol"].n_unique()
    if symbol_count < quality.min_symbols:
        issues.append(
            QualityIssue(
                "thin_universe",
                SEVERITY_ERROR,
                f"only {symbol_count} symbols in the bar store, expected at least "
                f"{quality.min_symbols} — the ingest probably did not finish",
            )
        )

    # --- staleness -------------------------------------------------------------
    # Deliberately NOT max(date). Observed 2026-07-24: 42 of 11,574 symbols carried a bar
    # for a session the vendor was otherwise missing entirely. Taking the raw maximum
    # reported the data as fresh while 99.6% of the market was a day behind. Staleness is
    # therefore measured against the newest *well-covered* session.
    newest_raw = bars["date"].max()
    newest = effective_latest_session(bars, quality.min_universe_coverage)

    if newest is None:
        issues.append(
            QualityIssue(
                "thin_coverage",
                SEVERITY_ERROR,
                f"no session reaches {quality.min_universe_coverage:.0%} coverage; the "
                "ingest is incomplete",
            )
        )
        newest = newest_raw
    elif newest != newest_raw:
        ragged = bars.filter(pl.col("date") > newest)["symbol"].n_unique()
        issues.append(
            QualityIssue(
                "ragged_edge",
                SEVERITY_WARNING,
                f"{ragged} symbol(s) have bars after {newest}, the newest well-covered "
                f"session (raw max is {newest_raw}). Those bars are excluded from the "
                "scan's cross-section so it compares one session against itself",
            )
        )

    staleness = max(0, trading_days_between(newest, expected_session))
    if staleness > quality.max_staleness_sessions:
        issues.append(
            QualityIssue(
                "stale_data",
                SEVERITY_ERROR,
                f"newest well-covered bar is {newest} but the last completed session is "
                f"{expected_session} — {staleness} session(s) missing. The vendor has a "
                "hole; re-run ingest, and see docs/DATA.md",
            )
        )

    # --- structural nulls ------------------------------------------------------
    null_counts = {
        column: bars[column].null_count()
        for column in ("open", "high", "low", "close", "volume")
    }
    if any(null_counts.values()):
        issues.append(
            QualityIssue(
                "null_ohlcv",
                SEVERITY_ERROR,
                f"null OHLCV values reached the store: {null_counts} — the parser is "
                "supposed to drop these",
            )
        )

    inverted = bars.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("close") > pl.col("high"))
        | (pl.col("close") < pl.col("low"))
    ).height
    if inverted:
        issues.append(
            QualityIssue(
                "impossible_bars",
                SEVERITY_ERROR,
                f"{inverted} bar(s) violate low <= close <= high",
            )
        )

    # --- unexplained jumps -----------------------------------------------------
    issues.extend(_check_jumps(bars, actions, cfg, newest))

    # --- universe drift --------------------------------------------------------
    if not universe.is_empty():
        stored = set(bars["symbol"].unique().to_list())
        listed = set(universe["symbol"].to_list())
        missing_share = len(listed - stored) / len(listed) if listed else 0.0
        if missing_share > (1.0 - cfg.quality.min_universe_coverage):
            issues.append(
                QualityIssue(
                    "universe_gap",
                    SEVERITY_WARNING,
                    f"{missing_share:.0%} of the listed universe has no bars at all — "
                    "expected after a partial backfill, suspicious otherwise",
                )
            )

    return sorted(issues, key=lambda i: i.severity != SEVERITY_ERROR)


def _check_jumps(
    bars: pl.DataFrame,
    actions: pl.DataFrame,
    cfg: ScanConfig,
    newest: dt.date,
) -> list[QualityIssue]:
    """Large overnight moves with no split to explain them.

    A missed split adjustment shows up exactly like this and would otherwise sail through
    the momentum ranking as a spectacular gainer. Some real names genuinely move this
    much, so a share of the universe is tolerated before it counts as an error.
    """
    quality = cfg.quality
    window_start = shift_sessions(newest, -quality.jump_lookback_sessions)

    recent = bars.filter(pl.col("date") >= window_start).sort(["symbol", "date"])
    if recent.is_empty():
        return []

    moves = recent.with_columns(
        (
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).abs() * 100.0
        ).alias("move_pct")
    ).filter(pl.col("move_pct") >= quality.jump_threshold_pct)

    if moves.is_empty():
        return []

    if not actions.is_empty():
        splits = actions.filter(pl.col("action") == "split").select(
            "symbol", pl.col("date").alias("split_date")
        )
        moves = moves.join(
            splits, left_on=["symbol", "date"], right_on=["symbol", "split_date"], how="anti"
        )

    if moves.is_empty():
        return []

    affected = moves["symbol"].n_unique()
    share = affected / max(1, bars["symbol"].n_unique())
    severity = (
        SEVERITY_ERROR if share > quality.max_unexplained_jump_share else SEVERITY_WARNING
    )
    examples = ", ".join(moves["symbol"].unique().to_list()[:5])
    return [
        QualityIssue(
            "unexplained_jump",
            severity,
            f"{affected} symbol(s) moved >={quality.jump_threshold_pct:.0f}% in a session "
            f"with no split on record ({share:.1%} of the store; e.g. {examples})",
        )
    ]


def enforce(issues: list[QualityIssue], allow_stale: bool = False) -> None:
    """Raise on any error-severity issue. Warnings are printed and tolerated."""
    for issue in issues:
        print(f"[quality] {issue}")

    blocking = [i for i in issues if i.severity == SEVERITY_ERROR]
    if allow_stale:
        blocking = [i for i in blocking if i.code != "stale_data"]
        if any(i.code == "stale_data" for i in issues):
            print("[quality] proceeding on stale data because --allow-stale was passed")

    if blocking:
        raise DataQualityError(blocking)

    if not issues:
        print("[quality] all checks passed")
