# Task 34 — the correction's bias, and how large a campaign should be

Two results, and the second is not the one the brief expected.

1. **The resampling bias is characterized and fixed by R ≥ 150.** The smoothed
   remedy was implemented, measured, and rejected: it replaces a +0.015 liberal
   error with a −0.022 conservative one.
2. **Detection is monotone in N over the range this design can see.** The
   interior optimum the brief predicted is a property of Benjamini–Hochberg, and
   Task 31 removed it by switching the gate to max-of-N. Where a turnover does
   appear — at N ≥ 800 — it is an artifact of the measurement's finite library,
   and the FWER-at-each-N check the brief required is what caught it.

---

## 1. `bias(R)`, and the remedy

Known standard-normal nulls, so the target is exactly q = 0.05 and any excess is
the bias. **4,000 campaigns per cell**; binomial se 0.0034, 95% half-width
0.0068 — resolves a bias of 0.01 comfortably and can see 0.005.

Cells are FWER (bias against q):

| R | N = 10 | N = 25 | N = 50 | N = 200 |
|---:|---|---|---|---|
| 20 | 0.0742 (+0.024) | 0.0658 (+0.016) | 0.0745 (+0.024) | 0.0715 (+0.021) |
| 30 | 0.0612 (+0.011) | 0.0650 (+0.015) | 0.0710 (+0.021) | 0.0653 (+0.015) |
| 50 | 0.0638 (+0.014) | 0.0568 (+0.007) | 0.0640 (+0.014) | 0.0585 (+0.009) |
| 75 | 0.0545 (+0.004) | 0.0537 (+0.004) | 0.0505 (+0.001) | 0.0490 (−0.001) |
| 99 | 0.0585 (+0.009) | 0.0565 (+0.006) | 0.0542 (+0.004) | 0.0530 (+0.003) |
| 150 | 0.0517 (+0.002) | 0.0532 (+0.003) | 0.0488 (−0.001) | 0.0548 (+0.005) |
| 250 | 0.0500 (+0.000) | 0.0563 (+0.006) | 0.0530 (+0.003) | 0.0532 (+0.003) |
| 500 | 0.0478 (−0.002) | 0.0428 (−0.007) | 0.0517 (+0.002) | 0.0470 (−0.003) |
| 999 | 0.0545 (+0.004) | 0.0508 (+0.001) | 0.0590 (+0.009) | 0.0470 (−0.003) |
| 2000 | 0.0485 (−0.002) | 0.0498 (−0.000) | 0.0522 (+0.002) | 0.0490 (−0.001) |

The bias is **+0.021 at R = 20, +0.011 at R = 50, and inside measurement noise
from R = 150**. It barely depends on N, which is mildly surprising and has a
reason: with more trials the maximum is taken over more independently truncated
samples, and the truncations partly cancel.

### The three remedies, priced

| remedy | cost | measured result |
|---|---|---|
| **raise R to 150** | 3× the R = 50 fallback: for a 200-candidate campaign at ~25% fallback, 7,500 runs ≈ **33 min** on eight workers | bias inside noise, **no assumption about tail shape** |
| subtract the measured bias | free | assumes the bias measured on normal nulls transfers to real ones — an assumption about tail shape, made where the data is thinnest |
| smoothed tail (`smooth=True`) | free | **rejected: measured to overcorrect.** At N = 25 it turns +0.015 at R = 50 into **−0.022**, and −0.028 at R = 99 where plain resampling is already unbiased |

**Adopted: R ≥ 150.** It is the only one of the three that costs money instead of
costing an assumption, and 33 minutes per campaign is affordable. `smooth` is
kept in the code, defaulted off, with the measurement in its docstring so the
idea is not re-invented as an improvement.

`resolution_cap(r)` now returns the BH cap, the p-value floor, **the resampling
bias**, and whether R meets the recommended floor — so a reader of a campaign
summary sees the error term rather than having to know it exists.

### Restating the hybrid FWER: by correction, not by construction

Task 33 measured **0.058, CI [0.039, 0.085]** with the fallback at R = 50. The
grid puts the bias at R = 50 at about **+0.011** (averaged over N; the individual
cells run +0.007 to +0.014 against a half-width of 0.0068). Backing it out gives
a bias-corrected point estimate near **0.047**.

**That is by correction.** Claiming it by construction needs a re-run at R = 150,
which is 883 fallback candidates × 150 = 132,450 isolated runs, about **10 hours**
— not done. The honest statement is: *the hybrid's measured FWER is 0.058 at
R = 50, of which about 0.011 is a known, measured artifact of the null sample
size; production should run R ≥ 150, where that artifact is not present.*

---

## 2. Detection versus campaign size

**Built from measured runs, not a fitted distribution.** Task 33 left a library of
real engine output on one realization: 4,000 edgeless candidates with their
routes and nulls, and 600 per planted strength. A campaign of size N draws π·N
from the edge library and the rest from the edgeless one, and goes through the
same `max_of_n` the gate uses.

**B = 400 replicates per cell**; se at 0.5 is 0.025, 95% half-width 0.049 —
resolves a 10-point difference in detection, not 3 points. Enough to locate an
optimum and say whether it is flat; not enough to pin N* to one grid value.

**Cells cut:** `s = 0.24` was not run. It would need 600 fresh candidates plus
fallbacks, about 40 minutes of engine time, to place a point between two the
library already has — and given the surface is monotone, interpolation says
everything a third row would. The N grid was **extended** past the brief's 400 to
800 and 1600, because the first pass put the optimum on the grid edge and an
optimum at the edge of a grid is a grid that stopped too early.

`P(campaign accepts a true-edge candidate)`:

**s = 0.15**

| π | 10 | 25 | 50 | 100 | 200 | 400 | 800 | 1600 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.01 | 0.028 | 0.022 | 0.005 | 0.005 | 0.028 | 0.045 | 0.033 | 0.020 |
| 0.05 | 0.020 | 0.020 | 0.015 | 0.062 | 0.085 | 0.165 | 0.198 | 0.138 |
| 0.20 | 0.083 | 0.105 | 0.120 | 0.240 | 0.340 | 0.532 | 0.535 | 0.340 |

**s = 0.30**

| π | 10 | 25 | 50 | 100 | 200 | 400 | 800 | 1600 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.01 | 0.265 | 0.237 | 0.215 | 0.210 | 0.403 | 0.552 | 0.777 | 0.920 |
| 0.05 | 0.290 | 0.237 | 0.388 | 0.725 | 0.900 | 0.988 | 1.000 | 1.000 |
| 0.20 | 0.458 | 0.748 | 0.938 | 0.985 | 1.000 | 1.000 | 1.000 | 1.000 |

### FWER at each N — and what it caught

| N | 10 | 25 | 50 | 100 | 200 | 400 | 800 | 1600 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| FWER | 0.072 | 0.062 | 0.062 | 0.050 | 0.080 | 0.040 | **0.007** | **0.000** |

**Control holds up to N = 400 and then collapses**, and the collapse is the
diagnostic. The brief required FWER at each N, and that requirement is what
turned an apparent finding into a detected artifact.

The cause is library exhaustion. The observed best is bounded by the library's own
maximum (+0.358 over 4,000 candidates); the simulated maximum is not. As N grows,
the campaign draws a larger share of the library — 10% at N = 400, 20% at 800,
**40% at 1600** — so the observed side saturates while the simulated side keeps
growing with N. The test becomes conservative, FWER falls to zero, and detection
falls with it.

**So the apparent interior optimum at N ≈ 800 is not real.** It is the
measurement's finite library, not the gate's arithmetic.

### Within the trustworthy range, detection is monotone in N

For N ≤ 400, where FWER is still at q, detection rises with N in every one of the
six (π, s) cells. **The interior optimum the brief predicted did not appear, and
there is a reason it should not:**

- Benjamini–Hochberg's penalty is `q/N`, which *shrinks linearly* in N. Against a
  linear gain in the number of edge candidates, that produces an interior optimum.
- `max_of_n`'s penalty is the null's maximum over N draws, which grows like
  **√(2 log N)** — sublinear. Against π·N linear gain, more trials always help.

**The interior optimum was a property of the correction Task 31 retired.** Nobody
noticed it went away with it.

### The recommended campaign size

**As large as the budget allows, up to at least N = 400**, which is where this
design's evidence stops rather than where the gate's behaviour changes.

That rests on an assumption about π that has to be stated: **at π = 0.01 and
s = 0.15, no campaign size in the grid gets detection above 0.045.** If only one
proposal in a hundred carries a weak edge, the gate does not find it at any N
that was tested, and running more trials is not the answer — the answer is a
proposal process with a higher π, or accepting that weak edges are out of reach.
At π = 0.05 and s = 0.30 the picture is the opposite: detection is 0.90 at
N = 200 and 0.99 at N = 400.

**No threshold was adjusted in response to any of this**, as the brief required.
The output is a recommended campaign size and a stated assumption, not a
recalibrated gate.

---

## 3. Rotation share as a campaign diagnostic

`campaign.summarise` now reports `null_routing` — the bucket/rotation split and
the share — when the routing is passed in, with the interpretation attached: a
rising rotation share means the loop is producing something atypical, because
candidates exploiting an edge have unusual profiles. 23.5% with no signal, 55.2%
at strength 0.50.

**A campaign's cost is therefore not known in advance, and the null budget grows
precisely when the loop starts working.** That is in the docstring and in
`metric.md`.

## 4. What Task 32's 0.045 actually was

Recorded in `metric.md` beside the FWER history: at strength 0.15 the bucket
path's power was **0.00** against the fallback's 0.67, and 48 of 60 campaigns
routed their best candidate to a path the profile-only gate did not have. Task
32's gate was controlling at 0.045 **largely by being blind to the candidates it
exists to find**, and the coverage gap sat exactly where a real discovery lands.

The clearest instance in the project of a validity number that looks like success
and is obtained by rejecting everything — and it was live for two tasks.

---

## What this brief did not anticipate

1. **The interior optimum does not exist for this gate.** It is a BH property,
   removed when Task 31 switched to max-of-N, and the switch's side effect on
   campaign sizing went unremarked for three tasks.
2. **The measurement's own library becomes the binding constraint before the
   gate does.** At N ≥ 800 the campaign consumes enough of a 4,000-candidate
   library that the observed maximum saturates, and the result looks exactly like
   an interior optimum. The FWER-at-each-N check is the only reason it was
   distinguishable from one.
3. **The smoothed remedy overcorrects by more than the bias it removes.** It is
   the obvious fix and it is worse than the problem, which is worth a line in the
   code rather than a rediscovery.
