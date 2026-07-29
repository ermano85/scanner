"""The SIC-based sector exclusion.

Codes below are the real ones observed on the 2026-07-27 shortlist, so these tests fail if
the classification mapping drifts away from what the scan actually saw.
"""

from __future__ import annotations

import polars as pl
import pytest

from qms.config import load_universe_config
from qms.ingest.sec_sic import SIC_SCHEMA
from qms.rules.gates import GATE_SECTOR, attach_sector, survivors

UNIVERSE_CFG = load_universe_config()
PASS = f"pass_{GATE_SECTOR}"

# symbol -> (sic, description), verbatim from SEC on 2026-07-27.
REAL = {
    "NRIX": (2834, "Pharmaceutical Preparations"),
    "CLYM": (2834, "Pharmaceutical Preparations"),
    "RLAY": (2836, "Biological Products, (No Diagnostic Substances)"),
    "AGEN": (2836, "Biological Products, (No Diagnostic Substances)"),
    "QDEL": (2835, "In Vitro & In Vivo Diagnostic Substances"),
    "SNOW": (7372, "Services-Prepackaged Software"),
    "DELL": (3571, "Electronic Computers"),
    "GRPN": (7311, "Services-Advertising Agencies"),
    "CBRL": (5812, "Retail-Eating  Places"),
}


def _sic_frame(symbols: list[str]) -> pl.DataFrame:
    rows = [
        {
            "symbol": s,
            "cik": "0000000001",
            "sic": REAL.get(s, (None, None))[0],
            "sic_description": REAL.get(s, (None, None))[1],
            "fetched_on": None,
        }
        for s in symbols
    ]
    return pl.DataFrame(rows, schema_overrides=dict(SIC_SCHEMA)).select(list(SIC_SCHEMA))


def _latest(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": symbols, "close": [10.0] * len(symbols)})


def test_shipped_config_excludes_the_pharma_block():
    assert set(UNIVERSE_CFG.exclude_sic) == {2833, 2834, 2835, 2836}


def test_pharma_and_biologics_are_excluded():
    symbols = ["NRIX", "CLYM", "RLAY", "AGEN", "SNOW"]
    out = attach_sector(_latest(symbols), _sic_frame(symbols), UNIVERSE_CFG.exclude_sic)
    kept = survivors(out)["symbol"].to_list()
    assert kept == ["SNOW"]


def test_non_pharma_survives_untouched():
    symbols = ["SNOW", "DELL", "GRPN", "CBRL"]
    out = attach_sector(_latest(symbols), _sic_frame(symbols), UNIVERSE_CFG.exclude_sic)
    assert survivors(out)["symbol"].to_list() == symbols


def test_diagnostics_are_excluded_by_the_shipped_config():
    """QDEL is 2835. Included in the block deliberately, not by accident."""
    out = attach_sector(_latest(["QDEL"]), _sic_frame(["QDEL"]), UNIVERSE_CFG.exclude_sic)
    assert not out[PASS][0]


def test_unclassified_symbols_pass_and_are_tagged():
    """ETFs and most foreign issuers have no SIC. Failing them closed would delete a
    large, arbitrary slice of the universe rather than the sector actually targeted."""
    symbols = ["SPY", "SNOW"]
    out = attach_sector(_latest(symbols), _sic_frame(symbols), UNIVERSE_CFG.exclude_sic)
    spy = out.filter(pl.col("symbol") == "SPY").row(0, named=True)
    assert spy["sic"] is None
    assert spy["sic_unknown"]
    assert spy[PASS]
    assert survivors(out)["symbol"].to_list() == symbols


def test_symbol_absent_from_the_lookup_still_passes():
    """A join miss must behave like an unknown classification, not a silent drop."""
    out = attach_sector(_latest(["SNOW", "WEIRD"]), _sic_frame(["SNOW"]), UNIVERSE_CFG.exclude_sic)
    assert out.height == 2
    assert survivors(out)["symbol"].to_list() == ["SNOW", "WEIRD"]


def test_empty_lookup_passes_everything():
    """SEC unreachable and an empty cache must not empty the watchlist."""
    symbols = ["NRIX", "SNOW"]
    out = attach_sector(_latest(symbols), pl.DataFrame(schema=dict(SIC_SCHEMA)), [2834])
    assert out[PASS].to_list() == [True, True]


def test_empty_exclusion_list_is_a_passthrough():
    symbols = ["NRIX", "RLAY", "SNOW"]
    out = attach_sector(_latest(symbols), _sic_frame(symbols), [])
    assert survivors(out)["symbol"].to_list() == symbols


def test_attach_sector_never_drops_rows():
    """Gates mark; `survivors` filters. Keeping them separate is what makes the funnel
    counts in the report explainable."""
    symbols = ["NRIX", "RLAY", "SNOW", "SPY"]
    out = attach_sector(_latest(symbols), _sic_frame(symbols), UNIVERSE_CFG.exclude_sic)
    assert out.height == len(symbols)


@pytest.mark.parametrize("code", [2833, 2834, 2835, 2836])
def test_every_configured_code_actually_excludes(code):
    frame = pl.DataFrame({"symbol": ["X"], "close": [10.0]})
    sic = pl.DataFrame(
        {"symbol": ["X"], "cik": ["1"], "sic": [code], "sic_description": ["t"],
         "fetched_on": [None]},
        schema_overrides=dict(SIC_SCHEMA),
    ).select(list(SIC_SCHEMA))
    assert not attach_sector(frame, sic, UNIVERSE_CFG.exclude_sic)[PASS][0]
