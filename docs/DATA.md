# Data policy and known failure modes

Spec §2 requires the split/dividend adjustment policy to be an explicit, documented
decision. This is that document.

## Adjustment policy: split-adjusted, NOT dividend-adjusted

### The decision

Bars are stored **split-adjusted and not dividend-adjusted**. The `adjclose` column
(split *and* dividend adjusted) is carried alongside for provenance but **no v1 feature
reads it**.

### Why

Momentum and ADR% need split continuity — without it, every split registers as a -50% or
-90% one-day move and the momentum ranking becomes noise.

Dividend adjustment is the opposite: it is correct for total-return backtesting and wrong
for this tool. It restates every historical price by a compounding factor, so the numbers
the scanner prints stop matching the numbers on the screen. That breaks two things
concretely:

- The `min_price` gate ($5) would test an adjusted price, not the tradeable one.
- The whole of §5 position sizing — `stop = low * 0.995`, `entry`, `risk_share` — is
  arithmetic on prices the user will actually see in a broker window. Dividend-adjusted
  inputs would produce stop prices that cannot be entered.

For a tool whose primary output is a stop price and a share count, matching the screen
wins over total-return purity.

### Verification

Yahoo's `indicators.quote` OHLCV already *is* split-adjusted and dividend-unadjusted, so
this policy needs no post-processing. Verified 2026-07-27 against NVDA's 10:1 split of
2024-06-10:

| Date | stored `close` | real screen price that day | stored `volume` |
|---|---|---|---|
| 2024-06-06 | 121.00 | ~$1,210 | 664,696,000 (~10x the real 66M) |
| 2024-06-07 | 120.89 | ~$1,209 | 412,386,000 |
| 2024-06-10 (split) | 121.79 | $121.79 | 313,434,100 |

Prices and volumes before the split are both restated by the 10:1 factor, and `adjclose`
on the same rows is *slightly lower* than `close` (120.79 vs 121.00), which is the
dividend adjustment we are deliberately not using.

## Known failure modes of the free feed

### Yahoo drops whole sessions — and the data is not actually missing

**Observed 2026-07-27:** Yahoo returned a row for session 2026-07-24 with `close`, `open`,
`high`, `low` and `volume` **all null**, for every symbol tested — AAPL, MSFT, NVDA, SPY,
KO, JNJ. Not a parser bug and not symbol-specific. Reproduced under both `range=5d` and
`range=1mo`.

> **Correction, twice over.** This was first written up as a whole-market hole — data that
> simply did not exist anywhere for free — and presented as an unavoidable cost of not
> paying a vendor. Both halves were wrong.
>
> 1. **The data existed.** Nasdaq's free endpoint had the session all along: AAPL closed
>    $333.02 on 47,489,420 shares.
> 2. **The gap was transient.** By 2026-07-28, Yahoo had backfilled 2026-07-24 for every
>    symbol checked, at values matching Nasdaq exactly.
>
> The error mattered because it made a Monday-evening report show Thursday's close and
> blamed the feed rather than the ingest.

**So what is the gap-fill actually for?** Not permanently missing data — for *same-evening*
scanning. Yahoo's most recent session is unreliable at the moment you want to scan and
settles over the following day. Without a fallback you either scan on stale prices or wait
a day for a watchlist that was meant for tomorrow morning. `ingest/nasdaq_bars.py` closes
that window.

It follows that the nightly order matters: Yahoo refresh **first** (fast, and it may
already have the session), then repair only what is still thin. `nightly.run_nightly` does
exactly that, and `--gapfill-only` skips the cheap step — useful for diagnosis, wasteful as
a routine.

Consequences and handling:

- A null-OHLC row is **not a bar**; the parser drops it rather than storing zeros or
  forward-filling. Silently interpolating here would fabricate a session.
- The data-quality gate measures the newest *well-covered* session, not `max(date)`, and
  **fails the run** when it lags the last completed session.
- The nightly job refetches a `nightly_lookback_days` window rather than a single bar, so
  a vendor backfill is picked up automatically.

### The trailing edge is ragged, not cleanly absent

On the same date, 42 of 11,574 symbols *did* carry a 2026-07-24 bar. That is worse than a
clean absence, because logic keyed on `max(date)` then gets it wrong twice: it reports the
store as fresh, and it builds a cross-section mixing two sessions, quietly handing those 42
names an extra day of return in every percentile.

Everything that needs a reference date therefore goes through
`quality.effective_latest_session`, which returns the newest session covered by at least
`quality.min_universe_coverage` of the population.

**Measured population must equal repaired population.** The gap-fill repairs only the
*active* universe — a name below the refetch floor cannot clear the scan's dollar-volume
gate, so repairing it buys nothing. If coverage were then measured across all 11,574
stored symbols, it would sit at the active share of the universe forever and the gate could
never pass. `find_gap_sessions`, `check_quality` and `latest_cross_section` are all scoped
the same way.

### Fallback source: Nasdaq historical quotes

`api.nasdaq.com/api/quote/{SYMBOL}/historical` — unofficial, like the earnings calendar.

Verified 2026-07-27:

| Property | Result |
|---|---|
| OHLCV | Complete |
| History depth | 519 rows back to 2024-07-01 |
| ETFs | Supported via `assetclass=etf` |
| Split adjustment | Yes, matching our policy |
| Agreement with Yahoo | Exact on close (AAPL 07/23 = 321.66 both) |
| Volume agreement | Within Yahoo's rounding to 100 (40,840,800 vs 40,840,780) |
| Throughput | ~2.4 s per request; 15 sequential with no throttling |

Two consequences of that last row: it is a repair path, never a bulk loader; and it
publishes no dividend-adjusted close, so `adjclose` is stored **null** for repaired rows
rather than being faked from `close`.

#### What a repair actually costs

Measured 2026-07-28: roughly 2.4 s per request idle, 3.3 s under sustained load, six
workers, so a full pass over the ~5,400-symbol active universe takes **45–60 minutes**.

That is the cost of a gap day, not of every night. The nightly refreshes from Yahoo first
(~6,000 symbols in a few minutes) and only repairs what is still thin afterwards, so a
clean night skips this entirely. If it turns out to fire nightly, the levers in order of
preference are: raise `DEFAULT_MAX_WORKERS` in `ingest/http.py` (the endpoint tolerated six
concurrent for an hour with no 429s, so twelve is likely fine); narrow the repair
population from the refetch floor toward the scan's own `min_dollar_vol`; or move to an
authenticated source.

#### Yahoo's `range=` and explicit-date queries disagree

Observed 2026-07-28: `range=10d` returned a populated 2026-07-24 bar while
`period1`/`period2` covering the same session still returned null, for the same symbol.
The ingest uses explicit dates, so a spot-check with `range=` can say the gap is closed
while the pipeline still sees it open. Check the way the ingest queries, not the way that
is convenient.

#### A single-day request returns nothing

`fromdate=2026-07-24&todate=2026-07-24` returns an **empty** table, while
`fromdate=2026-07-23&todate=2026-07-24` returns both sessions. The endpoint needs
`fromdate < todate`.

This is a nasty trap for exactly this module, because repairing one missing session is the
normal case. Found the expensive way: a full pass over 5,470 symbols fetched precisely
nothing while appearing to work, since an empty result is indistinguishable from "the
vendor doesn't have it either". `fetch_symbol_bars` now widens every request to span at
least a week and lets the caller filter, and `tests/test_gapfill.py` pins that behaviour.

### Live bars

Yahoo returns a partially-formed bar for the current session while the market is open.
Ingesting it would mean the same date holds different values before and after the close —
which corrupts the feature store and would make any future backtest meaningless.
`parse_bars` therefore drops every bar dated after `calendar.last_completed_session()`.

### Survivorship bias

The universe file lists only *currently* listed securities. Nothing that delisted,
merged or went to zero is present.

- Harmless for live scanning: you cannot trade a delisted ticker tomorrow.
- **Fatal for backtesting.** A Phase 3 backtest on this data would silently select for
  survivors and report inflated results. It needs a point-in-time universe (Norgate or
  equivalent). Do not build one on this feed and believe the numbers.

### Unofficial endpoints

Two of four sources have no contract:

| Source | Endpoint | Status |
|---|---|---|
| Universe | `nasdaqtrader.com/dynamic/symdir/` | Official |
| Bars, splits, dividends | `query1.finance.yahoo.com/v8/finance/chart` | **Unofficial** |
| Earnings calendar | `api.nasdaq.com/api/calendar/earnings` | **Unofficial** |
| Ticker → CIK | `sec.gov/files/company_tickers.json` | Official |

They are used politely — bounded concurrency, exponential backoff with jitter, a
descriptive User-Agent, and a floor on request spacing, all in `ingest/http.py`.

### Stooq is not usable

Evaluated and rejected 2026-07-27. Stooq's bulk daily download now serves a SHA-256
proof-of-work bot wall instead of CSV. Defeating bot detection is out of scope, so the
source is excluded rather than worked around.

## Earnings coverage

The Nasdaq calendar supplies a `time` field that maps to `when`:

| Nasdaq value | stored `when` | meaning |
|---|---|---|
| `time-pre-market` | `bmo` | before the open |
| `time-after-hours` | `amc` | after the close |
| `time-not-supplied` | `unknown` | timing not published |

`unknown` is the **most common** value — 1,980 of 4,022 rows over a 20-session window on
2026-07-27, against 1,163 `amc` and 879 `bmo`. The blackout gate must therefore treat
`unknown` conservatively rather than assuming a convenient time of day.

Symbols with no calendar entry at all **pass** the blackout gate and are tagged
`EARNINGS_UNKNOWN`. Hard-failing on absent data would silently delete a large slice of the
universe whenever the free calendar has a bad night — a failure mode invisible in the
output, which is the worst kind.
