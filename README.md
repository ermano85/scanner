# qms — Qullamaggie swing scanner

A nightly scanner over US equities and ETFs that produces a **ranked shortlist of candidates for
manual chart review**, plus the position-sizing arithmetic for each one.

It reduces ~13,000 tickers to 20–60 charts worth looking at. That is the whole job.

## What this is not

The tool **does not emit buy signals, does not place orders, and does not judge whether a
consolidation is "tight."** That judgment stays with the human reading the charts. A large share
of the documented edge in this strategy lives in the exit rules and the position sizing — neither
of which a screener touches — so nobody should expect this scan to reproduce the source's
published results.

**This is a research and triage tool, not a trading system, and nothing it produces is financial
advice. Paper-trade the output for a meaningful period before it informs real positions.**

## Quick start

```bash
uv sync
```

```bash
uv run qms config-check
```

```bash
uv run qms ingest --backfill
```

```bash
uv run qms features --rebuild
```

```bash
uv run qms nightly
```

Output lands in `out/<scan-date>/` as `index.html`, `ranked.csv` and `charts/*.png`. The HTML
inlines every chart as base64, so a scan directory is one portable file.

`qms nightly` **fails closed**: if the vendor is missing recent sessions the pipeline stops
before writing anything, rather than producing a normal-looking report built on stale prices.
Pass `--allow-stale` to override, and the staleness is stamped across the top of the report.

### Observed scale

A full backfill is ~12,000 symbols and takes roughly a quarter of an hour at the shipped
politeness settings. After that a run looks like this:

| Step | Cost |
|---|---|
| Bar store | 4.9M rows, ~11,600 symbols, 2 years |
| Feature build | 51 features over the whole panel in ~13 s |
| Scan A funnel | 11,326 → 545 after liquidity → 60 candidates |
| Report | 60 charts + HTML in well under a minute |

## Provenance convention

Every rule in the codebase is tagged:

- `[DOC]` — stated in the Laws of Swing doc or Qullamaggie's FAQ. Treated as authoritative.
- `[EXT]` — extrapolation by the implementer. Tunable, unvalidated, and to be treated with
  suspicion.

`[EXT]` rules **may contribute to ranking and may never act as a filter.** This is enforced
structurally: the `[EXT]` consolidation metrics live in a separate feature namespace that the
gate module cannot import, so violating it is an import error rather than a code-review miss.

## Configuration

Every threshold, window and weight lives in `config/scan.yaml` and `config/universe.yaml`.
**There are no defaults in the Python config model** — a missing key is an error and an unknown
key is an error, because a default value in code is a numeric literal in code, which the spec
forbids for rule logic. `tests/test_no_literals.py` enforces the same rule by walking the AST of
the rules and sizing modules.

## Data sources

All free, all verified reachable on 2026-07-27.

| Need | Source | Official? |
|---|---|---|
| Universe, ETF and test-issue flags | NASDAQ Trader symbol directory | Yes |
| Daily OHLCV (split-adjusted), primary | Yahoo `v8/finance/chart` | **No** |
| Daily OHLCV, gap-fill fallback | Nasdaq `api.nasdaq.com/api/quote/{sym}/historical` | **No** |
| Split and dividend events | Yahoo, same endpoint | **No** |
| Earnings calendar, past and upcoming | Nasdaq `api.nasdaq.com/api/calendar/earnings` | **No** |
| Industry classification (SIC) | SEC EDGAR `submissions` | Yes |

### Read this before trusting the output

**Two of the four sources are unofficial endpoints that can change or disappear without
notice.** The spec this was built from explicitly warns against running a nightly job on free
Yahoo data. Free was chosen deliberately, so the mitigation is structural rather than
contractual:

- Every source sits behind the `Provider` protocol in `src/qms/ingest/base.py`. Moving to a paid
  vendor (EODHD, Polygon, Tiingo) is one new file and a config line.
- A data-quality gate runs after every ingest and **fails the pipeline loudly** rather than
  scanning stale data quietly. It checks last-bar recency, universe coverage, null OHLCV blocks,
  and unexplained large price jumps with no corresponding split record.

**Survivorship bias.** These feeds contain only currently-listed tickers. That is irrelevant for
live scanning and fatal for backtesting — so the Phase 3 backtest harness is *not* viable on this
data. It would need a point-in-time universe (Norgate or similar). Do not add a backtest on top
of this feed and believe its numbers.

**Adjustment policy: split-adjusted, not dividend-adjusted.** See [docs/DATA.md](docs/DATA.md)
for the verification and the reasoning.

## Layout

Four layers, strictly separated, per the spec:

```
ingest   →  data/bars, data/earnings, data/universe   (idempotent, resumable)
features →  data/features/features.parquet            (never recomputed during rule tuning)
rules    →  pure functions over the feature store     (all thresholds from YAML)
output   →  out/<date>/ ranked table + charts + HTML
```

Two constraints hold from the first commit and are what make a future backtest harness a weekend
rather than a rewrite:

- **Causality.** No feature may read a bar at index `> i`. Enforced by `tests/test_causality.py`,
  which enumerates the feature *registry* — so a feature added tomorrow is covered tonight — and
  checks each one two ways: tail-truncation equivalence, and invariance under corruption of all
  future bars.
- **`as_of_date`.** The rule engine is parameterised by scan date and filters the feature store
  to bars strictly before it. `as_of_date` is the session the watchlist is *for*, so the
  Monday-evening run passes Tuesday and legitimately sees Monday's close.

Verified end to end on the real store: a scan for 2026-07-10 run against the full store produced
a **byte-identical** ranked table to the same scan run against a store physically truncated at
that date, across 11,326 symbols. Today's scan shares only 18 of 60 names with it, so the check
is not vacuous.

## Two things worth knowing before you tune anything

**One position-sizing cap can never bind.** With the doc's own numbers — 1% of average volume,
and position × 200 ≤ average dollar volume — the dollar-volume cap works out to exactly half the
share-liquidity cap at every price and volume, so `liquidity` will never be reported as the
binding constraint. Both rules are `[DOC]` so neither was dropped. See `src/qms/sizing/calculator.py`.

**The vendor's trailing edge is ragged.** On 2026-07-24, 42 of 11,574 symbols carried a bar for a
session missing for everyone else. Anything keyed on `max(date)` therefore gets it wrong twice
over: it calls stale data fresh, and it builds a cross-section mixing two sessions, handing those
42 names an extra day of return in every percentile. The scan pins to the newest *well-covered*
session instead, and drops symbols whose last bar is more than `quality.max_bar_age_sessions`
behind it.

**That missing session was a Yahoo defect, not missing market data.** It was originally documented
here as a whole-market hole and an unavoidable cost of not paying for a feed. That was wrong.
Nasdaq's free endpoint had the session all along — AAPL closed $333.02 on 47.5M shares — so the
fix was a fallback chain, not a subscription. Yahoo stays primary for bulk history; the gap-fill
in `src/qms/ingest/gapfill.py` repairs the trailing edge from Nasdaq. Full write-up in
[docs/DATA.md](docs/DATA.md).

## Sector exclusion

`config/universe.yaml` excludes SEC SIC codes 2833–2836 — pharmaceuticals, biologics and
diagnostics.

**This is an operator preference, not a rule from the source material.** Nothing in the Laws of
Swing doc says avoid pharma; it is here because clinical-stage biotech moves on binary trial and
FDA outcomes that no chart pattern anticipates. It is tagged as neither `[DOC]` nor `[EXT]` for
that reason, and it lives in config so the judgment stays visible and reversible.

Measured cost: this removes **25 of 60** candidates on the 2026-07-27 scan. The strategy needs
ADR% ≥ 5 and biotech is where much of that volatility lives, so expect materially shorter lists.
If they get too short, loosen `min_adr` or `min_dollar_vol` rather than re-admitting the sector.

Classifications come from SEC EDGAR — free, official, and the only such source in the pipeline.
Lookups are lazy and permanently cached in `data/reference/sic.parquet`: only names that clear the
liquidity gates are ever requested, roughly 545 on the first run. Symbols with no classification
(ETFs, most foreign issuers) **pass** and are tagged `SIC_UNKNOWN`.

## Exports

Each scan writes, alongside the HTML report:

| File | Purpose |
|---|---|
| `ranked.csv` | The full numeric table |
| `claude-brief.md` | Self-contained summary for pasting into a conversation |
| `claude-brief.json` | The same content, machine-readable |
| `tradingview.txt` | Comma-separated `EXCHANGE:SYMBOL` for *Upload list…* |

The brief carries its own caveats — what the tool does not do, which numbers are unvalidated,
that sizing is a pre-open estimate. Caveats left behind in this README are caveats that do not
travel with the file.

## Scheduling

Two independent runs, neither waiting on the other. Both are deterministic for a given session,
so whichever runs produces the same watchlist.

- **Local, authoritative.** `scripts/nightly.ps1` under Task Scheduler at 00:30 local (~17:30 New
  York, ninety minutes after the close). Registration command is in the script header. Runs from
  your own IP, which is what these unofficial endpoints tolerate.
- **GitHub Actions, backup.** `.github/workflows/nightly.yml`, 05:00 UTC Tue–Sat, publishing to
  Pages from an artifact so nothing lands in git history. Free and unmetered on a public repo.

**Known risk:** Yahoo and Nasdaq are unofficial endpoints and are known to rate-limit datacenter
IP ranges — which is exactly what GitHub runners are. The Actions job may simply not work. That is
why local is primary. If Actions proves unreliable, the fix is an authenticated source (EODHD is
about $20/month) rather than more retries.
