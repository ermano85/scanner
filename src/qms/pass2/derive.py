"""The arithmetic that becomes possible once there is a session low and a current price.

Every constant comes from `config/scan.yaml`, and the share caps come from
`sizing/calculator.py::size_one` rather than being restated here. That is deliberate: the
nightly scan and this tool must not hold two opinions about what a stop is, and the way to
guarantee that is to have one implementation, not two that agree today.

**The order type is the highest-stakes field in this file.** An entry above the market is a
buy-stop; a buy *limit* placed above the market fills immediately at the quote instead of
resting. `journal/orders.csv` records that happening on CBRL on 2026-07-30: the limit
filled at 56.28, below the 30-minute low of 56.40, the 57.80 trigger was never reached, and
the position stopped out the same day for -1.44R. So the label is derived only from a
price confirmed live, and is `UNAVAILABLE` rather than assumed whenever it is not.
"""

from __future__ import annotations

import math

from qms.config import ScanConfig
from qms.pass2.model import Candidate, EntryLevel, Quote, Value
from qms.sizing.calculator import CAP_COLUMNS, SizingInputs, size_one

ORDER_BUY_STOP = "BUY-STOP"
ORDER_BUY_LIMIT = "BUY LIMIT"

FLAG_EXTENDED = "EXTENDED"
FLAG_STOP_INSIDE_NOISE = "STOP INSIDE NOISE"
FLAG_STOP_EXCEEDS_ATR = "STOP EXCEEDS ATR"
FLAG_PARTIAL_CAPS = "PARTIAL CAPS"

# Caps that need liquidity inputs. When those are missing the cap cannot be evaluated, and
# the run says so rather than pretending four caps were applied.
_LIQUIDITY_CAPS = ("liquidity", "dollar_vol")


def _levels(cfg: ScanConfig) -> list[tuple[str, float]]:
    sizing = cfg.sizing
    return [
        ("minimum", sizing.preferred_entry_atr_low),
        ("preferred", sizing.preferred_entry_atr_high),
        ("maximum", sizing.max_entry_atr_multiple),
    ]


def order_type(entry: float, quote: Quote) -> Value:
    """BUY-STOP above the market, BUY LIMIT below it, UNAVAILABLE without a live price."""
    price = quote.current_price
    if not price.ok:
        return Value.unavailable(
            reason="no current price, so the order type cannot be determined",
            source="derived",
        )
    if not quote.is_live:
        return Value.unavailable(
            reason=(
                f"the quote is not live ({price.note or 'stale'}), so which side of the "
                "market this entry sits on is unknown"
            ),
            source="derived",
        )

    current = float(price.value)
    if entry > current:
        return Value.computed(
            ORDER_BUY_STOP,
            formula=(
                f"entry {entry:.2f} is ABOVE the live price {current:.2f}, so the order "
                "must rest above the market: a buy-stop. A buy limit here would fill "
                "instantly at the quote."
            ),
            source="derived",
            as_of=price.as_of,
        )
    if entry < current:
        return Value.computed(
            ORDER_BUY_LIMIT,
            formula=(
                f"entry {entry:.2f} is BELOW the live price {current:.2f}, so the order "
                "rests below the market: a buy limit."
            ),
            source="derived",
            as_of=price.as_of,
        )
    return Value.computed(
        ORDER_BUY_LIMIT,
        formula=(
            f"entry {entry:.2f} equals the live price; a limit at the quote. It may fill "
            "immediately."
        ),
        source="derived",
        as_of=price.as_of,
    )


def enrich(candidate: Candidate, cfg: ScanConfig) -> Candidate:
    """Attach stop, extension check and the three entry levels. Mutates and returns."""
    sizing = cfg.sizing
    quote = candidate.quote
    low = quote.session_low
    atr = candidate.daily.atr if candidate.daily else Value.unavailable(reason="no daily data")
    price = quote.current_price

    if not low.ok:
        candidate.stop = Value.unavailable(
            reason=f"no session low ({low.reason})", source="derived"
        )
        candidate.extended_move = Value.unavailable(reason=low.reason or "no low", source="derived")
        candidate.extended_atr = Value.unavailable(reason=low.reason or "no low", source="derived")
        return candidate

    low_value = float(low.value)

    # ------------------------------------------------------------------------ stop
    candidate.stop = Value.computed(
        low_value * sizing.stop_buffer,
        formula=(
            f"session low * {sizing.stop_buffer} = {low_value:.4f} * {sizing.stop_buffer}"
        ),
        source="derived",
        as_of=low.as_of,
    )

    # ------------------------------------------------------- extension from the low
    if price.ok and quote.is_live:
        move = float(price.value) - low_value
        candidate.extended_move = Value.computed(
            move,
            formula=f"current {float(price.value):.4f} - session low {low_value:.4f}",
            source="derived",
            as_of=price.as_of,
        )
        if atr.ok and float(atr.value) > 0:
            in_atr = move / float(atr.value)
            candidate.extended_atr = Value.computed(
                in_atr,
                formula=f"(current - low) / ATR = {move:.4f} / {float(atr.value):.4f}",
                source="derived",
                as_of=price.as_of,
            )
            if in_atr > sizing.max_entry_atr_multiple:
                candidate.flags.append(FLAG_EXTENDED)
        else:
            candidate.extended_atr = Value.unavailable(
                reason="ATR unavailable", source="derived"
            )
    else:
        reason = (
            "no current price" if not price.ok else "the quote is not live"
        )
        candidate.extended_move = Value.unavailable(reason=reason, source="derived")
        candidate.extended_atr = Value.unavailable(reason=reason, source="derived")

    # ---------------------------------------------------------------- entry levels
    if not atr.ok:
        return candidate

    atr_value = float(atr.value)
    daily = candidate.daily
    have_liquidity = bool(
        daily and daily.avg_vol and daily.avg_vol.ok and daily.avg_dollar_vol and daily.avg_dollar_vol.ok
    )
    # An unevaluable cap must not silently bind. Infinity is the honest neutral element
    # here — "this constraint imposes no ceiling" — and the omission is reported as a flag
    # rather than left for the operator to infer from a share count.
    avg_vol = float(daily.avg_vol.value) if have_liquidity else math.inf
    avg_dollar_vol = float(daily.avg_dollar_vol.value) if have_liquidity else math.inf

    risk_budget = sizing.account * sizing.risk_pct

    for label, multiple in _levels(cfg):
        entry_price = low_value + atr_value * multiple
        sized = size_one(
            SizingInputs(
                entry_price=entry_price,
                low_of_day=low_value,
                atr=atr_value,
                avg_vol_20=avg_vol,
                avg_dollar_vol_20=avg_dollar_vol,
            ),
            cfg,
        )

        risk_share = sized["risk_per_share"]
        flags: list[str] = []

        # floor(): a share count is an integer. size_one leaves it fractional because the
        # nightly report ranks on it; an order ticket cannot.
        shares = int(math.floor(sized["shares"])) if sized["shares"] > 0 else 0
        binding = sized["binding_cap"]
        if not have_liquidity and binding in _LIQUIDITY_CAPS:
            binding = "risk"  # unreachable in practice, but never report a cap not evaluated
        if not have_liquidity:
            flags.append(FLAG_PARTIAL_CAPS)

        # Note this flag is unreachable at the shipped config. Stop distance in ATR units
        # works out to `multiple + (1 - stop_buffer) * low / ATR`, and the second term is
        # strictly positive, so at the minimum entry the ratio always exceeds
        # `preferred_entry_atr_low` itself. Implemented regardless because the rule is
        # [DOC] and a lower entry multiple would make it bite; the relationship is pinned
        # in tests/test_pass2_derive.py so the consequence stays visible rather than
        # looking like a check that silently never runs.
        stop_atr = risk_share / atr_value if atr_value > 0 else None
        if stop_atr is not None and stop_atr < sizing.preferred_entry_atr_low:
            flags.append(FLAG_STOP_INSIDE_NOISE)
        if sized["stop_exceeds_atr"]:
            flags.append(FLAG_STOP_EXCEEDS_ATR)

        cap_detail = ", ".join(
            f"{name}={sized[column]:,.1f}"
            for name, column in CAP_COLUMNS.items()
            if math.isfinite(sized[column])
        )

        candidate.levels.append(
            EntryLevel(
                label=label,
                atr_multiple=multiple,
                entry=Value.computed(
                    entry_price,
                    formula=(
                        f"session low + {multiple} * ATR = "
                        f"{low_value:.4f} + {multiple} * {atr_value:.4f}"
                    ),
                    source="derived",
                    as_of=low.as_of,
                ),
                order_type=order_type(entry_price, quote),
                risk_per_share=Value.computed(
                    risk_share,
                    formula=f"entry - stop = {entry_price:.4f} - {sized['stop_price']:.4f}",
                    source="derived",
                ),
                stop_distance_atr=(
                    Value.computed(
                        stop_atr,
                        formula=(
                            f"risk per share / ATR = {risk_share:.4f} / {atr_value:.4f}; "
                            f"flagged below {sizing.preferred_entry_atr_low}"
                        ),
                        source="derived",
                    )
                    if stop_atr is not None
                    else Value.unavailable(reason="ATR unavailable", source="derived")
                ),
                shares=Value.computed(
                    shares,
                    formula=(
                        f"floor(min of caps) = floor({sized['shares']:,.2f}); "
                        f"caps: {cap_detail}"
                        + ("" if have_liquidity else " (liquidity caps not evaluated)")
                    ),
                    source="derived",
                ),
                binding_cap=Value.computed(
                    binding,
                    formula=(
                        "the smallest cap wins; "
                        + (
                            f"risk = {risk_budget:,.2f} / {risk_share:.4f}"
                            if binding == "risk"
                            else f"concentration = {sizing.max_account_concentration} "
                            f"* {sizing.account:,.0f} / {entry_price:.4f}"
                            if binding == "concentration"
                            else f"{binding}"
                        )
                    ),
                    source="derived",
                ),
                position_cost=Value.computed(
                    shares * entry_price,
                    formula=f"shares * entry = {shares} * {entry_price:.4f}",
                    source="derived",
                ),
                actual_risk=Value.computed(
                    shares * risk_share,
                    formula=(
                        f"shares * risk per share = {shares} * {risk_share:.4f} "
                        f"(budget {risk_budget:,.2f})"
                    ),
                    source="derived",
                ),
                flags=flags,
            )
        )

    return candidate
