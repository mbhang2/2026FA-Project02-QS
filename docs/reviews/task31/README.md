# Task 31 — a null that knows which candidate it is

**The realized error rate on known-null campaigns went from 0.575 to 0.115, and
the reference-versus-variant gap that caused it is closed. It is still not
control**, and 200 campaigns resolve that rather than leaving it ambiguous: the
95% interval is [0.083, 0.177], which excludes 0.05 and contains 0.10.

The remaining 2.3× has a measured cause. **31.5% of a candidate's score variance
is shared across the campaign's data realization**, and the profile-indexed null
pools across realizations, so it cannot reproduce a component the observed best
carries. The per-candidate rotation null holds the realization fixed by
construction and its p-values came out uniform — so the expensive null is right
and the cheap approximation is what breaks control.

---

## Part A — the randomization design

**The null hypothesis, stated before the design.** *This candidate, with its
trading behaviour unchanged, has no edge.*

**Rotating by whole-session offsets was ruled out before implementing it.**
Sortino is computed over per-session returns; permuting sessions leaves the ratio
identical. A no-op, exactly as the brief suspected.

**Block-resampling the tick stream was rejected on the preservation criterion.**
It destroys the most structure and also destroys what must be preserved: a
candidate keyed to intraday shape trades at a different rate on a resampled path,
so the null would describe a different strategy.

**Chosen: circular rotation of each session's price path against the clock.** The
sequence of tick-to-tick increments within a session is rotated and the path is
rebuilt from the session's own open.

| | preserved | destroyed |
|---|---|---|
| tick count, timestamps, session labels | ✓ | |
| multiset of within-session increments | ✓ (asserted exactly) | |
| each session's net move | ✓ (rotating increments leaves the sum) | |
| where in the session a move happened | | ✓ (88.9% of prices change) |

Preserving the session's net move is deliberate. A design that also randomized it
would test "does this candidate have an edge, *and separately*, was this session
up or down", and the second is not the candidate's fault. The cost is that a
candidate sensitive only to the net move has no null distribution at all, which
`CandidateNull.degenerate` reports rather than returning `1/(R+1)`.

**Behavioural profile, real data against the median rotation**, over 60
candidates at R = 99:

| axis | median drift | p90 |
|---|---:|---:|
| trades | 2.6% | 23.8% |
| fills | 2.8% | 23.3% |
| sessions with exposure | 4.3% | 12.5% |
| mean position size | 0.0% | 0.9% |
| median holding period | 7.0% | 14.3% |
| per-session return sd | 7.5% | 18.2% |

The profile survives. **If it had not, the null would not be this candidate's**
and the design would be wrong — which is why it is measured rather than argued.

## Part B — the validation the brief asked for cannot pass, and why

Applied to the reference strategy, R = 200: the rotation null does **not**
reproduce the committed table. Largest disagreement over the resolvable grid is
0.2045, which is 3.0 null standard deviations.

```
      far      table   rotations    ratio
    0.005     0.0261     -0.1784    -6.83
     0.01    -0.0116     -0.1817    15.61
     0.05    -0.1044     -0.2226     2.13
      0.5    -0.2846     -0.3504     1.23
```

The brief's premise — *they should agree, the table was built from that strategy*
— does not hold, and the reason is structural rather than a bug in either:

| | question it answers |
|---|---|
| the committed table | what would this strategy score on a **fresh draw** of the market? A bootstrap over sessions pooled from 120 independent runs, so it carries across-session variation. |
| the rotation null | what would this strategy score if its timing **within each session** were destroyed? Conditional on the observed sessions, whose net moves rotation preserves by design. |

The rotation null is tighter (sd 0.068) precisely because it holds fixed what the
table resamples. **Conditioning is the point, not a defect** — it is what makes
the p-value robust to the candidate's behavioural profile, which is the failure
Task 30 measured. But it means Part B's check cannot pass, and a check that
cannot pass is not evidence that anything is right.

### The substitute, whose answer is also known in advance

On the synthetic generator every candidate is edgeless by construction, so a
valid null must give p-values that are **Uniform(0,1)**. 60 candidates, R = 99:

```
  degenerate nulls skipped   0
  p-values at the floor      1
  KS against Uniform(0,1)    D=0.1033   5% critical 0.1756   UNIFORM

    nominal  realized  expected  binomial se
       0.05    0.0333      0.05       0.0281   ok
        0.1    0.0500       0.1       0.0387   ok
        0.2    0.1667       0.2       0.0516   ok
        0.5    0.4500       0.5       0.0645   ok
```

**Resolvable effect:** the KS 5% critical value is 1.36/√C, so C = 60 resolves a
0.176 departure in the CDF and no less. This says the p-values are not grossly
non-uniform; it does not say they are uniform to within 5%.

## Part C — cost, resolution, and the cheaper alternative

**R, the floor, and the wall clock.** R = 99 puts the floor at 1/100 = 0.01;
R = 999 at 0.001. Measured per run, 105 training sessions:

| | per run | R = 99 per candidate | 200-candidate campaign |
|---|---:|---:|---:|
| in process | 0.175 s | 18 s | 1.0 h |
| **behind the process boundary** | **0.996 s** | **100 s** | **5.5 h** |

**The isolation multiplier is the cost the brief did not price.** The candidate is
untrusted code and per-candidate randomization runs it R times; doing that in
process to save 5.7× would reopen findings 2 and 3 once per candidate.
`RunTicket.for_null_calibration` is the route — it checks no digest, because the
strategies are generated rather than committed, and in exchange it forbids
in-process execution. A test asserts every call site pairs it with
`RemoteStrategy`.

At R = 999 a 200-candidate campaign is 55 hours. **Per-candidate randomization
does not survive campaign scale**, which is why the cheap option was measured
rather than skipped.

### The profile-indexed null

**Not indexed on dispersion**, despite dispersion being Task 30's headline
number. Sortino is `mean / downside_deviation`, both scale with the series, so
the ratio is scale-invariant — a dispersion index would index on something the
metric cannot see. Measured spread over 24 edgeless variants:

| axis | range | reference sits at |
|---|---|---|
| **active fraction** | 0.343 – 1.000 | 1.000 |
| **downside fraction** | 0.162 – 0.752 | 0.590 |
| dispersion | 1.697 – 5.507 | 2.699 |
| excess kurtosis | −0.090 – 8.577 | 0.878 |

A candidate trading in a third of the sessions has two thirds of its return
vector at exactly zero: no contribution to the mean, none to the downside
deviation, and still counted in the denominator. Invisible to a table tabulated
at active = 1.

Tabulated by running 60 generated strategies over 40 independent data
realizations — 2,400 runs, 514 s — and bucketing each by its own measured
profile. **1,701 of 2,400 runs (70.9%) land in a bucket with ≥ 200 scores**;
thinner buckets are refused at lookup rather than answered, because a tail of a
dozen points is not a tail.

### Is the cheap null adequate? Partly, and the gap is where it matters

12 candidates through both, R = 99:

```
  correlation                 +0.628   (se about 0.50 at this C)
  |p_profile - p_rotation|    median 0.178   max 0.384
  same accept/reject at 0.05  7/7
  no usable bucket            5/12
```

**Read that honestly.** 7/7 agreement on the decision sounds strong and is weak
evidence: every one of those candidates had p ≥ 0.13, nowhere near the boundary,
so the agreement was never tested. And the one candidate that *did* come in below
0.05 under rotation (p = 0.03) had **no usable bucket**, so the profile null could
not answer at all. The coverage gap bites exactly where the decision is made.

Correlation +0.628 with se ≈ 0.50 says "related", and cannot say more at C = 7.

## Part D — the correction

`N ≤ q(R+1)`. At q = 0.05 with R = 1,000 that is **N ≤ 50**, against 21,600 trials
overnight. `correction.max_of_n` simulates the null distribution of the
best-of-N: B replicates, one null draw per trial from that trial's own null, take
the maximum. Resolution becomes B, which is free — B = 10,000 gives a floor of
1e-4 for any N. `resolution_cap()` reports the BH limit beside it so a campaign
that has outgrown BH says so.

## Part E — acceptance, 40 known-null campaigns

40 campaigns × 50 edgeless trials, q = 0.05, profile-indexed null and the new
correction:

| test | Task 30 (40 campaigns) | Task 31 (40) | Task 31 (**200**) |
|---|---:|---:|---:|
| **max-of-N (FWER)** | — | 0.100 (4/40) | **0.115** (23/200) |
| BH over the p column | **0.575** | 0.000 | 0.005 (1/200) |
| Holm | 0.500 | 0.000 | 0.000 |
| uncorrected | 1.000 | 0.775 | 0.805 (161/200) |
| reference strategy at p ≤ 0.05 | 0.000 | 0.025 | 0.020 (4/200) |

**The gap is closed**: the reference now sits at 0.025 against the variants'
campaign rate, where before the reference was at 0/40 and the variants at 10×.

**Sampling error, and why 40 campaigns were not enough.** At C = 40 the binomial
se at p = 0.05 is 0.0345, so the ±2se band is 0.05 ± 0.07: enough to distinguish
0.05 from 0.20, not from 0.10. Task 30's 0.575 was eleven times the target and
far outside that band, which is why 40 campaigns condemned it — and 40 campaigns
could not have certified this one. **So it was run at C = 200**, where se is
0.0154 and the 95% interval around the observed 0.115 is **[0.083, 0.177]**.

**0.05 is outside that interval. max-of-N over the profile null does not
control.** It is 2.3× the target rather than 11×, and the difference between
"five times better" and "correct" is the whole point of stating the interval.

### The remaining 2.3×, measured rather than guessed

Correlation between trials is *not* the cause and goes the other way: trials in a
campaign share one realization, so they are positively correlated, and the
maximum of correlated variables is less extreme than the maximum of independent
ones. Drawing each trial's null independently overstates the spread and is
conservative.

The cause is that **the profile null is not conditional on the data
realization**. A campaign's candidates all sit on one synthetic seed; if the seed
is favourable they score high together, and the observed best carries a shared
component that a bucket pooled across 40 seeds cannot reproduce. Decomposing the
variance of the Sortino score within each usable bucket, 20 strategies × 15
seeds:

| bucket | n | total variance | between-seed | share |
|---|---:|---:|---:|---:|
| a2d1 | 87 | 0.01003 | 0.00287 | 28.6% |
| a2d2 | 56 | 0.00830 | 0.00344 | 41.5% |
| a3d2 | 55 | 0.01242 | 0.00304 | 24.5% |

**Mean between-seed share: 31.5%.** That is the component the pooled null is
missing. The rotation null removes it by construction — it holds the realization
fixed — which is consistent with its p-values coming out uniform while the
bucket's do not deliver control.

**So the expensive null is the right object and the cheap one is not adequate**,
which is the answer to Part C's question. The fix is either to pay for rotation
(5.5 h per 200-candidate campaign at R = 99), or to add a realization-level
random effect to the bucket draws — a design that is not in this task and should
be measured, not assumed, when it is.

**31.6% of candidates land in a bucket the table will not answer for**, and enter
the correction at p = 1.

> **The `BH 0.005` and `Holm 0.000` rows above are not successes and must not be
> quoted as any.** They come from a column that is a third ones. The correction
> was not controlling; it was being fed nulls by default. And the one candidate
> in twelve whose rotation p-value fell below 0.05 was one of the excluded ones —
> **the pipeline is blind to unusual profiles, which are precisely the candidates
> most likely to be genuine.** Task 32 records this beside the numbers wherever
> they appear.

The max-of-N test only sees the candidates that have nulls, so a campaign whose
*best* trial has no usable bucket is not tested at all.

## What this brief did not anticipate

1. **Part B's check cannot pass**, because the committed table and a conditional
   randomization null answer different questions. The substitute — uniformity
   under a known null — is the check that was available, and it passes.
2. **Isolation multiplies the null's cost by 5.7×.** Per-candidate randomization
   at R = 999 is 55 hours for a 200-candidate campaign. The brief priced R
   rotations at real-data speed; it did not price running untrusted code R times
   behind a boundary.
3. **The profile index could not be dispersion**, which was the number Task 30's
   diagnosis led with. Sortino is scale-invariant, so the axes had to be found by
   measuring what actually varies in a way the metric can see.
4. **Bucket coverage, not bucket accuracy, is the binding constraint.** The
   approximation looks fine where it answers and declines to answer a third of
   the time, including on the one candidate in twelve whose rotation p-value was
   below 0.05.
