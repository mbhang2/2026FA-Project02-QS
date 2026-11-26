# btc-mm-orderflow-01 is superseded, not resumable — 2026-11-25

**1 of 20 trials ran, and it crashed because of a harness defect.** The campaign is left exactly as it stopped and nothing has been deleted.

## Why it stopped

`docs/agent/kernel-api.md` described the `orders` table as 64 rows of 6 floats but never showed how to write a field. The agent wrote `orders[row * 6 + column]`, which addresses whole rows in a two-dimensional table, and read the 64-row limit as a budget per day. The engine refused the rows it produced and stopped the run, as designed. The campaign was stopped by hand after this first trial.

## What replaced it

The document now shows `orders[row, column]`, states the limit is per call, and includes a worked example that a test checks. A stopped run's row now names the rule it broke. The same approved question, budget and settings run as `btc-mm-orderflow-02`.

## How N treats it

Its one row is superseded in `runs/supersessions.jsonl` as a platform failure, so it is excluded from N. The row stays in the ledger.
