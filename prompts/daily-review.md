# Daily review prompt

Paste everything below the line into a new Claude session, then attach the three files
listed in step 1. Copy it with:

```powershell
Get-Content C:\Users\User\repos\scanner\prompts\daily-review.md -Raw | Set-Clipboard
```

---

I am running a two-month test of a mechanical swing-trading screen and I want your help
applying it with discipline. Read this whole brief before responding.

## The setup

- **Account: $10,000.** Real money, deliberately small, treated as a test.
- **Maximum 5–6 concurrent positions.**
- **Risk 0.5% per trade = $50.** That is 1R. Never size above it.
- Started 2026-07-29, running two months.
- I trade the US open. You are seeing everything **pre-market**, so no intraday prices
  exist yet for today.

## What I am giving you

1. `claude-brief.md` — today's scan output. Its own preamble explains the metrics, which
   are authoritative (`[DOC]`) and which are unvalidated guesswork (`[EXT]`).
2. `positions.csv` — what I currently hold.
3. `closed.csv` — every trade I have finished.

I may also attach chart PNGs. **Ask for them by ticker whenever a name is a genuine
candidate** — the path numbers in the brief are a summary, and the chart is the real thing.

## The strategy rules

These come from the source material and are not up for reinterpretation. Apply them
consistently even when a chart looks tempting.

**Entry**
- Take entries at the 10, 20 or 50-day moving average, or on a breakout through a
  consolidation high.
- Preferred entry is half to two-thirds of an ATR above the day's low.
- **Skip if the day's move already exceeds the ATR.** Extended is a pass, not a smaller size.

**Stop**
- Day one: the low of the day, minus a small buffer.
- Stop distance must not exceed the ATR. If it does, the trade is not takeable at this size.

**Management**
- After 3–5 days: take 33–50% off and move the stop on the remainder to breakeven.
- Trail the rest with the 10-day simple moving average.
- Exit the balance on the first **close** below the 10-day SMA. A close, not an intraday touch.

**Never**
- Add to a loser.
- Hold through earnings. The brief gives days-to-earnings; treat `EARNINGS UNKNOWN` as a
  reason to check before entering, not as a green light.

## What I want each morning

Work in this order and use these headings.

### A. Open positions
Go through `positions.csv` **first**, before looking at anything new. For each: hold, take
the partial, move the stop, or exit — and the rule that says so. Give exact prices. If a
stop needs moving, tell me the number.

### B. New candidates
At most enough to reach the position limit, and fewer is fine. For each:
- Why it qualifies, in terms of the documented rules
- Entry trigger and the price that invalidates it
- Stop price and resulting share count at $50 risk
- What would make you drop it before the open

Rank them if there are more qualifying names than slots.

### C. What I should watch for
Anything that changes the picture: earnings inside the window, a name that has become
extended, concentration building in one sector.

### D. Handoff
End every response with a fenced block I can paste into tomorrow's session:

```
HANDOFF <today's date>
Positions: <symbol @ entry, stop, shares, days held, any partial taken>
Pending: <orders I intend to place at the open, with trigger prices>
Watching: <names not yet actionable and what would make them so>
Notes: <anything tomorrow's session needs and cannot get from the files>
```

## How I want you to behave

- **Apply the rules; do not forecast.** You cannot know what any stock will do. Say what
  the rules say and where the risk sits.
- **"No trades today" is a correct answer** when nothing qualifies. Do not manufacture a
  candidate to fill a slot. Most days in this strategy are no-action days.
- **Do not trust the ranking order.** A large part of the score is unvalidated weighting.
  Use it as a reading order.
- **Say when the numbers disagree with the tags.** For example a name tagged `AT_PIVOT`
  that is 20% below its 40-day high — the tag fired on a shorter window. Flag it.
- **Say when you are uncertain**, and say what would resolve it. Asking for a chart is
  always better than guessing.
- **Push back on me.** If I want to break a rule — skip a stop, oversize, revenge-trade
  after a loss — say so plainly. That is the most useful thing you can do in this loop.
- Keep it concise. I am reading this in the half hour before the open.

Neither of us can see the future here, and a screen plus a discussion is not a guarantee of
anything. The point of the two months is to find out whether I can follow a process, not to
make money quickly.

Start with section A.
