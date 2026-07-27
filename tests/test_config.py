"""The config model is the only place numbers live, so its strictness is load-bearing.

A typo'd threshold that silently falls back to a default would leave the scanner running
and screening on something other than what the YAML says. These tests assert it cannot.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from qms.config import (
    ScanConfig,
    UniverseConfig,
    load_scan_config,
    load_universe_config,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="module")
def scan_raw() -> dict:
    return yaml.safe_load((CONFIG_DIR / "scan.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def universe_raw() -> dict:
    return yaml.safe_load((CONFIG_DIR / "universe.yaml").read_text(encoding="utf-8"))


def test_shipped_config_is_valid():
    scan = load_scan_config()
    universe = load_universe_config()
    assert scan.data.adjustment_policy == "split_only"
    assert universe.enabled_exchanges()


def test_unknown_key_is_rejected(scan_raw):
    """A misspelled threshold must fail, not be ignored."""
    bad = copy.deepcopy(scan_raw)
    bad["scan_a"]["gates"]["min_pryce"] = 5.0
    with pytest.raises(ValidationError, match="min_pryce"):
        ScanConfig.model_validate(bad)


def test_missing_key_is_rejected(scan_raw):
    """No field has a default, so a dropped key is an error rather than a hidden literal."""
    bad = copy.deepcopy(scan_raw)
    del bad["scan_a"]["gates"]["min_adr"]
    with pytest.raises(ValidationError, match="min_adr"):
        ScanConfig.model_validate(bad)


def test_momentum_percentile_must_be_a_fraction(scan_raw):
    bad = copy.deepcopy(scan_raw)
    bad["scan_a"]["gates"]["momentum_pctile"] = 90  # 90, not 0.90
    with pytest.raises(ValidationError):
        ScanConfig.model_validate(bad)


def test_ma_stack_k_cannot_exceed_m(scan_raw):
    bad = copy.deepcopy(scan_raw)
    bad["scan_a"]["ma_stack"]["k"] = bad["scan_a"]["ma_stack"]["m"] + 1
    with pytest.raises(ValidationError, match="can never pass"):
        ScanConfig.model_validate(bad)


def test_gate_cannot_reference_an_uncomputed_sma(scan_raw):
    """Cross-section coherence: the rule layer may only use what the feature layer builds."""
    bad = copy.deepcopy(scan_raw)
    bad["features"]["sma"]["periods"] = [20, 50, 200]  # drops the 10 the MA stack needs
    with pytest.raises(ValidationError, match="does not compute"):
        ScanConfig.model_validate(bad)


def test_ranking_bucket_must_be_computed(scan_raw):
    bad = copy.deepcopy(scan_raw)
    bad["scan_a"]["ranking"]["score_bucket"] = 999
    with pytest.raises(ValidationError, match="score_bucket"):
        ScanConfig.model_validate(bad)


def test_adr_primary_must_be_computed(scan_raw):
    bad = copy.deepcopy(scan_raw)
    bad["features"]["adr"]["primary"] = 33
    with pytest.raises(ValidationError, match="primary"):
        ScanConfig.model_validate(bad)


def test_config_is_frozen():
    """Rules receive the config by reference; nothing downstream may mutate a threshold."""
    scan = load_scan_config()
    with pytest.raises(ValidationError):
        scan.scan_a.gates.min_price = 1.0


def test_universe_unknown_key_is_rejected(universe_raw):
    bad = copy.deepcopy(universe_raw)
    bad["include_etf"] = True  # missing trailing s
    with pytest.raises(ValidationError, match="include_etf"):
        UniverseConfig.model_validate(bad)
