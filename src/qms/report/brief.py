"""A compact, self-contained scan summary for discussion.

Written to be pasted into a conversation that has none of this repository's context. Two
consequences shape the format:

* **No charts.** The HTML report inlines 60 base64 PNGs and runs to ~7 MB. This is text,
  and lands around 20 KB for a full shortlist.
* **The caveats live inside the file.** A reader who sees only this document must still
  learn that the scanner emits no buy signals, that sizing figures are pre-open estimates
  computed from the previous session's low, and which numbers are unvalidated
  extrapolation. Caveats that stay behind in the README are caveats that do not travel.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl

from qms.config import ScanConfig
from qms.rules.scan_a import ScanResult

PREAMBLE = """\
## What this is

A nightly mechanical screen over ~11,500 US equities and ETFs, based on Qullamaggie's
published swing-trading approach. It ranks names by momentum and proximity to an entry
level, and computes position sizing. **It does not decide what to buy.**

Read it as "these charts are worth looking at", not "these are trades".

## What the numbers mean

- **ADR%** — average daily range over 20 sessions, the mean of each day's high/low ratio.
  Excludes overnight gaps. It is the "how much does this move on a normal day" measure.
- **ATR** — average true range over 14 sessions, Wilder-smoothed. Unlike ADR it *includes*
  gaps, which is why stops and extension checks use it and ranking does not.
- **Distance to MA** is given in percent and in **ADR units**. The ADR figure is the
  useful one: it says how many average days of movement away the moving average is, which
  makes a $7 stock and a $400 stock comparable. Under ~1.0 means price is sitting on it.
- **Momentum percentile** is cross-sectional against the liquid universe *on this date*,
  not a fixed threshold. 95 means top 5% of tradeable names today.
- **Trigger tags** say why the name surfaced: `AT_10MA`/`AT_20MA`/`AT_50MA` for price at a
  moving average, `AT_PIVOT` for price near a consolidation high.

## Provenance, and what to distrust

- Gates and formulas marked `[DOC]` come from the source material and are treated as
  authoritative: price, dollar volume, ADR, momentum ranking, the 10/20 moving-average
  stack, being above the 50-day, and the earnings blackout.
- The consolidation metrics that influence *ranking order* — tightness, volatility
  contraction, volume dry-up, higher-lows slope — are `[EXT]`: the implementer's
  unvalidated guesswork. They never remove a candidate, and the weights behind them have
  never been tested against anything. **Treat the ordering as a reading order, not a
  ranking of quality.**
- Excluding pharma and biotech is an operator preference, not a rule from the source.

## Limits you should hold in mind

- **Sizing figures are pre-open estimates.** There is no intraday low yet, so the stop is
  computed from the *previous* session's low and the entry from its close. Real numbers
  must be recomputed against the actual session low.
- The scan cannot see whether a consolidation is genuinely tight, whether the sector is in
  favour, or whether the broader market is risk-on. A human looking at the chart can.
- Most of the documented edge in this strategy lives in the exit rules and position
  sizing, neither of which a screener touches.
- Nothing here is financial advice, and no part of this file should be treated as a
  recommendation to buy or sell anything.
"""

CLOSING = """\
---

## If you are being asked for an opinion on this

Useful things to say: which charts merit a closer look and why, what would invalidate the
setup, where the risk sits, what is inconsistent between the numbers and the tags, and
which names look like statistical artefacts rather than real setups.

Please do not treat the ranking as a quality ordering — a large part of it is unvalidated
weighting. And note that no one in this loop can see the actual chart shape, which is the
thing the whole tool exists to send a human to look at.

This is a research and triage tool, not a trading system. Nothing in this file is
financial advice.
"""


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.{digits}f}{suffix}"


def _pct_rank(value) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.0f}p"


def _flags(row: dict) -> list[str]:
    flags = []
    if row.get("extended"):
        flags.append("EXTENDED (close is already more than an ATR above the prior low)")
    if row.get("stop_exceeds_atr"):
        flags.append("STOP > ATR (day-1 stop distance exceeds the ATR)")
    if row.get("earnings_unknown"):
        flags.append("EARNINGS UNKNOWN (no calendar entry — verify before trading)")
    if row.get("sic_unknown"):
        flags.append("SIC UNKNOWN (no SEC industry classification)")
    return flags


def _candidate_block(row: dict, cfg: ScanConfig) -> str:
    tags = row.get("triggers") or "none"
    earnings = row.get("next_earnings_date")
    if earnings is None:
        earnings_text = "none known"
    else:
        when = row.get("earnings_when") or "timing unknown"
        earnings_text = f"{earnings} ({row.get('days_to_earnings')} trading days, {when})"

    lines = [
        f"### {row.get('rank')}. {row['symbol']} — {_fmt(row.get('close'))}  [{tags}]",
        "",
        f"- Character: ADR {_fmt(row.get('adr_pct'), 1, '%')}, "
        f"ATR {_fmt(row.get('atr_14'))}, "
        f"avg $vol {_fmt(row.get('avg_dollar_vol_20'), 0)}",
        f"- Momentum: 1m {_fmt(row.get('gain_1m'), 1, '%')} ({_pct_rank(row.get('gain_1m_pctile'))}), "
        f"3m {_fmt(row.get('gain_3m'), 1, '%')} ({_pct_rank(row.get('gain_3m_pctile'))}), "
        f"6m {_fmt(row.get('gain_6m'), 1, '%')} ({_pct_rank(row.get('gain_6m_pctile'))})",
        f"- Distance to MA: "
        f"10 → {_fmt(row.get('dist_to_sma_10_pct'), 1, '%')} / {_fmt(row.get('dist_to_sma_10_adr'))} ADR; "
        f"20 → {_fmt(row.get('dist_to_sma_20_pct'), 1, '%')} / {_fmt(row.get('dist_to_sma_20_adr'))} ADR; "
        f"50 → {_fmt(row.get('dist_to_sma_50_pct'), 1, '%')} / {_fmt(row.get('dist_to_sma_50_adr'))} ADR",
        f"- Sector: {row.get('sic_description') or 'unclassified'}",
        f"- Earnings: {earnings_text}",
        f"- Sizing (estimate): {_fmt(row.get('shares'), 0)} shares, bound by "
        f"{row.get('binding_cap')}; stop {_fmt(row.get('stop_price'))}, "
        f"risk/share {_fmt(row.get('risk_per_share'))}, "
        f"dollar risk {_fmt(row.get('actual_risk_dollars'), 0)}, "
        f"position {_fmt(row.get('position_dollars'), 0)}",
        f"- Entry zone: preferred {_fmt(row.get('preferred_entry_low'))}–"
        f"{_fmt(row.get('preferred_entry_high'))}, max {_fmt(row.get('max_entry'))}",
    ]

    flags = _flags(row)
    lines.append(f"- Flags: {'; '.join(flags) if flags else 'none'}")
    lines.append("")
    return "\n".join(lines)


def render_brief(result: ScanResult, cfg: ScanConfig) -> str:
    sizing = cfg.sizing
    risk_dollars = sizing.account * sizing.risk_pct

    parts = [
        f"# Qullamaggie scan — watchlist for {result.as_of_date}",
        "",
        f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} from bars through "
        f"**{result.data_date}**.",
        "",
    ]

    if result.is_stale:
        parts += [
            f"> **Warning: the data is {result.staleness_sessions} session(s) stale.** The "
            f"newest bar is {result.data_date} but this watchlist is for "
            f"{result.as_of_date}. Every price below is out of date by that much.",
            "",
        ]

    parts += [
        PREAMBLE,
        "## This run",
        "",
        f"- Universe scanned: {result.universe_size:,}",
        f"- Passed liquidity (price, dollar volume, ADR): {result.after_liquidity:,}",
        f"- Final candidates: {result.candidates.height}",
        f"- Account {_fmt(sizing.account, 0)}, risking {sizing.risk_pct:.2%} "
        f"= {_fmt(risk_dollars, 0)} per trade",
        "",
        "Removed by each gate (counts overlap — a name can fail several):",
        "",
    ]
    parts += [f"- {gate}: {count:,}" for gate, count in sorted(result.rejections.items())]
    parts.append("")

    if result.candidates.is_empty():
        parts += ["## Candidates", "", "None survived the gates.", "", CLOSING]
        return "\n".join(parts)

    parts += ["## Candidates", ""]
    parts += [_candidate_block(row, cfg) for row in result.candidates.iter_rows(named=True)]
    parts.append(CLOSING)
    return "\n".join(parts)


BRIEF_JSON_COLUMNS = [
    "rank", "symbol", "date", "close", "adr_pct", "atr_14",
    "gain_1m", "gain_3m", "gain_6m",
    "gain_1m_pctile", "gain_3m_pctile", "gain_6m_pctile", "momentum_pctile",
    "dist_to_sma_10_pct", "dist_to_sma_10_adr",
    "dist_to_sma_20_pct", "dist_to_sma_20_adr",
    "dist_to_sma_50_pct", "dist_to_sma_50_adr",
    "triggers", "sic", "sic_description",
    "next_earnings_date", "earnings_when", "days_to_earnings",
    "avg_vol_20", "avg_dollar_vol_20",
    "shares", "binding_cap", "stop_price", "risk_per_share", "actual_risk_dollars",
    "position_dollars", "preferred_entry_low", "preferred_entry_high", "max_entry",
    "extended", "stop_exceeds_atr", "score",
]


def render_brief_json(result: ScanResult, cfg: ScanConfig) -> str:
    available = [c for c in BRIEF_JSON_COLUMNS if c in result.candidates.columns]
    rows = (
        result.candidates.select(available).to_dicts()
        if not result.candidates.is_empty()
        else []
    )
    payload = {
        "disclaimer": (
            "Mechanical screen output, not investment advice and not a buy signal. "
            "Sizing figures are pre-open estimates computed from the previous session's "
            "low and close, and must be recomputed intraday."
        ),
        "as_of_date": str(result.as_of_date),
        "data_date": str(result.data_date),
        "stale_sessions": result.staleness_sessions,
        "universe_size": result.universe_size,
        "after_liquidity": result.after_liquidity,
        "rejections": result.rejections,
        "sizing_config": {
            "account": cfg.sizing.account,
            "risk_pct": cfg.sizing.risk_pct,
            "risk_dollars_per_trade": cfg.sizing.account * cfg.sizing.risk_pct,
        },
        "candidates": [
            {k: (str(v) if isinstance(v, dt.date) else v) for k, v in row.items()}
            for row in rows
        ],
    }
    return json.dumps(payload, indent=2, default=str)


def write_brief(result: ScanResult, cfg: ScanConfig, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / "claude-brief.md"
    markdown_path.write_text(render_brief(result, cfg), encoding="utf-8")
    (out_dir / "claude-brief.json").write_text(
        render_brief_json(result, cfg), encoding="utf-8"
    )
    size_kb = markdown_path.stat().st_size / 1024
    print(f"[brief] {result.candidates.height} candidate(s) -> {markdown_path} ({size_kb:.0f} KB)")
    return markdown_path
