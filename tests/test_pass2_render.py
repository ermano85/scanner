"""Provenance invariants and rendering.

The operator's stated failure mode is "a wrong number that looks right", so the model is
built to make an unlabelled number unconstructable rather than merely discouraged. These
tests pin that, and pin the two output properties that make the text usable: alerts above
everything, and pure ASCII.
"""

from __future__ import annotations

import datetime as dt

import pytest

from qms.config import load_scan_config
from qms.pass2 import render_json, render_text
from qms.pass2.model import Alert, Candidate, Packet, Quote, Value

NOW = dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_scan_config()


# ------------------------------------------------------------------ the Value contract


def test_a_value_cannot_hold_a_number_without_a_provenance():
    with pytest.raises(TypeError):
        Value(56.21)  # kind is required


def test_a_present_value_cannot_be_none():
    with pytest.raises(ValueError):
        Value(value=None, kind="fetched", source="test")


def test_an_unavailable_value_cannot_hold_a_number():
    with pytest.raises(ValueError):
        Value(value=56.21, kind="unavailable", reason="test")


def test_timestamps_must_be_timezone_aware():
    with pytest.raises(ValueError):
        Value.fetched(1.0, source="test", as_of=dt.datetime(2026, 7, 30, 10, 0))


def test_map_propagates_unavailability_rather_than_substituting():
    """There is no arithmetic path that turns a missing input into a number."""
    missing = Value.unavailable(reason="no data")
    assert missing.map(lambda v: v * 2).value is None
    assert not missing.map(lambda v: v * 2).ok

    present = Value.computed(2.0, formula="test")
    assert present.map(lambda v: v * 2).value == 4.0


# ------------------------------------------------------------------------- rendering


def _packet(**overrides) -> Packet:
    base = dict(
        generated_at=NOW,
        market_state="OPEN",
        session_date=dt.date(2026, 7, 30),
        minutes_since_open=30.0,
        session_close_et=None,
        half_day=False,
        account=10000.0,
        risk_budget=50.0,
    )
    base.update(overrides)
    return Packet(**base)


def _quote() -> Quote:
    return Quote(
        symbol="TEST",
        current_price=Value.fetched(56.60, source="yahoo:chart:meta", as_of=NOW),
        price_time=Value.fetched("10:00:00 EDT", source="yahoo:chart:meta", as_of=NOW),
        session_low=Value.fetched(56.21, source="yahoo:chart:1m", as_of=NOW),
        session_low_time=Value.fetched("09:59:00 EDT", source="yahoo:chart:1m", as_of=NOW),
        session_high=Value.fetched(56.94, source="yahoo:chart:1m", as_of=NOW),
        premarket_low_excluded=Value.fetched(
            56.00, source="yahoo:chart:1m", as_of=NOW, note="EXCLUDED from the session low"
        ),
        crosscheck=Value.unavailable(reason="not comparable"),
        is_live=True,
    )


def test_fetched_computed_and_missing_are_marked_distinctly(cfg):
    packet = _packet(candidates=[Candidate(symbol="TEST", quote=_quote())])
    text = render_text.render(packet, cfg)

    lines = {line.split()[1]: line for line in text.splitlines() if len(line.split()) > 1}
    # A fetched value carries its source; a computed one is marked '='; a gap is '!'.
    assert any(line.startswith(" ") and "[yahoo:chart:1m" in line for line in text.splitlines())
    assert any(line.startswith("!") and "UNAVAILABLE" in line for line in text.splitlines())
    assert "legend:" in text


def test_the_reason_travels_with_an_unavailable_field(cfg):
    quote = _quote()
    quote.session_low = Value.unavailable(reason="regular trading hours have not started")
    packet = _packet(candidates=[Candidate(symbol="TEST", quote=quote)])

    text = render_text.render(packet, cfg)
    assert "UNAVAILABLE" in text
    assert "regular trading hours have not started" in text


def test_critical_alerts_print_above_the_header(cfg):
    packet = _packet(
        alerts=[
            Alert(rank=5, tag="BELOW 10MA ON CLOSE", symbol="AAA", detail="quiet"),
            Alert(
                rank=0,
                tag="*** STOP BREACHED ***",
                symbol="BBB",
                detail="price is below the stop",
                critical=True,
            ),
        ]
    )
    text = render_text.render(packet, cfg)

    breached = text.index("*** STOP BREACHED ***")
    quiet = text.index("BELOW 10MA ON CLOSE")
    header = text.index("pass2   ")
    # Criticals first, then the quieter alerts, and only then the header. A position in
    # trouble outranks knowing what time it is.
    assert breached < quiet < header
    # The critical one is banner-wrapped so it cannot be skimmed past.
    assert text.startswith("!" * 78)


def test_the_output_is_pure_ascii(cfg):
    """The operator's console is cp1257; a stray em-dash becomes a question mark."""
    quote = _quote()
    packet = _packet(
        candidates=[Candidate(symbol="TEST", quote=quote)],
        alerts=[
            Alert(rank=0, tag="*** STOP BREACHED ***", symbol="X", detail="d", critical=True)
        ],
    )
    text = render_text.render(packet, cfg, verbose=True)
    text.encode("ascii")  # raises if anything slipped above 0x7F


def test_verbose_shows_the_formula(cfg):
    candidate = Candidate(symbol="TEST", quote=_quote())
    candidate.stop = Value.computed(55.93, formula="session low * 0.995 = 56.2100 * 0.995")
    packet = _packet(candidates=[candidate])

    assert "session low * 0.995" not in render_text.render(packet, cfg)
    assert "session low * 0.995" in render_text.render(packet, cfg, verbose=True)


def test_degraded_sources_are_named_and_do_not_stop_the_report(cfg):
    from qms.pass2.model import SourceFailure

    packet = _packet(
        candidates=[Candidate(symbol="TEST", quote=_quote())],
        failures=[SourceFailure(source="fmp", detail="rate limited", rate_limited=True)],
    )
    text = render_text.render(packet, cfg)
    assert "SOURCES DEGRADED" in text
    assert "fmp" in text
    assert "TEST" in text  # the rest of the packet still printed


def test_json_carries_the_same_envelope_on_every_field(cfg):
    packet = _packet(candidates=[Candidate(symbol="TEST", quote=_quote())])
    payload = render_json.render(packet, cfg)

    quote = payload["candidates"][0]["quote"]
    for field in ("current_price", "session_low", "session_high"):
        assert set(quote[field]) >= {"value", "kind", "source", "as_of"}
        assert quote[field]["kind"] in {"fetched", "computed", "unavailable"}

    # value is null if and only if kind is unavailable
    assert quote["session_low_crosscheck"]["kind"] == "unavailable"
    assert quote["session_low_crosscheck"]["value"] is None
    assert "reason" in quote["session_low_crosscheck"]


def test_json_is_serialisable(cfg):
    import json

    packet = _packet(candidates=[Candidate(symbol="TEST", quote=_quote())])
    json.dumps(render_json.render(packet, cfg))
