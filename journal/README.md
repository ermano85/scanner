# Trade journal

Three CSVs you maintain by hand. They are the memory of the test — the scanner is stateless
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

## `orders.csv` — every order you placed, filled or not

| Column | Meaning |
|---|---|
| `date` | `YYYY-MM-DD` you placed it |
| `symbol` | Ticker |
| `side` | `buy` or `sell` |
| `order_type` | `limit`, `stop`, `stop_limit`, `market` — **what you actually placed**, not what the rules called for |
| `limit_price` | The price you actually entered in the broker |
| `stop_price` | The stop you intended, if you had set one |
| `shares` | Quantity ordered |
| `status` | `pending`, `filled`, `cancelled`, `expired`, `partial` |
| `reason` | Why it ended that way — `not_filled`, `changed_mind`, `invalidated`, blank if filled |
| `note` | Anything worth remembering |

`positions.csv` records what you own. This records what you *tried* to do, which is a
different and equally interesting question. Over two months it answers things the trade log
cannot: are your limits too tight to fill, are you chasing above the maximum entry, do you
cancel names that then run.

The PRGS order on day one is the example. It never filled, so it leaves no trace anywhere
else — but the limit was above that session's maximum entry, and that is worth knowing.

Write the row when you **place** the order, with `status: pending`, and edit it when the
order resolves. Waiting until it resolves means a resting order exists only in your memory
and in a handoff someone has to remember to carry, which is exactly the kind of state this
file is for. A `pending` row is also the only record of the derivation — the session low and
ATR that produced the trigger — and those become unrecoverable the moment the day ends.

## `closed.csv` — every finished trade

| Column | Meaning |
|---|---|
| `risk_dollars` | Copied from `positions.csv` at entry. Stored here so the R-multiple can be checked rather than trusted |
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
