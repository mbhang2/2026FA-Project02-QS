# Task 32 — the guarantee is conditional, and under it the cheap null controls

**Decision: the gate's guarantee is conditional on the observed training data.**
Under that framing, measured over 400 independent campaigns on one realization,
the profile-indexed null gives **FWER 0.045, 95% CI [0.029, 0.070]** — consistent
with q = 0.05 and excluding 0.09. Task 30's number was 0.575 and Task 31's
marginal number was 0.115.

**So the cheap null is the operating null**, and production costs a dictionary
lookup rather than 5.5 hours of rotations. That is a measurement, not a
preference: the same framing is what made Task 31's 2.3× residual disappear,
because the 31.5% of score variance that sits between data realizations does not
arise when there is only one.

---

## 0. Two numbers that look like success and are not

`BH 0.005` and `Holm 0.000` in Task 31's acceptance table came from a p-value
column that was **31.6% ones**: a candidate with no usable profile bucket enters
at p = 1. The correction was not controlling, it was being fed nulls by default.

And **the one candidate in twelve whose rotation p-value fell below 0.05 was one
of the excluded ones.** The pipeline is blind to unusual profiles — precisely the
candidates most likely to be genuine.

Both facts are now recorded beside those numbers in `docs/reviews/task31/README.md`
and in `docs/design/metric.md`. They are quotable and wrong.

Under the conditional buckets the exclusion rate is **21.8%**, better and still
the largest remaining hole in the design.

## 1. The decision

> **The gate's false-discovery guarantee is conditional on the observed training
> data.** Given these sessions, the probability that a campaign produces any
> false discovery is at most q. The randomness the guarantee is over is strategy
> generation and the randomization, not over which market happened. The guarantee
> does not extend to a period unlike the training data.

That sentence is in `docs/design/metric.md`. Why conditional:

- It is what a permutation test gives, it is standard, and it is honest about
  what a null built by randomizing one dataset can support.
- It dissolves the failure Task 31 measured. 31.5% of a candidate's score
  variance sits between realizations, and a null pooled across realizations
  cannot reproduce a component that a campaign's candidates all share. Production
  has one realization; a null built on it conditions on that by construction.
- The caveat it carries — *not a period unlike the training data* — is the same
  one `metric.md` already carries from the transfer work, not a new liability.

**It is a decision, not a measurement**, taken once and recorded so it is not
revisited because a later result would prefer the marginal claim.

## 2. The conditional calibration

**Sizing, pre-registered before the runs.** Task 31 needed 200 campaigns to
separate 0.05 from 0.10 and 40 could not, so the arms were sized deliberately and
differently, because they cost three orders of magnitude apart.

| arm | campaigns | N | runs | binomial se at 0.05 | resolves |
|---|---:|---:|---:|---:|---|
| **profile (conditional)** | 400 | 25 | 10,000 | 0.0109 | 0.05 from **0.09** |
| rotation, R = 30 | 40 | 25 | 31,000 | 0.0345 | 0.05 from 0.19 only |

Campaigns use **distinct** candidates rather than resampling a shared pool, so
they are independent and the binomial interval is the right one. The
profile-null buckets come from a **disjoint** calibration pool of 1,500
candidates on the same realization, so the check is held out rather than
circular.

Everything runs through the process boundary — `RunTicket.for_null_calibration`
requires it, and item 3 gives the reason it matters here in particular.

### Result

```
conditional calibration, one data realization (seed 4242), q=0.05
  calibration pool: 1,500 candidates, 1,500 scored
  buckets: 10 total, 4 with >= 200 (1,167 of 1,500 scores)

  null                         C    FWER             95% CI  excludes q   no null
  profile (conditional)      400   0.045 [0.029, 0.070]        False       21.8%
```

**0.045, CI [0.029, 0.070].** The interval contains 0.05 and its upper end is
below 0.09, which is exactly the effect the arm was sized to resolve. Under the
conditional guarantee the profile-indexed null controls.

### The rotation arm, side by side

```
  null                         C    FWER             95% CI  excludes q   no null
  profile (conditional)      400   0.045 [0.029, 0.070]        False       21.8%
  rotation R=30               40   0.125 [0.055, 0.261]         True        0.0%
```

**That 0.125 is not evidence against rotation, and reading it as such would be
the mistake this arm was pre-registered to avoid.** At C = 40 the interval spans
[0.055, 0.261] — the arm was sized to separate 0.05 from 0.19 and nothing finer.
Two things are known to push it up, and together they account for it:

- **R = 30 makes `max_of_n` liberal.** The simulated maximum resamples each
  trial's null with replacement, so it can never exceed the largest draw that
  null happens to contain; a 30-point sample truncates its own upper tail.
  Measured directly on known standard-normal nulls at 2,000 campaigns
  (`docs/reviews/task33/r_sensitivity.py`): FWER **0.059 at R = 30** against
  **0.050 at R = 999**, a +0.009 bias.
- With a true rate near 0.059, observing 5 of 40 has probability about 0.11 —
  ordinary sampling noise.

And Task 31 measured rotation's p-values as uniform at R = 99 (KS D = 0.1033
against a 5% critical 0.1756). The consistent reading is that **rotation is
sound, R = 30 is too small for a 25-trial campaign, and C = 40 cannot tell the
difference.** The profile arm is the one sized to decide which null operates, and
it decided.

| | Task 30 | Task 31 (marginal) | Task 32 (conditional) |
|---|---:|---:|---:|
| known-null FWER | 0.575 | 0.115, CI [0.083, 0.177] | **0.045, CI [0.029, 0.070]** |
| null used | one strategy's table | profile-indexed, pooled over seeds | profile-indexed, conditional on the seed |

## 3. Rotation, priced and characterised anyway

Not required, because item 2 came out the way it did — but the cost and the risks
belong on the record, because the decision could be revisited if the
guarantee ever needs to be marginal.

**Parallelized cost on this machine** (M1 Pro, 8 performance cores): a serial
isolated run is 1.34 s; eight workers sustain **3.8 runs/s**. So R = 99 is 26 s
per candidate wall-clock, and a **200-candidate campaign is 87 minutes**, not the
5.5 hours the serial figure implied. At R = 999 it is 14.5 hours.

**Never amortize rotations into one child process.** A candidate that persisted
across rotations could recognise repeated session sets and **sandbag** — trade
badly on what it identifies as null runs, inflating its own null so its real
score looks exceptional. A fresh child per rotation is what the 5.7× isolation
multiplier buys. Amortizing is the obvious optimisation and it destroys the test.
Written into `candidate_null`'s docstring and `architecture.md`.

**Residual risk, named for the third adversarial pass rather than solved here.**
Rotation preserves timestamps, session labels, tick counts, the increment
multiset and each session's net move — so a candidate that measures
autocorrelation can tell rotated data from real, and one that can detect it is
being null-tested can sandbag even with fresh children per rotation.

**Stated limitation of the null itself.** Behavioural drift across rotations is
2.6–7.5% at the median per axis and reaches **23.8% at the p90 for trade count,
23.3% for fills**. In the tail of rotations the null partly describes a
differently-behaving strategy, which makes it a slightly different null from the
one the hypothesis names.

## 4. The per-trial gate at far = 0.001 is retired

No affordable R resolves it: R = 99 floors at 0.01, and R = 999 floors at 0.001
exactly with no headroom at 14.5 hours a campaign.

- **`correction.max_of_n` is the gate.** It needs resolution only at the 5% level
  of the maximum's distribution, which R = 30 already supports.
- **BH and Holm are reported diagnostics** with `resolution_cap()` — the `N ≤
  q(R+1)` limit — printed beside them, and never the accept decision.
- **`threshold_for` and `far_for` are demoted to screening and diagnostics**, not
  retired. Their docstrings say so, in those words, at the point of use. They are
  still a cheap first look and a useful column in a run record; what they must
  not be is fed to BH as if they were calibrated p-values, which is the mistake
  Task 30 measured at 10–17× the stated rate.
- **The committed tables stay** as screening artifacts. `empirical-null.json` and
  `empirical-null-nq-trades.json` are unchanged and still carry their own
  provenance; what changed is the claim made about them.
- `campaign.summarise` now reports `gate` separately from `diagnostics_only`, and
  **a campaign with no per-candidate nulls has no accept decision at all** — the
  summary says so rather than letting BH stand in.

## What this brief did not anticipate

1. **The conditional framing did not merely reduce the error rate, it removed
   the need for the expensive null.** The brief's item 3 was written as the
   likely branch; it turned out to be the contingency. That is the strongest
   argument for making the guarantee decision *before* paying for calibration,
   which is what the brief was for.
2. **Parallelism changes the rotation economics by more than the brief assumed.**
   87 minutes for a 200-candidate campaign at R = 99 is an overnight-job cost,
   not a prohibitive one — so the fallback is affordable if the guarantee ever
   has to be marginal.
3. **The exclusion rate improved but did not go away.** 21.8% of candidates still
   have no usable bucket and enter at p = 1, and the gate never sees them. The
   conditional framing halves the problem's other symptoms and leaves this one
   untouched; it is the largest remaining hole and it is where the next task
   should go.
