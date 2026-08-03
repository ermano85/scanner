"""Derived arithmetic, and the order-type label above all.

`journal/orders.csv` records the failure this file exists to prevent: on 2026-07-30 CBRL
was placed as a buy *limit* at a level above the market. A limit above the market fills
immediately at the quote, so it filled at 56.28 rather than resting at the 57.80 trigger
that was never reached, and stopped out the same day for -1.44R.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from qms.config import load_scan_config
from qms.pass2 import derive
from qms.pass2.model import Candidate, Daily, Quote, Value
from qms.sizing.calculator import SizingInputs, size_one

NOW = dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_scan_config()


def _quote(price: float | None, *, low: float | None = 56.21, live: bool = True) -> Quote:
    price_value = (
        Value.fetched(price, source="test", as_of=NOW)
        if price is not None
        else Value.unavailable(reason="no price")
    )
    low_value = (
        Value.fetched(low, source="test", as_of=NOW)
        if low is not None
        else Value.unavailable(reason="no session low")
    )
    return Quote(
        symbol="TEST",
        current_price=price_value,
        price_time=price_value,
        session_low=low_value,
        session_low_time=low_value,
        session_high=low_value,
        premarket_low_excluded=low_value,
        crosscheck=low_value,
        is_live=live,
    )


def _daily(atr: float = 2.80, *, liquidity: bool = True) -> Daily:
    ok = Value.computed(1e7, formula="test")
    gap = Value.unavailable(reason="insufficient history")
    return Daily(
        symbol="TEST",
        atr=Value.computed(atr, formula="test"),
        adr_pct=Value.computed(5.0, formula="test"),
        avg_vol=ok if liquidity else gap,
        avg_dollar_vol=ok if liquidity else gap,
    )


# ------------------------------------------------------------------------ order type


def test_an_entry_above_the_market_is_a_buy_stop(cfg):
    assert derive.order_type(57.60, _quote(56.60)).value == derive.ORDER_BUY_STOP


def test_an_entry_below_the_market_is_a_limit(cfg):
    assert derive.order_type(57.60, _quote(60.00)).value == derive.ORDER_BUY_LIMIT


def test_the_cbrl_2026_07_30_order_would_have_been_labelled_a_buy_stop(cfg):
    """Regression for the trade in journal/orders.csv that was placed as a limit."""
    result = derive.order_type(57.80, _quote(56.28))
    assert result.value == derive.ORDER_BUY_STOP
    assert "fill instantly at the quote" in result.formula


def test_order_type_is_unavailable_without_a_live_price(cfg):
    stale = derive.order_type(57.60, _quote(56.60, live=False))
    assert not stale.ok
    assert "not live" in stale.reason

    missing = derive.order_type(57.60, _quote(None, live=False))
    assert not missing.ok


# ------------------------------------------------------------------- entry and stop


def test_stop_and_entry_band_come_from_the_configured_multiples(cfg):
    candidate = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(56.60, low=56.00), daily=_daily(2.00)), cfg
    )

    assert candidate.stop.value == pytest.approx(56.00 * cfg.sizing.stop_buffer)
    entries = {level.label: level.entry.value for level in candidate.levels}
    assert entries["minimum"] == pytest.approx(56.00 + 2.00 * cfg.sizing.preferred_entry_atr_low)
    assert entries["preferred"] == pytest.approx(56.00 + 2.00 * cfg.sizing.preferred_entry_atr_high)
    assert entries["maximum"] == pytest.approx(56.00 + 2.00 * cfg.sizing.max_entry_atr_multiple)


def test_extended_fires_when_the_move_off_the_low_exceeds_one_atr(cfg):
    extended = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(59.00, low=56.00), daily=_daily(2.00)), cfg
    )
    assert extended.extended_atr.value == pytest.approx(1.5)
    assert derive.FLAG_EXTENDED in extended.flags

    calm = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(57.00, low=56.00), daily=_daily(2.00)), cfg
    )
    assert derive.FLAG_EXTENDED not in calm.flags


def test_the_stop_inside_noise_flag_cannot_fire_at_the_shipped_multiples(cfg):
    """A structural consequence of the config, pinned so it stays visible.

    At the minimum entry, stop distance in ATR units is

        (entry - stop) / ATR = (low + k*ATR - stop_buffer*low) / ATR
                             = k + (1 - stop_buffer) * low / ATR

    With `k = preferred_entry_atr_low = 0.5` and `stop_buffer < 1`, the second term is
    strictly positive, so the ratio always exceeds `k`. The `< 0.5` flag the rules ask for
    is therefore unreachable unless `preferred_entry_atr_low` is lowered. The check is
    implemented anyway because the rule is [DOC] and the config can change — the same
    reasoning `sizing/calculator.py` applies to its inert liquidity cap.
    """
    for atr, low in ((2.0, 56.0), (20.0, 56.0), (0.5, 13.0)):
        candidate = derive.enrich(
            Candidate(symbol="TEST", quote=_quote(low + 0.1, low=low), daily=_daily(atr)), cfg
        )
        minimum = next(level for level in candidate.levels if level.label == "minimum")
        expected = cfg.sizing.preferred_entry_atr_low + (1 - cfg.sizing.stop_buffer) * low / atr
        assert minimum.stop_distance_atr.value == pytest.approx(expected)
        assert minimum.stop_distance_atr.value > cfg.sizing.preferred_entry_atr_low
        assert derive.FLAG_STOP_INSIDE_NOISE not in minimum.flags


def test_shares_are_floored_and_the_binding_cap_is_named(cfg):
    candidate = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(56.60, low=56.00), daily=_daily(2.00)), cfg
    )
    for level in candidate.levels:
        assert isinstance(level.shares.value, int)
        assert level.binding_cap.value in {"risk", "liquidity", "dollar_vol", "concentration"}
        # Never size above the risk budget once the count is floored.
        assert level.actual_risk.value <= cfg.sizing.account * cfg.sizing.risk_pct + 1e-9


def test_the_arithmetic_matches_size_one_exactly(cfg):
    """pass2 must not develop a second opinion about the stop formula."""
    low, atr, price = 56.00, 2.00, 56.60
    candidate = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(price, low=low), daily=_daily(atr)), cfg
    )
    for level in candidate.levels:
        expected = size_one(
            SizingInputs(
                entry_price=level.entry.value,
                low_of_day=low,
                atr=atr,
                avg_vol_20=1e7,
                avg_dollar_vol_20=1e7,
            ),
            cfg,
        )
        assert level.risk_per_share.value == pytest.approx(expected["risk_per_share"])
        assert level.shares.value == math.floor(expected["shares"])
        assert candidate.stop.value == pytest.approx(expected["stop_price"])


def test_missing_liquidity_inputs_do_not_silently_bind(cfg):
    """An unevaluable cap must not be reported as the one that bound."""
    candidate = derive.enrich(
        Candidate(
            symbol="TEST", quote=_quote(56.60, low=56.00), daily=_daily(2.00, liquidity=False)
        ),
        cfg,
    )
    for level in candidate.levels:
        assert derive.FLAG_PARTIAL_CAPS in level.flags
        assert level.binding_cap.value in {"risk", "concentration"}


def test_no_session_low_means_no_stop_and_no_levels(cfg):
    """Nothing downstream may invent a low."""
    candidate = derive.enrich(
        Candidate(symbol="TEST", quote=_quote(56.60, low=None), daily=_daily()), cfg
    )
    assert not candidate.stop.ok
    assert candidate.levels == []
