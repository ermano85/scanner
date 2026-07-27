"""Per-candidate daily charts.

Spec §7: "The chart matters more than any of the columns. The entire point of v1 is fast
visual triage." So this module gets the care, and the columns are supporting evidence.

One PNG per survivor — not per universe member. Rendering 13,000 charts nightly would
take longer than the rest of the pipeline combined and nobody would look at them.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

# Must be set before pyplot is imported: the nightly job has no display.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import polars as pl  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from qms.config import ScanConfig  # noqa: E402

# Roughly a trading month, used to convert the configured chart span into bars.
SESSIONS_PER_MONTH = 21

_SMA_COLOURS = {10: "#2563eb", 20: "#f59e0b", 50: "#dc2626", 200: "#7c3aed"}

_STYLE = mpf.make_mpf_style(
    base_mpf_style="yahoo",
    marketcolors=mpf.make_marketcolors(
        up="#16a34a",
        down="#dc2626",
        edge={"up": "#16a34a", "down": "#dc2626"},
        wick={"up": "#16a34a", "down": "#dc2626"},
        volume={"up": "#86efac", "down": "#fca5a5"},
    ),
    gridstyle=":",
    gridcolor="#e5e7eb",
    facecolor="#ffffff",
    figcolor="#ffffff",
    rc={"font.size": 9},
)


def render_chart(
    bars: pl.DataFrame,
    symbol: str,
    cfg: ScanConfig,
    out_path: Path,
    subtitle: str = "",
    pivot_price: float | None = None,
    stop_price: float | None = None,
) -> Path:
    """Render one candidate's chart and return the path written.

    `bars` is that symbol's history; the last `chart_months` of it is drawn. SMAs are
    computed on the **full** history passed in and then sliced, so the 50-day average is
    correct at the left edge instead of starting blank.
    """
    history = bars.filter(pl.col("symbol") == symbol).sort("date")
    if history.is_empty():
        raise ValueError(f"no bars for {symbol}")

    frame = history.select("date", "open", "high", "low", "close", "volume").to_pandas()
    frame = frame.set_index("date")
    frame.index.name = "Date"
    frame.columns = ["Open", "High", "Low", "Close", "Volume"]

    overlays = []
    for period in cfg.report.chart_sma:
        # min_periods=period keeps a partially-filled average off the chart rather than
        # drawing a misleading line over the first few sessions.
        overlays.append(frame["Close"].rolling(period, min_periods=period).mean())

    window = cfg.report.chart_months * SESSIONS_PER_MONTH
    frame = frame.tail(window)
    overlays = [series.tail(window) for series in overlays]

    addplots = [
        mpf.make_addplot(
            series,
            color=_SMA_COLOURS.get(period, "#6b7280"),
            width=1.1,
            label=f"SMA {period}",
        )
        for period, series in zip(cfg.report.chart_sma, overlays, strict=True)
        if series.notna().any()
    ]

    horizontals: list[float] = []
    hcolors: list[str] = []
    if pivot_price is not None:
        horizontals.append(float(pivot_price))
        hcolors.append("#0f766e")
    if stop_price is not None:
        horizontals.append(float(stop_price))
        hcolors.append("#b91c1c")

    kwargs = {
        "type": "candle",
        "style": _STYLE,
        "volume": True,
        "addplot": addplots,
        "figsize": (
            cfg.report.chart_width_px / cfg.report.chart_dpi,
            cfg.report.chart_height_px / cfg.report.chart_dpi,
        ),
        "panel_ratios": (4, 1),
        "tight_layout": True,
        "returnfig": True,
        "warn_too_much_data": len(frame) + 1,
        "datetime_format": "%b %d",
        "xrotation": 0,
    }
    if horizontals:
        kwargs["hlines"] = {
            "hlines": horizontals,
            "colors": hcolors,
            "linestyle": "--",
            "linewidths": 1.0,
        }

    fig, axes = mpf.plot(frame, **kwargs)

    # set_title on the price axes rather than fig.suptitle: suptitle floats over the axes
    # and collides with any hline drawn near the top of the range.
    title = symbol if not subtitle else f"{symbol}   {subtitle}"
    axes[0].set_title(title, loc="left", fontsize=11, fontweight="bold", pad=10)

    # Build the legend from explicit proxies. Passing bare labels to ax.legend() attaches
    # them to whatever artists matplotlib finds first — which here are the candle bodies
    # and the hlines, so the colours come out wrong and meaningless.
    handles = [
        Line2D([0], [0], color=_SMA_COLOURS.get(period, "#6b7280"), lw=1.1, label=f"SMA {period}")
        for period, series in zip(cfg.report.chart_sma, overlays, strict=True)
        if series.notna().any()
    ]
    if pivot_price is not None:
        handles.append(Line2D([0], [0], color="#0f766e", lw=1.0, ls="--", label="pivot high"))
    if stop_price is not None:
        handles.append(Line2D([0], [0], color="#b91c1c", lw=1.0, ls="--", label="stop"))
    if handles:
        axes[0].legend(handles=handles, loc="upper left", fontsize=8, frameon=False)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=cfg.report.chart_dpi, bbox_inches="tight", facecolor="#ffffff")
    plt.close(fig)
    return out_path
