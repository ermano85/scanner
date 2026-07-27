"""Typed, strict loader for config/scan.yaml and config/universe.yaml.

Two rules govern every model in this file:

1. ``extra="forbid"`` — an unknown key is an error, not a silently ignored typo. A
   misspelled threshold that falls back to a default is the worst kind of bug here,
   because the scanner keeps running and quietly screens on something else.

2. **No field has a default.** A missing key is an error. This is not pedantry: spec §9
   forbids numeric literals in rule code, and a default value in a pydantic model *is* a
   numeric literal in code. Every number the scanner acts on has exactly one home, and
   it is the YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = _REPO_ROOT / "config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- data


class DataConfig(_Strict):
    adjustment_policy: Literal["split_only"]
    backfill_years: int
    nightly_lookback_days: int


# ----------------------------------------------------------------------- features


class AdrConfig(_Strict):
    periods: list[int]
    primary: int

    @model_validator(mode="after")
    def _primary_must_be_computed(self) -> AdrConfig:
        if self.primary not in self.periods:
            raise ValueError(
                f"adr.primary={self.primary} is not in adr.periods={self.periods}; "
                "the primary ADR window must actually be computed"
            )
        return self


class AtrConfig(_Strict):
    period: int


class MomentumConfig(_Strict):
    gain_1m: int
    gain_3m: int
    gain_6m: int

    def as_dict(self) -> dict[str, int]:
        return {"gain_1m": self.gain_1m, "gain_3m": self.gain_3m, "gain_6m": self.gain_6m}


class SmaConfig(_Strict):
    periods: list[int]


class LiquidityConfig(_Strict):
    avg_vol_window: int
    avg_dollar_vol_window: int


class ConsolidationConfig(_Strict):
    """[EXT] throughout. Ranking only — never a gate. See features/consolidation.py."""

    buckets: list[int]
    atr_fast: int
    atr_slow: int
    vol_fast: int
    vol_slow: int


class FeaturesConfig(_Strict):
    adr: AdrConfig
    atr: AtrConfig
    momentum: MomentumConfig
    sma: SmaConfig
    liquidity: LiquidityConfig
    consolidation: ConsolidationConfig


# -------------------------------------------------------------------------- scan A


class GatesConfig(_Strict):
    min_price: float
    min_dollar_vol: float
    min_adr: float
    momentum_pctile: float = Field(ge=0.0, le=1.0)
    above_50: bool
    earnings_blackout_days: int


class MaStackConfig(_Strict):
    fast: int
    slow: int
    k: int
    m: int

    @model_validator(mode="after")
    def _k_within_m(self) -> MaStackConfig:
        if self.k > self.m:
            raise ValueError(f"ma_stack.k={self.k} exceeds ma_stack.m={self.m}; gate can never pass")
        if self.fast >= self.slow:
            raise ValueError(
                f"ma_stack.fast={self.fast} must be shorter than ma_stack.slow={self.slow}"
            )
        return self


class TriggersConfig(_Strict):
    near_ma_adr: float
    near_pivot_pct: float
    pivot_buckets: list[int]
    ma_periods: list[int]


class RankingWeights(_Strict):
    momentum_pctile: float
    trigger_bonus: float
    tightness_adr: float
    contraction: float
    vol_dryup: float
    low_slope: float
    depth_from_high: float


class RankingConfig(_Strict):
    weights: RankingWeights
    score_bucket: int
    neutral_percentile: float = Field(ge=0.0, le=1.0)
    max_candidates: int


class ScanAConfig(_Strict):
    gates: GatesConfig
    ma_stack: MaStackConfig
    triggers: TriggersConfig
    ranking: RankingConfig


# -------------------------------------------------------------------------- sizing


class SizingConfig(_Strict):
    account: float
    risk_pct: float
    stop_buffer: float
    max_pct_of_avg_vol: float
    dollar_vol_multiple: float
    max_account_concentration: float
    preferred_entry_atr_low: float
    preferred_entry_atr_high: float
    max_entry_atr_multiple: float


# -------------------------------------------------------------------------- report


class QualityConfig(_Strict):
    max_staleness_sessions: int
    min_universe_coverage: float = Field(ge=0.0, le=1.0)
    max_bar_age_sessions: int
    min_symbols: int
    jump_threshold_pct: float
    jump_lookback_sessions: int
    max_unexplained_jump_share: float = Field(ge=0.0, le=1.0)


class ReportConfig(_Strict):
    chart_months: int
    chart_sma: list[int]
    chart_width_px: int
    chart_height_px: int
    chart_dpi: int


# ------------------------------------------------------------------------ top level


class ScanConfig(_Strict):
    data: DataConfig
    features: FeaturesConfig
    scan_a: ScanAConfig
    sizing: SizingConfig
    quality: QualityConfig
    report: ReportConfig

    @model_validator(mode="after")
    def _cross_section_coherence(self) -> ScanConfig:
        """Catch config that is individually valid but jointly nonsensical."""
        sma = set(self.features.sma.periods)
        needed = {self.scan_a.ma_stack.fast, self.scan_a.ma_stack.slow, *self.scan_a.triggers.ma_periods}
        missing = needed - sma
        if missing:
            raise ValueError(
                f"scan_a references SMA period(s) {sorted(missing)} that features.sma.periods "
                f"{sorted(sma)} does not compute"
            )
        if self.scan_a.ranking.score_bucket not in self.features.consolidation.buckets:
            raise ValueError(
                f"ranking.score_bucket={self.scan_a.ranking.score_bucket} is not in "
                f"consolidation.buckets={self.features.consolidation.buckets}"
            )
        unknown_pivots = set(self.scan_a.triggers.pivot_buckets) - set(
            self.features.consolidation.buckets
        )
        if unknown_pivots:
            raise ValueError(
                f"triggers.pivot_buckets {sorted(unknown_pivots)} are not computed; "
                f"consolidation.buckets={self.features.consolidation.buckets}"
            )
        return self


class UniverseConfig(_Strict):
    exchanges: dict[str, bool]
    include_etfs: bool
    exclude_test_issues: bool
    exclude_deficient: bool
    exclude_suffixes: list[str]
    exclude_name_patterns: list[str]
    exclude_symbols: list[str]
    active_universe_floor_dollar_vol: float

    def enabled_exchanges(self) -> set[str]:
        return {code for code, on in self.exchanges.items() if on}


# ------------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} did not parse to a mapping (got {type(loaded).__name__})")
    return loaded


def load_scan_config(config_dir: Path | None = None) -> ScanConfig:
    directory = config_dir or DEFAULT_CONFIG_DIR
    return ScanConfig.model_validate(_read_yaml(directory / "scan.yaml"))


def load_universe_config(config_dir: Path | None = None) -> UniverseConfig:
    directory = config_dir or DEFAULT_CONFIG_DIR
    return UniverseConfig.model_validate(_read_yaml(directory / "universe.yaml"))
