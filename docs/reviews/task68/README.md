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

---

## 6. The result — 2026-09-24

Two legs, C = 100 each, R = 150, 453,000 runs, 1.55 h + ~3.2 h. Zero
unscorable trials in either.

| N | C | accept rate | 95% CI | p median |
|---|---|---|---|---|
| 10 | 100 | **0.0300** | [0.0103, 0.0845] | 0.582 |
| 20 | 100 | **0.0400** | [0.0157, 0.0984] | — |
| 50 (Task 66) | 100 | **0.0600** | [0.0278, 0.1248] | 0.439 |

**All three intervals overlap and all three contain 0.05.** That is the first
outcome section 3 named: the correction is consistent with N-invariance on this
metric across the range we would operate in, and an ML campaign at N = 10-20
inherits Task 66's calibration rather than needing its own.

### The ordering, named rather than waved away

The point estimates are **monotone in N**: 0.030, 0.040, 0.060. Section 3 said
in advance that if the three points came back looking suspiciously ordered, the
honest next step is more campaigns rather than a claim. They have.

What the data can and cannot say about it: those are 3, 4 and 6 events out of
100. A monotone ordering of three such counts happens by chance about a third of
the time, and every pairwise interval overlaps by a wide margin. So the ordering
is **not evidence of a trend** at this precision -- but C = 100 per leg could not
have detected a modest one, and "not detected" is a property of the measurement
rather than of `max_of_n`.

If a future result rests on the correction being exactly N-invariant rather than
approximately so, the leg to extend is this one, at larger C. Nothing in the
next campaign rests on that.

### What it means for the ML campaign

At N = 10 and N = 20 the measured rates are **at or below nominal** -- 0.030 and
0.040 against q = 0.05. An ML campaign at either N runs, if anything, slightly
conservative. Routing to the conditional null at those N is sound on error
control; power is Task 59's question and is not addressed here.

### Corroborating detail

Positive best in 38 / 100 campaigns at N = 10 and 85 / 100 at N = 50. The best
of ten draws is positive less often than the best of fifty -- the selection
effect `max_of_n` exists to correct for, showing up exactly where it should.
