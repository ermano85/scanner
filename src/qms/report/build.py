"""Render a scan result: charts, a self-contained HTML page, and a CSV.

Spec §7. The chart is the product; the columns are supporting evidence. PNGs are inlined
as base64 so a scan directory is one portable file that survives being emailed to
yourself, and the CSV is there for anything that wants the numbers without the pictures.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path

import polars as pl
from jinja2 import Environment, FileSystemLoader, select_autoescape

from qms import paths
from qms.config import ScanConfig, load_scan_config
from qms.ingest.base import BARS_SCHEMA
from qms.ingest.store import read_parquet_or_empty
from qms.report.brief import write_brief
from qms.report.charts import render_chart
from qms.report.tradingview import write_watchlist
from qms.rules.scan_a import ScanResult, run_scan_a

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "scan.html.j2"

CSV_COLUMNS = [
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


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value, digits: int = 0) -> str:
    """Percent-suffixed number that degrades to a bare dash rather than '—%'."""
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}%"


def _pctile(value) -> str:
    """Render a 0-1 percentile as a whole number, tolerating null.

    A recently listed name has no 6-month return, so its percentile is legitimately null.
    Arithmetic in the template would crash on it — which it did, on the first real run.
    """
    if value is None:
        return "—"
    return f"{float(value) * 100:.0f}"


def _money(value) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(amount) >= threshold:
            return f"${amount / threshold:,.1f}{suffix}"
    return f"${amount:,.0f}"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["num"] = _num
    env.filters["money"] = _money
    env.filters["pctile"] = _pctile
    env.filters["pct"] = _pct
    return env


def _earnings_label(row: dict) -> str:
    date = row.get("next_earnings_date")
    if date is None:
        return "unknown"
    days = row.get("days_to_earnings")
    return f"{date} ({days}d)" if days is not None else str(date)


def build_report(
    as_of_date: dt.date | None = None,
    cfg: ScanConfig | None = None,
    result: ScanResult | None = None,
    bars: pl.DataFrame | None = None,
) -> Path:
    cfg = cfg or load_scan_config()
    result = result or run_scan_a(as_of_date=as_of_date, cfg=cfg)
    bars = bars if bars is not None else read_parquet_or_empty(paths.BARS_FILE, BARS_SCHEMA)

    out_dir = paths.scan_out_dir(result.as_of_date)
    charts_dir = out_dir / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    adr_column = f"adr_pct_{cfg.features.adr.primary}"
    candidates = result.candidates
    if not candidates.is_empty() and adr_column in candidates.columns:
        candidates = candidates.with_columns(pl.col(adr_column).alias("adr_pct"))
        # Push the alias back onto the result too — the brief and the CSV both read
        # `adr_pct`, and leaving it local meant the brief printed "ADR n/a" for every row.
        result.candidates = candidates

    rows: list[dict] = []
    for row in candidates.iter_rows(named=True):
        symbol = row["symbol"]
        row = dict(row)
        row["earnings_label"] = _earnings_label(row)

        chart_path = charts_dir / f"{symbol}.png"
        try:
            render_chart(
                bars,
                symbol,
                cfg,
                chart_path,
                subtitle=(
                    f"close {_num(row.get('close'))}  "
                    f"ADR {_num(row.get('adr_pct'), 1)}%  "
                    f"ATR {_num(row.get('atr_14'))}  "
                    f"3m {_num(row.get('gain_3m'), 0)}%"
                ),
                pivot_price=row.get(f"pivot_high_{cfg.scan_a.ranking.score_bucket}"),
                stop_price=row.get("stop_price"),
            )
            encoded = base64.b64encode(chart_path.read_bytes()).decode("ascii")
            row["chart_uri"] = f"data:image/png;base64,{encoded}"
        except Exception as exc:  # noqa: BLE001 — one bad chart must not kill the report
            print(f"[report] chart failed for {symbol}: {exc}")
            row["chart_uri"] = None

        rows.append(row)

    html = _environment().get_template(TEMPLATE_NAME).render(
        as_of=result.as_of_date,
        data_date=result.data_date,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        candidates=rows,
        universe_size=result.universe_size,
        after_liquidity=result.after_liquidity,
        rejections=result.rejections,
        stale=result.is_stale,
        staleness=result.staleness_sessions,
    )

    html_path = out_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")

    available = [c for c in CSV_COLUMNS if c in candidates.columns]
    csv_path = out_dir / "ranked.csv"
    if candidates.is_empty():
        pl.DataFrame(schema={c: pl.Utf8 for c in available or ["symbol"]}).write_csv(csv_path)
    else:
        candidates.select(available).write_csv(csv_path)

    # Both exports are derived from the same ranked frame, so they can never disagree with
    # the HTML about what tonight's list is.
    write_brief(result, cfg, out_dir, bars=bars)
    write_watchlist(candidates, result.as_of_date, out_dir)

    print(f"[report] {len(rows)} candidate(s) -> {html_path}")
    return html_path
