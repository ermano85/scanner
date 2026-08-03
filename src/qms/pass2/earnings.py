"""Earnings dates, reconciled across independent sources.

The operator's rule is never to hold through a report, so a wrong date here is not a
cosmetic defect — it puts a position into an event the strategy exists to avoid. The
governing principle is therefore that **an honest `UNKNOWN` beats a confident guess**, and
the code is arranged so that guessing is not reachable:

* `confirmed` is only ever set by a source that publishes confirmation as a fact. Nothing
  is promoted to it by inference, agreement, or plausibility.
* When sources disagree the status is `conflict` and **both** dates are reported with
  their provenance. No tie-break, no precedence order, no silent winner.
* Quarterly cadence is never used to project a date. The past dates from SEC EDGAR are
  reported for the operator's own volume-spike check and may *demote* a date to
  `estimated`, but they can never manufacture one.

Three sources, answering deliberately different questions:

  nasdaq  (keyless)  future date + bmo/amc timing, and `lastYearRptDt`, which is the tell
                     for a row that is really an anniversary projection
  fmp     (key)      the only free source publishing a native confirmed-vs-estimated flag
  sec     (keyless)  8-K Item 2.02 filings — authoritative *past* report dates

Without an FMP key the tool still runs; it simply can never say `confirmed`, which is the
truthful outcome rather than a degraded one.
"""

from __future__ import annotations

import datetime as dt
import json

from qms import calendar as mcal
from qms.calendar import sessions_in_range
from qms.config import ScanConfig
from qms.ingest.http import HttpClient, HttpError
from qms.ingest.nasdaq_earnings import EARNINGS_URL, _TIME_MAP
from qms.ingest.sec_sic import SUBMISSIONS_URL
from qms.paths import DATA_DIR
from qms.pass2.model import EarningsReport, SourceFailure, Value

CACHE_DIR = DATA_DIR / "pass2-cache" / "earnings"

SOURCE_NASDAQ = "nasdaq:calendar/earnings"
SOURCE_FMP_CONFIRMED = "fmp:earning-calendar-confirmed"
SOURCE_FMP_CALENDAR = "fmp:earnings-calendar"
SOURCE_SEC = "sec:8-K item 2.02"

STATUS_CONFIRMED = "confirmed"
STATUS_ESTIMATED = "estimated"
STATUS_CONFLICT = "conflict"
STATUS_UNKNOWN = "unknown"

FMP_CONFIRMED_URL = "https://financialmodelingprep.com/api/v4/earning-calendar-confirmed"
FMP_CALENDAR_URL = "https://financialmodelingprep.com/stable/earnings-calendar"

# How far ahead to scan. One quarter plus slack: far enough to catch the next report for a
# name that has just reported, without paying for a request per session all year.
FORWARD_SESSIONS = 70

_ANNIVERSARY_TOLERANCE_DAYS = 21

# A quarter plus slack. Past this, a filer's most recent Item 2.02 is probably not its most
# recent *report*: Item 2.02 tagging is a filer choice, not an obligation, and some tag
# their results 8-K with only 9.01. Verified on AMN, whose 2026-02-19 and 2026-05-07
# earnings 8-Ks carry 9.01 alone, leaving 2025-11-06 as the newest 2.02. Reporting that as
# "the most recent report" would be wrong in the quiet way that matters, so it is labelled.
_SEC_CADENCE_STALE_DAYS = 130


# --------------------------------------------------------------------------- nasdaq


def _nasdaq_day(client: HttpClient, day: dt.date, use_cache: bool) -> dict[str, dict]:
    """Every symbol reporting on `day`, keyed by symbol. Cached per calendar date."""
    path = CACHE_DIR / f"nasdaq-{day.isoformat()}.json"
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    payload = client.get_json(EARNINGS_URL, {"date": day.isoformat()})
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out: dict[str, dict] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out[symbol] = {
            "date": day.isoformat(),
            "when": _TIME_MAP.get(row.get("time") or "", "unknown"),
            "last_year": (row.get("lastYearRptDt") or "").strip(),
        }

    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


def scan_nasdaq(
    client: HttpClient,
    symbols: set[str],
    start: dt.date,
    *,
    use_cache: bool = True,
    failures: list[SourceFailure] | None = None,
) -> dict[str, dict]:
    """First forward report date per symbol, from the date-indexed Nasdaq calendar.

    Nasdaq has no per-symbol endpoint (verified: 404), so the calendar must be walked one
    session at a time. It is walked in date order and a symbol is kept on first sight,
    which makes the result the *next* report rather than an arbitrary one.
    """
    days = sessions_in_range(start, mcal.shift_sessions(start, FORWARD_SESSIONS))
    found: dict[str, dict] = {}
    remaining = set(symbols)
    for day in days:
        if not remaining:
            break
        try:
            rows = _nasdaq_day(client, day, use_cache)
        except HttpError as exc:
            if failures is not None:
                failures.append(
                    SourceFailure(
                        source=SOURCE_NASDAQ,
                        detail=f"{day}: {exc}",
                        rate_limited=exc.status == 429,
                    )
                )
            continue
        for symbol in list(remaining):
            hit = rows.get(symbol)
            if hit:
                found[symbol] = hit
                remaining.discard(symbol)
    return found


# ------------------------------------------------------------------------------ fmp


def _as_date(raw) -> dt.date | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None


def _fmp_timing(row: dict) -> str:
    """Normalise FMP's timing vocabulary, which differs between endpoint generations."""
    raw = str(row.get("when") or row.get("time") or "").strip().lower()
    if raw in {"bmo", "pre market", "pre-market", "premarket", "before market open"}:
        return "bmo"
    if raw in {"amc", "post market", "post-market", "aftermarket", "after market close"}:
        return "amc"
    if ":" in raw:  # an explicit clock time, e.g. "16:05"
        try:
            hour = int(raw.split(":")[0])
        except ValueError:
            return "unknown"
        return "bmo" if hour < 12 else "amc"
    return "unknown"


def fetch_fmp(
    client: HttpClient,
    symbol: str,
    api_key: str,
    *,
    failures: list[SourceFailure] | None = None,
) -> list[dict]:
    """Candidate dates from FMP: the confirmed feed first, then the general calendar.

    Field names differ across FMP's endpoint generations, so every field is read
    tolerantly and a row whose date cannot be parsed is dropped rather than guessed at.
    """
    out: list[dict] = []
    today = dt.date.today()

    for url, params, source, confirmed in (
        (
            FMP_CONFIRMED_URL,
            {"from": today.isoformat(), "to": (today + dt.timedelta(days=180)).isoformat()},
            SOURCE_FMP_CONFIRMED,
            True,
        ),
        (FMP_CALENDAR_URL, {"symbol": symbol}, SOURCE_FMP_CALENDAR, False),
    ):
        try:
            payload = client.get_json(url, {**params, "apikey": api_key})
        except HttpError as exc:
            if failures is not None:
                failures.append(
                    SourceFailure(
                        source=source,
                        detail=f"{symbol}: {exc}",
                        rate_limited=exc.status == 429,
                    )
                )
            continue

        if isinstance(payload, dict):
            # FMP reports auth and quota problems as a JSON object, not an HTTP error.
            message = payload.get("Error Message") or payload.get("error")
            if message and failures is not None:
                failures.append(
                    SourceFailure(
                        source=source,
                        detail=str(message),
                        rate_limited="limit" in str(message).lower(),
                    )
                )
            continue
        if not isinstance(payload, list):
            continue

        for row in payload:
            if not isinstance(row, dict):
                continue
            if (row.get("symbol") or "").strip().upper() != symbol.upper():
                continue
            when = _as_date(row.get("date"))
            if when is None or when < today:
                continue
            out.append(
                {
                    "date": when,
                    "timing": _fmp_timing(row),
                    "source": source,
                    "confirmed": confirmed,
                    "updated": _as_date(
                        row.get("publicationDate")
                        or row.get("lastUpdated")
                        or row.get("updatedFromDate")
                    ),
                }
            )
    return out


# ------------------------------------------------------------------------------ sec


def fetch_sec_past(
    client: HttpClient,
    cik: str,
    *,
    limit: int = 8,
    failures: list[SourceFailure] | None = None,
) -> list[dt.date]:
    """Filing dates of recent 8-Ks carrying Item 2.02 (Results of Operations).

    Item 2.02 is the results announcement itself, so these are report dates as filed with
    the regulator rather than a vendor's recollection of them. Verified against CBRL:
    2026-06-09, 2026-03-04, 2025-12-09, 2025-09-17.
    """
    try:
        payload = client.get_json(SUBMISSIONS_URL.format(cik=cik)) or {}
    except HttpError as exc:
        if failures is not None:
            failures.append(
                SourceFailure(
                    source=SOURCE_SEC, detail=str(exc), rate_limited=exc.status == 429
                )
            )
        return []

    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    items = recent.get("items") or []
    dates = recent.get("filingDate") or []

    out: list[dt.date] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        tags = items[i] if i < len(items) else ""
        if "2.02" not in (tags or ""):
            continue
        when = _as_date(dates[i] if i < len(dates) else None)
        if when:
            out.append(when)
        if len(out) >= limit:
            break
    return sorted(out, reverse=True)


# -------------------------------------------------------------------------- reconcile


def _looks_like_anniversary(candidate: dt.date, last_year: str) -> bool:
    """True when a date is roughly one year after the prior-year report.

    A calendar row that is simply last year's date plus twelve months is a projection
    wearing a date's clothing. It is a demotion signal only — it can mark something
    `estimated`, and it can never be used to construct a date in the first place.
    """
    prior = _as_date(last_year)
    if prior is None:
        return False
    delta = abs((candidate - prior).days - 365)
    return delta <= _ANNIVERSARY_TOLERANCE_DAYS


def reconcile(
    symbol: str,
    today: dt.date,
    *,
    nasdaq: dict | None,
    fmp: list[dict],
    sec_past: list[dt.date],
    failures: list[SourceFailure],
) -> EarningsReport:
    """Combine the sources into one verdict, preserving disagreement rather than hiding it."""
    candidates: list[dict] = []

    if nasdaq:
        when = _as_date(nasdaq.get("date"))
        if when:
            candidates.append(
                {
                    "date": when,
                    "timing": nasdaq.get("when") or "unknown",
                    "source": SOURCE_NASDAQ,
                    # Nasdaq publishes projections and confirmations in one feed with no
                    # flag distinguishing them, so nothing from here is ever `confirmed`.
                    "confirmed": False,
                    "updated": None,
                    "anniversary": _looks_like_anniversary(when, nasdaq.get("last_year") or ""),
                }
            )
    for row in fmp:
        candidates.append({**row, "anniversary": False})

    if sec_past:
        age = (today - sec_past[0]).days
        note = "8-K Item 2.02 filing date"
        if age > _SEC_CADENCE_STALE_DAYS:
            note += (
                f"; {age}d old - longer than a reporting quarter, so this filer likely "
                "tags some results 8-Ks without Item 2.02. Treat as the most recent "
                "TAGGED report, not necessarily the most recent one"
            )
        last_past = Value.fetched(sec_past[0], source=SOURCE_SEC, note=note)
    else:
        last_past = Value.unavailable(
            reason="no 8-K Item 2.02 filings found", source=SOURCE_SEC
        )

    if not candidates:
        gap = Value.unavailable(
            reason="no scheduled date found in any source", source="reconciled"
        )
        return EarningsReport(
            symbol=symbol,
            status=STATUS_UNKNOWN,
            next_date=gap,
            timing=gap,
            trading_days_until=gap,
            last_past_date=last_past,
            candidates=[],
            failures=failures,
        )

    distinct = sorted({c["date"] for c in candidates})
    confirmed_dates = {c["date"] for c in candidates if c.get("confirmed")}

    if len(distinct) > 1:
        status = STATUS_CONFLICT
        chosen = None
    elif confirmed_dates:
        status = STATUS_CONFIRMED
        chosen = distinct[0]
    else:
        status = STATUS_ESTIMATED
        chosen = distinct[0]

    if chosen is None:
        detail = "; ".join(
            f"{c['date'].isoformat()} ({c['source']})" for c in sorted(candidates, key=lambda c: c["date"])
        )
        conflict = Value.unavailable(
            reason=f"sources disagree - {detail}; no date chosen",
            source="reconciled",
        )
        return EarningsReport(
            symbol=symbol,
            status=STATUS_CONFLICT,
            next_date=conflict,
            timing=conflict,
            trading_days_until=conflict,
            last_past_date=last_past,
            candidates=candidates,
            failures=failures,
        )

    agreeing = [c for c in candidates if c["date"] == chosen]
    sources = ", ".join(sorted({c["source"] for c in agreeing}))
    updated = max((c["updated"] for c in agreeing if c.get("updated")), default=None)
    timings = {c["timing"] for c in agreeing if c["timing"] != "unknown"}

    note = f"{status} via {sources}"
    if updated:
        note += f"; source last updated {updated.isoformat()}"
    if status == STATUS_ESTIMATED and any(c.get("anniversary") for c in agreeing):
        note += "; matches last year's report date +1y - likely a projection"

    if chosen < today:
        note += "; DATE IS IN THE PAST - stale calendar entry"

    return EarningsReport(
        symbol=symbol,
        status=status,
        next_date=Value.fetched(chosen, source=sources, note=note),
        timing=(
            Value.fetched(sorted(timings)[0], source=sources)
            if len(timings) == 1
            else Value.fetched("unknown", source=sources, note="no source supplied a timing")
            if not timings
            else Value.unavailable(
                reason=f"sources disagree on timing: {sorted(timings)}", source="reconciled"
            )
        ),
        trading_days_until=Value.computed(
            mcal.trading_days_between(today, chosen),
            formula=(
                f"trading days from {today.isoformat()} to {chosen.isoformat()} "
                "over the XNYS calendar (holidays and half-days included)"
            ),
            source="reconciled",
        ),
        last_past_date=last_past,
        candidates=candidates,
        failures=failures,
    )
