# The harness

What turns one strategy file into one scored, recorded trial. Everything
above it — proposer, campaign, session — calls it the same way a human would:
write `src/strategies/current.py`, run `run.py`, read the ledger row.

[Documentation map](../README.md) · deep design: [design/architecture.md](../design/architecture.md) §2-5c and §7

## Files

| File | Role |
|---|---|
| `run.py` | The one entry point. Takes a profile name, never a strategy or a dataset |
| `config/run.yaml` | Profiles: dataset, split, fill model, seed, caps, wall-clock budget, commission |
| `src/core/data/` | Datasets (`nq.py`, `synthetic.py`, `options_spx.py`), the split, the holdout lock |
| `src/core/engine/engine.py`, `fills.py`, `bars.py`, `context.py` | The walk: ticks, resting orders, fills, bar closes, strategy callbacks |
| `src/core/engine/remote/` | The strategy runs in its own child process (`child.py`), behind a whitelisted protocol |
| `src/core/engine/reconstruct.py` | Rebuilds the blotter from the fill log alone |
| `src/core/scoring/` | Turns the blotter into a score (see [scoring.md](scoring.md)) |
| `src/core/runlog.py` | Appends the ledger row; derives N |
| `src/core/scoring/integrity.py` | The fingerprints every row carries |
| `src/core/loop/campaign.py` (`run_trial`) | How a campaign invokes all of the above |
| `scripts/worker_tree.py` | Isolated copies of the repo for parallel trials (Amendment 12) |

## One trial, in order

1. `run_trial` writes the proposal to `src/strategies/current.py` and runs
   `python run.py --profile <campaign profile>`. With generation width above 1,
   each slot does this in its own worker tree under `.scratch/workers/`.
2. `run.py` reads the profile from `config/run.yaml`, builds the dataset with the
   profile's seed, and starts the strategy in a **child process**.
3. The engine walks the split. The strategy sees only what its callbacks are
   given; its orders come back as whitelisted actions. Every fill is appended to
   a fill log.
4. `reconstruct(fills)` rebuilds the blotter from the log; the scorer turns it,
   with the split's session calendar, into a score. A disagreement with the
   engine's running total invalidates the trial.
5. `run.py` appends **exactly one row** to the ledger, crashed runs included, and
   saves the source and the fill log under the campaign directory.

## What a row is, and what counts

Every row carries `outcome`, `failure_kind`, `score`, `wall_clock_s`, `purpose`,
and fingerprints of the code, the artifacts (`config/*.yaml`, prompts,
hypotheses, calibration tables, `docs/agent/*.md`), the scoring and the
environment. **N is the number of rows with `purpose == "trial"`** — a filter over
the ledger, never a shorter ledger. Rehearsals and checks write `purpose: check`.

| `failure_kind` | Counts toward N |
|---|---|
| `strategy_raised`, `strategy_timed_out`, `strategy_exceeded_resources`, `strategy_violated_protocol` | yes |
| `harness_failed` | **no**: "we broke" must never inflate N |

## Limits

- **Wall clock**: `wall_clock_budget_s` per profile (`nq-train`: 7,200 s;
  `rehearsal`: 120 s).
- **CPU**: 1,800 s of CPU time (`RLIMIT_CPU`, `engine/remote/adapter.py`).
- **Memory**: bound on Linux only. On macOS isolation is best-effort, and every
  row records its isolation level so a best-effort row can be discounted.
- **Actions**: `action_cap` per session (64 on NQ).

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| A trial runs 1-2 hours, then `strategy_exceeded_resources` | The CPU limit is 1,800 s but the wall clock allows 7,200 s; on a loaded machine CPU time accrues slowly | A wall-clock budget nearer the CPU limit; strategies that subscribe to every tick are the usual cause |
| `harness_failed` | A bug on our side, not the strategy's | The row's `child_traceback`; it does not count toward N |
| The test suite refuses: "a campaign is running" | Tests and campaigns share `src/strategies/current.py` | Wait, or set `SUITE_DURING_CAMPAIGN_REASON` |
| Scores differ between machines | They should not: bit-identical across platforms since Task 28 | Compare the row fingerprints first |
| A config or prompt edit "invalidated" a running campaign | Those files are in the artifact fingerprint | Do not edit fingerprinted files mid-campaign |
