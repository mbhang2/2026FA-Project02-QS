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
