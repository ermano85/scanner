"""Open positions: what they are worth now, and what needs attention.

An open position in trouble outranks any new idea, so these alerts print above everything
else in the output.

The parser's central rule is that **`current_stop` is not assumed to be a number.** The
live `journal/positions.csv` currently reads
`NONE - CANCELLED BY HAND 2026-07-29`, which is the operator's own record that no stop
order is live at the broker. A parser that called `float()` on that would crash, and one
that coerced it to 0.0 would compute a position sitting comfortably above its stop, which
is worse. It is read as text, and its unparseability is itself the alert.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import polars as pl

from qms import calendar as mcal
from qms.config import ScanConfig
from qms.pass2.model import Alert, PositionReport, SourceFailure, Value

SOURCE_CSV = "journal/positions.csv"

REQUIRED_COLUMNS = [
    "symbol",
    "entry_date",
    "entry_price",
    "shares",
    "initial_stop",
    "current_stop",
    "risk_dollars",
    "partial_taken",
    "thesis",
]

# rank 0 is loudest. The two criticals are the ones that mean money is unprotected right
# now, as opposed to a position that merely needs a decision today.
ALERT_STOP_BREACHED = ("*** STOP BREACHED ***", 0, True)
ALERT_NO_STOP = ("*** NO STOP ORDER LIVE ***", 1, True)
ALERT_CONCENTRATION = ("CONCENTRATION", 2, False)
ALERT_PARTIAL = ("PARTIAL WINDOW", 3, False)
ALERT_EARNINGS = ("EARNINGS SOON", 4, False)
ALERT_BELOW_MA = ("BELOW 10MA ON CLOSE", 5, False)


def _number(raw: str | None) -> float | None:
    """Parse a CSV cell as a number, or return None. Never raises, never guesses."""
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_positions(path: Path) -> tuple[list[dict], list[SourceFailure]]:
    """Rows as plain dicts, plus any structural complaint about the file itself."""
    failures: list[SourceFailure] = []
    if not path.exists():
        return [], [SourceFailure(source=SOURCE_CSV, detail=f"{path} not found")]

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [r for r in reader if (r.get("symbol") or "").strip()]
        header = reader.fieldnames or []

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        failures.append(
            SourceFailure(
                source=SOURCE_CSV,
                detail=f"{path.name} is missing column(s): {', '.join(missing)}",
            )
        )
    return rows, failures


def evaluate(
    row: dict,
    cfg: ScanConfig,
    *,
    today: dt.date,
    price: Value,
    is_live: bool,
    bars: pl.DataFrame,
    earnings_days: int | None,
) -> PositionReport:
    """One position's numbers and its alerts."""
    sizing = cfg.sizing
    symbol = (row.get("symbol") or "").strip().upper()
    alerts: list[Alert] = []

    entry_price = _number(row.get("entry_price"))
    shares = _number(row.get("shares"))
    initial_stop = _number(row.get("initial_stop"))
    risk_dollars = _number(row.get("risk_dollars"))
    entry_date = _date(row.get("entry_date"))
    raw_stop = (row.get("current_stop") or "").strip()
    current_stop = _number(raw_stop)
    partial = (row.get("partial_taken") or "").strip().lower()

    def num(value: float | None, label: str, source: str = SOURCE_CSV) -> Value:
        if value is None:
            return Value.unavailable(
                reason=f"{label} is not a number in {SOURCE_CSV}", source=source
            )
        return Value.fetched(value, source=source)

    v_entry = num(entry_price, "entry_price")
    v_shares = num(shares, "shares")
    v_risk = num(risk_dollars, "risk_dollars")
    v_entry_date = (
        Value.fetched(entry_date, source=SOURCE_CSV)
        if entry_date
        else Value.unavailable(reason="entry_date is not a date", source=SOURCE_CSV)
    )

    # ------------------------------------------------------------------ current stop
    if current_stop is None:
        v_stop = Value.unavailable(
            reason=(
                f"current_stop reads {raw_stop!r}, which is not a price"
                if raw_stop
                else "current_stop is empty"
            ),
            source=SOURCE_CSV,
        )
        tag, rank, critical = ALERT_NO_STOP
        alerts.append(
            Alert(
                rank=rank,
                tag=tag,
                symbol=symbol,
                detail=(
                    f"current_stop reads {raw_stop!r} - no stop order is live at the broker. "
                    f"This position is unprotected."
                    if raw_stop
                    else "current_stop is empty - no stop order is live at the broker."
                ),
                critical=critical,
            )
        )
    else:
        v_stop = Value.fetched(current_stop, source=SOURCE_CSV)

    # ---------------------------------------------------------------------- P&L
    have_px = price.ok and entry_price is not None and shares is not None
    if have_px:
        px = float(price.value)
        pnl = (px - entry_price) * shares
        value_now = px * shares
        note = None if is_live else f"computed on a non-live price ({price.note or 'stale'})"
        v_pnl = Value.computed(
            pnl,
            formula=f"(price - entry) * shares = ({px:.4f} - {entry_price:.4f}) * {shares:g}",
            source="derived",
            as_of=price.as_of,
            note=note,
        )
        v_value = Value.computed(
            value_now,
            formula=f"price * shares = {px:.4f} * {shares:g}",
            source="derived",
            as_of=price.as_of,
            note=note,
        )
        v_pct = Value.computed(
            value_now / sizing.account * 100.0,
            formula=f"position value / account * 100 = {value_now:,.2f} / {sizing.account:,.0f} * 100",
            source="derived",
            note=note,
        )
        if risk_dollars:
            v_r = Value.computed(
                pnl / risk_dollars,
                formula=f"P&L / risk_dollars = {pnl:,.2f} / {risk_dollars:,.2f}",
                source="derived",
                note=note,
            )
        else:
            v_r = Value.unavailable(
                reason="risk_dollars is missing or zero, so R cannot be computed",
                source="derived",
            )

        if value_now > sizing.max_account_concentration * sizing.account:
            tag, rank, critical = ALERT_CONCENTRATION
            alerts.append(
                Alert(
                    rank=rank,
                    tag=tag,
                    symbol=symbol,
                    detail=(
                        f"position is {value_now:,.2f} = {value_now / sizing.account * 100:.1f}% "
                        f"of the account, over the {sizing.max_account_concentration:.0%} cap "
                        f"({sizing.max_account_concentration * sizing.account:,.0f})"
                    ),
                )
            )
    else:
        why = price.reason if not price.ok else "entry_price or shares is not a number"
        v_pnl = Value.unavailable(reason=why, source="derived")
        v_value = Value.unavailable(reason=why, source="derived")
        v_pct = Value.unavailable(reason=why, source="derived")
        v_r = Value.unavailable(reason=why, source="derived")

    # ------------------------------------------------------------- stop comparison
    if price.ok and current_stop is not None:
        px = float(price.value)
        distance = px - current_stop
        v_distance = Value.computed(
            distance,
            formula=f"price - current_stop = {px:.4f} - {current_stop:.4f}",
            source="derived",
            as_of=price.as_of,
        )
        if distance < 0:
            in_r = (
                f", {abs(distance) * (shares or 0) / risk_dollars:.2f}R"
                if risk_dollars and shares
                else ""
            )
            tag, rank, critical = ALERT_STOP_BREACHED
            alerts.append(
                Alert(
                    rank=rank,
                    tag=tag,
                    symbol=symbol,
                    detail=(
                        f"price {px:.2f} is {abs(distance):.2f} BELOW the stop "
                        f"{current_stop:.2f}{in_r}"
                        + ("" if is_live else " - NOTE: computed on a non-live price")
                    ),
                    critical=critical,
                )
            )
    else:
        v_distance = Value.unavailable(
            reason=(price.reason if not price.ok else "current_stop is not a price"),
            source="derived",
        )

    # ------------------------------------------------------------------- days held
    if entry_date:
        held = mcal.trading_days_between(entry_date, today)
        v_days = Value.computed(
            held,
            formula=(
                f"trading days from {entry_date.isoformat()} to {today.isoformat()} "
                "over the XNYS calendar"
            ),
            source="derived",
        )
        low, high = cfg.pass2.partial_window_days
        if low <= held <= high and partial in {"no", "none", ""}:
            tag, rank, critical = ALERT_PARTIAL
            alerts.append(
                Alert(
                    rank=rank,
                    tag=tag,
                    symbol=symbol,
                    detail=(
                        f"{held} trading days held and no partial taken; the {low}-{high} "
                        "day window for taking 33-50% off and moving the stop to breakeven is open"
                    ),
                )
            )
    else:
        v_days = Value.unavailable(reason="entry_date is not a date", source="derived")

    # ----------------------------------------------------------------- 10-day SMA
    period = cfg.pass2.trail_sma_period
    frame = (
        bars.filter((pl.col("symbol") == symbol) & (pl.col("date") < today)).sort("date")
        if not bars.is_empty()
        else bars
    )
    if frame.height >= period:
        enriched = frame.with_columns(
            pl.col("close").rolling_mean(window_size=period, min_samples=period).alias("sma")
        )
        last = enriched.row(-1, named=True)
        bar_date = last["date"]
        sma_value, close_value = last["sma"], last["close"]
        v_sma = Value.computed(
            float(sma_value),
            formula=f"SMA({period}) of close over the {period} sessions through {bar_date}",
            source="derived",
        )
        v_close = Value.fetched(
            float(close_value), source="qms:data/bars/bars.parquet", note=f"close of {bar_date}"
        )
        below = close_value < sma_value
        v_below = Value.computed(
            below,
            formula=(
                f"last close {close_value:.4f} {'<' if below else '>='} "
                f"SMA({period}) {sma_value:.4f} on {bar_date}. A close, not an intraday touch."
            ),
            source="derived",
        )
        if below:
            tag, rank, critical = ALERT_BELOW_MA
            alerts.append(
                Alert(
                    rank=rank,
                    tag=tag,
                    symbol=symbol,
                    detail=(
                        f"close {close_value:.2f} on {bar_date} is below the {period}-day SMA "
                        f"{sma_value:.2f} - the trail rule exits the balance on a close below"
                    ),
                )
            )
    else:
        gap = Value.unavailable(
            reason=f"fewer than {period} daily bars available", source="derived"
        )
        v_sma = v_close = v_below = gap

    # ------------------------------------------------------------------- earnings
    if earnings_days is not None and 0 <= earnings_days <= cfg.pass2.earnings_soon_days:
        tag, rank, critical = ALERT_EARNINGS
        alerts.append(
            Alert(
                rank=rank,
                tag=tag,
                symbol=symbol,
                detail=(
                    f"report is {earnings_days} trading day(s) away, inside the "
                    f"{cfg.pass2.earnings_soon_days}-day window; the rule is never to hold through it"
                ),
            )
        )

    return PositionReport(
        symbol=symbol,
        entry_date=v_entry_date,
        entry_price=v_entry,
        shares=v_shares,
        current_price=price,
        current_stop=v_stop,
        unrealized_dollars=v_pnl,
        unrealized_r=v_r,
        position_value=v_value,
        pct_of_account=v_pct,
        days_held=v_days,
        stop_distance=v_distance,
        trail_sma=v_sma,
        last_close=v_close,
        below_sma_on_close=v_below,
        thesis=(row.get("thesis") or "").strip(),
        alerts=alerts,
    )
