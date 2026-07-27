"""Scan A gates, triggers, ranking and the as_of_date contract.

These run on synthetic panels rather than the bar store, so the suite never touches the
network and never depends on what the market did.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from qms.config import load_scan_config
from qms.features.registry import build_features
from qms.rules import gates, rank, triggers
from qms.rules.scan_a import latest_cross_section, resolve_as_of, run_scan_a

CFG = load_scan_config()

SESSION_A = dt.date(2026, 7, 22)
SESSION_B = dt.date(2026, 7, 23)
SESSION_C = dt.date(2026, 7, 24)


# --------------------------------------------------------------------- panel builders


def _series(
    symbol: str,
    n: int = 260,
    start_price: float = 100.0,
    daily_drift: float = 0.004,
    range_pct: float = 0.06,
    volume: float = 5_000_000.0,
    end_date: dt.date = SESSION_C,
) -> pl.DataFrame:
    closes, highs, lows = [], [], []
    price = start_price
    for _ in range(n):
        price *= 1.0 + daily_drift
        closes.append(price)
        highs.append(price * (1 + range_pct / 2))
        lows.append(price * (1 - range_pct / 2))

    # Business-day dates ending at `end_date`, so the panel lines up with real sessions.
    dates: list[dt.date] = []
    cursor = end_date
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
            "volume": [volume] * n,
            "adjclose": closes,
        },
        schema_overrides={"date": pl.Date},
    )


def _panel(*frames: pl.DataFrame) -> pl.DataFrame:
    return build_features(pl.concat(frames), CFG)


def _latest(features: pl.DataFrame) -> pl.DataFrame:
    """Just the frame from `latest_cross_section`, which also returns bookkeeping."""
    frame, _reference, _dropped = latest_cross_section(features, CFG)
    return frame


# ------------------------------------------------------------------- as_of semantics


def test_as_of_date_excludes_its_own_session():
    """`as_of_date` is the session the watchlist is FOR; bars are strictly before it.

    The Monday-evening run passes Tuesday and must see Monday's close but not Tuesday's.
    """
    features = _panel(_series("AAA"))
    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())
    assert result.data_date == SESSION_B, "the as_of session's own bar must be excluded"


def test_earlier_as_of_sees_less_data():
    features = _panel(_series("AAA"))
    earlier = run_scan_a(as_of_date=SESSION_B, cfg=CFG, features=features, earnings=pl.DataFrame())
    later = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())
    assert earlier.data_date < later.data_date


def test_filtering_by_as_of_equals_never_having_the_data():
    """The strongest form of the as_of contract, and the one a backtest depends on.

    A scan run for a past date against today's full store must produce exactly what it
    would have produced against a store that physically ended on that date. Any
    difference means some feature or ranking step saw the future.

    Verified at full scale on the real store as well: as_of 2026-07-10 over 11,326
    symbols gave byte-identical ranked tables both ways.
    """
    features = _panel(
        _series("AAA", daily_drift=0.006),
        _series("BBB", daily_drift=0.002),
        _series("CCC", daily_drift=0.004),
    )
    past = SESSION_A

    filtered = run_scan_a(as_of_date=past, cfg=CFG, features=features, earnings=pl.DataFrame())
    truncated = run_scan_a(
        as_of_date=past,
        cfg=CFG,
        features=features.filter(pl.col("date") < past),
        earnings=pl.DataFrame(),
    )

    assert filtered.data_date == truncated.data_date
    assert filtered.candidates.equals(truncated.candidates)


def test_resolve_as_of_defaults_to_the_next_session():
    from qms.calendar import last_completed_session, next_session

    assert resolve_as_of(None) == next_session(last_completed_session())
    assert resolve_as_of(SESSION_B) == SESSION_B


def test_latest_cross_section_is_one_row_per_symbol():
    features = _panel(_series("AAA"), _series("BBB"))
    latest = _latest(features)
    assert latest.height == 2
    assert sorted(latest["symbol"].to_list()) == ["AAA", "BBB"]


def test_cross_section_ignores_a_ragged_trailing_edge():
    """The real 2026-07-24 case: a few symbols carry a bar nobody else has.

    Ranking those against everyone else's prior close hands them an extra day of return
    and quietly distorts every cross-sectional percentile.
    """
    normal = [_series(f"N{i}") for i in range(10)]
    ahead = _series("AHEAD", end_date=dt.date(2026, 7, 27))
    frame, reference, _dropped = latest_cross_section(_panel(*normal, ahead), CFG)

    assert reference == SESSION_C, "reference is the well-covered session, not max(date)"
    assert frame["date"].unique().to_list() == [SESSION_C], "one session, not two"
    assert frame.height == 11, "the early symbol is kept, just at the reference date"


def test_cross_section_drops_symbols_that_stopped_trading():
    """A halted name keeps its last bar forever and would otherwise pass every gate."""
    fresh = [_series(f"N{i}") for i in range(10)]
    quiet = _series("QUIET", end_date=dt.date(2026, 3, 2))
    frame, _reference, dropped = latest_cross_section(_panel(*fresh, quiet), CFG)

    assert dropped == 1
    assert "QUIET" not in frame["symbol"].to_list()


# -------------------------------------------------------------------------- the gates


def test_price_gate_rejects_sub_five_dollar_names():
    features = _panel(_series("PENNY", start_price=1.0, daily_drift=0.0))
    latest = gates.apply_liquidity_gates(_latest(features), CFG)
    assert not latest["pass_price"][0]


def test_dollar_volume_gate_rejects_thin_names():
    features = _panel(_series("THIN", volume=1_000.0))
    latest = gates.apply_liquidity_gates(_latest(features), CFG)
    assert not latest["pass_dollar_vol"][0]


def test_adr_gate_rejects_a_sleepy_name():
    """A 0.5%-range stock cannot produce the moves this strategy needs."""
    features = _panel(_series("SLEEPY", range_pct=0.005))
    latest = gates.apply_liquidity_gates(_latest(features), CFG)
    assert not latest["pass_adr"][0]
    assert latest["pass_price"][0] and latest["pass_dollar_vol"][0]


def test_downtrend_fails_the_trend_gates():
    features = _panel(_series("FALLING", start_price=500.0, daily_drift=-0.004))
    latest = gates.apply_trend_gates(_latest(features), CFG)
    assert not latest["pass_ma_stack"][0]
    assert not latest["pass_above_50"][0]


def test_uptrend_passes_the_trend_gates():
    features = _panel(_series("RISING"))
    latest = gates.apply_trend_gates(_latest(features), CFG)
    assert latest["pass_ma_stack"][0]
    assert latest["pass_above_50"][0]


def test_null_gate_values_are_treated_as_failures():
    """A symbol without enough history must not slip through on a null."""
    features = _panel(_series("NEW", n=25))
    latest = gates.apply_trend_gates(_latest(features), CFG)
    assert latest["ma_stack_ok"][0] is None
    assert latest["pass_ma_stack"][0] is False


# --------------------------------------------------------------- earnings blackout


def _earnings(symbol: str, date: dt.date, when: str = "amc") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol],
            "earnings_date": [date],
            "when": [when],
            "eps_forecast": [None],
            "market_cap": [None],
            "fiscal_quarter_ending": [None],
        },
        schema_overrides={
            "earnings_date": pl.Date,
            "eps_forecast": pl.Float64,
            "market_cap": pl.Float64,
            "fiscal_quarter_ending": pl.Utf8,
        },
    )


def test_earnings_inside_the_blackout_are_rejected():
    latest = _latest(_panel(_series("AAA")))
    soon = dt.date(2026, 7, 27)  # the next session after as_of 2026-07-24
    out = gates.attach_earnings(latest, _earnings("AAA", soon), CFG, SESSION_C)
    assert out["days_to_earnings"][0] == 1
    assert not out["pass_earnings"][0]


def test_earnings_beyond_the_blackout_are_allowed():
    latest = _latest(_panel(_series("AAA")))
    far = dt.date(2026, 8, 14)
    out = gates.attach_earnings(latest, _earnings("AAA", far), CFG, SESSION_C)
    assert out["days_to_earnings"][0] > CFG.scan_a.gates.earnings_blackout_days
    assert out["pass_earnings"][0]


def test_before_open_reports_get_one_extra_day_of_blackout():
    """A bmo release gaps before you can act, so the exposure starts a session earlier."""
    latest = _latest(_panel(_series("AAA")))
    boundary = dt.date(2026, 7, 30)  # exactly blackout+1 sessions out

    amc = gates.attach_earnings(latest, _earnings("AAA", boundary, "amc"), CFG, SESSION_C)
    bmo = gates.attach_earnings(latest, _earnings("AAA", boundary, "bmo"), CFG, SESSION_C)

    assert amc["days_to_earnings"][0] == bmo["days_to_earnings"][0]
    assert amc["pass_earnings"][0], "amc at the boundary is allowed"
    assert not bmo["pass_earnings"][0], "bmo at the same boundary is not"


def test_unknown_earnings_passes_and_is_tagged():
    """Hard-failing on absent data would silently delete a slice of the universe."""
    latest = _latest(_panel(_series("AAA")))
    out = gates.attach_earnings(latest, _earnings("ZZZ", dt.date(2026, 7, 27)), CFG, SESSION_C)
    assert out["pass_earnings"][0]
    assert out["earnings_unknown"][0]
    assert out["next_earnings_date"][0] is None


def test_past_earnings_are_ignored():
    latest = _latest(_panel(_series("AAA")))
    out = gates.attach_earnings(latest, _earnings("AAA", dt.date(2026, 7, 1)), CFG, SESSION_C)
    assert out["pass_earnings"][0]
    assert out["earnings_unknown"][0]


def test_empty_earnings_feed_does_not_block_everything():
    latest = _latest(_panel(_series("AAA")))
    out = gates.attach_earnings(latest, pl.DataFrame(), CFG, SESSION_C)
    assert out["pass_earnings"][0]
    assert out["earnings_unknown"][0]


# ------------------------------------------------------------------------ percentiles


def test_momentum_percentile_is_the_max_of_the_three_ranks():
    """Not the percentile of the max gain — the lookbacks are not comparable in percent."""
    frame = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "gain_1m": [1.0, 2.0, 3.0, 40.0],
            "gain_3m": [50.0, 2.0, 3.0, 4.0],
            "gain_6m": [1.0, 2.0, 3.0, 4.0],
        }
    )
    out = rank.add_momentum_percentiles(frame)
    by_symbol = dict(zip(out["symbol"], out["momentum_pctile"], strict=True))
    assert by_symbol["A"] == pytest.approx(1.0), "top of the 3m ranking"
    assert by_symbol["D"] == pytest.approx(1.0), "top of the 1m ranking"
    assert by_symbol["B"] < by_symbol["C"]


def test_null_momentum_does_not_rank_as_the_worst():
    """A load-bearing polars assumption, pinned because the whole ranking rests on it.

    `rank()` must leave nulls null and rank only the populated values, and
    `max_horizontal` must ignore nulls. If nulls instead ranked as the smallest value, a
    recently listed name with no 6-month history would be dragged to the bottom of the
    cross-section by a lookback it simply cannot have yet.
    """
    frame = pl.DataFrame(
        {
            "symbol": ["NEW", "OLD_A", "OLD_B", "OLD_C"],
            "gain_1m": [99.0, 1.0, 2.0, 3.0],
            "gain_3m": [None, 1.0, 2.0, 3.0],
            "gain_6m": [None, 1.0, 2.0, 3.0],
        }
    )
    out = rank.add_momentum_percentiles(frame)
    by_symbol = dict(zip(out["symbol"], out["momentum_pctile"], strict=True))

    assert out["gain_3m_pctile"][0] is None, "a null input yields a null percentile"
    assert by_symbol["NEW"] == pytest.approx(1.0), "ranked on the lookback it does have"
    # OLD_C also scores 1.0 — it tops its own lookbacks — so the meaningful comparison is
    # against the weakest name, which is what NEW would sink to if nulls ranked lowest.
    assert by_symbol["NEW"] > by_symbol["OLD_A"]
    assert by_symbol["OLD_A"] < by_symbol["OLD_B"] < by_symbol["OLD_C"]


def test_percentiles_are_computed_over_the_liquid_population_only():
    """Illiquid names must not dilute the cross-section the top decile is measured against.

    Two liquid names plus a wall of junk: the liquid ones should keep their relative
    ranking rather than both being pushed into the same percentile bucket.
    """
    liquid = [_series(f"L{i}", daily_drift=0.001 * i) for i in range(1, 3)]
    junk = [_series(f"J{i}", volume=100.0, start_price=1.0) for i in range(20)]
    features = _panel(*liquid, *junk)

    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())
    assert result.universe_size == 22
    assert result.after_liquidity == 2, "only the liquid names should be ranked"


# -------------------------------------------------------------------------- triggers


def test_trigger_tags_are_attached_and_never_filter():
    features = _panel(_series("AAA"))
    latest = _latest(features)
    tagged = triggers.add_trigger_tags(latest, CFG)
    assert tagged.height == latest.height, "tagging must not drop rows"
    assert "triggers" in tagged.columns
    assert "trigger_count" in tagged.columns


def test_at_ma_trigger_fires_when_price_sits_on_the_average():
    features = _panel(_series("AAA", daily_drift=0.0005))
    tagged = triggers.add_trigger_tags(_latest(features), CFG)
    row = tagged.row(0, named=True)
    assert abs(row["dist_to_sma_10_adr"]) <= CFG.scan_a.triggers.near_ma_adr
    assert row["trigger_at_10ma"]
    assert "AT_10MA" in row["triggers"]


def test_untagged_candidate_still_appears():
    """A name with no trigger ranks last rather than vanishing. Spec §4.2."""
    frame = pl.DataFrame(
        {
            "symbol": ["A"],
            "close": [100.0],
            "dist_to_sma_10_adr": [9.0],
            "dist_to_sma_20_adr": [9.0],
            "dist_to_sma_50_adr": [9.0],
            "pivot_high_5": [200.0],
            "pivot_high_15": [200.0],
            "pivot_high_40": [200.0],
        }
    )
    tagged = triggers.add_trigger_tags(frame, CFG)
    assert tagged.height == 1
    assert tagged["triggers"][0] == ""
    assert tagged["trigger_count"][0] == 0


# --------------------------------------------------------------------------- ranking


def test_score_is_higher_for_stronger_momentum():
    frame = pl.DataFrame(
        {
            "symbol": ["WEAK", "STRONG"],
            "momentum_pctile": [0.1, 0.99],
            "trigger_count": [1, 1],
            "tightness_adr_15": [5.0, 5.0],
            "contraction": [1.0, 1.0],
            "vol_dryup": [1.0, 1.0],
            "low_slope_pct_15": [0.1, 0.1],
            "depth_from_high_15": [5.0, 5.0],
        }
    )
    scored = rank.add_score(frame, CFG)
    by_symbol = dict(zip(scored["symbol"], scored["score"], strict=True))
    assert by_symbol["STRONG"] > by_symbol["WEAK"]


def test_more_triggers_scores_higher_all_else_equal():
    frame = pl.DataFrame(
        {
            "symbol": ["ONE", "THREE"],
            "momentum_pctile": [0.9, 0.9],
            "trigger_count": [1, 3],
            "tightness_adr_15": [5.0, 5.0],
            "contraction": [1.0, 1.0],
            "vol_dryup": [1.0, 1.0],
            "low_slope_pct_15": [0.1, 0.1],
            "depth_from_high_15": [5.0, 5.0],
        }
    )
    scored = rank.add_score(frame, CFG)
    by_symbol = dict(zip(scored["symbol"], scored["score"], strict=True))
    assert by_symbol["THREE"] > by_symbol["ONE"]


def test_missing_ext_metrics_score_neutrally_rather_than_zero():
    """A recent listing must not be punished for windows that have not filled."""
    frame = pl.DataFrame(
        {
            "symbol": ["FULL", "SPARSE"],
            "momentum_pctile": [0.9, 0.9],
            "trigger_count": [1, 1],
            "tightness_adr_15": [5.0, None],
            "contraction": [1.0, None],
            "vol_dryup": [1.0, None],
            "low_slope_pct_15": [0.1, None],
            "depth_from_high_15": [5.0, None],
        }
    )
    scored = rank.add_score(frame, CFG)
    assert scored["score"].null_count() == 0


def test_ranking_respects_max_candidates():
    n = CFG.scan_a.ranking.max_candidates + 10
    frame = pl.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(n)],
            "score": [float(i) for i in range(n)],
        }
    )
    ranked = rank.rank_candidates(frame, CFG)
    assert ranked.height == CFG.scan_a.ranking.max_candidates
    assert ranked["rank"].to_list() == list(range(1, CFG.scan_a.ranking.max_candidates + 1))
    assert ranked["symbol"][0] == f"S{n - 1}", "highest score ranks first"


# ------------------------------------------------------------------- end to end shape


def test_scan_reports_the_funnel_and_survivors():
    strong = _series("STRONG", daily_drift=0.006)
    weak = _series("WEAK", start_price=500.0, daily_drift=-0.005)
    penny = _series("PENNY", start_price=1.0, daily_drift=0.001)
    features = _panel(strong, weak, penny)

    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())

    assert result.universe_size == 3
    assert set(result.rejections) >= {"price", "dollar_vol", "adr", "ma_stack", "above_50"}
    assert "STRONG" in result.candidates["symbol"].to_list()
    assert "PENNY" not in result.candidates["symbol"].to_list()


def test_liquidity_rejections_are_counted_before_filtering():
    """Regression: counting them on the post-filter frame reported 0 for every gate.

    The funnel is how a disappointing scan gets explained, so a gate that always reports
    zero removals is worse than useless — it points the reader at the wrong culprit.
    """
    features = _panel(
        _series("GOOD"),
        _series("PENNY", start_price=1.0),
        _series("THIN", volume=100.0),
        _series("SLEEPY", range_pct=0.004),
    )
    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())

    assert result.rejections["price"] >= 1, "the sub-$5 name must be counted"
    assert result.rejections["dollar_vol"] >= 1
    assert result.rejections["adr"] >= 1


def test_scan_on_empty_features_does_not_crash():
    empty = _panel(_series("AAA")).clear()
    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=empty, earnings=pl.DataFrame())
    assert result.universe_size == 0
    assert result.candidates.is_empty()


def test_candidates_carry_sizing_columns():
    features = _panel(_series("STRONG", daily_drift=0.006))
    result = run_scan_a(as_of_date=SESSION_C, cfg=CFG, features=features, earnings=pl.DataFrame())
    if result.candidates.is_empty():
        pytest.skip("no survivor in this fixture")
    for column in ("shares", "binding_cap", "stop_price", "max_entry", "extended"):
        assert column in result.candidates.columns
