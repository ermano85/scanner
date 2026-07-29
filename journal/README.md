# Trade journal

Two CSVs you maintain by hand. They are the memory of the test — the scanner is stateless
and has no idea you hold anything.

Keep them accurate even when a trade goes badly. A journal edited to look better than
reality tells you nothing after two months, which is the entire point of running a test.

## `positions.csv` — what you hold right now

| Column | Meaning |
|---|---|
| `symbol` | Ticker |
| `entry_date` | `YYYY-MM-DD` you actually filled |
| `entry_price` | Your real fill, not the planned entry |
| `shares` | Filled quantity |
| `initial_stop` | The stop you set on day one. **Never edit this** — it is the denominator of your R-multiple |
| `current_stop` | Today's stop. Moves to breakeven after a partial, then trails the 10-day SMA |
| `risk_dollars` | `(entry_price - initial_stop) * shares` at entry |
| `partial_taken` | `no`, or the date and size you sold, e.g. `2026-08-04 33%` |
| `thesis` | One line: why you took it. Written *before* the outcome is known |

Delete the row when the position is fully closed, and add it to `closed.csv`.

## `closed.csv` — every finished trade

| Column | Meaning |
|---|---|
| `pnl_dollars` | Realised, after any partial |
| `r_multiple` | `pnl_dollars / risk_dollars`. **The number that matters.** A +2R and a +$180 mean different things on different position sizes; only R is comparable |
| `exit_reason` | `stop`, `10ma_close_below`, `target`, `time`, `discretionary` |
| `lesson` | Optional, and worth more than the P&L |

## Why R-multiple rather than P&L

Over 5-6 trades, dollar profit is mostly noise from position size. R tells you whether the
*process* has an edge. A useful test result after two months looks like "14 trades,
average +0.3R, win rate 40%", not "made $240".

With a $10,000 account risking 0.5%, 1R is about $50.

## Honest expectations

Twenty or thirty trades cannot distinguish a real edge from luck. Two months of this tells
you whether you can *follow the process* — whether you take the entries, honour the stops,
and take the partials. That is worth knowing on its own, and it is a prerequisite for any
conclusion about whether the strategy works.
