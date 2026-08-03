"""Earnings reconciliation.

The governing rule is that a confident wrong date is worse than an honest unknown, so most
of these tests assert what the code refuses to do rather than what it produces.
"""

from __future__ import annotations

import datetime as dt

from qms.pass2 import earnings as E

TODAY = dt.date(2026, 7, 31)


def _nasdaq(date: str, when: str = "amc", last_year: str = "N/A") -> dict:
    return {"date": date, "when": when, "last_year": last_year}


def _fmp(date: dt.date, *, confirmed: bool, timing: str = "amc", updated=None) -> dict:
    return {
        "date": date,
        "timing": timing,
        "source": E.SOURCE_FMP_CONFIRMED if confirmed else E.SOURCE_FMP_CALENDAR,
        "confirmed": confirmed,
        "updated": updated,
    }


def test_nothing_found_reports_unknown_and_invents_no_date():
    report = E.reconcile("TEST", TODAY, nasdaq=None, fmp=[], sec_past=[], failures=[])
    assert report.status == E.STATUS_UNKNOWN
    assert not report.next_date.ok
    assert report.next_date.value is None


def test_quarterly_cadence_is_never_used_to_project_a_date():
    """Four clean quarterly past dates, and still no future date is manufactured."""
    past = [dt.date(2026, 6, 9), dt.date(2026, 3, 4), dt.date(2025, 12, 9), dt.date(2025, 9, 17)]
    report = E.reconcile("TEST", TODAY, nasdaq=None, fmp=[], sec_past=past, failures=[])

    assert report.status == E.STATUS_UNKNOWN
    assert report.next_date.value is None
    # The past dates are still reported, for the operator's own volume-spike check.
    assert report.last_past_date.value == dt.date(2026, 6, 9)


def test_an_unconfirmed_source_alone_is_estimated_never_confirmed():
    report = E.reconcile(
        "TEST", TODAY, nasdaq=_nasdaq("2026-08-06"), fmp=[], sec_past=[], failures=[]
    )
    assert report.status == E.STATUS_ESTIMATED
    assert report.next_date.value == dt.date(2026, 8, 6)


def test_only_a_confirming_source_produces_confirmed():
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06"),
        fmp=[_fmp(dt.date(2026, 8, 6), confirmed=True, updated=dt.date(2026, 7, 20))],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_CONFIRMED
    assert "source last updated 2026-07-20" in report.next_date.note


def test_an_unconfirmed_fmp_row_agreeing_with_nasdaq_is_still_only_estimated():
    """Agreement is not confirmation."""
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06"),
        fmp=[_fmp(dt.date(2026, 8, 6), confirmed=False)],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_ESTIMATED


def test_disagreement_reports_both_dates_and_picks_neither():
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06"),
        fmp=[_fmp(dt.date(2026, 8, 13), confirmed=True)],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_CONFLICT
    assert not report.next_date.ok
    assert "2026-08-06" in report.next_date.reason
    assert "2026-08-13" in report.next_date.reason
    assert {c["date"] for c in report.candidates} == {dt.date(2026, 8, 6), dt.date(2026, 8, 13)}


def test_a_confirmed_date_does_not_win_a_conflict():
    """Even a confirming source does not get to overrule a disagreement silently."""
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06"),
        fmp=[_fmp(dt.date(2026, 8, 20), confirmed=True)],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_CONFLICT
    assert report.next_date.value is None


def test_an_anniversary_of_last_years_date_is_called_out_as_a_projection():
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06", last_year="8/07/2025"),
        fmp=[],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_ESTIMATED
    assert "likely a projection" in report.next_date.note


def test_a_date_far_from_last_years_is_not_called_a_projection():
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06", last_year="2/07/2025"),
        fmp=[],
        sec_past=[],
        failures=[],
    )
    assert "projection" not in (report.next_date.note or "")


def test_trading_days_until_uses_the_market_calendar():
    report = E.reconcile(
        "TEST", TODAY, nasdaq=_nasdaq("2026-08-06"), fmp=[], sec_past=[], failures=[]
    )
    # 2026-07-31 is a Friday; Aug 3-6 are Mon-Thu, so four sessions.
    assert report.trading_days_until.value == 4


def test_a_stale_sec_history_is_labelled_rather_than_trusted():
    """Item 2.02 tagging is a filer choice. AMN tags some results 8-Ks 9.01 only, leaving
    a months-old newest 2.02 that is not actually its most recent report."""
    report = E.reconcile(
        "TEST", TODAY, nasdaq=None, fmp=[], sec_past=[dt.date(2025, 11, 6)], failures=[]
    )
    assert report.last_past_date.value == dt.date(2025, 11, 6)
    assert "most recent TAGGED report" in report.last_past_date.note


def test_a_recent_sec_history_carries_no_caveat():
    report = E.reconcile(
        "TEST", TODAY, nasdaq=None, fmp=[], sec_past=[dt.date(2026, 6, 9)], failures=[]
    )
    assert "TAGGED" not in report.last_past_date.note


def test_a_past_date_from_a_stale_calendar_is_flagged():
    report = E.reconcile(
        "TEST", TODAY, nasdaq=_nasdaq("2026-07-01"), fmp=[], sec_past=[], failures=[]
    )
    assert "DATE IS IN THE PAST" in report.next_date.note


def test_timing_disagreement_is_reported_not_resolved():
    report = E.reconcile(
        "TEST",
        TODAY,
        nasdaq=_nasdaq("2026-08-06", when="bmo"),
        fmp=[_fmp(dt.date(2026, 8, 6), confirmed=True, timing="amc")],
        sec_past=[],
        failures=[],
    )
    assert report.status == E.STATUS_CONFIRMED
    assert not report.timing.ok
    assert "disagree" in report.timing.reason


def test_sec_filing_parser_keeps_only_item_2_02():
    """9.01-only 8-Ks are exhibits, not the results announcement."""
    payload = {
        "filings": {
            "recent": {
                "form": ["8-K", "8-K", "10-Q", "8-K"],
                "items": ["2.02,9.01", "5.02,7.01", "", "9.01"],
                "filingDate": ["2026-06-09", "2026-05-01", "2026-04-01", "2026-02-19"],
            }
        }
    }

    class _Client:
        def get_json(self, *_args, **_kwargs):
            return payload

    assert E.fetch_sec_past(_Client(), "0000000000") == [dt.date(2026, 6, 9)]
