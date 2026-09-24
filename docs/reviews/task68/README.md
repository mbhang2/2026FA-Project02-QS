# Task 68 — is the correction N-invariant? Rotation at N = 10 and N = 20

**Pre-registered before anything ran.**

## 1. Why

Michael intends ML campaigns at **N = 10-20**, against the registered default of
50. Every error-rate measurement on the current metric is at N = 50:

| | null | N | result |
|---|---|---|---|
| Task 64 | sign-flip | 50 | 0.0020, CI [0.0005, 0.0073] |
| Task 66 | rotation | 50 | 0.0600, CI [0.0278, 0.1248] |
| Task 57 | sign-flip | 10 and 50 | 0.0000, 0/800 |
| Task 38 | conditional | 10 | 0.060, CI [0.041, 0.088] |

So rotation at the N an ML campaign would actually use is unmeasured under this
metric.

**And the question is worth more than filling a cell.** `max_of_n` is a
family-wise correction: its entire job is to absorb the multiplicity, so the
error rate should be **the same at every N**. That is a property the method
claims, and this project has spent two days finding out that claimed properties
need checking. Demonstrating N-invariance across 10, 20 and 50 is a stronger
statement than three unrelated numbers, and it is the statement the practicum's
contribution actually rests on.

## 2. The measurement

Two legs, each **C = 100 campaigns x R = 150 rotations**, differing only in N.

| leg | N | runs |
|---|---|---|
| A | 10 | 151,000 |
| B | 20 | 302,000 |

453,000 runs total. Task 66 did 755,000 in 7.69 h, so roughly **4.6 hours**.

Everything else is Task 66's, unchanged and deliberately so: same generator
stream seed, same dataset spec, same per-campaign dataset seeds, same rotation
seed block, same `idle_charge = 0.18`, same `max_of_n(b=10_000, seed=17,
q=0.05)`, same accept rule, same in-process substitution (verified there at
20/20 byte-identical blotters). **Only N moves.** A difference between legs is
then a property of N and not of anything else.

## 3. What gets reported — fixed now

1. `reject_rate` and `accept_rate` with Wilson intervals, per leg.
2. The three-point series **N = 10, 20, 50**, with Task 66's N = 50 as the third
   point.
3. Whether the intervals overlap.

**What each outcome would mean:**

- **All three consistent, near 0.05** — the correction is N-invariant on this
  metric, an ML campaign at N = 10-20 inherits Task 66's calibration, and no
  further measurement is needed before running one.
- **A trend in N** — the correction does not absorb multiplicity as claimed, the
  operating N becomes an evaluation condition that has to be registered and
  measured per campaign, and the practicum has a finding about `max_of_n` rather
  than about a strategy.
- **Erratic across N** — something is wrong in the harness or the method that
  neither Task 66 nor this can localise, and the next step is diagnosis rather
  than another rate.

## 4. What this does not establish

Power at small N, which is a separate and probably more consequential question:
`max_of_n` at N = 10 corrects for fewer looks, so it should be *more* powerful,
and Task 59 measured power only for the sign-flip null. Not claimed here.

## 5. Running it

    python docs/reviews/task68/fwer_n.py --stage run --n 10
    python docs/reviews/task68/fwer_n.py --stage run --n 20
    python docs/reviews/task68/fwer_n.py --stage report
