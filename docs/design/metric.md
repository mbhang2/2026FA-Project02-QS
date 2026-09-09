# The scoring metric

The single scalar the loop accepts or rejects on, its conventions, its
diagnostics, and the screening criterion any replacement has to pass.

Implemented in `src/core/metrics.py`; `src/core/scorer.py` is the fixed surface
the loop calls. Neither is importable from `src.strategies` — a strategy that can
compute its own objective can optimize a number it controls, so both refuse the
import at import time and a static check over the strategy tree backs it up.

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

### The accept rule

`AcceptRule` thresholds `ScoreTuple.significance` — the ratio times `sqrt(N)` —
at **3.0**, carried over from Task 3 unchanged. What changed is the statistic,
not the threshold: moving the threshold to make a curve look monotone would hide
a broken metric rather than fix one. `significance` is exact for Sharpe and an
approximation for Sortino, and is labelled as such.

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
- **The `inf` case**, above.

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

It now lives at `src/core/calibration/reference.py`, immutable, and
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
assumed. `src/core/calibration/null.py` measures it:

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
