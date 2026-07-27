"""Chart rendering and the HTML/CSV report."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.features.registry import build_features
from qms.report.build import _money, _num, _pctile, build_report
from qms.report.charts import render_chart
from qms.rules.scan_a import run_scan_a

CFG = load_scan_config()
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _bars(symbol: str = "AAA", n: int = 200, drift: float = 0.005) -> pl.DataFrame:
    closes, highs, lows = [], [], []
    price = 100.0
    for _ in range(n):
        price *= 1.0 + drift
        closes.append(price)
        highs.append(price * 1.03)
        lows.append(price * 0.97)

    dates: list[dt.date] = []
    cursor = dt.date(2026, 7, 23)
    while len(dates) < n:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= dt.timedelta(days=1)
    dates.reverse()

    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [5_000_000.0] * n,
            "adjclose": closes,
        },
        schema_overrides={"date": pl.Date},
    )


# ---------------------------------------------------------------------------- charts


def test_render_chart_writes_a_real_png(tmp_path):
    out = render_chart(_bars(), "AAA", CFG, tmp_path / "AAA.png")
    assert out.exists()
    payload = out.read_bytes()
    assert payload.startswith(PNG_MAGIC)
    assert len(payload) > 10_000, "a chart this empty is probably a blank canvas"


def test_render_chart_accepts_reference_lines(tmp_path):
    out = render_chart(
        _bars(), "AAA", CFG, tmp_path / "AAA.png", pivot_price=250.0, stop_price=200.0
    )
    assert out.read_bytes().startswith(PNG_MAGIC)


def test_render_chart_survives_a_short_history(tmp_path):
    """A recent listing has fewer bars than the longest SMA; it must still draw."""
    out = render_chart(_bars(n=15), "AAA", CFG, tmp_path / "AAA.png")
    assert out.read_bytes().startswith(PNG_MAGIC)


def test_render_chart_rejects_an_unknown_symbol(tmp_path):
    with pytest.raises(ValueError, match="no bars"):
        render_chart(_bars(), "ZZZ", CFG, tmp_path / "ZZZ.png")


# --------------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("value", "digits", "expected"),
    [(1234.5678, 2, "1,234.57"), (None, 2, "—"), (0.0, 0, "0"), (-5.5, 1, "-5.5")],
)
def test_num_filter(value, digits, expected):
    assert _num(value, digits) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(0.9, "90"), (1.0, "100"), (0.0, "0"), (None, "—")]
)
def test_pctile_filter_tolerates_null(value, expected):
    """A recent listing has no 6-month return, so its percentile is legitimately null.

    Regression: the template did the arithmetic inline and crashed the whole report on
    the first real run the moment one such name reached the shortlist.
    """
    assert _pctile(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_500_000_000.0, "$1.5B"),
        (25_000_000.0, "$25.0M"),
        (4_500.0, "$4.5K"),
        (250.0, "$250"),
        (None, "—"),
    ],
)
def test_money_filter(value, expected):
    assert _money(value) == expected


# ---------------------------------------------------------------------------- report


def _scan_and_report(tmp_path, monkeypatch, frames):
    from qms import paths

    monkeypatch.setattr(paths, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paths, "scan_out_dir", lambda d: tmp_path / d.isoformat())

    bars = pl.concat(frames)
    features = build_features(bars, CFG)
    result = run_scan_a(
        as_of_date=dt.date(2026, 7, 24), cfg=CFG, features=features, earnings=pl.DataFrame()
    )
    return result, build_report(cfg=CFG, result=result, bars=bars)


def test_report_writes_html_and_csv(tmp_path, monkeypatch):
    result, html_path = _scan_and_report(tmp_path, monkeypatch, [_bars("AAA"), _bars("BBB", drift=0.004)])

    assert html_path.exists()
    assert (html_path.parent / "ranked.csv").exists()

    html = html_path.read_text(encoding="utf-8")
    assert "Scan A" in html
    assert "2026-07-24" in html
    for symbol in result.candidates["symbol"].to_list():
        assert symbol in html


def test_report_inlines_charts_as_data_uris(tmp_path, monkeypatch):
    """One portable file: the HTML must not depend on the charts directory."""
    result, html_path = _scan_and_report(tmp_path, monkeypatch, [_bars("AAA")])
    if result.candidates.is_empty():
        pytest.skip("no survivor in this fixture")

    html = html_path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html
    assert (html_path.parent / "charts").exists(), "PNGs are also archived on disk"


def test_report_carries_the_not_advice_disclaimer(tmp_path, monkeypatch):
    _, html_path = _scan_and_report(tmp_path, monkeypatch, [_bars("AAA")])
    html = html_path.read_text(encoding="utf-8")
    assert "not a trading system" in html
    assert "financial advice" in html


def test_report_labels_sizing_as_an_estimate(tmp_path, monkeypatch):
    """Pre-open numbers must never be presented as final. Spec §5."""
    result, html_path = _scan_and_report(tmp_path, monkeypatch, [_bars("AAA")])
    if result.candidates.is_empty():
        pytest.skip("no survivor in this fixture")
    html = html_path.read_text(encoding="utf-8")
    assert "estimate" in html.lower()
    assert "Recompute" in html or "recompute" in html


def test_report_shows_the_funnel_counts(tmp_path, monkeypatch):
    result, html_path = _scan_and_report(
        tmp_path, monkeypatch, [_bars("AAA"), _bars("PENNY", drift=-0.004)]
    )
    html = html_path.read_text(encoding="utf-8")
    assert f"{result.universe_size}" in html
    assert "after liquidity" in html


def test_report_handles_no_candidates(tmp_path, monkeypatch):
    """An empty scan must still produce a readable page, not a crash."""
    result, html_path = _scan_and_report(tmp_path, monkeypatch, [_bars("DOWN", drift=-0.01)])
    assert result.candidates.is_empty()
    html = html_path.read_text(encoding="utf-8")
    assert "No candidates" in html
    assert (html_path.parent / "ranked.csv").exists()


def test_csv_is_readable_and_has_the_key_columns(tmp_path, monkeypatch):
    # Distinct drifts on purpose: two identical series tie on the momentum rank, and an
    # average-method tie in a two-name universe lands at 0.75 — below the top decile — so
    # identical fixtures would produce no candidates at all.
    result, html_path = _scan_and_report(
        tmp_path, monkeypatch, [_bars("AAA", drift=0.006), _bars("BBB", drift=0.003)]
    )
    assert not result.candidates.is_empty()

    csv = pl.read_csv(html_path.parent / "ranked.csv")
    for column in ("rank", "symbol", "close", "shares", "binding_cap", "stop_price", "triggers"):
        assert column in csv.columns
    assert csv.height == result.candidates.height
