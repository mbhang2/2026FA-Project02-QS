# Task 67 — what resolution does the volume profile need?

`config/hypotheses/auction-market-features.md` left this open and committed to
writing the answer back before trial 0. This is the answer.

**Ticks are exact and do not fit the budget. One-second bars are within 3% of the
tick profile and cost a quarter of it. The hypothesis registers 1s.**

## Fidelity — `fidelity.py`

Five real NQ train sessions, 1.95M ticks, ~390,000 a session. The tick-built
profile is ground truth; each bar resolution spreads a bar's volume evenly across
the price levels its own high-low range touched.

**Total variation distance from the tick profile**, which is the honest fidelity
measure:

| | session 739072 | 739075 | 739076 |
|---|---|---|---|
| 1s | **0.037** | **0.026** | **0.032** |
| 5s | 0.046 | 0.033 | 0.040 |
| 30s | 0.061 | 0.043 | 0.051 |

Monotone, as expected. 1s reproduces the tick profile to about 3%.

### VPOC error is not a fidelity measure, and finding that out mattered

The first pass compared VPOC, Value Area bounds and LVN counts against the
tick-built versions, and produced a **non-monotone** answer: mean VPOC error of
9.20 ticks at 1s against 4.00 at 30s. Finer resolution looked worse.

It is not. **VPOC is an argmax over a histogram, and the top two bins are
frequently near-tied** -- measured on these sessions, the peak beats its
runner-up by 9.8%, 5.7% and **0.7%**. In the third session the VPOC is close to a
coin flip between two price levels, so any perturbation moves it a long way and
"error in VPOC" measures that instability rather than the profile's accuracy.

**This is a finding about the feature, not about the measurement.** ViperQ uses
Volume Point of Control as a model input. On NQ it is a statistic that can jump
between price levels for reasons unrelated to market structure, and a model
trained on it will fit some of that noise. Value Area bounds are quantile-like
and do not have this problem -- the VA-high error at 1s was 0.40 ticks. Worth
carrying into the hypothesis as a caution on the feature itself.

## Cost — `cost.py`

Three real NQ sessions, through the **real** remote boundary: strategy in its own
process, one pipe round-trip per callback. Extrapolated to the 384-session train
split against `nq-train-fitted`'s 7,200s budget.

| subscription | per session | full split | |
|---|---|---|---|
| `tick` | 21.7s | **8,338s** | **over budget** |
| `1s` | 4.5s | **1,726s** | fits, 24% of budget |
| `5s` | 1.8s | 706s | fits |
| `30s` | 0.9s | 337s | fits |

**The probe does no work.** It counts callbacks and places no orders, so these
are delivery costs and a real strategy adds profile building and a model on top.
At `tick` the budget is already exceeded before any of that. At `1s` there is
about 5,470s of headroom, roughly 14s a session, which is ample.

`TickEvent`'s docstring says tick granularity is "expensive by design" and that
"the wall-clock budget makes that an honest cost rather than a hidden one". That
is exactly what happened: the budget priced it and the answer is no.

## The decision

**1s bars.** Closest to the tick profile of the affordable options, 3% total
variation, 24% of the budget. Written into the hypothesis.

Ticks would be preferable on fidelity alone and there is no way to pay for them
here. If the budget or the boundary changes -- a cheaper transport, a coarser
callback that batches ticks -- this is worth re-measuring rather than inheriting.
