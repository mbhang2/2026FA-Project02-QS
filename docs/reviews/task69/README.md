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

---

## 5. The result — 2026-09-24

Ten completed trials of `nq-ml-02`, replayed from fill logs. **The replay
reproduces the ledger's `primary` on 10/10** (first attempt matched 0/10 -- the
calendar was built from split indices instead of the dataset's native session
ids, and `session_bounds(285)` silently returned a span in year 1; the check
caught it, which is what it is for).

| trial | active | ledger | uncharged | active-only dd | idle dd% |
|---|---|---|---|---|---|
| 0 | 88 | −0.378 | −0.375 | −0.355 | 0.0% |
| 1 | 53 | +0.005 | +0.016 | +0.004 | 0.0% |
| **2** | **14** | **+1.781** | +1.955 | **+0.670** | 1.1% |
| 3 | 6 | −0.159 | −0.141 | −0.039 | 0.0% |
| 4 | 37 | +0.379 | +0.423 | +0.230 | 0.1% |
| 5 | 14 | +0.382 | +0.423 | +0.143 | 0.1% |
| 6 | 19 | −0.144 | −0.130 | −0.063 | 0.0% |
| **7** | **12** | **+0.929** | +0.995 | **+0.322** | 0.3% |
| **8** | **14** | **+1.188** | +1.291 | **+0.446** | 0.6% |
| 9 | 10 | −0.092 | −0.078 | −0.029 | 0.0% |

### The inflation is real. The mechanism I wrote down was wrong.

Section 1 predicted that 86 near-identical idle losses would **dominate** the
downside sum of squares and so stabilise the denominator -- and named a large
`dd_share_idle` as the signature. **`dd_share_idle` is 0-1.1%.** The idle
sessions contribute almost nothing to the downside sum: (−0.18)² is 0.032, and
an active losing session contributes tens of points squared. They do not
stabilise anything by being many and uniform.

Yet `sortino_active_only_dd` sits **far below** the gate's number on every
clustered trial: trial 2 is +1.78 by the gate and **+0.67** with the downside
deviation taken over its 14 active sessions -- a factor of 2.66. Trials 7 and 8
are 2.9× and 2.7×.

Both facts are true at once, and together they name the actual mechanism:

> Downside deviation is `sqrt(sum_sq / n)`. The 86 idle sessions contribute
> ~0 to `sum_sq` but **count fully in `n`**. Dividing the same sum by 100
> instead of 14 shrinks the RMS by `sqrt(100/14) = 2.67`.

That is the **1/√n padding Amendment 3 was written about**, arriving through the
denominator instead of the numerator. The idle sessions are not stabilising the
downside -- they are *diluting* it, by being observations that contain no loss.

### What the charge does and does not do

`uncharged` is above `charged` on every trial (+1.955 vs +1.781 on trial 2), so
the charge is doing its job on the **numerator**: 86 sessions at −0.18 pull the
mean down. It cannot touch the **denominator's divisor**, because that is
structural to computing Sortino over every session in the calendar. A
clustered strategy is inflated ~2.7× with the charge and would be inflated
slightly more without it.

**So Amendment 3 has no defect on the numerator and a limit on the denominator
that no coefficient can fix.** Raising the charge would pull the mean down
further; it would not change `n`.

### What this means for Amendment 6

The exposure floor is not treating a symptom; it is removing the strategies for
which this inflation is large. A candidate exposed in 70 of 100 sessions is
inflated by at most `sqrt(100/70) = 1.20`; one exposed in 14 by 2.67. The floor
bounds the distortion. That is a stronger argument for it than the one
Amendment 6 was registered with.

A denominator computed over active sessions only would remove the distortion
entirely -- and would also make a strategy that trades once and wins look
infinitely good, which is why the metric was defined over the whole calendar in
the first place. Recorded as considered; not adopted.

### What the committee should see tonight

Trial 2's +1.78 is +0.67 on its own trading days. Whichever of the two the
committee finds more honest, it should see both.
