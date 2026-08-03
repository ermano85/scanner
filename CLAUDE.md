# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` explains what the scanner is and why. This file covers what breaks if you do not
know it. Read `README.md` before changing rule logic or data sources.

## Commands

```bash
uv sync
```

```bash
uv run pytest -q
```

```bash
uv run pytest tests/test_sizing.py::test_risk_cap_matches_hand_arithmetic -q
```

345 tests, about 100 seconds. There is no `conftest.py`; the only network guard is an autouse
`no_network` fixture scoped to `tests/test_nightly.py`, added because `run_nightly` calls the
gap-fill and the suite quietly started hitting Nasdaq for every fixture session (62s → 181s).
Nothing stops a *new* test module reaching the network — keep new tests offline by extending
the captured fixtures in `tests/test_ingest.py`, and be suspicious of a sudden slowdown.

Pipeline, in order. Each step is idempotent and resumable:

```bash
uv run qms config-check
```

```bash
uv run qms nightly
```

```bash
uv run pass2 CBRL AMN --positions journal/positions.csv --verbose
```

`qms nightly` is ingest → gap-fill → features → quality gate → scan → report. It **fails
closed** on stale data; `--allow-stale` overrides and stamps the report. Useful subcommands:
`scan --as-of YYYY-MM-DD`, `report`, `brief`, `packet [--clip]`, `export-tradingview`,
`ingest --gapfill-only`, `quality`.

A full backfill is ~15 minutes over ~12,000 symbols. A nightly run is ~2 minutes. The feature
store rebuilds in ~13 seconds, so `qms features --rebuild` is cheap; re-ingesting is not.

## Invariants that fail the build

These are enforced by tests, not by review. Violating one is an error, not a style note.

- **No numeric literals in `rules/` or `sizing/`.** `tests/test_no_literals.py` walks the AST;
  the allowlist is `{0, 1, -1, 2, 100}`. Every threshold comes from YAML. This has caught
  real mistakes — a console `tbl_width_chars=200` in the rules layer had to move to
  `report/console.py`.
- **No defaults in the config model.** `src/qms/config.py` uses strict pydantic models: a
  missing key is an error, an unknown key is an error. A default in code is a literal in code.
- **Causality.** No feature may read a bar at index `> i`. `tests/test_causality.py` enumerates
  the feature *registry*, so a feature added tomorrow is covered tonight, and checks each two
  ways: tail-truncation equivalence and invariance under corruption of all future bars.
- **`[EXT]` may rank, never filter.** Enforced structurally — the `[EXT]` consolidation
  metrics live in a namespace `rules/gates.py` cannot import, so a violation is an ImportError.
  `tests/test_ext_quarantine.py` also AST-scans the gate module.

## Architecture

Four layers, strictly separated:

```
ingest   →  data/bars, data/earnings, data/universe, data/reference
features →  data/features/features.parquet
rules    →  pure functions over the feature store
output   →  out/<date>/ + journal/ + prompts/
```

**`as_of_date` is the session the watchlist is FOR**, not the date the data is from. The rule
engine filters the feature store to bars strictly *before* it, so a Monday-evening run passes
Tuesday and legitimately sees Monday's close. Every date-taking function threads it; nothing
calls `date.today()` below the CLI.

**Never key on `max(date)`.** The vendor's trailing edge is ragged — on 2026-07-24, 42 of
11,574 symbols carried a bar missing for everyone else. Use
`quality.effective_latest_session()`, which returns the newest *well-covered* session. Getting
this wrong calls stale data fresh *and* builds a cross-section mixing two sessions, handing
those 42 names a free day of return in every percentile.

**Providers are a protocol.** `ingest/base.py` defines it; Yahoo is primary for bulk history
and Nasdaq repairs the trailing edge via `ingest/gapfill.py`. Adding a paid vendor is one file
and a config line. Gap-fill is a separate step so its failure cannot corrupt the primary
ingest.

Two counterintuitive facts documented in `README.md` and worth not rediscovering: the
`liquidity` sizing cap can never bind at the doc's own numbers, and a single-day date range
against the Nasdaq historical endpoint returns zero rows (hence `_MIN_SPAN_DAYS = 7`).

## The live paper test

Since 2026-07-29 this repo also runs a two-month paper test, and several files are *live state*
rather than code. Editing them carelessly corrupts the only record of the experiment.

- `config/scan.yaml` → `sizing.account` is the **current equity**, updated after every closed
  trade, with the running total in the comment above it. Both passes read it, so a stale value
  makes every share count in every report quietly wrong. It is not the opening balance.
- `journal/positions.csv`, `orders.csv`, `closed.csv` — hand-maintained. `initial_stop` is
  **never edited**; it is the denominator of every R-multiple. Orders get a `pending` row when
  placed, not when they resolve. `journal/README.md` has the schemas and the reasoning.
- `prompts/daily-review.md` — the standing prompt pasted into a fresh chat each morning.
  Strategy rules live here, in prose, deliberately: the scanner ranks and sizes but decides
  nothing.
- `qms packet` concatenates that prompt, the journal and the day's brief into one file.

Findings from the test belong in the journal or the prompt, not in new gates. Three trades is
not evidence for a new threshold, and every rule in `scan.yaml` currently traces to the source
document or to a written-down operator preference.

## Conventions

- **Evidence over assertion.** Comments and commit messages here record what was measured and
  what was wrong before — the 45-minute gap-fill that fetched nothing, the two incorrect
  diagnoses of the stale session. Corrections are stated, not quietly overwritten. Match that
  register rather than writing a changelog.
- **Provenance travels with the data.** Exports carry their own caveats, because a caveat left
  in the README does not reach a conversation the file is pasted into. `pass2` goes further:
  every field is a `Value` that cannot be constructed without declaring fetched / computed /
  unavailable, so there is no code path that emits a bare number.
- **Windows.** PowerShell is primary; the Bash tool is Git Bash. Multi-line commit messages go
  through `git commit -F -` with a bash heredoc — PowerShell here-strings break on embedded
  double quotes. `python` is not on PATH; use `uv run python`.
- The repo is **public**, and the journal in it contains real position sizes.

## What this is not

Not a trading system. It emits no buy signals, places no orders, and judges no chart. It
reduces ~13,000 tickers to 20–60 worth looking at, and produces the sizing arithmetic. Nothing
it outputs is financial advice, and the survivorship-biased free feeds make a backtest on this
data actively misleading — see `README.md`.
