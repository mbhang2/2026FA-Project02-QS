# The scoring metric

The single scalar the loop accepts or rejects on, its conventions, its
diagnostics, and the screening criterion any replacement has to pass.

Implemented in `src/core/scoring/metrics.py`; `src/core/scoring/scorer.py` is the fixed surface
the loop calls. Neither is importable from `src.strategies` — a strategy that can
compute its own objective can optimize a number it controls, so both refuse the
import at import time and a static check over the strategy tree backs it up.

## What is true now

**This document is layered by task, and later sections supersede earlier ones.**
It is kept that way on purpose — the reasoning that was wrong is evidence about
how the gate was arrived at, and deleting it would leave conclusions with no
argument behind them. But a reader needs to know which layer is current, so:

| | current | superseded |
|---|---|---|
| **Metric** | Sortino on per-session returns, MAR 0, downside deviation over all N | a t-statistic on per-trade P&L (Task 3) |
| **Accept rule** | a threshold read off the tabulated null for a stated false-acceptance rate | `ratio × √N ≥ 3.0`, kept only as a committed ablation |
| **The null** | measured per dataset, on that dataset's own training sessions, with a stationary block bootstrap whose block length is measured | assumed parametric; then measured but only on synthetic data |
| **The `inf` case** | closed — `MIN_DOWNSIDE_SESSIONS` with a Sharpe fallback that carries its own name and its own null | listed as an open question |
| **Does the gate have a known error rate?** | **yes**, per dataset, held out and verified | no |

Everything below the first horizontal rule is the record of how each of those
was arrived at, in the order it happened.

## Why the placeholder was replaced

Task 3 shipped a t-statistic on per-trade P&L, and the calibration sweep proved
it broken: acceptance **fell** as the planted edge got stronger while net P&L
kept climbing. A per-trade statistic is not invariant to decision frequency, and
for this strategy the per-trade dispersion grows with the signal faster than the
mean does — the t-statistic therefore falls as the edge improves.

Both curves are committed under `data/calibration/`, computed on the **same runs**
so they differ only in the metric. `metrics.placeholder_score` is kept callable
for exactly that reason: the contrast is the evidence that this rig catches
metric pathologies and not only gate pathologies.

## The metric

**Primary objective: Sortino ratio on per-session returns.**

```
sortino = (mean(r) - MAR) / sqrt( (1/N) * sum( min(0, r_i - MAR)^2 ) )
```

**[decided] Per-session returns, not per-trade.** This is what makes the metric
invariant to decision frequency: a strategy trading fifty times a day and one
trading once a day both have daily returns and are comparable. It is also the
observation unit the DSR and PBO literature assumes.

**[decided] Downside deviation divides by all N**, not by the count of losing
observations. The latter shrinks as a strategy improves, so Sortino explodes on a
handful of losses — the classic Sortino pathology, and another non-monotone
curve. Asserted in `test_downside_deviation_divides_by_all_observations`.

**[decided] MAR = 0**, stated rather than defaulted.

**[decided] Constant notional base.** A ratio is invariant to *constant* leverage
by construction; a varying one shows up as dispersion, which is what the ratio
penalizes. Returns are P&L divided by a fixed `notional_base`.

**[decided] Every session in the split counts, including flat ones.** A strategy
holding no position still ties up capital, and conditioning on days-it-traded is
what makes three lucky trades look extraordinary. `sessions_with_exposure` is
recorded as a diagnostic so selectivity is still visible.

**[decided] Minimum-observations floor of 30 sessions, pre-registered.** Below it
the score is the reject sentinel (`-inf`) with a reason, not a large ratio
computed from five trades. `ScoreTuple.rejected` carries it.

**Sharpe, skew and excess kurtosis** are computed on the same return series.
Deflated Sharpe and PBO are defined for Sharpe; if the loop's scalar is Sortino
and nothing computes Sharpe, the committee has nothing to deflate. Cheap now,
awkward later.

### P&L attribution

A blotter records realized P&L at fills, so a session's return is the realized
P&L of the fills that happened in it, net of commission. **An overnight hold's
P&L lands in the session it is closed**, not spread across the sessions it was
carried through. `end_of_session_positions` in the run record makes that
distortion visible. Mark-to-market attribution needs a per-session close price
the `Dataset` protocol does not yet expose; that is the right fix and it is not
this task.

### The accept rule *as it stood at Task 4* — superseded

> **Superseded by the empirical rule.** `AcceptRule` now thresholds the score
> against the tabulated null for a stated false-acceptance rate; the parametric
> form below survives only as the committed ablation
> `ablation-vol-scaled-parametric-rule.json`, which is what makes the comparison
> possible. See *Calibration* below.

`AcceptRule` thresholds `ScoreTuple.significance` — the ratio times `sqrt(N)` —
at **3.0**, carried over from Task 3 unchanged. What changed is the statistic,
not the threshold: moving the threshold to make a curve look monotone would hide
a broken metric rather than fix one. `significance` is exact for Sharpe and an
approximation for Sortino, and is labelled as such — and measuring exactly what
that approximation costs is what motivated the empirical null.

## Diagnostics — recorded, never in the objective

Max drawdown, profit concentration (share of gross profit from the best 5% of
sessions, and from the single best), seasonality by month, weekday and hour,
sessions with exposure, trade count, win rate, average holding period, turnover,
and the favorable-vs-unfavorable fill spread.

These are **not** terms in the score. They become **pre-registered veto
thresholds** at the committee's robustness auditor — "concentration above X
vetoes" — which gives them teeth while keeping the loop's accept rule a single
scalar and staying out of multi-objective optimization. All are recorded per run
regardless, so post-hoc analysis is possible.

**Seasonality is descriptive, not inferential.** At a few hundred sessions a
month-by-month breakdown has tiny per-cell counts. The caveat ships inside the
diagnostics object so it travels with the numbers.

## Screening — the acceptance criterion for any metric

**Acceptance rate must be monotonically non-decreasing in planted signal
strength**, with 0% false acceptance on the null. Run the calibration sweep; if
the curve is not monotone the metric is wrong, and the threshold does not move to
hide it.

Four invariances, each a property test on a blotter in `tests/test_metrics.py`:

1. **Trade-splitting.** Split every trade into two half-size trades at the same
   prices: same positions, same returns, different bookkeeping. Score identical.
2. **Leverage.** Scale all position sizes by k. Score unchanged.
3. **Price-scale.** Multiply the price process by k. Score unchanged — the one
   the placeholder failed.
4. **Session padding.** Adding flat sessions lowers the score. A chosen property,
   asserted deliberately.

### The exemption in invariance 3, stated rather than discovered

Price-scale invariance holds for the ratio **only if costs are expressed in the
same units as the price**. A fixed per-contract fee is not: at a hundredth of the
price scale it is a hundred times the cost in relative terms, and the score is
right to fall. `test_a_per_contract_fee_is_a_scale_breaking_term_and_the_test_says_so`
asserts both halves, so the first real adapter meets this as a documented
property rather than as a surprise.

## Screening result

Both curves, computed on the **same runs** so they differ only in the metric.
175 sessions (35 in validation), 390 ticks per session, 20 seeds per point,
`unfavorable` fill model, accept rule `significance >= 3.0 and net > 0`.
Reproduce with `python scripts/calibrate.py --seeds 20`.

| signal | sortino accept | sortino | signif | sharpe | placeholder accept | placeholder t | mean net |
|---|---|---|---|---|---|---|---|
| 0.00 | 0.00 | −0.573 | −3.39 | −0.675 | 0.00 | −3.78 | −75.98 |
| 0.05 | 0.00 | −0.532 | −3.15 | −0.594 | 0.00 | −3.25 | −68.71 |
| 0.10 | 0.00 | −0.492 | −2.91 | −0.533 | 0.00 | −2.75 | −63.20 |
| 0.20 | 0.00 | −0.326 | −1.93 | −0.307 | 0.00 | −1.57 | −43.44 |
| 0.35 | 0.05 | +0.112 | +0.66 | +0.074 | 0.05 | +0.56 | +11.95 |
| 0.50 | **0.80** | +0.881 | +5.21 | +0.478 | 0.30 | +2.75 | +97.82 |
| 0.75 | **1.00** | +1.090 | +6.45 | +0.631 | 0.55 | +3.45 | +225.92 |
| 1.00 | 0.90 | +0.846 | +5.01 | +0.500 | 0.40 | +2.67 | +270.60 |

Null false-acceptance is 0% for both. The new metric reaches full power at signal
0.75 where the placeholder manages 0.55, and it separates the null from a real
edge far more sharply.

**The strict screen still fails at the last point: 1.00 → 0.90.** The threshold
was not moved. The cause is the strategy, not the metric, and the evidence is
measured rather than argued:

| signal | mean win | mean loss | trades/session | mean session return | sd |
|---|---|---|---|---|---|
| 0.35 | 0.740 | −1.535 | 13.2 | +0.174 | 5.29 |
| 0.50 | 0.740 | −1.843 | 16.0 | +2.619 | 6.38 |
| 0.75 | 0.740 | −3.718 | 20.8 | +6.599 | 10.46 |
| 1.00 | 0.740 | −10.337 | 25.6 | +8.560 | 15.49 |
| 1.50 | 0.740 | −31.446 | 33.4 | +17.015 | inf¹ |

The mean win is **pinned at 0.740** at every signal strength — the bracket caps
it — while the mean loss grows twentyfold, because a stop gapped through under
the pessimistic fill model fills at whatever price went through it, and a
stronger trend gaps further. Past signal ≈0.75 the loss tail grows faster than
the edge, session dispersion outruns session mean, and *any* mean-over-risk ratio
must fall. Sharpe does the same thing on the same runs (0.631 → 0.500). A metric
that kept rising there would be the broken one.

Two tests keep this honest: `test_sortino_is_monotone_in_edge_when_the_risk_profile_is_held_fixed`
shows the metric is monotone with dispersion held constant, and
`test_the_sweep_turns_over_at_high_signal_and_the_cause_is_the_payoff` pins the
diagnosis so it fails if the payoff shape ever changes.

The fix belongs to the strategy — a volatility-scaled bracket rather than a fixed
one — and the strategy is the agent's file, so it is not changed here.

¹ At signal 1.50 no session finishes below the MAR and the ratio is genuinely
infinite. The guard reports `inf` rather than dividing by zero, and the accept
rule accepts it. That is a hole worth closing at the committee.

## Did Sortino need the observation floor?

Yes, and the floor at 30 is not generous. On pure-noise, zero-mean session
returns with the floor disabled (4,000 trials per point):

| N sessions | P(Sortino = ∞) | P(accepted) |
|---|---|---|
| 5 | 3.2% | **17.7%** |
| 10 | 0.03% | 11.7% |
| 20 | 0% | 7.9% |
| 30 | 0% | 6.1% |
| 40 | 0% | 5.2% |
| 100 | 0% | 3.5% |

At five sessions, one noise sample in thirty has **no losing session at all** and
scores infinity. That alone justifies the floor. The residual false-acceptance
rate at the floor is ~6%, well above the ~0.1% a t of 3.0 would suggest for a
normal statistic — `significance = ratio × sqrt(N)` is exact for Sharpe but only
an approximation for Sortino, and it over-accepts. The end-to-end null shows 0%
only because this strategy *loses* money on noise rather than breaking even.

That gap is an input to the committee task: DSR with the global N is the proper
correction, and this is the size of the problem it has to solve.

## Open

- **Flat-session handling is a chosen property and worth revisiting with
  evidence.** Every session in the split counts, so a strategy that trades on
  three days out of a hundred is scored across all hundred. On the synthetic
  runs this never bites — `sessions_with_exposure` is 35 of 35 at every signal
  strength, because the placeholder strategy trades every day. The choice
  therefore has not actually been tested by anything yet, and the first
  selective strategy is where it will show. `sessions_with_exposure` is recorded
  precisely so that comparison can be made rather than assumed.
- **Mark-to-market attribution**, once `Dataset` exposes a per-session close.
- ~~**The `inf` case**, above.~~ **Closed in Task 6** — see *The infinite-Sortino
  guard*. Below `MIN_DOWNSIDE_SESSIONS` losing sessions the score falls back to
  Sharpe, which is always defined, and the fallback carries its own name so it is
  calibrated against its own null rather than Sortino's.

---

# Calibration (Task 6)

The metric's definition is unchanged. What changed is that it now ships with the
sampling distribution of its own score under no signal, so whoever sets a
threshold gets a known false-acceptance rate instead of a guess.

Three things had been sharing the word "gate". The scorer and its null are here;
the accept rule — applying a threshold, keep or reset — belongs to the loop
harness owner; the committee's multiple-testing correction is later and separate.
**Nothing below decides accept policy.** It supplies the numbers policy needs.

## The reference strategy is apparatus, not strategy

It was `src/strategies/current.py` — the agent's file — so a power curve measured
the metric, the threshold **and** whatever that file contained. Task 4's sweep
turned over at the strongest planted signal and it took a separate diagnostic to
establish the cause. That diagnostic should not have been needed.

It now lives at `src/core/scoring/calibration/reference.py`, immutable, and
`src.strategies` cannot import the rig for the same reason it cannot import the
scorer. Two changes make its payoff shape invariant to signal strength, which is
what "acceptance must be monotone in signal strength" needs in order to be a
property of the *metric*:

**A volatility-scaled bracket.** Sized in units of recent average bar range
rather than in absolute price. Measured across the sweep:

| signal | mean win | mean loss | win/loss | fixed bracket win/loss |
|---|---|---|---|---|
| 0.00 | 0.809 | −0.948 | 0.854 | 0.707 |
| 0.50 | 0.993 | −1.144 | 0.868 | 0.523 |
| 1.00 | 1.451 | −1.605 | 0.904 | 0.091 |
| 1.50 | 2.018 | −2.203 | 0.916 | 0.043 |

Win and loss now scale together. With the fixed bracket the ratio collapsed by a
factor of sixteen across the same range.

**Flat at every session boundary.** The synthetic generator starts every session
at the same price, so a position carried across the gap realizes the whole of the
previous session's drift. At signal 1.0, same-session trades averaged **+1.30**
and the twenty-two overnight ones averaged **−16.78**; all five of the worst
losses in the run were overnight holds. Carrying overnight measures the
generator's discontinuity, not the metric. The session length is learned from bar
counts rather than assumed, so this works on any dataset — but a dataset whose
price process is continuous across sessions would not need it, and **the
generator's session reset is a modelling artefact worth fixing at source** when
the first real adapter makes the comparison possible.

The fixed-bracket result is kept as a recorded finding: mean win pinned at 0.740
while mean loss grew from 1.54 to 31.45. It is a good demonstration that the rig
separates metric behaviour from strategy behaviour.

## The empirical null

`significance = ratio × √N` is exact for Sharpe and only an approximation for
Sortino, and Task 4 measured what the approximation costs: **6.1% false
acceptance at N=30 against a nominal 0.1%.** So the relation is no longer
assumed. `src/core/scoring/calibration/null.py` measures it:

1. simulate no-signal runs of the reference strategy
2. pool every per-session return they produce
3. bootstrap N sessions per draw and compute the metric
4. tabulate the quantiles

`threshold_for(far, n_sessions, metric=...)` returns the score needed for a
stated false-acceptance rate; `far_for(score, n_sessions)` inverts it. Exact for
any metric, no closed form, no Sharpe-only assumption. The table is committed at
`data/calibration/empirical-null.json` **with the session-return pool inside it**,
so the whole thing is auditable and re-tabulating does not mean re-simulating.

Provenance: 120 no-signal runs × 35 validation sessions = 4,200 pooled session
returns, 50,000 bootstrap draws per session count, seeds recorded in the file.

**Why bootstrap.** A stable 0.1% quantile needs tens of thousands of draws and a
simulated run costs seconds. The expensive part — what a session's returns look
like under this strategy and this fill model — is simulated; the cheap part — the
sampling distribution of a statistic over N of them — is resampled. It assumes
sessions are exchangeable under the null, which holds here because the generator
seeds each session independently and the reference strategy is flat at every
boundary. **On a dataset with genuine cross-session dependence the pool would have
to be blocked**, and that is a real constraint on reusing this for the NQ adapter.

### Calibrated thresholds

Sortino on per-session returns:

| N | far=0.05 | far=0.01 | far=0.001 |
|---|---|---|---|
| 30 | −0.014 | +0.189 | **+0.485** |
| 50 | −0.099 | +0.030 | +0.196 |
| 100 | −0.182 | −0.102 | −0.002 |

The thresholds are negative at loose rates because the null itself has a negative
mean: the reference strategy loses money on noise through the pessimistic fill
model, so beating the null does not require making money. That is why the accept
rule keeps a separate `net > 0` condition.

### Verification

Held out, not in sample: the pool is split by run, quantiles are re-tabulated
from one half and the false-acceptance rate is measured on bootstrap draws from
the other.

| N | promised | measured (held out) |
|---|---|---|
| 30 | 0.05 | 0.0486 |
| 30 | 0.01 | 0.0095 |
| 30 | **0.001** | **0.00112** |
| 50 | 0.001 | 0.00101 |
| 100 | 0.001 | 0.00105 |

Against 6.1% actual for a nominal 0.1% before. On *freshly simulated* noise from
seeds the table never saw, the measured rate is 0.00085 at N=30 — slightly below
target, because a smaller verification pool contains fewer extreme sessions and
so measures a thinner tail. Both checks are in `tests/test_calibration_null.py`;
the held-out one is the two-sided test and the fresh-simulation one is one-sided.

## What the null is, and what it is not (Task 29)

The one section to read before quoting a FAR at anybody. Everything here is
measured; the measurements are in `docs/reviews/task27/` and
`docs/reviews/task28/`.

**What a threshold is.** A tail quantile of a stationary block bootstrap (block
length 7, measured from the pool's |returns| ACF) over **384 pooled session
returns of one edgeless reference strategy**. Not a distribution-free bound, not
a parametric tail, and not a statement about any strategy but that one.

**It is marginal over the calibration period, not conditional on the evaluation
window.** The null is tabulated over 2024-07 → 2025-12, whose two halves have
measurably different return distributions — downside deviation 179.2 in the first
half against 259.8 in the second, a factor of 1.45, and Sortino divides by it. A
FAR is therefore an average over the regimes in that period, not a rate that
holds in whatever regime the evaluation window turns out to be.

**At far ≤ 0.001 the quantiles are not verifiable held out, by construction.**
The deployed table consumes the whole pool, so any held-out check necessarily
validates a *smaller, noisier* estimator than the one in use. Sub-window checks
show median realized FAR ≈ promised with a very wide tail — p90 ratios of 4 to 61
even between *adjacent* windows. That is a sample-size property, not a regime
property, and it is the honest limit of what 384 sessions can support at these
alphas.

**No regime-transfer adjustment is applied, and none is warranted by the
evidence.** A distance test at fixed build size found no measurable transfer
signal: median ratio 1.06, 1.09, 0.97, 1.00, 1.08 at separations of one through
five windows, slope −0.005 per window. The apparent 3.12 from a single two-fold
split was estimation noise, not regime change.

**The single-strategy assumption stands**, as recorded in *"The null is one
strategy's, and that assumption is now visible"* above. A candidate whose return
profile differs from the reference has a different null and the table does not
know it.

**A period more extreme than anything in the pool is outside all of this.** No
statement here bounds the false-acceptance rate in a regime the calibration
period does not contain.

**The per-candidate randomization null is the intended answer to both the
tail-estimation weakness and the single-strategy assumption.** It calibrates
against the candidate's own return series in the window it is being scored on, so
it is conditional on that regime by construction and does not rest on a tail
quantile estimated from 384 pooled sessions. It was the fix for the
single-strategy finding; on the Task 28 evidence it is the fix for the transfer
question too, and it is what should be built rather than an inflation factor.

### The single-strategy assumption, measured (Task 30)

The first campaign ever run put a number on it, and the number is large enough to
break the thing the project is for.

40 known-null campaigns of 50 edgeless proposals each, on 40 independent data
realizations, corrected at q = 0.05:

| | measured | promised |
|---|---:|---:|
| campaigns with any BH discovery | **0.575** | ≤ 0.05 |
| campaigns with any Holm discovery | 0.500 | ≤ 0.05 |
| per-trial accepts at far ≤ 0.001 | **0.0175** | 0.001 |

The cause is not the correction and not the loop. Running the **reference
strategy** — the one the null was tabulated from — over the same 40 seeds gives
0 accepts in 40 at far ≤ 0.001, a rate of 0.00×. The variants give 10×. The table
is right about the strategy it was built from and wrong about the others, because
their per-session return distributions are not the reference's: median dispersion
2.85 against the reference's 2.49, downside deviation 2.50 against 2.16.

**So the per-candidate randomization null is blocking rather than intended.**
Without it the correction does not control, and a procedure that consumes
p-values cannot manufacture validity its inputs do not have. Full measurement in
`docs/reviews/task30/README.md`.

### What the gate guarantees: conditional, and stated (Task 32)

**The gate's false-discovery guarantee is conditional on the observed training
data.** Given these sessions, the probability that a campaign produces any false
discovery is at most q. The randomness the guarantee is over is **strategy
generation and the randomization**, not over which market happened.

It is not the marginal guarantee — *across markets that might have occurred* —
which is stronger and is what Task 31's multi-seed calibration was measuring.

*The guarantee does not extend to a period unlike the training data*, which is
the same caveat already carried above from the transfer work rather than a new
one.

**Why conditional.** It is what a permutation test gives, it is standard, and it
is honest about what a null built by randomizing one dataset can support. It also
dissolves the specific failure Task 31 measured: 31.5% of a candidate's score
variance sits between data realizations, and a null pooled across realizations
cannot reproduce a component that a campaign's candidates all share. Production
has one realization. A null built on it conditions on that by construction, so
the between-realization component does not arise.

**This is a decision, not a measurement**, taken once and recorded here so it is
not revisited because a later result would prefer the other one.

### Two numbers from Task 31 that look like success and are not

`BH 0.005` and `Holm 0.000` in the Task 31 acceptance table came from a p-value
column that was **31.6% ones**: a candidate with no usable profile bucket enters
at p = 1. The correction was not controlling, it was being fed nulls by default.

Worse, the one candidate in twelve whose rotation p-value fell below 0.05 was one
of the excluded ones. **The pipeline was blind to unusual profiles** — precisely
the candidates most likely to be genuine. Both facts are recorded wherever those
numbers appear, because the numbers are quotable and wrong.

### Under that framing, the cheap null controls (Task 32)

Measured over **400 independent campaigns** on one data realization, each of 25
distinct edgeless candidates, with buckets built from a **disjoint** calibration
pool of 1,500 candidates on the same realization:

| | known-null FWER | 95% CI |
|---|---:|---|
| Task 30, single-strategy table + BH | 0.575 | — |
| Task 31, profile null pooled over seeds, marginal | 0.115 | [0.083, 0.177] |
| **Task 32, profile null conditional on the seed** | **0.045** | **[0.029, 0.070]** |

The interval contains q = 0.05 and its upper end is below 0.09, which is the
effect the arm was sized to resolve. **The profile-indexed null is the operating
null**, and production costs a dictionary lookup rather than hours of rotations.

Two things that did not change and are the honest edges of this result. **21.8%
of candidates still fall in a bucket with too few scores to answer for**, enter
the correction at p = 1, and are never seen by the gate — the largest remaining
hole. And the guarantee is conditional: it says nothing about a period unlike the
training data.

### Where the correction stands after Task 31

The per-candidate null was built and it works; the cheap approximation to it does
not deliver control, and that is measured rather than suspected.

| known-null campaigns, q = 0.05 | realized error rate |
|---|---:|
| Task 30, single-strategy table + BH | **0.575** |
| Task 31, profile-indexed null + max-of-N, C = 200 | **0.115**, 95% CI [0.083, 0.177] |

0.05 is outside that interval. The remaining factor of 2.3 has a cause:
**31.5% of a candidate's score variance is shared across the campaign's data
realization**, and a null pooled across realizations cannot reproduce it. The
per-candidate rotation null holds the realization fixed and its p-values are
uniform (KS D = 0.1033 against a 5% critical value of 0.1756 at 60 candidates).

So the conditional null is the right object. What is not yet affordable is
running it at campaign scale: behind the process boundary it is 100 s per
candidate at R = 99, or 5.5 hours for 200 candidates.
`docs/reviews/task31/README.md` has the full measurement.

### The hybrid null, and the rule that keeps it valid (Task 33)

Task 32's gate controls and **21.8% of candidates never reach it**: a candidate
whose behavioural profile lands in a thin bucket has no null, enters at p = 1,
and can never be a discovery. The measured 0.045 is conservative *because of that
gap*, and the gap is in the tails of profile space, where unusual strategies
live.

That is not only a power loss. Once an agent is hill-climbing, novel profiles are
silently unacceptable, so the loop drifts toward the region the gate can see, and
**the calibration's coverage becomes an implicit prior over strategy space,
imposed by nobody and documented nowhere.**

So: **bucket when the profile's bucket holds at least 200 scores, rotation when
it does not.**

> *Mixing two valid procedures is valid when the choice between them is
> independent of the outcome, and invalid the moment it is not.*

**The routing rule keys on the profile and nothing else, and is fixed before the
campaign runs.** Never on the score, never on which null gives the nicer answer.
It is enforced by the signature rather than by a convention: `choose_null` takes
a return series and a table and has no parameter through which a score could
reach it, and the rule is recorded in the campaign record so it is auditable
rather than remembered.

Under the conditional guarantee the profile table is a **per-campaign step**,
built on the campaign's own data — the null is "other edgeless strategies with
this profile *on this data*". The committed `profile-null.json` is a screening
artifact from Task 31, pooled across realizations, and is not the operating null.

### The gate, characterized (Task 33)

**The gate controls FWER at q = 0.05 and detects a planted edge of strength 0.24
— a best-of-ten Sortino of about +0.68 over 105 training sessions — with 80%
power at N = 10.** Either half of that statement alone is not a characterization,
and until Task 33 the project had only ever measured the first half.

| planted strength | power | 95% CI | median best Sortino |
|---:|---:|---|---:|
| 0.00 | 0.07 | [0.03, 0.16] | +0.010 |
| 0.15 | 0.53 | [0.41, 0.65] | +0.095 |
| 0.30 | 0.97 | [0.89, 0.99] | +1.048 |
| 0.50 | 1.00 | [0.94, 1.00] | +6.756 |

Hybrid FWER over 400 campaigns: **0.058, CI [0.039, 0.085]**, with **no candidate
left without a null**. It rose from Task 32's 0.045 because that number was
conservative *by the size of the coverage gap*, not because anything regressed.

**And the coverage gap was not randomly placed.** A candidate that exploits a
signal trades differently from an edgeless one, so it lands in a thin bucket: at
strength 0.15, 48 of 60 campaigns had their best candidate on the rotation
fallback; at 0.50, all 60 did. Under a profile-only gate every one of those would
have been auto-rejected at p = 1. The fallback is where the power is.

### Two decisions not to tune a parameter, recorded so they are not re-opened

Both look like omissions if the reasoning is not written down, and someone will
helpfully fix them.

**Block length stays at 7.** It is measured from the pool's |returns|
autocorrelation, not chosen. Longer blocks trade the two split directions against
each other — worst-over-both-directions bottoms out near block 14 at 2.67 against
block 7's 3.34 — which moves the anti-conservative cell rather than removing it.
Selecting 14 on that basis would be tuning a parameter on the diagnostic that
judges it, which is the rule this project already applies to thresholds. The
measurement is in `docs/reviews/task27/README.md`.

**No inflation factor was adopted.** Task 28 was asked to measure a transfer
distribution and pre-register a quantile of it. The same-window control it needs
does not work on this pool: at build-window size S the control necessarily builds
on S/2 sessions, so it is always noisier than the thing it calibrates, and the
resulting "transfer factor" swings from 1.51 at K=4 to 0.22 at K=8. A number that
moves with a free parameter is not a measurement, and pre-registering a quantile
of it would be the appearance of rigour rather than rigour. The control cannot be
made size-matched on 384 sessions: a no-transfer control at build size S needs 2S
sessions from one regime, and 2S contiguous sessions *are* two adjacent windows,
which is a cross pair.

## The Sortino → Sharpe boundary is not strategy-choosable (Task 29)

`score` falls back from Sortino to Sharpe below `MIN_DOWNSIDE_SESSIONS` below-MAR
sessions. How many sessions finish below the MAR is something a strategy
influences, so while each side was thresholded against its own null a candidate
could **choose which null judged it** — and an agent hill-climbing a score drifts
onto a boundary like that without anyone writing the exploit on purpose.

**The rule: every trial is charged `max(far_sortino, far_sharpe)`.** Both legs,
on both sides, always — `null.far_for_score`. There is no band, no free
parameter, and nothing to stand on.

Two series that differ by 0.002 in total return but land on opposite sides — four
clear losses plus two marginal sessions at ±0.001 — used to be charged 0.00050
and 0.00384, a factor of 7.7 for crossing. They are now charged the same to
within 3e-6.

**What it removes and what it does not.** It removes any gain from the metric
switch. It does not remove the incentive to have fewer losing sessions, and
nothing can, because that is also what a real edge looks like. What remains is
that a Sortino estimated from three below-MAR sessions is unreliable in the
strategy's favour, and the Sharpe leg is what backstops it;
`ScoreTuple.downside_sessions` is recorded so a reader can see how near that
region a trial sits.

**The other option was implemented first and measured.** Pre-registering Sortino
and rejecting below the floor rejects a strong planted signal outright —
acceptance went to 0% on `test_power_the_gate_finds_a_planted_signal`, because a
strategy with a real edge has *few* losing sessions by construction. A rule that
rejects exactly what the gate exists to find is not conservative, it is broken.
That measurement is why the conservative-charge option was taken instead.

## The infinite-Sortino guard

Sortino needs losses to have a denominator. With no below-MAR session it is
genuinely infinite, and an accept rule thresholding it accepts unconditionally —
which does not just pass one bad candidate, it steers the search toward whatever
produces one.

**[decided] Below `MIN_DOWNSIDE_SESSIONS = 5` below-MAR sessions, the score falls
back to Sharpe**, which is always defined. The metric name changes with it, so
the fallback is compared against its own tabulated null rather than Sortino's. If
even Sharpe is infinite — every session returned exactly the same thing — the
trial is **rejected**, not accepted.

One thing the tabulation shows that is worth stating: **the fallback never fires
under the null.** In 50,000 draws at every session count, no draw had fewer than
five losing sessions. The Sharpe null is therefore tabulated unconditionally,
because the conditioning event has no observable sample — and the fact that a
candidate reaching the fallback is somewhere the null never goes is itself
information.

Note that the primary scalar is **not comparable across the switch**: a Sortino
of 3.99 and a Sharpe of 1.84 are different statistics. Each is thresholded
against its own null, which is what makes the comparison valid at the level of
the decision rather than the number.

## The power curve, and what fixed it

`data/calibration/sortino-empirical-null.json`, 20 seeds, 35 validation sessions,
accept rule `far ≤ 0.001` off the tabulated null:

| signal | accept | sortino | sharpe | metric used | mean net |
|---|---|---|---|---|---|
| 0.00 | 0.00 | −0.280 | −0.262 | sortino | −23.44 |
| 0.05 | 0.00 | −0.168 | −0.152 | sortino | −13.71 |
| 0.10 | 0.05 | −0.031 | −0.042 | sortino | −2.91 |
| 0.20 | 0.75 | +0.843 | +0.367 | sortino | +38.24 |
| 0.35 | 1.00 | +3.985 | +1.108 | both | +128.86 |
| 0.50 | 1.00 | — | +1.843 | sharpe | +241.18 |
| 0.75 | 1.00 | — | +3.686 | sharpe | +507.07 |
| 1.00 | 1.00 | — | +5.874 | sharpe | +780.83 |

**Monotone, and 0% on the null.** The curve is also more sensitive than before:
full power arrives at signal 0.35 where the old configuration needed 0.75.

### How much was the strategy and how much was the metric

An ablation, committed as `ablation-vol-scaled-parametric-rule.json`: the *new*
reference strategy scored with the *old* parametric rule (`ratio × √N ≥ 3.0`).

| signal | empirical rule | parametric rule | Task 4 (fixed bracket, parametric) |
|---|---|---|---|
| 0.20 | 0.75 | 0.75 | — |
| 0.50 | 1.00 | 1.00 | 0.80 |
| 0.75 | 1.00 | 1.00 | 1.00 |
| 1.00 | 1.00 | 1.00 | **0.90** |

The two rules give the same power curve to within one point at one signal
strength. **Essentially all of the old non-monotonicity was the reference
strategy's payoff shape**, not the metric and not the threshold relation.

That is not an argument for keeping the parametric rule. The two differ in the
thing the power curve cannot see: at N=30 the parametric rule's actual false
acceptance is 6.1% and the empirical rule's is 0.1%. The calibration buys a known
error rate, not more power.

## Is 30 still the right floor?

It can come down to about 20, and no further. Held out, thresholds tabulated on
half the pool and measured on the other:

| N | promised far | measured |
|---|---|---|
| 10 | 0.001 | **0.0087** |
| 20 | 0.001 | 0.0010 |
| 30 | 0.001 | 0.0011 |
| 50 | 0.001 | 0.0007 |

At ten sessions the calibration itself stops generalizing — the 0.1% quantile of
a ten-session statistic is estimated from too few effective extremes and misses
by nearly ninefold out of sample. At twenty it holds. **Recommendation: keep 30
unless sessions are genuinely scarce**, since 20 is at the edge of where the
tabulation is trustworthy and the cost of the floor is only power.

## For the committee: DSR against an empirical null

DSR deflates by the expected maximum of N draws from the null. The closed form
for that expectation exists only for Sharpe. With an empirical null it is a
simulation, for **any** metric — `null.expected_max(n_trials, n_sessions,
metric=...)`. Nothing in the bibliography does this, and it is what lets the
global-N correction apply to whatever scalar the loop ends up using rather than
forcing the loop to use Sharpe because that is what the maths supports.

The version here approximates from the tabulated moments. An exact one resamples
the stored draws, which the table deliberately does not keep in order to stay
reviewable — worth revisiting when the committee is built.

## Two statistical points, recorded because they are the first questions asked

### The pool's exchangeability assumption, and what replaces it on real data

The empirical null bootstraps N sessions from a pool of no-signal session
returns, which assumes those sessions are **exchangeable**. That holds for the
synthetic generator: sessions are seeded independently, and the reference
strategy is flat at every boundary so no position links one to the next.

**Real futures sessions are not obviously exchangeable.** Volatility clusters
across days, a trend persists over weeks, and a strategy's session returns
inherit both. So the null is now measured on each dataset's own training
sessions, with a **stationary block bootstrap** (Politis & Romano) whose block
length is measured rather than chosen. Both halves of that turned out to matter,
and by very different amounts.

**The dataset was the large error.** Synthetic sessions have far less dispersion
than real NQ ones, so the synthetic table sets thresholds a strategy with no edge
can clear. At N=86, `threshold_for(0.001)` is **0.0336 synthetic against 0.4636
on real NQ training sessions — 13.8×**. (Both numbers have since moved: see the
Task 17 and Task 18 entries below. The ratio is the point, not the digits.) Task 15's first real run scored a Sortino
of +0.0076 with a significance of 0.07, which is nothing; against the synthetic
table that read as a false-acceptance rate of **0.18%**, almost an accept at a
0.1% policy, and against the real one it is **32%**. The run completed, the
number was plausible, and it meant nothing.

**The block length was a smaller error, in the opposite direction from the one
predicted.** The session returns themselves are near-uncorrelated — lag-1
autocorrelation +0.0067 against a ±0.1034 significance band — but their absolute
values are not: +0.2212, decisively outside the band. Volatility clustering is
real and drift dependence is not, which is why the block length is measured over
both series and taken from whichever persists longer. The decorrelation lag is
**7 sessions**.

The prediction was that an i.i.d. resample would understate the variance and set
thresholds too low. Measured on the real pool it does the opposite: i.i.d.
thresholds run **1.25–1.52× higher** than block-7 thresholds across every N and
FAR. For a *ratio* statistic that makes sense once seen — independent resampling
can assemble a run of favourable sessions that never occurred contiguously, which
fattens the upper tail; block resampling draws chunks of history that actually
happened. Held out on sessions the table was not built from, the block-7
thresholds deliver between 0.34× and 1.64× their promised rate while the i.i.d.
ones deliver 0.02× to 0.30× — badly conservative. The block bootstrap is better
calibrated; it is just not conservative in the direction the argument assumed.

**One null per dataset, enforced rather than remembered.** Every table carries a
`dataset` tag and `threshold_for` / `far_for` raise `WrongNullTable` when asked
about a dataset the table was not measured on, and `FileNotFoundError` naming the
script when no table exists for it. The synthetic table is kept — it is still
correct for synthetic runs, and the contrast is the evidence.

**The method transfers; a particular table does not.** `threshold_for` and
`far_for` are agnostic to how the draws were produced — only `null.tabulate`
changes, and block length 1 reproduces the i.i.d. resample exactly, to the same
integers from the same generator state, so the old behaviour is a special case of
the new code path rather than a parallel one.

**Task 17: the generator was itself an assumption.** Every real-data defect found
so far was invisible here for the same reason — the synthetic generator was
uniform in exactly the ways markets are not. It is now irregular by default:
variable session lengths with early closes, runs of intervals with no trade,
and volatility clustering across sessions. The effect on the numbers above is
large and in the expected direction:

| `threshold_for(0.001, 86)` | uniform | irregular |
|---|---|---|
| synthetic | 0.0336 | **0.1506** |

Most of the synthetic-versus-real gap was the generator's regularity, not
something irreducible about simulation: 0.0336 → 0.1506 against a real 0.51 closes
roughly two thirds of it. The rest is what a generator still cannot imitate, and
it is why the null is measured per dataset rather than approximated by a better
simulator.

The generator's own sessions now cluster too — lag-1 |r| of +0.1315 against a
±0.0302 band — so the synthetic null is no longer exempt from the block
bootstrap. Its measured block length is 3. The previous exemption was justified
in a comment ("the generator seeds each session independently"), which stopped
being true the moment clustering was added; a null exempt from its own method
because of a comment is the shape of every defect this project keeps finding.

The acceptance curve moved with it. At a planted signal of 0.20 acceptance fell
from 0.75 to 0.35 and at 0.35 from 1.00 to 0.90 — the harness needs roughly twice
the edge to see the same thing, because the easier world was flattering it. False
acceptance on a known null stayed at 0.00% and acceptance stayed monotone in
signal strength.

### The null is one strategy's, and that assumption is now visible

Every threshold in the tabulated null is the distribution of
`ReferenceStrategy`'s per-session returns. A candidate that trades at a different
frequency, or holds for a different length of time, or has a different return
dispersion, **has a different null — and the table does not know that.** This was
implicit until Task 19; it is stated here because it is the assumption a
statistician will find next, and finding it in a report is better than finding it
in the code.

The proper fix is a **per-candidate randomization null** at the committee's
promotion gate: run the candidate's own logic against time-shifted or rotated
data to get *its* null. Too expensive to do per trial, correct to do on
promotion. That is a committee deliverable.

What exists now is the visible version of the assumption. Every run records
`Diagnostics.null_reference_divergence` — trades per session, exposure share,
holding period and return dispersion, each as a ratio to the reference's, where
1.0 means "the same". It is a diagnostic and never a term in the score. **A
candidate far from 1.0 is a candidate whose FAR is least trustworthy**, and the
committee can see that without recomputing anything.

The reference's own profile, measured on the NQ training pool: 43.5 round trips
per session, exposure in every session, a mean hold of about half an hour, and a
pooled session-return standard deviation of 275. A test asserts those still
describe the committed pool, so changing the apparatus surfaces here rather than
silently rebasing every divergence number already in the ledger.

### Is the FAR column actually a p-value column? (Task 19)

The ledger's FAR column is the input to false-discovery control, and until Task
19 nothing had checked it was valid. Benjamini–Hochberg needs **validity** —
P(p ≤ α) ≤ α for every α — not exact uniformity. Conservative costs power and
stays valid; anti-conservative makes the reported FDR precise and wrong.

Held out, tabulating on one contiguous half of the pool and measuring on the
other, over the full 5 × 10 grid of session counts and false-acceptance rates
including n = 73 (the holdout) and n = 86 (validation):

| | NQ | synthetic |
|---|---|---|
| worst measured/promised | **0.83** | **0.86** |
| anti-conservative cells | **0** | **0** |
| FAR column valid inside the grid, n = 73 and 86 | yes | yes |

**No anti-conservatism anywhere.** The 1.64 at n=30 that prompted this check was
measured on the 359-session pool of Task 17 and was tail-estimation noise: at
50,000 draws the far = 0.001 quantile rests on about 50 exceedances, its sampling
standard deviation is 0.026, and the held-out ratio moved over a 1.4× range
across seeds. At 500,000 draws the standard deviation is 0.008 and the range is
1.1×. **More draws was enough; no extreme-value tail fit was needed**, and the
bootstrap costs seconds because the expensive part — the pooled session runs —
is already done.

The remaining conservatism, 0.3 to 0.86, is **not** estimation error and more
draws will not close it. It is period heterogeneity: the second half of the
training period has a different return distribution from the first, so thresholds
built on one are exceeded less often on the other. That is a real property of the
data, it costs power, and it is in the safe direction.

Two smaller results. The 1/√N interpolation between tabulated session counts is
accurate to within 0.2% at n = 73 and n = 86 — measured against tabulating them
directly, which the grid now does anyway, since the two numbers every real
decision is made at should be measured rather than inferred. And the KS statistic
against Uniform(0,1) on the interior of the grid rejects exact uniformity
(0.012 and 0.019 against a 1% critical value near 0.008) in the conservative
direction: the column is **valid but not uniform**, which is the correct and
usable state. `far_for` clamps at the table's edges, so the endpoint α = 0.5 is
censored by construction and is reported rather than scored.

Evidence: `data/calibration/empirical-null*-verification.json`.

**Task 18: the pool is whole.** The real-data null excluded 25 sessions — early
closes and the session after one — because the reference strategy could not be
flat on a half day. The cause was never the strategy: `NQDataset.session_bounds`
computed every session as running to 16:00 CT, so on a half day it was told it
had four hours it did not have. The adapter reads committed early-close times
now, all 384 training sessions end flat, and the exclusion is gone.

| `threshold_for(0.001, 86)` on NQ | pool | value |
|---|---|---|
| Task 16 | 359 sessions, 25 excluded | 0.4636 |
| Task 17 (reference strategy rewritten) | 359 | 0.5143 |
| Task 18 (exclusion removed) | **384** | **0.5282** |

The block length did not move: still 7, with lag-1 |r| at +0.2041 against a
±0.100 band. The recovered sessions are half days and the sessions after them, so
the pool gained its thinnest and most irregular observations and the threshold
rose slightly — the direction it should move when the easy sessions stop being
the only ones measured.

The exclusion switch is kept rather than deleted, and stays **structural**: if a
future adapter has a session type the strategy cannot exit before, excluding it
by exchange calendar is the honest response. Excluding by outcome conditions the
null on the thing it is measuring.

*Cost:* 190 seconds end to end for the NQ table — 141.5M ticks over 384 training
sessions to build the pool, and the bootstrap itself is seconds. It reruns
whenever the data extends.

### `far_for` returns a p-value, and that is worth saying out loud

`far_for(score, n)` is the probability of seeing a score at least this good when
there is nothing there. That is a p-value. It follows that **the ledger's FAR
column is a table of p-values**, one per trial, computed against a null that was
measured rather than assumed.

Which means false-discovery control over it is off the shelf:

- **Benjamini–Hochberg** over the column controls FDR at a chosen level;
- **Holm** controls the family-wise error rate if that is preferred;
- **expected-max deflation** — `null.expected_max(n_trials, n_sessions)` — is the
  DSR-shaped version, and with an empirical null it works for any metric rather
  than only for Sharpe.

And **global N becomes the length of that column**, not a bespoke construction:
every trial the harness ran, on every dataset, including the crashed and
abandoned ones, is one p-value in the table.

That reframing is worth more than it looks. It turns the project's central claim
from "we built a gate" — which invites the question of whether the gate is any
good — into "we log calibrated p-values and apply standard multiple-testing
machinery to them", which is a far easier thing to defend, and whose failure
modes are already documented in the statistics literature rather than in ours.

The remaining bespoke part is the calibration itself: producing a p-value that is
actually uniform under the null for a metric with no closed form. That is what
Task 6 built, and it is the contribution.

## Correction (Task 7): the root cause was the generator, not the bracket

Task 6 attributed the fixed-bracket strategy's exploding loss tail — mean loss
1.54 → 31.45 as the planted signal strengthened — to the bracket capping every
win while a gapped stop lost more in a stronger trend, and fixed it with a
volatility-scaled reference strategy. Task 7 made the generator carry price
across session boundaries, and measured the same strategy again:

| signal | mean win | mean loss | win/loss | overnight holds |
|---|---|---|---|---|
| 0.00 | 0.740 | −1.022 | 0.724 | 27.8 |
| 0.50 | 0.740 | −1.024 | 0.723 | 21.0 |
| 1.00 | 0.740 | −1.032 | 0.717 | 13.0 |
| 1.50 | 0.740 | −1.022 | 0.724 | 5.0 |

**The loss tail is flat.** With continuous prices the *fixed*-bracket strategy's
payoff shape is already invariant to signal strength, and its acceptance curve is
monotone: 0.00, 0.00, 0.00, 0.25, 1.00, 1.00, 1.00, 1.00
(`ablation-fixed-bracket-continuous.json`).

So the proximate description was right and the attribution was wrong. The cause
was the generator restarting every session at the same price, and a strategy
holding overnight realizing the whole of the previous session's drift as a loss
that grew with the signal. The bracket was where the symptom surfaced.

Two things follow, and neither is that Task 6 was wasted.

The vol-scaled reference strategy is still the right apparatus. A bracket sized
in absolute price will misbehave on real data whose volatility differs across
regimes, and the rig should not be measuring that. It was fixing a real problem;
it was not fixing *this* one.

And the more useful lesson is about the rig rather than the metric: a
diagnostic that separates metric behaviour from strategy behaviour found the
strategy, correctly, and stopped there — because the dataset was not one of the
things it was set up to vary. **The generator was apparatus too, and nothing was
holding it to the same standard.** That is worth remembering before the first
real adapter, where the dataset is no longer something anyone can inspect by
reading forty lines of numpy.

None of this moved the calibration: the null and the power curve are unchanged
to floating point, because the reference strategy was already flat at every
session boundary and so never crossed the discontinuity.
