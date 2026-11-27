# btc-mm-wide-01 is superseded, not resumable — 2026-11-26

**4 of 20 trials completed before the campaign was stopped by hand, during trial 5.** It is left exactly as it stopped and nothing has been deleted.

## Why it stopped

Trial 4 scored +1.07 from 153 fills. It went short 600 lots (0.06 BTC) at about 17,270 USDT on 2023-01-10 and held the short to the end of the train split, when BTC was about 108,200. The scorer booked profit or loss only when a position closed, so the funding paid to the short was counted every day and the open loss, about 5,460 USDT, never was. Amendment 24 D4, kept by Amendment 25, says market-making returns are measured after inventory marks, so the scoring did not implement the rule as written.

## The trials with inventory marked

Re-scored offline from each trial's stored fills. Each recorded score was reproduced exactly before the marks were added.

| Trial | Recorded | With marks | Lots open at the end |
|---|---|---|---|
| 1 | -0.1194 | -0.1170 | -200 |
| 2 | -0.0358 | -0.0403 | 241 |
| 3 | -0.0215 | -0.0250 | 28 |
| 4 | +1.0714 | -0.1012 | -600 |

## What replaced it

Market-making returns now carry each session's change in the open position's value at that session's last trade price, in the native runner and in the Python holdout run. The market-making gate is refused until Amendment 26 is in force, because Amendment 25's control was scored without the marks.

## How N treats it

All five rows are superseded in `runs/supersessions.jsonl` as a platform failure, so they are excluded from N. The rows stay in the ledger.
