"""Pass 2: the intraday data packet, assembled ~30 minutes after the open.

The nightly scan (`qms scan`) is built from the previous close, so it can compute an
entry band but not an entry: it has no session low and no current price. Pass 2 supplies
exactly those two measurements and the arithmetic that follows from them.

It decides nothing. No ranking, no scoring, no recommendation, no forecast, no broker
contact. It fetches, it computes, it labels which is which, and it says `UNAVAILABLE`
whenever it cannot do either — because the failure mode that costs money here is not a
missing number, it is a wrong number that looks right.
"""
