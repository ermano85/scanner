"""The Claude brief and the TradingView watchlist."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.report.brief import render_brief, render_brief_json, write_brief
from qms.report.tradingview import (
    render_watchlist,
    to_tradingview_symbol,
    write_watchlist,
)
from qms.rules.scan_a import ScanResult

CFG = load_scan_config()


def _candidates(n: int = 2) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "rank": list(range(1, n + 1)),
            "symbol": ["CERT", "SNOW"][:n],
            "date": [dt.date(2026, 7, 24)] * n,
            "close": [7.11, 265.13][:n],
            "adr_pct": [5.86, 5.14][:n],
            "atr_14": [0.36, 13.18][:n],
            "gain_1m": [31.7, 15.1][:n],
            "gain_3m": [11.4, 70.4][:n],
            "gain_6m": [-26.3, None][:n],
            "gain_1m_pctile": [0.95, 0.84][:n],
            "gain_3m_pctile": [0.59, 0.92][:n],
            "gain_6m_pctile": [0.23, None][:n],
            "momentum_pctile": [0.95, 0.92][:n],
            "dist_to_sma_10_pct": [2.5, -1.2][:n],
            "dist_to_sma_10_adr": [0.43, -0.23][:n],
            "dist_to_sma_20_pct": [5.2, 1.1][:n],
            "dist_to_sma_20_adr": [0.88, 0.21][:n],
            "dist_to_sma_50_pct": [17.4, 8.0][:n],
            "dist_to_sma_50_adr": [2.98, 1.56][:n],
            "triggers": ["AT_10MA AT_20MA AT_PIVOT", "AT_10MA"][:n],
            "sic": [7372, 7372][:n],
            "sic_description": ["Services-Prepackaged Software"] * n,
            "next_earnings_date": [dt.date(2026, 8, 4), None][:n],
            "earnings_when": ["bmo", None][:n],
            "days_to_earnings": [6, None][:n],
            "earnings_unknown": [False, True][:n],
            "sic_unknown": [False, False][:n],
            "avg_vol_20": [4_000_000.0, 5_000_000.0][:n],
            "avg_dollar_vol_20": [28_300_000.0, 1_300_000_000.0][:n],
            "shares": [1856.0, 75.0][:n],
            "binding_cap": ["risk", "concentration"][:n],
            "stop_price": [6.84, 259.0][:n],
            "risk_per_share": [0.27, 6.13][:n],
            "actual_risk_dollars": [500.0, 460.0][:n],
            "position_dollars": [13_200.0, 19_884.0][:n],
            "preferred_entry_low": [7.06, 262.0][:n],
            "preferred_entry_high": [7.12, 264.0][:n],
            "max_entry": [7.24, 266.0][:n],
            "extended": [False, True][:n],
            "stop_exceeds_atr": [False, False][:n],
            "score": [1.122, 1.02][:n],
        },
        schema_overrides={"next_earnings_date": pl.Date, "date": pl.Date},
    )


def _result(candidates: pl.DataFrame | None = None, stale: int = 0) -> ScanResult:
    frame = _candidates() if candidates is None else candidates
    return ScanResult(
        as_of_date=dt.date(2026, 7, 27),
        data_date=dt.date(2026, 7, 24),
        candidates=frame,
        universe_size=11326,
        after_liquidity=545,
        rejections={"price": 1667, "adr": 8788, "sector": 25},
        staleness_sessions=stale,
    )


# ----------------------------------------------------------------------- the brief


def _flat(text: str) -> str:
    """Lower-cased, whitespace-collapsed — so a line wrap cannot break a content check."""
    return " ".join(text.lower().split())


def test_brief_carries_its_own_caveats():
    """Someone reading only this file must still learn what it is not."""
    text = _flat(render_brief(_result(), CFG))
    for phrase in (
        "does not decide what to buy",
        "nothing in this file is financial advice",
        "pre-open estimates",
        "unvalidated",
        "not a trading system",
    ):
        assert phrase in text, f"missing caveat: {phrase!r}"


def test_brief_explains_ext_versus_doc():
    text = render_brief(_result(), CFG)
    assert "[DOC]" in text and "[EXT]" in text
    assert "reading order" in _flat(text)


def test_brief_states_the_sizing_config():
    """Share counts are uninterpretable without the account size behind them."""
    text = render_brief(_result(), CFG)
    assert f"{CFG.sizing.account:,.0f}" in text
    assert f"{CFG.sizing.risk_pct:.2%}" in text


def test_brief_includes_every_candidate_with_its_numbers():
    text = render_brief(_result(), CFG)
    assert "CERT" in text and "SNOW" in text
    assert "0.43 ADR" in text
    assert "bound by risk" in text
    assert "2026-08-04 (6 trading days, bmo)" in text


def test_brief_surfaces_risk_flags():
    text = render_brief(_result(), CFG)
    assert "EXTENDED" in text
    assert "EARNINGS UNKNOWN" in text


def test_brief_handles_null_momentum():
    """A recent listing has no 6-month return; the brief must not crash or print None."""
    text = render_brief(_result(), CFG)
    assert "None" not in text
    assert "n/a" in text


def test_brief_warns_when_stale():
    assert "stale" in render_brief(_result(stale=2), CFG).lower()
    assert "stale" not in render_brief(_result(stale=0), CFG).split("## What this is")[0].lower()


def test_brief_handles_an_empty_scan():
    text = render_brief(_result(_candidates().clear()), CFG)
    assert "None survived the gates." in text


def test_brief_stays_small_enough_to_paste(tmp_path):
    """The HTML report is ~7 MB of base64. This has to be pasteable."""
    big = pl.concat([_candidates()] * 30)
    big = big.with_columns(pl.int_range(pl.len()).add(1).alias("rank"))
    path = write_brief(_result(big), CFG, tmp_path)
    size_kb = path.stat().st_size / 1024
    assert size_kb < 100, f"brief is {size_kb:.0f} KB for {big.height} candidates"


def test_brief_json_is_valid_and_carries_the_disclaimer(tmp_path):
    payload = json.loads(render_brief_json(_result(), CFG))
    assert "not investment advice" in payload["disclaimer"]
    assert payload["as_of_date"] == "2026-07-27"
    assert payload["sizing_config"]["risk_dollars_per_trade"] == pytest.approx(
        CFG.sizing.account * CFG.sizing.risk_pct
    )
    assert [c["symbol"] for c in payload["candidates"]] == ["CERT", "SNOW"]


def test_write_brief_emits_both_files(tmp_path):
    write_brief(_result(), CFG, tmp_path)
    assert (tmp_path / "claude-brief.md").exists()
    assert (tmp_path / "claude-brief.json").exists()


# ------------------------------------------------------------------- tradingview


@pytest.mark.parametrize(
    ("symbol", "exchange", "expected"),
    [
        ("AAPL", "Q", "NASDAQ:AAPL"),
        ("GE", "N", "NYSE:GE"),
        ("UAMY", "A", "AMEX:UAMY"),
        ("SPY", "P", "AMEX:SPY"),
        ("IWM", "Z", "AMEX:IWM"),
        ("AGM.A", "N", "NYSE:AGM.A"),
        ("WEIRD", None, "NASDAQ:WEIRD"),
        ("WEIRD", "X", "NASDAQ:WEIRD"),
    ],
)
def test_exchange_prefix_mapping(symbol, exchange, expected):
    assert to_tradingview_symbol(symbol, exchange) == expected


def test_watchlist_is_comma_separated_with_a_section_header():
    text = render_watchlist(["CERT", "SNOW"], dt.date(2026, 7, 27), {"CERT": "Q", "SNOW": "N"})
    lines = text.strip().split("\n")
    assert lines[0] == "###Qullamaggie 2026-07-27"
    assert lines[1] == "NASDAQ:CERT,NYSE:SNOW"


def test_watchlist_payload_survives_a_dropped_header():
    """The `###` syntax is community lore, not documented. If TradingView ignores it, the
    remaining line must still be the documented comma-separated format."""
    text = render_watchlist(["CERT"], dt.date(2026, 7, 27), {"CERT": "Q"})
    payload = [ln for ln in text.strip().split("\n") if not ln.startswith("###")]
    assert payload == ["NASDAQ:CERT"]
    assert "," not in payload[0], "single symbol needs no separator"


def test_empty_watchlist_is_still_a_valid_file(tmp_path):
    path = write_watchlist(_candidates().clear(), dt.date(2026, 7, 27), tmp_path, {})
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("###Qullamaggie")


def test_write_watchlist_uses_the_ranked_order(tmp_path):
    path = write_watchlist(
        _candidates(), dt.date(2026, 7, 27), tmp_path, {"CERT": "Q", "SNOW": "N"}
    )
    assert path.read_text(encoding="utf-8").strip().split("\n")[1] == "NASDAQ:CERT,NYSE:SNOW"
