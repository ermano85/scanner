"""The same content as the text form, as structured data.

Every field serialises to the same envelope — `{value, kind, source, as_of, formula, note,
reason}` — so a consumer can tell a measurement from a derivation without knowing anything
about the individual field, exactly as the text marker column does. `kind` is always
present; `value` is null if and only if `kind` is `"unavailable"`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from qms.config import ScanConfig
from qms.pass2.model import Alert, Candidate, Packet, PositionReport, Value


def _scalar(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _value(value: Value | None) -> dict | None:
    if value is None:
        return None
    out = {
        "value": _scalar(value.value),
        "kind": value.kind,
        "source": value.source,
        "as_of": value.as_of.isoformat() if value.as_of else None,
    }
    if value.formula:
        out["formula"] = value.formula
    if value.note:
        out["note"] = value.note
    if value.reason:
        out["reason"] = value.reason
    return out


def _alert(alert: Alert) -> dict:
    return {
        "rank": alert.rank,
        "tag": alert.tag,
        "symbol": alert.symbol,
        "detail": alert.detail,
        "critical": alert.critical,
    }


def _position(report: PositionReport) -> dict:
    return {
        "symbol": report.symbol,
        "entry_date": _value(report.entry_date),
        "entry_price": _value(report.entry_price),
        "shares": _value(report.shares),
        "current_price": _value(report.current_price),
        "current_stop": _value(report.current_stop),
        "price_vs_stop": _value(report.stop_distance),
        "unrealized_dollars": _value(report.unrealized_dollars),
        "unrealized_r": _value(report.unrealized_r),
        "position_value": _value(report.position_value),
        "pct_of_account": _value(report.pct_of_account),
        "trading_days_held": _value(report.days_held),
        "trail_sma": _value(report.trail_sma),
        "last_close": _value(report.last_close),
        "close_below_sma": _value(report.below_sma_on_close),
        "thesis": report.thesis,
        "alerts": [_alert(a) for a in sorted(report.alerts, key=lambda a: a.rank)],
    }


def _candidate(candidate: Candidate) -> dict:
    quote, daily, earnings = candidate.quote, candidate.daily, candidate.earnings
    out: dict = {
        "symbol": candidate.symbol,
        "flags": sorted(set(candidate.flags + quote.flags)),
        "quote": {
            "current_price": _value(quote.current_price),
            "price_time": _value(quote.price_time),
            "is_live": quote.is_live,
            "session_low": _value(quote.session_low),
            "session_low_time": _value(quote.session_low_time),
            "session_high": _value(quote.session_high),
            "session_low_crosscheck": _value(quote.crosscheck),
            "premarket_low_excluded": _value(quote.premarket_low_excluded),
        },
        "stop": _value(candidate.stop),
        "move_off_low": _value(candidate.extended_move),
        "move_off_low_atr": _value(candidate.extended_atr),
        "entry_levels": [
            {
                "label": level.label,
                "atr_multiple": level.atr_multiple,
                "entry": _value(level.entry),
                "order_type": _value(level.order_type),
                "risk_per_share": _value(level.risk_per_share),
                "stop_distance_atr": _value(level.stop_distance_atr),
                "shares": _value(level.shares),
                "binding_cap": _value(level.binding_cap),
                "position_cost": _value(level.position_cost),
                "actual_risk_dollars": _value(level.actual_risk),
                "flags": level.flags,
            }
            for level in candidate.levels
        ],
        "failures": [
            {"source": f.source, "detail": f.detail, "rate_limited": f.rate_limited}
            for f in candidate.failures
        ],
    }

    if daily:
        out["daily"] = {
            "bars_through": daily.bars_through.isoformat() if daily.bars_through else None,
            "atr_14": _value(daily.atr),
            "adr_pct_20": _value(daily.adr_pct),
            "sma": {str(p): _value(v) for p, v in daily.smas.items()},
            "sma_distance_pct": {str(p): _value(v) for p, v in daily.sma_dist_pct.items()},
            "sma_distance_adr": {str(p): _value(v) for p, v in daily.sma_dist_adr.items()},
            "previous_session": {
                "date": _value(daily.prev_date),
                "open": _value(daily.prev_open),
                "high": _value(daily.prev_high),
                "low": _value(daily.prev_low),
                "close": _value(daily.prev_close),
                "volume": _value(daily.prev_volume),
            },
            "avg_dollar_volume_20": _value(daily.avg_dollar_vol),
            "avg_volume_20": _value(daily.avg_vol),
        }

    if earnings:
        out["earnings"] = {
            "status": earnings.status,
            "next_date": _value(earnings.next_date),
            "timing": _value(earnings.timing),
            "trading_days_until": _value(earnings.trading_days_until),
            "most_recent_past": _value(earnings.last_past_date),
            "sources": [
                {
                    "date": c["date"].isoformat(),
                    "timing": c["timing"],
                    "source": c["source"],
                    "confirmed": bool(c.get("confirmed")),
                    "source_updated": c["updated"].isoformat() if c.get("updated") else None,
                }
                for c in sorted(earnings.candidates, key=lambda c: c["date"])
            ],
            "failures": [
                {"source": f.source, "detail": f.detail, "rate_limited": f.rate_limited}
                for f in earnings.failures
            ],
        }
    return out


def render(packet: Packet, cfg: ScanConfig) -> dict:
    return {
        "tool": "pass2",
        "disclaimer": (
            "Measurements and arithmetic only. No orders, no ranking, no recommendation, "
            "no prediction."
        ),
        "generated_at": packet.generated_at.isoformat(),
        "forced_time": packet.forced_time,
        "forced_ahead_seconds": packet.forced_ahead_seconds,
        "market": {
            "state": packet.market_state,
            "session_date": packet.session_date.isoformat() if packet.session_date else None,
            "minutes_since_open": packet.minutes_since_open,
            "session_close_et": (
                packet.session_close_et.isoformat() if packet.session_close_et else None
            ),
            "half_day": packet.half_day,
        },
        "config": {
            "account": packet.account,
            "risk_pct": cfg.sizing.risk_pct,
            "risk_budget": packet.risk_budget,
            "stop_buffer": cfg.sizing.stop_buffer,
            "max_account_concentration": cfg.sizing.max_account_concentration,
            "entry_atr_multiples": [
                cfg.sizing.preferred_entry_atr_low,
                cfg.sizing.preferred_entry_atr_high,
                cfg.sizing.max_entry_atr_multiple,
            ],
        },
        "alerts": [_alert(a) for a in sorted(packet.alerts, key=lambda a: (a.rank, a.symbol))],
        "positions": [_position(p) for p in packet.positions],
        "candidates": [_candidate(c) for c in packet.candidates],
        "failures": [
            {"source": f.source, "detail": f.detail, "rate_limited": f.rate_limited}
            for f in packet.failures
        ],
    }
