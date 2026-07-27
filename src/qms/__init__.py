"""Qullamaggie swing scanner.

A nightly triage funnel over US equities and ETFs. It reduces ~13,000 tickers to a ranked
shortlist of charts worth reviewing by hand, with the position-sizing arithmetic attached.

It emits no buy signals and places no orders. Whether a consolidation is actually
tradeable is a judgment this tool does not make.
"""

__version__ = "0.1.0"
