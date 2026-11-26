# btc-mm-orderflow-02 is superseded, not resumable — 2026-11-25

**1 of 20 trials ran, and it stopped on a harness limit.** The campaign is left exactly as it stopped and nothing has been deleted.

## Why it stopped

The native runner refused any run over 5,000,000 fills, because its scoring step built one Python object per fill. The first kernel was an ordinary market maker quoting 10-lot clips, filled piece by piece: 56,681,717 fills over 960 sessions, a median of 43,792 a day. The campaign was stopped by hand after this trial.

## What replaced it

`src/core/engine/column_score.py` scores large runs from their fill columns and equals the blotter path exactly in tests. The limit is now 80,000,000 fills. This kernel then ran end to end over all train days in under 10 minutes at 8.4 GB peak memory, with its audit matched. The same question, budget and settings run as `btc-mm-orderflow-03`.

## How N treats it

Its one row is superseded in `runs/supersessions.jsonl` as a platform failure, so it is excluded from N. The row stays in the ledger.
