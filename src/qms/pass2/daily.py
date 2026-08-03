"""Everything computed from daily bars: ATR, ADR, the moving averages, the prior session.

Two decisions worth stating outright, because both are the kind that quietly produce
numbers that do not reconcile against the operator's own screener:

**Only completed sessions feed the indicators.** ATR, ADR and the SMAs are computed from
bars strictly *before* the session in progress. Folding today's half-formed bar in would
make the ATR drift minute by minute, so the entry band would move under the operator
while they were reading it, and the same command would give different answers at 10:00
and 10:05. The band is anchored to the previous close's ATR — which is also exactly what
the nightly scan does, so pass 1 and pass 2 agree by construction.

**The distance-to-MA convention is inherited, not reinvented.** `features/trend.py`
defines it as `(price - sma) / price`, divided by the price rather than by the average.
Reimplementing that "more correctly" here would produce a number that disagrees with
`ranked.csv` in the third decimal and cost an afternoon to track down.

The bar store is the nightly job's `data/bars/bars.parquet`. Pass 2 reads it and never
writes to it: `docs/DATA.md` is explicit that a partial bar must not enter that file, and
a tool that runs mid-session has nothing but partial bars to offer.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms import calendar as mcal
from qms.config import ScanConfig
from qms.features.volatility import adr_pct, wilder_atr
from qms.ingest.base import BARS_SCHEMA, conform, valid_bars
from qms.ingest.http import HttpClient
from qms.ingest.yahoo import fetch_symbol_chart, parse_bars
from qms.paths import BARS_FILE, DATA_DIR
from qms.pass2.model import Daily, SourceFailure, Value

CACHE_DIR = DATA_DIR / "pass2-cache" / "bars"
SOURCE_STORE = "qms:data/bars/bars.parquet"
SOURCE_LIVE = "yahoo:chart:1d"

# How much history to pull when the store cannot serve a symbol at all. Comfortably more
# than the longest warm-up any pass-2 field needs (a 50-day SMA plus slack for holidays).
BACKFILL_DAYS = 400


def _required_history(cfg: ScanConfig) -> int:
    """The longest warm-up any reported field needs, in sessions."""
    return max(
        cfg.features.atr.period,
        cfg.features.adr.primary,
        cfg.features.liquidity.avg_dollar_vol_window,
        max(cfg.pass2.report_sma_periods),
        cfg.pass2.trail_sma_period,
    )


def load_bars(
    symbols: list[str],
    through: dt.date,
    *,
    client: HttpClient | None = None,
    use_cache: bool = True,
    failures: list[SourceFailure] | None = None,
) -> pl.DataFrame:
    """Daily bars for `symbols` up to and including `through`.

    Serves from the nightly store when it reaches `through`, and fetches the shortfall
    per symbol when it does not — into pass 2's own cache, never into the nightly store.
    """
    frames: list[pl.DataFrame] = []
    stored = pl.DataFrame(schema=BARS_SCHEMA)
    if BARS_FILE.exists():
        stored = (
            pl.scan_parquet(BARS_FILE)
            .filter(pl.col("symbol").is_in(symbols) & (pl.col("date") <= through))
            .collect()
        )
    if not stored.is_empty():
        frames.append(stored)

    covered = (
        dict(
            stored.group_by("symbol")
            .agg(pl.col("date").max().alias("last"))
            .iter_rows()
        )
        if not stored.is_empty()
        else {}
    )

    missing = [s for s in symbols if covered.get(s) != through]
    for symbol in missing:
        have = covered.get(symbol)
        cached = _read_cache(symbol, through) if use_cache else None
        if cached is not None:
            frames.append(cached)
            continue
        if client is None:
            continue
        start = (have + dt.timedelta(days=1)) if have else (through - dt.timedelta(days=BACKFILL_DAYS))
        try:
            result = fetch_symbol_chart(client, symbol, start, through)
            fetched = valid_bars(conform(parse_bars(symbol, result, through), BARS_SCHEMA))
        except Exception as exc:  # noqa: BLE001 — reported, then this symbol degrades
            if failures is not None:
                failures.append(
                    SourceFailure(
                        source=SOURCE_LIVE,
                        detail=f"{symbol}: daily-bar top-up failed ({exc})",
                        rate_limited=getattr(exc, "status", None) == 429,
                    )
                )
            continue
        if not fetched.is_empty():
            frames.append(fetched)
            if use_cache:
                _write_cache(symbol, through, fetched)

    if not frames:
        return pl.DataFrame(schema=BARS_SCHEMA)
    return (
        pl.concat(frames, how="vertical_relaxed")
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )


def _cache_path(symbol: str, through: dt.date):
    # The through-date is in the filename, so a new session cannot be served yesterday's
    # tail. Expiry by naming rather than by a TTL that has to be checked correctly.
    safe = symbol.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{safe}-{through.isoformat()}.parquet"


def _read_cache(symbol: str, through: dt.date) -> pl.DataFrame | None:
    path = _cache_path(symbol, through)
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except Exception:  # noqa: BLE001 — a corrupt cache is a re-fetch, not a crash
        return None


def _write_cache(symbol: str, through: dt.date, frame: pl.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(_cache_path(symbol, through))
    except OSError:
        pass  # a cache that cannot be written is a slower run, not a failed one


def compute(
    symbol: str,
    bars: pl.DataFrame,
    cfg: ScanConfig,
    reference_session: dt.date,
    current_price: Value,
) -> Daily:
    """Indicators for one symbol, from bars strictly before `reference_session`."""
    atr_period = cfg.features.atr.period
    adr_period = cfg.features.adr.primary
    vol_window = cfg.features.liquidity.avg_dollar_vol_window
    periods = sorted({*cfg.pass2.report_sma_periods, cfg.pass2.trail_sma_period})

    # Strictly before: today's bar, if the vendor has begun forming one, is partial.
    frame = (
        bars.filter((pl.col("symbol") == symbol) & (pl.col("date") < reference_session))
        .sort("date")
    )

    def missing(reason: str) -> Daily:
        gap = Value.unavailable(reason=reason, source=SOURCE_STORE)
        return Daily(
            symbol=symbol,
            atr=gap,
            adr_pct=gap,
            smas={p: gap for p in periods},
            sma_dist_pct={p: gap for p in periods},
            sma_dist_adr={p: gap for p in periods},
            prev_open=gap,
            prev_high=gap,
            prev_low=gap,
            prev_close=gap,
            prev_volume=gap,
            prev_date=gap,
            avg_dollar_vol=gap,
            avg_vol=gap,
        )

    if frame.is_empty():
        return missing("no daily bars available for this symbol")

    needed = _required_history(cfg)
    if frame.height < needed:
        return missing(
            f"only {frame.height} daily bars available; {needed} needed for the "
            f"longest window (SMA {max(periods)}, ADR {adr_period}, ATR {atr_period})"
        )

    enriched = frame.with_columns(
        wilder_atr(atr_period).alias("atr"),
        adr_pct(adr_period).alias("adr"),
        (pl.col("close") * pl.col("volume"))
        .rolling_mean(window_size=vol_window, min_samples=vol_window)
        .alias("advol"),
        pl.col("volume")
        .rolling_mean(window_size=vol_window, min_samples=vol_window)
        .alias("avol"),
        *[
            pl.col("close")
            .rolling_mean(window_size=p, min_samples=p)
            .alias(f"sma_{p}")
            for p in periods
        ],
    )
    last = enriched.row(-1, named=True)
    bar_date = last["date"]
    stamp = dt.datetime.combine(bar_date, dt.time(16, 0), tzinfo=mcal.EXCHANGE_TZ)

    def computed(key: str, formula: str) -> Value:
        raw = last[key]
        if raw is None:
            return Value.unavailable(
                reason=f"insufficient history to compute (needs a full window through {bar_date})",
                source=SOURCE_STORE,
            )
        return Value.computed(float(raw), formula=formula, source=SOURCE_STORE, as_of=stamp)

    atr = computed(
        "atr",
        f"Wilder ATR({atr_period}) = EWM(alpha=1/{atr_period}, adjust=False) of "
        f"TR; TR = max(H-L, |H-Cprev|, |L-Cprev|). Includes gaps. "
        f"Daily bars through {bar_date}.",
    )
    adr = computed(
        "adr",
        f"ADR%({adr_period}) = 100 * (mean(H/L over {adr_period} sessions) - 1). "
        f"Intraday range only, so gaps are excluded. Daily bars through {bar_date}.",
    )

    smas: dict[int, Value] = {}
    dist_pct: dict[int, Value] = {}
    dist_adr: dict[int, Value] = {}
    for p in periods:
        sma = computed(f"sma_{p}", f"SMA({p}) = mean(close) over the last {p} sessions through {bar_date}.")
        smas[p] = sma
        if sma.ok and current_price.ok and current_price.value:
            price = float(current_price.value)
            pct = (price - float(sma.value)) / price * 100.0
            dist_pct[p] = Value.computed(
                pct,
                formula=(
                    f"(price - SMA({p})) / price * 100 "
                    f"= ({price:.4f} - {float(sma.value):.4f}) / {price:.4f} * 100. "
                    "Divided by price, matching features/trend.py."
                ),
                source="derived",
                as_of=stamp,
            )
            if adr.ok and float(adr.value) > 0:
                dist_adr[p] = Value.computed(
                    pct / float(adr.value),
                    formula=(
                        f"distance% / ADR% = {pct:.4f} / {float(adr.value):.4f} "
                        "(distance expressed in average-days)"
                    ),
                    source="derived",
                    as_of=stamp,
                )
            else:
                dist_adr[p] = Value.unavailable(reason="ADR% unavailable", source="derived")
        else:
            reason = (
                "current price unavailable" if not current_price.ok else f"SMA({p}) unavailable"
            )
            dist_pct[p] = Value.unavailable(reason=reason, source="derived")
            dist_adr[p] = Value.unavailable(reason=reason, source="derived")

    def prev(key: str) -> Value:
        raw = last[key]
        if raw is None:
            return Value.unavailable(reason=f"no {key} on {bar_date}", source=SOURCE_STORE)
        return Value.fetched(float(raw), source=SOURCE_STORE, as_of=stamp)

    return Daily(
        symbol=symbol,
        atr=atr,
        adr_pct=adr,
        smas=smas,
        sma_dist_pct=dist_pct,
        sma_dist_adr=dist_adr,
        prev_open=prev("open"),
        prev_high=prev("high"),
        prev_low=prev("low"),
        prev_close=prev("close"),
        prev_volume=prev("volume"),
        prev_date=Value.fetched(bar_date, source=SOURCE_STORE, as_of=stamp),
        avg_dollar_vol=computed(
            "advol",
            f"mean(close * volume) over the last {vol_window} sessions through {bar_date}.",
        ),
        avg_vol=computed(
            "avol", f"mean(volume) over the last {vol_window} sessions through {bar_date}."
        ),
        bars_through=bar_date,
    )
