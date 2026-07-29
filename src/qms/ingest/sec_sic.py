"""Industry classification from SEC EDGAR, for excluding sectors you do not want to trade.

Free, official, no key — the only such source in the pipeline. Two endpoints:

* `sec.gov/files/company_tickers.json` — ticker to CIK for ~10,400 filers.
* `data.sec.gov/submissions/CIK##########.json` — carries `sic` and `sicDescription`.

**Lazy and permanently cached.** Only symbols that reach the gate get looked up — roughly
545 on the first run against the liquid universe, then a handful as names come and go.
Enumerating all 10,400 filers up front would cost twenty minutes to answer a question about
a few hundred.

Deliberately *not* using the legacy `browse-edgar?action=getcompany&SIC=` bulk feed. It does
return usable CIK and SIC values, but corrupts every company name to `ARRAY(0x...)`, which
is a poor foundation to build on.

Verified 2026-07-27 against the live shortlist: NRIX/CLYM/ERAS 2834, RLAY/VOR/AGEN 2836,
QDEL 2835, SNOW 7372, DELL 3571, CBRL 5812.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from qms import paths
from qms.ingest.http import HttpClient, HttpConfig
from qms.ingest.store import read_parquet_or_empty, write_parquet_atomic

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

SIC_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "cik": pl.Utf8,
    "sic": pl.Int32,
    "sic_description": pl.Utf8,
    "fetched_on": pl.Date,
}

# SEC publishes a limit of 10 requests/second and asks for a descriptive User-Agent with a
# contact address. The shared client is tuned to ~20/s for Yahoo, so this gets its own.
SEC_HTTP = HttpConfig(max_workers=5, min_interval_s=0.11)
SEC_USER_AGENT = "qms-swing-scanner/0.1 (personal research; ermano85@gmail.com)"

# How long a cached classification is trusted. SIC codes effectively never change, so this
# is about eventually noticing a reclassification, not about freshness.
CACHE_DAYS = 90


def sec_client() -> HttpClient:
    return HttpClient(config=SEC_HTTP, headers={"User-Agent": SEC_USER_AGENT})


def fetch_ticker_cik_map(client: HttpClient) -> dict[str, str]:
    """Ticker -> zero-padded 10-digit CIK."""
    payload = client.get_json(COMPANY_TICKERS_URL) or {}
    mapping: dict[str, str] = {}
    for entry in payload.values():
        ticker = (entry.get("ticker") or "").strip().upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            mapping[ticker] = str(cik).zfill(10)
    return mapping


def fetch_sic(client: HttpClient, cik: str) -> tuple[int | None, str | None]:
    payload = client.get_json(SUBMISSIONS_URL.format(cik=cik)) or {}
    raw = str(payload.get("sic") or "").strip()
    description = payload.get("sicDescription") or None
    try:
        return int(raw), description
    except ValueError:
        return None, description


def load_cache() -> pl.DataFrame:
    return read_parquet_or_empty(paths.SIC_FILE, SIC_SCHEMA)


def ensure_sic(
    symbols: list[str],
    client: HttpClient | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    """Return SIC rows for `symbols`, fetching and caching whatever is missing.

    Symbols with no SEC filer record — ETFs, most foreign issuers — are cached with a null
    `sic` so they are not re-requested every night. They must still pass the gate; see
    `rules.gates.attach_sector`.
    """
    paths.ensure_dirs()
    cache = load_cache()
    wanted = set(symbols)

    if refresh:
        known: set[str] = set()
    else:
        cutoff = dt.date.today() - dt.timedelta(days=CACHE_DAYS)
        fresh = cache.filter(pl.col("fetched_on") >= cutoff) if not cache.is_empty() else cache
        known = set(fresh["symbol"].to_list()) if not fresh.is_empty() else set()

    missing = sorted(wanted - known)
    if not missing:
        return cache.filter(pl.col("symbol").is_in(list(wanted)))

    client = client or sec_client()
    print(f"[sic] looking up {len(missing)} symbol(s) at SEC ({len(known)} cached)")

    ticker_map = fetch_ticker_cik_map(client)
    today = dt.date.today()
    records: list[dict] = []

    def lookup(symbol: str) -> tuple[int | None, str | None]:
        cik = ticker_map.get(symbol)
        if cik is None:
            return None, None
        return fetch_sic(client, cik)

    def on_error(symbol: str, exc: Exception) -> None:
        print(f"[sic] {symbol}: {str(exc)[:120]}")

    resolved = {
        symbol: value
        for symbol, value in client.map(lookup, missing, on_error=on_error)
    }

    for symbol in missing:
        sic, description = resolved.get(symbol, (None, None))
        records.append(
            {
                "symbol": symbol,
                "cik": ticker_map.get(symbol),
                "sic": sic,
                "sic_description": description,
                "fetched_on": today,
            }
        )

    fetched = pl.DataFrame(
        records,
        schema_overrides={"sic": pl.Int32, "fetched_on": pl.Date, "cik": pl.Utf8,
                          "sic_description": pl.Utf8},
    ).select(list(SIC_SCHEMA))

    combined = (
        pl.concat([cache, fetched], how="vertical_relaxed")
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )
    write_parquet_atomic(combined, paths.SIC_FILE)
    classified = int(fetched["sic"].is_not_null().sum())
    print(f"[sic] resolved {classified}/{len(missing)}; cache now {combined.height} symbols")

    return combined.filter(pl.col("symbol").is_in(list(wanted)))
