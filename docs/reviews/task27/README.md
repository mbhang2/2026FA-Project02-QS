# Task 27, item 0 — the gate failed. Stop and report.

**The held-out FAR validity check is anti-conservative in one of its two split
directions, in all 50 cells, with a worst ratio of 3.12 and 2.15 at the operating
point (n=86, far≤0.001).** Item 0 gates the rest of Task 27, so items 1 through 4
are not started.

Reproduce with `python docs/reviews/task27/validity_table.py`; the numbers below
are 500,000 trials per cell on the committed 384-session NQ training pool at the
committed block length of 7.

---

## The table, both directions

Cells are **measured / promised**. `≤ 1.00` is valid — conservative costs power
and stays valid. Above 1.00, the reported false-acceptance rate is precise and
wrong, and the FAR column is the input to Benjamini–Hochberg.

### Forward: build on the first 192 sessions, check on the second 192

```
    n    0.0005    0.001    0.002    0.005     0.01     0.02     0.05      0.1      0.2      0.5
   30      0.83     0.83     0.80     0.74     0.73     0.73     0.75     0.73     0.69     0.65
   50      0.57     0.57     0.61     0.61     0.61     0.62     0.60     0.58     0.55     0.53
   73      0.52     0.48     0.49     0.51     0.50     0.49     0.46     0.44     0.42     0.43
   86      0.46     0.44     0.41     0.43     0.43     0.42     0.40     0.38     0.37     0.38
  100      0.40     0.42     0.40     0.39     0.37     0.35     0.34     0.33     0.32     0.34
```

Worst ratio **0.83**, anti-conservative cells **0**. This is the direction the
committed verification artifact reports, and it is clean.

### Reverse: build on the second 192 sessions, check on the first 192

```
    n    0.0005    0.001    0.002    0.005     0.01     0.02     0.05      0.1      0.2      0.5
   30      1.13     1.31     1.32     1.32     1.38     1.36     1.34     1.40     1.50     1.46
   50      1.75     1.82     1.76     1.66     1.67     1.68     1.74     1.82     1.89     1.65
   73      1.86     1.74     1.89     1.94     2.05     2.12     2.26     2.37     2.33     1.79
   86      2.15     2.15     2.28     2.37     2.44     2.51     2.66     2.73     2.59     1.84
  100      2.60     2.54     2.59     2.68     2.79     2.96     3.10     3.12     2.87     1.88
```

Worst ratio **3.12**, anti-conservative cells **50 of 50**.

**Where it ended up, plainly:** measured ≤ promised is *not* true everywhere. It
is true in the direction that was checked and committed, and false in every cell
of the direction that was not. At the split the loop actually scores against —
n=86 — the reverse direction delivers **0.00215 where it promises 0.001**.

This is worse than the second pass's 2.64, not because anything regressed but
because that number came from a coarser run; at 500,000 trials per cell the worst
is 3.12 and the whole grid is over 1.

---

## Why: the two halves are two different regimes

Not a bug in the bootstrap. The pooled training returns:

| | n | mean | sd | **downside deviation** | sessions < 0 |
|---|---:|---:|---:|---:|---:|
| whole pool | 384 | −7.44 | 275.4 | 223.2 | 190 |
| first half | 192 | +10.20 | 251.5 | **179.2** | 91 |
| second half | 192 | −25.07 | 297.0 | **259.8** | 99 |

Downside deviation differs by **1.45×** between the halves, and Sortino divides by
it. A table built on the volatile second half sets thresholds calibrated to a wide
distribution, and measuring those thresholds against the calm first half produces
too many exceedances. Building the other way round is conservative for the same
reason. The direction that was committed is the flattering one.

## It is not a block-length tuning problem

The block bootstrap is doing most of the work already, and removing it is
catastrophic:

| block | forward worst | reverse worst | reverse n=86, far=0.001 | forward n=86, far=0.001 |
|---:|---:|---:|---:|---:|
| 1 | — | **17.73** | **13.72** | — |
| 7 *(committed)* | 0.76 | 3.34 | 2.14 | 0.42 |
| 14 | 1.27 | 2.67 | 0.73 | 1.15 |
| 21 | 2.14 | 2.45 | 0.51 | 1.88 |
| 30 | 2.91 | 2.26 | 0.24 | 2.47 |

(200,000 trials per cell; block 7 differs slightly from the 500,000-trial table
above for that reason.)

A longer block widens the bootstrap distribution, which helps whichever direction
needs widening and hurts the other. The worst-over-both-directions bottoms out
around block 14 at 2.67 versus 3.34 — a marginal improvement that moves the bad
cell rather than removing it.

**And choosing 14 for that reason would be tuning a threshold to make a curve look
better,** which this project has a standing rule against. The block length is
measured from the pool's |returns| ACF and measures 7. It stays 7, and the
transfer failure is reported instead.

---

## What this means for the column

The committed table is built on **all 384** training sessions and applied to the
**86-session validation split**, which is a later period than either half. So
neither direction is the operating case; both are estimates of *how badly a table
transfers across a regime change*, and the reverse direction is the honest upper
bound on that.

If the validation window's downside dispersion is closer to the calm half than to
the pool, the FAR column at n=86 is optimistic by roughly **2.2×** at the
operating alphas. Benjamini–Hochberg at q = 0.05 over that column would then
deliver a realized FDR near 0.11, and every corrected p-value in the eventual
report would be wrong in the direction that matters.

## What I did not do

Items 1 through 4 of Task 27 — exact money arithmetic, the marginal-validity
wording in `metric.md`, the Sortino→Sharpe boundary, and positive controls in the
isolation checks — are not started. The brief gates them on item 0 and item 0
failed. Item 1 is orthogonal and would be safe to do anyway; it is left alone
because the gate said stop, and because a bit-identical score computed from an
invalid p-value column is a more confident version of the same problem.

## Options, for a human to choose between

Listed rather than taken. Each has a different cost and none is free.

1. **Report FAR as a bound, not an estimate.** Multiply the reported rate by a
   measured transfer factor — the reverse-direction ratio at that (n, far) — and
   say in `metric.md` that the column is an upper bound on false acceptance under
   regime change. Costs power, keeps validity, and is the only option that needs
   no new data.
2. **Per-candidate null at the committee.** Already the plan for finding 5's
   expensive half. It sidesteps transfer entirely by calibrating against the
   candidate's own evaluation window, and it is expensive per candidate rather
   than once.
3. **Regime-conditional null.** The Task 27 brief explicitly rules this out —
   it splits an already thin tail sample — and these numbers are the argument for
   why the tail sample is thin: n=100 at far=0.0005 rests on very few exceedances
   in a 192-session build half.
4. **Widen the training pool.** The pool is 384 sessions because that is the
   pre-registered training split. More history would give more regimes to average
   over, and re-pre-registering the calendar after seeing this table is not
   something this project can do.
5. **Accept and document.** State the marginal-validity caveat with the 3.12
   number attached, in the section of `metric.md` that currently states the
   guarantee, and let the committee weigh it. This is Task 27 item 2 with a much
   larger number than the 2.6× the brief expected.

Options 1 and 5 are compatible and are the cheapest honest pair.
