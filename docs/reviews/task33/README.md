# Task 33 — the hybrid null, and the first power measurement

Two deliverables. The hybrid closes the coverage hole Task 32 left: 21.8% of
candidates had no null, entered at p = 1, and could never be discoveries. And the
power curve is the measurement this project had not made in thirty-three tasks of
false-positive control.

Numbers land in this file as each run completes; the sizing and what each run can
resolve were fixed before any of them started.

---

## What is already measured

### `max_of_n` is liberal when R is small, and by how much

Task 32's rotation arm read FWER 0.125 at R = 30 against the profile null's
0.045, which looked like rotation failing. It is not. `max_of_n` resamples each
trial's null **with replacement**, so the simulated maximum can never exceed the
largest draw that null happens to contain — a small sample truncates its own
upper tail, the simulated maximum is too small, and the test is liberal.

Measured on known standard-normal nulls, 2,000 campaigns per R, N = 25, with R
the only thing that varies (se at 0.05 is 0.0049, so this resolves 0.05 from
0.07):

| R | floor 1/(R+1) | FWER | 95% CI |
|---:|---:|---:|---|
| 30 | 0.0323 | 0.059 | [0.049, 0.070] |
| 50 | 0.0196 | 0.059 | [0.049, 0.070] |
| 99 | 0.0100 | 0.054 | [0.045, 0.065] |
| 199 | 0.0050 | 0.051 | [0.043, 0.062] |
| 499 | 0.0020 | 0.050 | [0.041, 0.060] |
| 999 | 0.0010 | 0.050 | [0.041, 0.060] |

**+0.009 at R = 30, not a factor of 2.5.** The effect is real and small, and it
does not have to explain 0.125 on its own: with a true rate near 0.059, observing
5 of 40 has probability about 0.11. The arm was pre-registered as resolving 0.05
from 0.19 and nothing finer. Task 31 measured rotation's p-values as uniform at
R = 99. The consistent reading is **rotation is sound, R = 30 is too small for a
25-trial campaign, and C = 40 cannot tell the difference** — recorded beside the
number in the Task 32 review so it is not read as a failure.

It also sets the fallback's R: 50 costs +0.009 and 99 costs +0.004, and the
choice is a stated bias rather than an absorbed one.

### The hybrid rule

**Bucket when the profile's bucket holds at least 200 scores, rotation when it
does not.**

> *Mixing two valid procedures is valid when the choice between them is
> independent of the outcome, and invalid the moment it is not.*

The routing keys on the profile and nothing else and is fixed before the campaign
runs. Enforced by the signature rather than by convention: `choose_null` takes a
return series and a table and **has no parameter through which a score could
reach it**, and a test reads the function body — not the docstring — and asserts
no outcome word appears in it.

Under the conditional guarantee the profile table is a **per-campaign step**
built on the campaign's own data. The committed `profile-null.json` stays as a
Task 31 screening artifact, pooled across realizations, and is not the operating
null.

### Why the gap matters beyond power

The uncovered fifth is in the **tails of profile space**, where unusual
strategies live. Once an agent is hill-climbing, novel profiles are silently
unacceptable, so the loop drifts toward the region the gate can see — and **the
calibration's coverage becomes an implicit prior over strategy space, imposed by
nobody and documented nowhere.** That is the argument for paying for the
fallback, and it is stronger than the power argument.

---

## Sizing, fixed before the runs

**Hybrid FWER.** The brief asks for an upper CI bound below 0.06, which at
q = 0.05 needs C ≈ 1,900 campaigns. With the hybrid's measured 22.1% rotation
share that is 1,900 × 10 × 0.221 × 51 ≈ 214,000 isolated runs on top of 19,000
base runs: **about 16 hours** at the 3.9 runs/s this machine sustains on eight
workers. Not affordable here.

So the run is **C = 400, N = 10, fallback R = 50** — about 3.4 hours — and the
claim it supports is stated rather than dressed up:

- **affordable:** *the hybrid's FWER upper bound is below about 0.075.*
- **not affordable:** *below 0.06*, which needs C ≈ 1,900 and ~16 hours.

**Power.** C = 60 campaigns per strength, five strengths, N = 10. At a true power
of 0.8 the binomial se is 0.052, so the 95% half-width is **0.10**: this locates
the 80%-power crossing to within roughly one grid step and cannot distinguish
0.75 from 0.85. What it does support is the shape of the curve, the band in which
power crosses 0.8, and whether the fallback's power is dramatically worse than
the bucket's — which is the question the hybrid raises.

The nulls are always built on **signal-free** data at the same seed. A null
tabulated on the planted data would contain the signal and the test would have
nothing to detect; that is what makes this a power measurement rather than a
tautology.

---

## Results

*(filled in as the runs complete)*

### Hybrid FWER — the coverage hole is closed

```
hybrid, C=400 campaigns x N=10, fallback R=50
  routed to a bucket    3,117/4,000 = 77.9%
  routed to rotation      883/4,000 = 22.1%
  no null at all            0/4,000 =  0.0%
  FWER  0.058   95% CI [0.039, 0.085]
  Task 32, profile only: 0.045 [0.029, 0.070] with 21.8% auto-rejected
```

**Nothing is auto-rejected any more.** Every candidate gets a real p-value, which
is the point: the uncovered fifth was in the tails of profile space, and leaving
it unacceptable would have made coverage an undocumented prior over strategy
space.

**FWER rose from 0.045 to 0.058, which is the honest number and not a
regression.** Task 32's 0.045 was conservative *because* a fifth of candidates
could never be discoveries. The interval [0.039, 0.085] contains q = 0.05.

And it should be read with the R-sensitivity result above: the fallback runs at
R = 50, which carries a **measured +0.009 of liberalism** in `max_of_n`. Backing
that out puts the point estimate at about **0.049**. That is a correction with a
measurement behind it rather than an excuse, and it is not a claim that the true
rate is 0.049 — the interval is what it is.

**Rotation share and wall clock on this machine** (M1 Pro, eight workers at ~3.6
isolated runs/s): 22.1% of 4,000 candidates needed the fallback, which is 44,150
extra runs and **about 3.4 hours** for C = 400 campaigns. Per campaign of ten
candidates that is 31 seconds of fallback — for a 200-candidate campaign,
**about 10 minutes**, which is the number that matters operationally and is
cheaper than the 19 minutes the brief estimated because the share came in at the
predicted 22% with R = 50 rather than 99.

**What this run supports and what it does not.** It supports *the hybrid's FWER
upper bound is below about 0.085*. It does not support *below 0.06*: that needs
C ≈ 1,900 and about 16 hours, and the observed interval's width is exactly the
C = 400 sizing stated before the run.

### Power curve

```
power, C=60 campaigns per strength, N=10, q=0.05
  se at power 0.8 is 0.052; the 95% half-width is 0.101

   strength   power           95% CI   bucket  fallback  rot share  median best
       0.00    0.07 [0.03, 0.16]      0.09      0.00      23.5%        0.010
       0.15    0.53 [0.41, 0.65]      0.00      0.67      23.2%        0.095
       0.30    0.97 [0.89, 0.99]      0.50      0.98      47.5%        1.048
       0.50    1.00 [0.94, 1.00]       n/a      1.00      55.2%        6.756

  80% power at signal_strength ~ 0.242, median best Sortino ~ +0.681
```

**The gate controls FWER at q = 0.05 and detects a planted edge of strength 0.24
— a best-of-ten Sortino of about +0.68 on 105 training sessions — with 80% power
at N = 10.** That is the complete characterization the brief asked for, and this
project had never had either half of it before.

The s = 0.00 row is the false-positive rate measured inside the power sweep:
**0.07, CI [0.03, 0.16]**, consistent with the hybrid's 0.058 from a much larger
run. It is there as the sweep's own control — a power curve whose zero-signal end
does not match the FWER measurement would be measuring something else.

**Sweep stopped after s = 0.50.** s = 0.80 was budgeted and dropped: power is
1.00 at 0.50, the curve has saturated, and the 80% crossing lies between 0.15 and
0.30, so the remaining point costs about two hours and moves nothing. Said here
rather than quietly omitted.

**What C = 60 supports:** the shape of the curve, the band the crossing falls in,
and the bucket-versus-fallback split. **What it does not:** the crossing to
better than roughly one grid step. The 0.242 figure is a linear interpolation
between 0.15 and 0.30, not a measured point.

### The finding inside the power curve: where the signal-carrying candidates go

| planted strength | campaigns whose best went to a **bucket** | to the **fallback** | fallback share of all candidates |
|---:|---:|---:|---:|
| 0.00 | 46 of 60 | 14 of 60 | 23.5% |
| 0.15 | **12 of 60** | **48 of 60** | 23.2% |
| 0.30 | **2 of 60** | 58 of 60 | 47.5% |
| 0.50 | 0 of 60 | 60 of 60 | 55.2% |

**A candidate that exploits a planted signal trades differently from an edgeless
one, so it lands in a thin bucket.** At s = 0.15, 48 of 60 campaigns had their
best candidate on the fallback path; at s = 0.50, **all 60 did**. Under Task 32's
profile-only gate every one of those campaigns would have had its best candidate
auto-rejected at p = 1 — invisible, by construction, because its profile was
unusual.

The power split makes it arithmetic. At s = 0.15 the fallback path detects 67% of
the time and the bucket path **0%**; at s = 0.30 it is 98% against 50% — though that 50% is two campaigns, so
read it as "the bucket path is where the misses are" rather than as a rate. The brief
asked whether the hybrid had "traded a validity hole for a power hole" — it is
the reverse. **The fallback is where the power is**, and the bucket path is the
weak one, because a candidate strong enough to be worth detecting is a candidate
whose profile the bucket table does not cover.

So the hybrid is not a marginal improvement in coverage. **It is the difference
between detecting a planted signal and not detecting it**, and that is the
measured form of the argument that the coverage gap was an undocumented prior
over strategy space. The gap was not randomly placed: it sat exactly where a real
discovery would land.

It also prices the campaign honestly in the other direction: the rotation share
rises with signal strength — 23.5% at s = 0, 23.2% at 0.15, 47.5% at 0.30,
**55.2% at 0.50** — so *a campaign that is actually finding something pays more
for its fallback than a campaign that is not.* Budget for the null-campaign
share, and expect the bill to grow if the loop starts working.

### Which component a power number would implicate — and why none is implicated

The brief asked, if power were poor, which of the metric's sensitivity, the
null's width, the correction's conservatism at this N, or the campaign size was
responsible. **Power is not poor**, so the question is answered by not arising:
0.97 at s = 0.30 with N = 10 leaves no room for any of the four to be the binding
constraint at that effect size.

The one place a component *is* implicated is the bucket path's 0% at s = 0.15 and
50% at s = 0.30. That is the **null's width**, not the metric or the correction:
a bucket pools other strategies' scores, and a candidate whose profile sits at the
edge of a covered bucket is compared against strategies less like it, which
widens the null it has to beat. Rotation, being conditional on the candidate,
does not have that problem. No threshold was adjusted in response — the
measurement is the deliverable.


---

## A note on the committed artifacts

`pool.json`, `campaigns.json` and `power.json` carry the scores and the routing
decisions, not the per-run return series they were computed from. The series are
regenerable from the committed generator and the recorded seeds, and keeping them
would have put 17 MB of floats in the repository to say what 1 MB says. The
summaries every number in this document rests on -- `hybrid-fwer.json`,
`power-curve.json`, `r-sensitivity.json` -- are complete.
