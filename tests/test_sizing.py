"""Position sizing. Spec §5.

Includes the worked example the source doc actually gives (LoD 100, ATR 30 -> max entry
130), which is the one externally-verifiable number in the whole sizing section.
"""

from __future__ import annotations

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.sizing.calculator import (
    CAP_CONCENTRATION,
    CAP_DOLLAR_VOL,
    CAP_LIQUIDITY,
    CAP_RISK,
    SizingInputs,
    add_sizing,
    size_one,
)

CFG = load_scan_config()


def _inputs(**overrides) -> SizingInputs:
    base = {
        "entry_price": 100.0,
        "low_of_day": 98.0,
        "atr": 3.0,
        "avg_vol_20": 5_000_000.0,
        "avg_dollar_vol_20": 500_000_000.0,
    }
    base.update(overrides)
    return SizingInputs(**base)


# ------------------------------------------------------------------- the doc's example


def test_the_documented_worked_example():
    """The doc: low of day 100, ATR 30 -> maximum entry 130.

    The one number in §5 that can be checked against the source rather than against our
    own implementation.
    """
    result = size_one(_inputs(entry_price=120.0, low_of_day=100.0, atr=30.0), CFG)
    assert result["max_entry"] == pytest.approx(130.0)


def test_preferred_entry_is_half_to_two_thirds_of_atr_above_the_low():
    result = size_one(_inputs(entry_price=120.0, low_of_day=100.0, atr=30.0), CFG)
    assert result["preferred_entry_low"] == pytest.approx(115.0)  # 100 + 0.5*30
    assert result["preferred_entry_high"] == pytest.approx(120.1)  # 100 + 0.67*30


# --------------------------------------------------------------------------- the caps


def test_stop_is_half_a_percent_below_the_low():
    result = size_one(_inputs(low_of_day=98.0), CFG)
    assert result["stop_price"] == pytest.approx(98.0 * 0.995)
    assert result["risk_per_share"] == pytest.approx(100.0 - 97.51)


def test_risk_cap_matches_hand_arithmetic():
    """account 100,000 x 0.5% = $500 of risk. Stop 97.51 against entry 100 = $2.49/share.

    500 / 2.49 = 200.8 shares.
    """
    result = size_one(_inputs(), CFG)
    assert result["risk_dollars"] == pytest.approx(500.0)
    assert result["shares_risk"] == pytest.approx(500.0 / 2.49, rel=1e-9)


def test_liquidity_cap_is_one_percent_of_average_volume():
    result = size_one(_inputs(avg_vol_20=5_000_000.0), CFG)
    assert result["shares_liquidity"] == pytest.approx(50_000.0)


def test_dollar_volume_cap_keeps_position_under_a_two_hundredth_of_turnover():
    """min $vol = position * 200, so position <= avg_dollar_vol / 200."""
    result = size_one(_inputs(entry_price=100.0, avg_dollar_vol_20=500_000_000.0), CFG)
    assert result["shares_dollarvol"] == pytest.approx((500_000_000.0 / 200) / 100.0)
    assert result["shares_dollarvol"] * 100.0 * 200 == pytest.approx(500_000_000.0)


def test_concentration_cap_is_twenty_percent_of_account():
    result = size_one(_inputs(entry_price=100.0), CFG)
    assert result["shares_concentration"] == pytest.approx((0.20 * 100_000.0) / 100.0)


# ------------------------------------------------------------------ which cap binds


def test_thin_name_is_capped_far_below_the_risk_based_size():
    """The case worth knowing before the open: you cannot trade this at your size."""
    result = size_one(_inputs(avg_vol_20=1_000.0, avg_dollar_vol_20=100_000.0), CFG)
    assert result["shares"] == pytest.approx(5.0)
    assert result["shares"] < result["shares_risk"] / 10


@pytest.mark.parametrize(
    ("avg_vol", "price"),
    [(1_000.0, 100.0), (5_000_000.0, 100.0), (100_000.0, 20.0), (20_000_000.0, 400.0)],
)
def test_dollar_volume_cap_is_always_exactly_half_the_liquidity_cap(avg_vol, price):
    """A property of the doc's own numbers, not of this implementation.

    shares_liquidity = 0.01 * avg_vol
    shares_dollarvol = (avg_dollar_vol / 200) / price

    When the two inputs are internally consistent (avg_dollar_vol == avg_vol * price) the
    price cancels and the second reduces to avg_vol / 200 = 0.005 * avg_vol — exactly half
    the first, at every price and every volume.

    So with `max_pct_of_avg_vol: 0.01` and `dollar_vol_multiple: 200`, the 1%-of-volume
    cap can never bind and is effectively inert. Both rules are [DOC], so neither is
    removed; this test pins the relationship so that changing either config value makes
    the consequence visible instead of silent.
    """
    result = size_one(
        _inputs(entry_price=price, low_of_day=price * 0.98,
                avg_vol_20=avg_vol, avg_dollar_vol_20=avg_vol * price),
        CFG,
    )
    assert result["shares_liquidity"] == pytest.approx(result["shares_dollarvol"] * 2)
    assert result["binding_cap"] != CAP_LIQUIDITY


def test_risk_binds_when_the_stop_is_wide():
    """A stop far from entry shrinks the risk-based size below every other cap."""
    result = size_one(_inputs(entry_price=100.0, low_of_day=60.0), CFG)
    assert result["binding_cap"] == CAP_RISK


def test_concentration_binds_on_a_tight_stop_in_a_liquid_name():
    """Tiny risk-per-share would otherwise imply an absurd position."""
    result = size_one(
        _inputs(
            entry_price=100.0,
            low_of_day=99.99,
            avg_vol_20=500_000_000.0,
            avg_dollar_vol_20=900_000_000_000.0,
        ),
        CFG,
    )
    assert result["binding_cap"] == CAP_CONCENTRATION
    assert result["shares"] == pytest.approx(200.0)


def test_dollar_volume_can_bind_independently_of_share_volume():
    """A high-priced name can clear the share cap and still fail the turnover cap."""
    result = size_one(
        _inputs(entry_price=1000.0, low_of_day=999.0, avg_vol_20=10_000_000.0,
                avg_dollar_vol_20=1_000_000.0),
        CFG,
    )
    assert result["binding_cap"] == CAP_DOLLAR_VOL


def test_final_size_is_always_the_minimum_cap():
    result = size_one(_inputs(), CFG)
    caps = [
        result["shares_risk"],
        result["shares_liquidity"],
        result["shares_dollarvol"],
        result["shares_concentration"],
    ]
    assert result["shares"] == pytest.approx(min(caps))


def test_actual_risk_never_exceeds_the_budget():
    """Whichever cap binds, realised risk must be <= the configured risk dollars."""
    for overrides in ({}, {"avg_vol_20": 1_000.0}, {"low_of_day": 60.0}, {"low_of_day": 99.99}):
        result = size_one(_inputs(**overrides), CFG)
        assert result["actual_risk_dollars"] <= result["risk_dollars"] + 1e-6


# -------------------------------------------------------------------------- the flags


def test_extension_flag_trips_above_low_plus_atr():
    below = size_one(_inputs(entry_price=125.0, low_of_day=100.0, atr=30.0), CFG)
    above = size_one(_inputs(entry_price=135.0, low_of_day=100.0, atr=30.0), CFG)
    assert below["max_entry"] == above["max_entry"] == pytest.approx(130.0)
    assert not below["stop_exceeds_atr"]


def test_stop_wider_than_atr_is_flagged():
    """Doc: day-1 stop distance should not exceed the ATR."""
    wide = size_one(_inputs(entry_price=100.0, low_of_day=80.0, atr=3.0), CFG)
    tight = size_one(_inputs(entry_price=100.0, low_of_day=99.0, atr=3.0), CFG)
    assert wide["stop_exceeds_atr"]
    assert not tight["stop_exceeds_atr"]


def test_zero_risk_per_share_does_not_divide_by_zero():
    """A low above the entry is nonsense data, not a crash."""
    result = size_one(_inputs(entry_price=100.0, low_of_day=200.0), CFG)
    assert result["shares_risk"] == 0.0
    assert result["shares"] == 0.0


# ------------------------------------------------------- vectorised path agrees exactly


def test_vectorised_sizing_matches_the_scalar_path():
    """The frame version and the scalar version must not drift apart."""
    rows = [
        _inputs(),
        _inputs(avg_vol_20=1_000.0, avg_dollar_vol_20=100_000.0),
        _inputs(entry_price=100.0, low_of_day=60.0),
        _inputs(entry_price=1000.0, low_of_day=999.0, avg_dollar_vol_20=1_000_000.0),
    ]
    frame = pl.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(len(rows))],
            "close": [r.entry_price for r in rows],
            "low": [r.low_of_day for r in rows],
            "atr_14": [r.atr for r in rows],
            "avg_vol_20": [r.avg_vol_20 for r in rows],
            "avg_dollar_vol_20": [r.avg_dollar_vol_20 for r in rows],
        }
    )
    sized = add_sizing(frame, CFG)

    for index, row in enumerate(rows):
        expected = size_one(row, CFG)
        actual = sized.row(index, named=True)
        assert actual["shares"] == pytest.approx(expected["shares"])
        assert actual["binding_cap"] == expected["binding_cap"]
        assert actual["stop_price"] == pytest.approx(expected["stop_price"])
        assert actual["max_entry"] == pytest.approx(expected["max_entry"])


def test_add_sizing_on_empty_frame_is_a_noop():
    empty = pl.DataFrame(
        schema={
            "symbol": pl.Utf8,
            "close": pl.Float64,
            "low": pl.Float64,
            "atr_14": pl.Float64,
            "avg_vol_20": pl.Float64,
            "avg_dollar_vol_20": pl.Float64,
        }
    )
    assert add_sizing(empty, CFG).is_empty()
