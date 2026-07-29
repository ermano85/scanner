"""Command-line entry point.

    qms config-check          validate config/*.yaml and print the resolved values
    qms ingest                fetch universe, bars and earnings
    qms features              build the feature store
    qms scan --as-of DATE     run Scan A
    qms report --as-of DATE   render HTML + CSV
    qms nightly               the whole pipeline, with the data-quality gate
"""

from __future__ import annotations

import datetime as dt

import typer

from qms import __version__
from qms.config import load_scan_config, load_universe_config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Qullamaggie swing scanner — nightly breakout triage. Not a trading system.",
)


@app.command("version")
def version_cmd() -> None:
    """Print the scanner version."""
    typer.echo(f"qms {__version__}")


@app.command("config-check")
def config_check() -> None:
    """Validate both config files and echo the resolved thresholds.

    Fails loudly on an unknown key, a missing key, or a cross-section incoherence such as
    a gate referencing an SMA period the feature layer never computes.
    """
    scan = load_scan_config()
    universe = load_universe_config()

    gates = scan.scan_a.gates
    typer.echo("config/scan.yaml OK")
    typer.echo(f"  adjustment policy : {scan.data.adjustment_policy}")
    typer.echo(f"  min price         : ${gates.min_price:,.2f}")
    typer.echo(f"  min $ volume (20d): ${gates.min_dollar_vol:,.0f}")
    typer.echo(f"  min ADR%          : {gates.min_adr}")
    typer.echo(f"  momentum pctile   : top {(1 - gates.momentum_pctile) * 100:.0f}%")
    typer.echo(
        f"  MA stack          : {scan.scan_a.ma_stack.fast}>{scan.scan_a.ma_stack.slow} "
        f"on {scan.scan_a.ma_stack.k} of last {scan.scan_a.ma_stack.m}"
    )
    typer.echo(f"  earnings blackout : {gates.earnings_blackout_days} trading days")
    typer.echo("config/universe.yaml OK")
    typer.echo(f"  exchanges         : {sorted(universe.enabled_exchanges())}")
    typer.echo(f"  include ETFs      : {universe.include_etfs}")
    typer.echo(f"  active floor $vol : ${universe.active_universe_floor_dollar_vol:,.0f}")


@app.command("ingest")
def ingest_cmd(
    backfill: bool = typer.Option(False, "--backfill", help="Full history for the whole universe."),
    full_universe: bool = typer.Option(
        False, "--full-universe", help="Refetch every symbol, not just the active set."
    ),
    symbols: str = typer.Option("", "--symbols", help="Comma-separated subset, for debugging."),
    gapfill_only: bool = typer.Option(
        False,
        "--gapfill-only",
        help="Skip the primary fetch; only repair under-covered sessions from the fallback source.",
    ),
) -> None:
    """Fetch universe, bars and earnings. Idempotent and resumable."""
    subset = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None

    if gapfill_only:
        from qms.calendar import last_completed_session
        from qms.config import load_scan_config as _scan, load_universe_config as _universe
        from qms.ingest.gapfill import run_gapfill

        run_gapfill(_scan(), _universe(), last_completed_session(), symbols=subset)
        return

    from qms.ingest.run import run_ingest

    run_ingest(backfill=backfill, full_universe=full_universe, symbols=subset)


@app.command("features")
def features_cmd(
    rebuild: bool = typer.Option(False, "--rebuild", help="Recompute the whole feature store."),
) -> None:
    """Build the feature store from the bar store."""
    from qms.features.build import build_features

    build_features(rebuild=rebuild)


@app.command("scan")
def scan_cmd(
    as_of: str = typer.Option("", "--as-of", help="Session the watchlist is FOR (YYYY-MM-DD)."),
) -> None:
    """Run Scan A and print the ranked shortlist."""
    from qms.rules.scan_a import run_scan_a

    run_scan_a(as_of_date=_parse_as_of(as_of), echo=True)


@app.command("report")
def report_cmd(
    as_of: str = typer.Option("", "--as-of", help="Session the watchlist is FOR (YYYY-MM-DD)."),
) -> None:
    """Render charts, HTML and CSV for a scan date."""
    from qms.report.build import build_report

    build_report(as_of_date=_parse_as_of(as_of))


@app.command("brief")
def brief_cmd(
    as_of: str = typer.Option("", "--as-of", help="Session the watchlist is FOR (YYYY-MM-DD)."),
) -> None:
    """Write the compact markdown/JSON summary, without re-rendering charts."""
    from qms.config import load_scan_config as _scan
    from qms.paths import scan_out_dir
    from qms.report.brief import write_brief
    from qms.rules.scan_a import run_scan_a

    cfg = _scan()
    result = run_scan_a(as_of_date=_parse_as_of(as_of), cfg=cfg)
    write_brief(result, cfg, scan_out_dir(result.as_of_date))


@app.command("export-tradingview")
def export_tradingview_cmd(
    as_of: str = typer.Option("", "--as-of", help="Session the watchlist is FOR (YYYY-MM-DD)."),
) -> None:
    """Write a TradingView-importable watchlist for a scan date."""
    from qms.paths import scan_out_dir
    from qms.report.tradingview import write_watchlist
    from qms.rules.scan_a import run_scan_a

    result = run_scan_a(as_of_date=_parse_as_of(as_of))
    write_watchlist(result.candidates, result.as_of_date, scan_out_dir(result.as_of_date))


@app.command("publish")
def publish_cmd(
    as_of: str = typer.Option("", "--as-of", help="Scan date to publish. Default: the newest."),
    dest: str = typer.Option("site", "--dest", help="Directory to assemble."),
) -> None:
    """Copy a scan's output into a directory ready for static hosting."""
    from pathlib import Path as _Path

    from qms.report.publish import publish

    publish(as_of=_parse_as_of(as_of), dest=_Path(dest))


@app.command("nightly")
def nightly_cmd(
    as_of: str = typer.Option("", "--as-of", help="Session the watchlist is FOR (YYYY-MM-DD)."),
    skip_ingest: bool = typer.Option(False, "--skip-ingest", help="Reuse the stored bars."),
    skip_gapfill: bool = typer.Option(
        False, "--skip-gapfill", help="Do not repair under-covered sessions."
    ),
    full_universe: bool = typer.Option(
        False, "--full-universe", help="Refetch every symbol, not just the active set."
    ),
    allow_stale: bool = typer.Option(
        False,
        "--allow-stale",
        help="Proceed even if the vendor is missing recent sessions. The staleness is "
        "stamped on the report.",
    ),
) -> None:
    """Ingest, features, quality gate, scan, report — the whole pipeline."""
    from qms.nightly import run_nightly

    run_nightly(
        as_of_date=_parse_as_of(as_of),
        skip_ingest=skip_ingest,
        skip_gapfill=skip_gapfill,
        full_universe=full_universe,
        allow_stale=allow_stale,
    )


@app.command("quality")
def quality_cmd(
    allow_stale: bool = typer.Option(False, "--allow-stale", help="Downgrade staleness."),
) -> None:
    """Run the data-quality gate against the current stores."""
    from qms.calendar import last_completed_session
    from qms.config import load_scan_config as _scan, load_universe_config as _universe
    from qms.ingest.base import ACTIONS_SCHEMA, BARS_SCHEMA, UNIVERSE_SCHEMA
    from qms.ingest.store import read_parquet_or_empty
    from qms.quality import active_universe, check_quality, enforce
    from qms import paths as _paths

    bars = read_parquet_or_empty(_paths.BARS_FILE, BARS_SCHEMA)
    enforce(
        check_quality(
            bars=bars,
            universe=read_parquet_or_empty(_paths.UNIVERSE_FILE, UNIVERSE_SCHEMA),
            actions=read_parquet_or_empty(_paths.ACTIONS_FILE, ACTIONS_SCHEMA),
            cfg=_scan(),
            expected_session=last_completed_session(),
            active=active_universe(bars, _universe().gapfill_floor_dollar_vol),
        ),
        allow_stale=allow_stale,
    )


def _parse_as_of(value: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"--as-of must be YYYY-MM-DD, got {value!r}") from exc


if __name__ == "__main__":
    app()
