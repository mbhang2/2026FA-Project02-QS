# Task 69 — does a uniform idle charge inflate Sortino for a clustered strategy?

**Pre-registered before anything ran.** Written 2026-09-24 while `nq-ml-02` runs;
this reads its fill logs and touches nothing the campaign imports.

## 1. The hypothesis under test

Amendment 6 records a mechanism as a hypothesis: the idle charge puts ≈ -0.18 on
every idle session, and a run exposed in 14 of 100 sessions therefore carries 86
near-identical small losses. Those have almost no variance among themselves, so
they **stabilise Sortino's denominator** while the 14 active sessions carry the
mean. If that is right, the charge -- built to penalise non-participation --
partly *rewards* a strategy that clusters its trading, through the denominator
rather than the numerator.

The signature is already in the rows: Sortino 11x Sharpe, 92 of 100 sessions
below MAR. That is consistent with the mechanism and does not establish it.

## 2. The measurement

For each completed `nq-ml-02` trial, from its fill log and the scored-window
calendar, reconstruct the per-session return series and compute:

| quantity | construction |
|---|---|
| `sortino_charged` | the gate's number: `idle_charge = 0.18`, all 100 sessions |
| `sortino_uncharged` | `idle_charge = 0`, all 100 sessions |
| `sortino_active_only` | `idle_charge = 0.18`, but the downside deviation is computed over **active sessions only** -- the mean still over all 100 |
| `dd_share_idle` | the fraction of the downside sum-of-squares contributed by idle sessions under the charge |

`sortino_charged` must reproduce the ledger's `primary` for every trial, or the
replay is not the run and nothing below counts. That is the check, and it is
run first.

**What each outcome means, fixed now:**

- **`sortino_active_only` well below `sortino_charged`, and `dd_share_idle`
  large** -- the mechanism is real: idle sessions are propping the denominator
  up (by being many and uniform) and the charged Sortino is inflated for
  clustered strategies. Amendment 3's construction has a defect independent of
  the exposure floor, and Amendment 6's floor is treating a symptom.
- **`sortino_active_only` close to `sortino_charged`** -- the denominator is not
  the story; the clustered trials score well because their active sessions
  genuinely score well, and the floor in Amendment 6 is the right and sufficient
  response.
- **`sortino_uncharged` far above `sortino_charged`** on the clustered trials --
  expected in either case (86 sessions of -0.18 pull the mean down) and reported
  for Amendment 5's purposes, not as evidence for this question.

## 3. What this does not do

It does not change `nq-ml-02`. It does not compute anything the campaign's gate
will use. It reads artefacts already on disk, exactly as `ledger_check` does, and
it is run once the trials it reads are complete.

## 4. Running it

    python docs/reviews/task69/decompose.py

Results to `docs/reviews/task69/decompose.json`.
