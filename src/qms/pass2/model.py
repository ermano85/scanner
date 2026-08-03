"""Every number pass 2 prints, wrapped in where it came from.

The operator's stated failure mode is not a missing value — it is "a wrong number that
looks right". Two things follow, and both are structural rather than conventions:

1. **A value cannot exist without a provenance.** `Value` has no default `kind`, so there
   is no way to construct one without saying whether it was measured, derived, or absent.
   A formatting convention for marking derived values decays in a month; a constructor
   argument does not.

2. **Absence is a first-class value, not None.** `Value.unavailable(reason=...)` carries
   *why* it is missing all the way to the renderer. The alternative — `None` floating
   through the arithmetic — is exactly how a gap acquires a plausible-looking filler.

The renderers never see a bare float, so "which of these did the machine measure and which
did it work out?" is answerable at a glance without the renderer having to know anything
about the field.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["fetched", "computed", "unavailable"]


@dataclass(frozen=True)
class Value:
    """A single field, with its provenance attached.

    `value` is None if and only if `kind == "unavailable"`.
    """

    value: Any
    kind: Kind
    source: str | None = None
    as_of: dt.datetime | None = None
    formula: str | None = None
    note: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "unavailable":
            if self.value is not None:
                raise ValueError(f"an unavailable Value must hold None, got {self.value!r}")
        elif self.value is None:
            raise ValueError(
                f"a {self.kind} Value must hold something; "
                "use Value.unavailable(reason=...) to express absence"
            )
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError(f"as_of must be timezone-aware, got {self.as_of!r}")

    @property
    def ok(self) -> bool:
        return self.kind != "unavailable"

    @classmethod
    def fetched(
        cls,
        value: Any,
        *,
        source: str,
        as_of: dt.datetime | None = None,
        note: str | None = None,
    ) -> Value:
        """A value that was measured by somebody else and transported here unchanged."""
        return cls(value=value, kind="fetched", source=source, as_of=as_of, note=note)

    @classmethod
    def computed(
        cls,
        value: Any,
        *,
        formula: str,
        source: str | None = None,
        as_of: dt.datetime | None = None,
        note: str | None = None,
    ) -> Value:
        """A value this tool worked out. `formula` is printed under --verbose."""
        return cls(
            value=value,
            kind="computed",
            source=source,
            as_of=as_of,
            formula=formula,
            note=note,
        )

    @classmethod
    def unavailable(cls, *, reason: str, source: str | None = None) -> Value:
        """A value that could not be obtained. A good outcome, when it is the true one."""
        return cls(value=None, kind="unavailable", source=source, reason=reason)

    def map(self, func, **kwargs) -> Value:
        """Apply `func` to the held value, propagating unavailability untouched.

        This is what stops a missing input from being quietly replaced downstream: there
        is no arithmetic path that turns an `unavailable` into a number.
        """
        if not self.ok:
            return self
        merged = {
            "kind": self.kind,
            "source": self.source,
            "as_of": self.as_of,
            "formula": self.formula,
            "note": self.note,
            **kwargs,
        }
        return Value(value=func(self.value), **merged)


def all_ok(*values: Value) -> bool:
    return all(v.ok for v in values)


def first_reason(*values: Value) -> str:
    """The reason the first unavailable input gives, for propagating into a derived field."""
    for v in values:
        if not v.ok:
            return v.reason or "upstream value unavailable"
    return "unavailable"


# ---------------------------------------------------------------------------- results


@dataclass(frozen=True)
class SourceFailure:
    """A source that did not answer. Reported, never silently swallowed."""

    source: str
    detail: str
    rate_limited: bool = False


@dataclass
class Quote:
    """Live-ish measurements for one symbol. All fields carry their own provenance."""

    symbol: str
    current_price: Value
    price_time: Value
    session_low: Value
    session_low_time: Value
    session_high: Value
    premarket_low_excluded: Value
    crosscheck: Value
    is_live: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class Daily:
    """Everything computed from daily bars."""

    symbol: str
    atr: Value
    adr_pct: Value
    smas: dict[int, Value] = field(default_factory=dict)
    sma_dist_pct: dict[int, Value] = field(default_factory=dict)
    sma_dist_adr: dict[int, Value] = field(default_factory=dict)
    prev_open: Value | None = None
    prev_high: Value | None = None
    prev_low: Value | None = None
    prev_close: Value | None = None
    prev_volume: Value | None = None
    prev_date: Value | None = None
    avg_dollar_vol: Value | None = None
    avg_vol: Value | None = None
    bars_through: dt.date | None = None


@dataclass
class EarningsReport:
    """The reconciled earnings verdict for one symbol."""

    symbol: str
    status: str  # confirmed | estimated | conflict | unknown
    next_date: Value
    timing: Value
    trading_days_until: Value
    last_past_date: Value
    candidates: list[dict] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)


@dataclass
class EntryLevel:
    """One of the three entry levels, with the order mechanics spelled out."""

    label: str
    atr_multiple: float
    entry: Value
    order_type: Value
    risk_per_share: Value
    stop_distance_atr: Value
    shares: Value
    binding_cap: Value
    position_cost: Value
    actual_risk: Value
    flags: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    """One ticker's complete block."""

    symbol: str
    quote: Quote
    daily: Daily | None = None
    earnings: EarningsReport | None = None
    stop: Value | None = None
    extended_move: Value | None = None
    extended_atr: Value | None = None
    levels: list[EntryLevel] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)


@dataclass
class Alert:
    """A monitor-mode alert. `rank` orders them; 0 is loudest."""

    rank: int
    tag: str
    symbol: str
    detail: str
    critical: bool = False


@dataclass
class PositionReport:
    symbol: str
    entry_date: Value
    entry_price: Value
    shares: Value
    current_price: Value
    current_stop: Value
    unrealized_dollars: Value
    unrealized_r: Value
    position_value: Value
    pct_of_account: Value
    days_held: Value
    stop_distance: Value
    trail_sma: Value
    last_close: Value
    below_sma_on_close: Value
    thesis: str = ""
    alerts: list[Alert] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)


@dataclass
class Packet:
    """The whole run."""

    generated_at: dt.datetime
    market_state: str
    session_date: dt.date | None
    minutes_since_open: float | None
    session_close_et: dt.datetime | None
    half_day: bool
    account: float
    risk_budget: float
    positions: list[PositionReport] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    forced_time: bool = False
    # Seconds by which --at runs ahead of the real clock. Data cannot exist for a moment
    # that has not happened, and "no trades recorded" would misdescribe that as a quiet
    # market rather than a request for the future.
    forced_ahead_seconds: float = 0.0
