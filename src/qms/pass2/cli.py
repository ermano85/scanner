"""`pass2` - assemble the intraday data packet.

    pass2 CBRL AMN BLLN
    pass2 --positions journal/positions.csv
    pass2 CBRL --json
    pass2 CBRL --at 16:05

Heavy imports live inside the command body so `pass2 --help` stays instant, matching the
convention in `qms/cli.py`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Pre-computed data packet for the pass-2 trading decision: session low, current "
        "price, volatility, verified earnings, and the arithmetic that follows. "
        "Makes no decisions and places no orders."
    ),
)

EXIT_OK = 0
EXIT_NOTHING = 2


@app.command()
def main(
    symbols: list[str] = typer.Argument(None, help="Tickers to assemble a packet for."),
    positions_file: str = typer.Option(
        None, "--positions", help="CSV of open positions to monitor."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    at: str = typer.Option(
        None,
        "--at",
        help="Force the moment: HH:MM (US Eastern), YYYY-MM-DDTHH:MM, or a full ISO timestamp.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Show the formula behind every computed value."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Ignore cached daily bars and earnings. Never affects intraday."
    ),
) -> None:
    from dotenv import load_dotenv

    from qms import calendar as mcal
    from qms.config import load_scan_config
    from qms.ingest.http import HttpClient, HttpConfig, HttpError
    from qms.ingest.sec_sic import fetch_ticker_cik_map, sec_client
    from qms.pass2 import clock as clockmod
    from qms.pass2 import daily as dailymod
    from qms.pass2 import derive, earnings as earningsmod, positions as posmod
    from qms.pass2 import quote as quotemod
    from qms.pass2 import render_json, render_text
    from qms.pass2.model import Candidate, Packet, SourceFailure, Value

    load_dotenv()
    cfg = load_scan_config()

    try:
        now, forced = clockmod.resolve_now(at)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    session_clock = clockmod.build(now, forced=forced)

    tickers = [s.strip().upper() for s in (symbols or []) if s.strip()]
    positions_path = Path(positions_file) if positions_file else None

    packet = Packet(
        generated_at=session_clock.now,
        market_state=session_clock.describe_state(),
        session_date=session_clock.session_date,
        minutes_since_open=session_clock.minutes_since_open,
        session_close_et=session_clock.session_close,
        half_day=session_clock.half_day,
        account=cfg.sizing.account,
        risk_budget=cfg.sizing.account * cfg.sizing.risk_pct,
        forced_time=forced,
        forced_ahead_seconds=(
            (session_clock.now - clockmod.now_utc()).total_seconds() if forced else 0.0
        ),
    )

    position_rows: list[dict] = []
    if positions_path is not None:
        position_rows, failures = posmod.read_positions(positions_path)
        packet.failures.extend(failures)

    position_symbols = [
        (r.get("symbol") or "").strip().upper() for r in position_rows
    ]
    all_symbols = sorted({*tickers, *[s for s in position_symbols if s]})

    if not all_symbols:
        typer.echo("pass2: no tickers given and no positions to monitor.", err=True)
        raise typer.Exit(EXIT_NOTHING)

    # Yahoo's unofficial endpoint: modest concurrency, as elsewhere in this codebase.
    market = HttpClient(
        config=HttpConfig(max_workers=4, min_interval_s=0.10),
        headers={"Accept": "application/json"},
    )
    reference = session_clock.reference_session
    bars_through = mcal.previous_session(reference)

    # -------------------------------------------------------------------- intraday
    quotes: dict = {}
    for symbol in all_symbols:
        try:
            payload = quotemod.fetch_chart(market, symbol)
        except (HttpError, Exception) as exc:  # noqa: BLE001 - degrade this symbol only
            packet.failures.append(
                SourceFailure(
                    source=quotemod.SOURCE,
                    detail=f"{symbol}: {exc}",
                    rate_limited=getattr(exc, "status", None) == 429,
                )
            )
            continue
        quotes[symbol] = quotemod.build_quote(symbol, payload, session_clock, cfg)

    # ----------------------------------------------------------------- daily bars
    bars = dailymod.load_bars(
        all_symbols,
        bars_through,
        client=market,
        use_cache=not no_cache,
        failures=packet.failures,
    )

    # ------------------------------------------------------------------- earnings
    today = reference
    nasdaq_hits: dict = {}
    sec_past: dict = {}
    fmp_key = os.environ.get("FMP_API_KEY", "").strip()

    if all_symbols:
        try:
            nasdaq_hits = earningsmod.scan_nasdaq(
                market,
                set(all_symbols),
                today,
                use_cache=not no_cache,
                failures=packet.failures,
            )
        except Exception as exc:  # noqa: BLE001
            packet.failures.append(
                SourceFailure(source=earningsmod.SOURCE_NASDAQ, detail=str(exc))
            )

        try:
            sec = sec_client()
            cik_map = fetch_ticker_cik_map(sec)
            for symbol in all_symbols:
                cik = cik_map.get(symbol)
                if cik:
                    sec_past[symbol] = earningsmod.fetch_sec_past(
                        sec, cik, failures=packet.failures
                    )
        except Exception as exc:  # noqa: BLE001
            packet.failures.append(SourceFailure(source=earningsmod.SOURCE_SEC, detail=str(exc)))

    if not fmp_key:
        packet.failures.append(
            SourceFailure(
                source="fmp",
                detail=(
                    "FMP_API_KEY is not set, so no source can confirm a date. Earnings "
                    "will report as 'estimated' at best. See .env.example."
                ),
            )
        )

    def earnings_for(symbol: str):
        fails: list[SourceFailure] = []
        fmp_rows = (
            earningsmod.fetch_fmp(market, symbol, fmp_key, failures=fails) if fmp_key else []
        )
        return earningsmod.reconcile(
            symbol,
            today,
            nasdaq=nasdaq_hits.get(symbol),
            fmp=fmp_rows,
            sec_past=sec_past.get(symbol, []),
            failures=fails,
        )

    earnings_reports = {s: earnings_for(s) for s in all_symbols}

    # ------------------------------------------------------------------ positions
    for row in position_rows:
        symbol = (row.get("symbol") or "").strip().upper()
        quote = quotes.get(symbol)
        price = (
            quote.current_price
            if quote
            else Value.unavailable(reason="no quote could be fetched", source=quotemod.SOURCE)
        )
        report = earnings_reports.get(symbol)
        days = (
            report.trading_days_until.value
            if report and report.trading_days_until.ok
            else None
        )
        packet.positions.append(
            posmod.evaluate(
                row,
                cfg,
                today=today,
                price=price,
                is_live=bool(quote and quote.is_live),
                bars=bars,
                earnings_days=days,
            )
        )

    # ----------------------------------------------------------------- candidates
    for symbol in tickers:
        quote = quotes.get(symbol)
        if quote is None:
            packet.candidates.append(
                Candidate(
                    symbol=symbol,
                    quote=quotemod.Quote(
                        symbol=symbol,
                        current_price=Value.unavailable(reason="quote fetch failed"),
                        price_time=Value.unavailable(reason="quote fetch failed"),
                        session_low=Value.unavailable(reason="quote fetch failed"),
                        session_low_time=Value.unavailable(reason="quote fetch failed"),
                        session_high=Value.unavailable(reason="quote fetch failed"),
                        premarket_low_excluded=Value.unavailable(reason="quote fetch failed"),
                        crosscheck=Value.unavailable(reason="quote fetch failed"),
                    ),
                    failures=[
                        SourceFailure(
                            source=quotemod.SOURCE, detail=f"{symbol}: no intraday data"
                        )
                    ],
                )
            )
            continue
        daily_data = dailymod.compute(symbol, bars, cfg, reference, quote.current_price)
        candidate = Candidate(
            symbol=symbol,
            quote=quote,
            daily=daily_data,
            earnings=earnings_reports.get(symbol),
        )
        packet.candidates.append(derive.enrich(candidate, cfg))

    # Alerts bubble up from the positions so the renderer can put them above everything.
    for report in packet.positions:
        packet.alerts.extend(report.alerts)

    if as_json:
        typer.echo(json.dumps(render_json.render(packet, cfg), indent=2))
    else:
        _write_text(render_text.render(packet, cfg, verbose=verbose))

    raise typer.Exit(EXIT_OK)


def _write_text(text: str) -> None:
    """Print without dying on a console that cannot encode the text.

    The operator's console is cp1257. The renderer is ASCII-only by construction, but a
    vendor-supplied string (a company name, an error message) can carry anything, and
    losing the whole packet to a UnicodeEncodeError at the last step would be a poor trade.
    """
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.buffer.write(text.encode(encoding, errors="replace") + b"\n")


if __name__ == "__main__":  # pragma: no cover
    app()
