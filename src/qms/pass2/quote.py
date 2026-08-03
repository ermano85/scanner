"""The session low, and the current price. The two measurements pass 2 exists to get.

This is the critical path. The stop is derived from the session low, so a pre-market print
leaking into it produces a stop that is wrong in the dangerous direction and looks
perfectly reasonable. Three defences, in order of how much they buy:

1. **Filter, don't trust.** The regular-hours window is taken from
   `meta.tradingPeriods.regular` and the 1-minute bars are filtered to it here. Pre- and
   post-market bars are fetched *deliberately* so the pre-market low can be reported as
   excluded — a filter that shows its work, rather than one that asserts it ran.

2. **Derive it twice.** Yahoo also publishes `meta.regularMarketDayLow`, computed on their
   side. Verified against CBRL 2026-07-30: bars gave 55.25 @ 10:34 ET, the meta field gave
   55.25. When the two disagree, both are reported and neither is chosen.

3. **Refuse a stale quote.** With the market shut this endpoint happily returns a
   `regularMarketPrice` whose `regularMarketTime` is the *previous* session's close —
   observed on CBRL, 57.08 dressed as a live quote. If the last trade predates today's
   opening bell, the price is labelled a previous close and the session low is
   UNAVAILABLE. It must never reach the stop arithmetic.

Nothing here is ever cached. A cached session low is a wrong stop.
"""

from __future__ import annotations

import datetime as dt

from qms import calendar as mcal
from qms.config import ScanConfig
from qms.ingest.http import HttpClient, HttpError
from qms.ingest.universe import to_vendor_symbol
from qms.ingest.yahoo import CHART_URL
from qms.pass2.clock import SessionClock, et_time
from qms.pass2.model import Quote, Value

SOURCE = "yahoo:chart:1m"
SOURCE_META = "yahoo:chart:meta"

FLAG_LOW_MISMATCH = "LOW MISMATCH"
FLAG_STALE = "STALE QUOTE"
FLAG_NO_SESSION = "NO SESSION DATA"
FLAG_WINDOW_MISMATCH = "RTH WINDOW MISMATCH"
FLAG_PAYLOAD_DATE = "PAYLOAD IS A DIFFERENT SESSION"


def fetch_chart(client: HttpClient, symbol: str) -> dict:
    """One minute-bar payload for `symbol`, covering the current session with pre/post."""
    params = {
        "interval": "1m",
        "range": "1d",
        # Fetched on purpose. The pre-market low is printed as an excluded value, which
        # is the only way the operator can see the filter actually ran.
        "includePrePost": "true",
    }
    payload = client.get_json(CHART_URL.format(symbol=to_vendor_symbol(symbol)), params)
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        code = (chart["error"] or {}).get("code", "")
        raise HttpError(
            f"{symbol}: {chart['error']}",
            permanent=code in {"Not Found", "Bad Request"},
        )
    results = chart.get("result")
    if not results:
        raise HttpError(f"{symbol}: chart response carried no result")
    return results[0]


def _regular_window(result: dict) -> tuple[int, int] | None:
    """The regular-hours window as (start_epoch, end_epoch), from the vendor's own metadata."""
    meta = result.get("meta") or {}
    periods = (meta.get("tradingPeriods") or {}).get("regular")
    if periods:
        # Shape is [[{start, end, ...}]] — a list of days, each a list of segments.
        last = periods[-1]
        segment = last[-1] if isinstance(last, list) else last
        if isinstance(segment, dict) and "start" in segment and "end" in segment:
            return int(segment["start"]), int(segment["end"])
    current = (meta.get("currentTradingPeriod") or {}).get("regular")
    if isinstance(current, dict) and "start" in current and "end" in current:
        return int(current["start"]), int(current["end"])
    return None


def _rows(result: dict) -> list[tuple[int, float, float]]:
    """(epoch, low, high) for bars that actually traded. Null bars are dropped, not zero-filled."""
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    lows = quote.get("low") or []
    highs = quote.get("high") or []
    out: list[tuple[int, float, float]] = []
    for i, ts in enumerate(timestamps):
        low = lows[i] if i < len(lows) else None
        high = highs[i] if i < len(highs) else None
        if low is None or high is None:
            continue
        out.append((int(ts), float(low), float(high)))
    return out


def build_quote(
    symbol: str,
    result: dict,
    clock: SessionClock,
    cfg: ScanConfig,
) -> Quote:
    """Turn a raw chart payload into measured values, or into labelled absences."""
    meta = result.get("meta") or {}
    flags: list[str] = []
    tolerance = cfg.pass2.low_crosscheck_tolerance

    # ---------------------------------------------------------------- current price
    raw_price = meta.get("regularMarketPrice")
    raw_time = meta.get("regularMarketTime")
    price_at = (
        dt.datetime.fromtimestamp(int(raw_time), tz=dt.UTC) if raw_time is not None else None
    )

    session = clock.session_date or clock.reference_session
    session_open = mcal.session_open(session) if mcal.is_session(session) else None

    # A price whose last trade precedes today's opening bell is a previous close, whatever
    # field it arrived in. A trade stamped *after* `now` is not live either: under --at the
    # payload contains the rest of the session, and letting it through would quietly hand
    # back the future rather than the moment being reconstructed.
    is_live = bool(
        price_at is not None
        and session_open is not None
        and session_open <= price_at <= clock.now
        and clock.is_open
    )
    quote_age = (clock.now - price_at).total_seconds() if price_at else None
    if is_live and quote_age is not None and quote_age > cfg.pass2.stale_quote_seconds:
        is_live = False
        flags.append(FLAG_STALE)

    def _staleness_note() -> str:
        if not clock.is_open:
            return "PREVIOUS CLOSE - not a live quote"
        if quote_age is None:
            return "no trade timestamp - cannot confirm this is live"
        if quote_age < 0:
            # Only reachable under --at: the payload runs past the reconstructed moment.
            return f"trade is stamped {abs(int(quote_age))}s AFTER the --at moment - not live then"
        return f"last trade {int(quote_age)}s ago - stale"

    if raw_price is None:
        current_price = Value.unavailable(reason="vendor returned no price", source=SOURCE_META)
        price_time = Value.unavailable(reason="no price", source=SOURCE_META)
    elif is_live:
        current_price = Value.fetched(float(raw_price), source=SOURCE_META, as_of=price_at)
        price_time = Value.fetched(et_time(price_at), source=SOURCE_META, as_of=price_at)
    else:
        note = _staleness_note()
        if FLAG_STALE not in flags:
            flags.append(FLAG_STALE)
        current_price = Value.fetched(
            float(raw_price), source=SOURCE_META, as_of=price_at, note=note
        )
        price_time = Value.fetched(
            et_time(price_at) if price_at else "unknown",
            source=SOURCE_META,
            as_of=price_at,
            note=note,
        )

    # ------------------------------------------------------------- regular-hours low
    window = _regular_window(result)
    if window is None:
        flags.append(FLAG_NO_SESSION)
        unavailable = Value.unavailable(
            reason="vendor gave no regular-hours window; cannot prove pre-market is excluded",
            source=SOURCE,
        )
        return Quote(
            symbol=symbol,
            current_price=current_price,
            price_time=price_time,
            session_low=unavailable,
            session_low_time=unavailable,
            session_high=unavailable,
            premarket_low_excluded=unavailable,
            crosscheck=unavailable,
            is_live=is_live,
            flags=flags,
        )

    start_epoch, end_epoch = window
    payload_date = dt.datetime.fromtimestamp(start_epoch, tz=mcal.EXCHANGE_TZ).date()

    # The intraday endpoint only ever serves the *current* session, so asking for a past
    # date via --at cannot be satisfied. Saying so plainly beats the alternative, which
    # looks indistinguishable from "the market has not opened yet" and would quietly
    # invite the operator to believe a past session had no low.
    if payload_date != session:
        flags.append(FLAG_PAYLOAD_DATE)
        reason = (
            f"intraday payload covers {payload_date.isoformat()} but this run is for "
            f"{session.isoformat()}; the 1-minute endpoint only serves the current "
            "session, so a past session's low cannot be reconstructed"
        )
        gap = Value.unavailable(reason=reason, source=SOURCE)
        return Quote(
            symbol=symbol,
            current_price=current_price,
            price_time=price_time,
            session_low=gap,
            session_low_time=gap,
            session_high=gap,
            premarket_low_excluded=gap,
            crosscheck=gap,
            is_live=False,
            flags=flags,
        )

    # Same day, but do the bells agree? If the vendor and the exchange calendar disagree
    # about when the session began, the session low is not a well-defined quantity.
    if session_open is not None and mcal.is_session(session):
        calendar_start = int(session_open.timestamp())
        if abs(calendar_start - start_epoch) > 60:
            flags.append(FLAG_WINDOW_MISMATCH)

    rows = _rows(result)
    now_epoch = int(clock.now.timestamp())
    session_begun = now_epoch >= start_epoch
    session_complete = now_epoch >= end_epoch
    # A 1-minute bar is stamped at its OPEN, so `>= start` is exactly what drops the 09:29
    # bar, and `< end` drops anything at or past the closing bell. The upper bound is
    # truncated at `now` so that --at is a real time machine and not just a header change;
    # the final bar may be partially formed, which is correct for a low "so far".
    upper = min(end_epoch, now_epoch)
    rth = [r for r in rows if start_epoch <= r[0] < upper]
    # Pre-market means *this session's* pre-market. Bounding the lower edge matters
    # because a multi-day payload would otherwise sweep in earlier sessions and report a
    # days-old print as "today's excluded pre-market low" — a wrong number that looks right,
    # in the very field whose job is to demonstrate correctness.
    premarket_open = int(
        (
            dt.datetime.fromtimestamp(start_epoch, tz=mcal.EXCHANGE_TZ).replace(
                hour=4, minute=0, second=0, microsecond=0
            )
        ).timestamp()
    )
    pre = [r for r in rows if premarket_open <= r[0] < start_epoch]

    # Was the window shortened below the data we hold? Then the vendor's own day-low
    # covers a longer span than ours and disagreement is expected, not a fault.
    latest_bar = max((r[0] for r in rows), default=None)
    time_shifted = latest_bar is not None and upper <= latest_bar

    if not session_begun:
        # The opening bell has not rung. There is no session low today, and the vendor's
        # day-low field at this moment still holds the *previous* session's — which is
        # exactly the value that must never reach a stop.
        reason = "regular trading hours have not started; no session low exists yet"
        session_low = Value.unavailable(reason=reason, source=SOURCE)
        session_low_time = Value.unavailable(reason=reason, source=SOURCE)
        session_high = Value.unavailable(reason=reason, source=SOURCE)
        crosscheck = Value.unavailable(reason="no session low to check", source=SOURCE)
    elif not rth:
        reason = "no trades recorded in regular hours so far"
        session_low = Value.unavailable(reason=reason, source=SOURCE)
        session_low_time = Value.unavailable(reason=reason, source=SOURCE)
        session_high = Value.unavailable(reason=reason, source=SOURCE)
        crosscheck = Value.unavailable(reason="no session low to check", source=SOURCE)
    else:
        low_epoch, low_value, _ = min(rth, key=lambda r: r[1])
        high_value = max(r[2] for r in rth)
        low_at = dt.datetime.fromtimestamp(low_epoch, tz=dt.UTC)
        bar_count = len(rth)

        span = "session complete" if session_complete else "so far"
        session_low = Value.fetched(
            low_value,
            source=SOURCE,
            as_of=low_at,
            note=f"regular hours only, {span}, {bar_count} bars from the opening bell",
        )
        session_low_time = Value.fetched(et_time(low_at), source=SOURCE, as_of=low_at)
        session_high = Value.fetched(high_value, source=SOURCE, as_of=clock.now)

        vendor_low = meta.get("regularMarketDayLow")
        if vendor_low is None:
            crosscheck = Value.unavailable(
                reason="vendor published no day-low to check against", source=SOURCE_META
            )
        elif time_shifted:
            crosscheck = Value.unavailable(
                reason=f"not comparable: window truncated at {et_time(clock.now)} by --at",
                source=SOURCE_META,
            )
        elif abs(float(vendor_low) - low_value) <= tolerance:
            crosscheck = Value.fetched(
                float(vendor_low),
                source=SOURCE_META,
                note=f"agrees with the bar-derived low within {tolerance}",
            )
        else:
            flags.append(FLAG_LOW_MISMATCH)
            crosscheck = Value.fetched(
                float(vendor_low),
                source=SOURCE_META,
                note=(
                    f"DISAGREES with the bar-derived low {low_value:.4f} "
                    f"by {abs(float(vendor_low) - low_value):.4f} - neither is chosen"
                ),
            )

    # The excluded pre-market low, printed as evidence the filter ran.
    if pre:
        pre_epoch, pre_low, _ = min(pre, key=lambda r: r[1])
        pre_at = dt.datetime.fromtimestamp(pre_epoch, tz=dt.UTC)
        premarket = Value.fetched(
            pre_low,
            source=SOURCE,
            as_of=pre_at,
            note=f"EXCLUDED from the session low (pre-market, {et_time(pre_at)})",
        )
    else:
        premarket = Value.unavailable(
            reason="no pre-market bars returned", source=SOURCE
        )

    return Quote(
        symbol=symbol,
        current_price=current_price,
        price_time=price_time,
        session_low=session_low,
        session_low_time=session_low_time,
        session_high=session_high,
        premarket_low_excluded=premarket,
        crosscheck=crosscheck,
        is_live=is_live,
        flags=flags,
    )
