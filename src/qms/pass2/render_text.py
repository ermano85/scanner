"""Plain-text rendering, for pasting into a chat window.

Two constraints shape everything here.

**Measured and derived must be distinguishable at a glance.** Every line carries a
one-character marker in column zero: a blank for a fetched value, `=` for a computed one,
`!` for one that is unavailable. A marker in a fixed column survives copy-paste,
proportional fonts and quoting, where colour or indentation would not.

**ASCII only.** This text is copied out of a Windows console — the operator's is cp1257 —
and an em-dash or an arrow becomes a question mark or raises UnicodeEncodeError on the way
out. Nothing here uses a character above 0x7F.
"""

from __future__ import annotations

import datetime as dt

from qms.config import ScanConfig
from qms.pass2 import clock as clockmod
from qms.pass2.model import Alert, Candidate, Packet, PositionReport, Value

MARK_FETCHED = " "
MARK_COMPUTED = "="
MARK_MISSING = "!"

LABEL_WIDTH = 22
RULE = "-" * 78


def _fmt_number(value, places: int = 4) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,d}"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.2f}"
        return f"{value:,.{places}f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _line(label: str, value: Value, *, places: int = 4, verbose: bool = False) -> list[str]:
    """One field, marked by provenance. Returns the line plus any --verbose detail."""
    if not value.ok:
        head = f"{MARK_MISSING} {label:<{LABEL_WIDTH}} UNAVAILABLE"
        reason = value.reason or "no reason recorded"
        return [f"{head}  ({reason})"]

    mark = MARK_COMPUTED if value.kind == "computed" else MARK_FETCHED
    rendered = _fmt_number(value.value, places)
    line = f"{mark} {label:<{LABEL_WIDTH}} {rendered}"

    trailer: list[str] = []
    if value.kind == "fetched" and value.source:
        stamp = f" @ {clockmod.et_time(value.as_of)}" if value.as_of else ""
        trailer.append(f"[{value.source}{stamp}]")
    if value.note:
        trailer.append(value.note)
    if trailer:
        line += "   " + "  ".join(trailer)

    out = [line]
    if verbose and value.formula:
        out.append(f"{' ' * (LABEL_WIDTH + 2)} ... {value.formula}")
    return out


def _alert_block(alerts: list[Alert]) -> list[str]:
    if not alerts:
        return []
    out: list[str] = []
    ordered = sorted(alerts, key=lambda a: (a.rank, a.symbol))
    critical = [a for a in ordered if a.critical]
    if critical:
        out.append("!" * 78)
        for a in critical:
            out.append(f"{a.tag}  {a.symbol}")
            out.append(f"    {a.detail}")
        out.append("!" * 78)
        out.append("")
    rest = [a for a in ordered if not a.critical]
    if rest:
        out.append("ALERTS")
        for a in rest:
            out.append(f"  {a.tag:<22} {a.symbol:<6} {a.detail}")
        out.append("")
    return out


def _header(packet: Packet, cfg: ScanConfig) -> list[str]:
    out = [
        f"pass2   {clockmod.format_both(packet.generated_at)}",
    ]
    since = (
        f"{packet.minutes_since_open:.0f} min since the open"
        if packet.minutes_since_open is not None
        else "market not open"
    )
    session = packet.session_date.isoformat() if packet.session_date else "none in progress"
    out.append(f"        market: {packet.market_state}   session: {session}   {since}")
    out.append(
        f"        account {packet.account:,.0f}   risk budget {packet.risk_budget:,.2f} "
        f"({cfg.sizing.risk_pct:.2%} per trade)   concentration cap "
        f"{cfg.sizing.max_account_concentration:.0%} = "
        f"{cfg.sizing.max_account_concentration * packet.account:,.0f}"
    )
    if packet.forced_time:
        out.append("        NOTE: --at was used; this is a reconstructed moment, not now")
    if packet.forced_ahead_seconds > 60:
        minutes = packet.forced_ahead_seconds / 60
        out.append(
            f"        WARNING: --at is {minutes:,.0f} min AHEAD of the real clock. No data "
            "exists for that moment yet; intraday fields will be empty."
        )
    out.append(
        f"        legend: '{MARK_FETCHED}' fetched (source shown)   "
        f"'{MARK_COMPUTED}' computed here   '{MARK_MISSING}' unavailable"
    )
    return out


def _failures(packet: Packet) -> list[str]:
    seen: dict[str, str] = {}
    for f in packet.failures:
        if f.source not in seen:
            seen[f.source] = f.detail
    for c in packet.candidates:
        for f in c.failures:
            seen.setdefault(f.source, f.detail)
        if c.earnings:
            for f in c.earnings.failures:
                seen.setdefault(f.source, f.detail)
    if not seen:
        return []
    out = ["", "SOURCES DEGRADED"]
    for source, detail in sorted(seen.items()):
        out.append(f"  {source}: {detail}")
    out.append("  Other fields are unaffected; anything missing is marked UNAVAILABLE above.")
    return out


def _position(report: PositionReport, cfg: ScanConfig, verbose: bool) -> list[str]:
    out = [f"{report.symbol}"]
    add = out.extend
    add(_line("entry", report.entry_price, places=2, verbose=verbose))
    add(_line("entry date", report.entry_date, verbose=verbose))
    add(_line("shares", report.shares, places=0, verbose=verbose))
    add(_line("current price", report.current_price, places=2, verbose=verbose))
    add(_line("current stop", report.current_stop, places=2, verbose=verbose))
    add(_line("price vs stop", report.stop_distance, places=2, verbose=verbose))
    add(_line("unrealized", report.unrealized_dollars, places=2, verbose=verbose))
    add(_line("unrealized R", report.unrealized_r, places=2, verbose=verbose))
    add(_line("position value", report.position_value, places=2, verbose=verbose))
    add(_line("pct of account", report.pct_of_account, places=2, verbose=verbose))
    add(_line("trading days held", report.days_held, places=0, verbose=verbose))
    add(_line(f"SMA({cfg.pass2.trail_sma_period})", report.trail_sma, places=3, verbose=verbose))
    add(_line("last close", report.last_close, places=2, verbose=verbose))
    add(_line("close below SMA", report.below_sma_on_close, verbose=verbose))
    if report.thesis:
        out.append(f"  {'thesis':<{LABEL_WIDTH}} {report.thesis}")
    return out


def _earnings(candidate: Candidate, verbose: bool) -> list[str]:
    report = candidate.earnings
    if report is None:
        return [f"{MARK_MISSING} {'earnings':<{LABEL_WIDTH}} UNAVAILABLE  (not looked up)"]

    out: list[str] = []
    status = report.status.upper()
    if report.status == "unknown":
        out.append(f"{MARK_MISSING} {'earnings':<{LABEL_WIDTH}} UNKNOWN  (no date found in any source)")
    elif report.status == "conflict":
        out.append(f"{MARK_MISSING} {'earnings':<{LABEL_WIDTH}} CONFLICT - sources disagree, no date chosen")
        for c in sorted(report.candidates, key=lambda c: c["date"]):
            updated = f", source updated {c['updated'].isoformat()}" if c.get("updated") else ""
            out.append(
                f"  {'':<{LABEL_WIDTH}}   {c['date'].isoformat()}  "
                f"[{c['source']}{updated}]  timing {c['timing']}"
            )
    else:
        out.extend(_line(f"earnings ({status})", report.next_date, verbose=verbose))
        out.extend(_line("  timing", report.timing, verbose=verbose))
        out.extend(_line("  trading days away", report.trading_days_until, places=0, verbose=verbose))
    out.extend(_line("  last report (SEC)", report.last_past_date, verbose=verbose))
    return out


def _candidate(candidate: Candidate, cfg: ScanConfig, verbose: bool) -> list[str]:
    quote, daily = candidate.quote, candidate.daily
    flags = " ".join(sorted(set(candidate.flags + quote.flags)))
    out = [f"{candidate.symbol}" + (f"   [{flags}]" if flags else "")]
    add = out.extend

    add(_line("current price", quote.current_price, places=2, verbose=verbose))
    add(_line("  as of", quote.price_time, verbose=verbose))
    add(_line("session low (RTH)", quote.session_low, places=4, verbose=verbose))
    add(_line("  low set at", quote.session_low_time, verbose=verbose))
    add(_line("session high (RTH)", quote.session_high, places=4, verbose=verbose))
    add(_line("  cross-check low", quote.crosscheck, places=4, verbose=verbose))
    add(_line("  pre-market low", quote.premarket_low_excluded, places=4, verbose=verbose))

    if daily:
        out.append("")
        add(_line("ATR(14) Wilder", daily.atr, places=4, verbose=verbose))
        add(_line("ADR%(20)", daily.adr_pct, places=3, verbose=verbose))
        for p in sorted(daily.smas):
            add(_line(f"SMA({p})", daily.smas[p], places=3, verbose=verbose))
            add(_line(f"  dist pct", daily.sma_dist_pct[p], places=2, verbose=verbose))
            add(_line(f"  dist ADR", daily.sma_dist_adr[p], places=2, verbose=verbose))
        out.append("")
        if daily.prev_date:
            add(_line("prev session", daily.prev_date, verbose=verbose))
        for label, value in (
            ("  open", daily.prev_open),
            ("  high", daily.prev_high),
            ("  low", daily.prev_low),
            ("  close", daily.prev_close),
        ):
            if value:
                add(_line(label, value, places=2, verbose=verbose))
        if daily.prev_volume:
            add(_line("  volume", daily.prev_volume, places=0, verbose=verbose))
        if daily.avg_dollar_vol:
            add(_line("avg $ volume (20d)", daily.avg_dollar_vol, places=0, verbose=verbose))

    out.append("")
    out.extend(_earnings(candidate, verbose))

    out.append("")
    if candidate.extended_move:
        add(_line("move off the low", candidate.extended_move, places=4, verbose=verbose))
    if candidate.extended_atr:
        add(_line("  in ATR units", candidate.extended_atr, places=3, verbose=verbose))
    if candidate.stop:
        add(_line("stop (low * 0.995)", candidate.stop, places=4, verbose=verbose))

    if candidate.levels:
        out.append("")
        out.append(
            f"  {'entry level':<17} {'price':>9} {'order':>11} {'risk/sh':>8} "
            f"{'stopATR':>8} {'shares':>7} {'bound by':>14} {'cost':>10} {'risk$':>8}"
        )
        for level in candidate.levels:
            order = level.order_type.value if level.order_type.ok else "UNAVAILABLE"
            label = f"{level.label} {level.atr_multiple:.2f}ATR"
            out.append(
                f"  {label:<17} "
                f"{level.entry.value:>9,.2f} "
                f"{order:>11} "
                f"{level.risk_per_share.value:>8,.3f} "
                f"{level.stop_distance_atr.value:>8,.2f} "
                f"{level.shares.value:>7,d} "
                f"{level.binding_cap.value:>14} "
                f"{level.position_cost.value:>10,.2f} "
                f"{level.actual_risk.value:>8,.2f}"
            )
            if level.flags:
                # Named, so a flag can never be read against the wrong row.
                out.append(f"  {'-> ' + level.label:<17} {' '.join(level.flags)}")
            if verbose:
                out.append(f"      ... entry: {level.entry.formula}")
                out.append(f"      ... order: {level.order_type.formula or level.order_type.reason}")
                out.append(f"      ... shares: {level.shares.formula}")
        out.append(
            "  (all three rows are computed; the order column is derived from the live "
            "price only)"
        )
    return out


def render(packet: Packet, cfg: ScanConfig, *, verbose: bool = False) -> str:
    """The whole packet as plain text."""
    lines: list[str] = []

    # Alerts first, above the header. An open position in trouble outranks everything,
    # including knowing what time it is.
    lines.extend(_alert_block(packet.alerts))
    lines.extend(_header(packet, cfg))

    if packet.positions:
        lines.append("")
        lines.append(RULE)
        lines.append("OPEN POSITIONS")
        lines.append(RULE)
        for report in packet.positions:
            lines.append("")
            lines.extend(_position(report, cfg, verbose))

    if packet.candidates:
        lines.append("")
        lines.append(RULE)
        lines.append("CANDIDATES")
        lines.append(RULE)
        for candidate in packet.candidates:
            lines.append("")
            lines.extend(_candidate(candidate, cfg, verbose))

    if not packet.positions and not packet.candidates:
        lines.append("")
        lines.append("Nothing to report: no tickers given and no positions file read.")

    lines.extend(_failures(packet))
    lines.append("")
    lines.append(
        "pass2 reports measurements and arithmetic only. It places no orders, ranks "
        "nothing, and predicts nothing."
    )
    return "\n".join(lines)
