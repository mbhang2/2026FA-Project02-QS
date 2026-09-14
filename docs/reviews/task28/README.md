# Task 28 — exact arithmetic (landed), and the transfer factor (not measurable)

Part A landed. **Part B did not produce a number to inflate by, and the reason is
the finding**: there is no measurable regime-transfer signal in the 384-session
training pool, and Task 27's 3.12 is consistent with build-sample estimation
error rather than with regime change.

---

## Part A — exact money arithmetic

**Result: bit-identical scores on macOS and Linux, verified field by field.**

`docs/reviews/task28/where_it_diverges.py` dumps every intermediate at full
precision on both platforms. Run before the fix, it says Task 26's attribution
was wrong twice over:

- numpy is **not in that code path at all**;
- every input was **already bit-identical** — all 426 row prices, every per-row
  realized amount and commission, every per-session return.

The divergence was `sum()`. **CPython 3.12 uses Neumaier compensated summation
for floats; 3.11 does not.** The identical list of commissions summed to
4.259999999999954 on one interpreter and 4.26 on the other:

```
  sum_commission     identical: False  4.259999999999954  vs  4.26
  fsum_commission    identical: True   4.26               vs  4.26
  sum_returns        identical: False  -24.647500000000015 vs -24.64750000000002
  fsum_returns       identical: True   -24.64750000000002  vs -24.64750000000002
```

The blotter digest never showed it because `BlotterRow.as_row()` rounds to ten
places before hashing. *"The digests match" was a weaker statement than it read
as* — worth remembering about any digest that rounds.

Money is summed with `math.fsum` now — exactly rounded, so the same answer on
every interpreter rather than on the ones that happen to compensate — in
`Blotter.realized_pnl`, `Blotter.commission`, `session_returns`, and every
statistic in `metrics.py`. After:

```
run.py, same profile, both platforms
  blotter  e20fe3a4eb59f920f3b5  same: True
  fill log bda5db2c289e8e35077f  same: True
  every score field              SAME
  diagnostics fields differing:  none
```

`net_pnl` is now exactly −48.86 where one platform had −48.85999999999994.

**Integer ticks were not needed and are not the fix.** Both datasets do sit on a
0.25 grid, so the brief's premise was right — but the measured problem was
aggregation, and retyping the engine would have changed per-row values while
leaving `sum()` in place. What integer ticks would still buy is *exactness* of
intermediate values rather than *agreement* between them: a running average entry
price is not exactly representable, so a per-row realized amount is off in the
last ULP even when both machines agree on which ULP. A real but separate
improvement, priced separately rather than bundled into a task whose target was
already met.

**Null re-tabulated** once on Linux with `provenance.arithmetic` recording
platform, interpreter, numpy and summation mode. Pool moved in 361 of 384
elements by at most 9.1e-13. **Worst threshold movement: 7.6e-15** on NQ, 1.3e-15
on synthetic. Nothing beyond rounding, as the brief predicted.

**Environment-drift warning**, revised rather than left standing. It no longer
means "this row's number is not comparable" — it means the row was produced under
different pinned versions, which still matters because a changed adapter
dependency can change what the data *is*. Read it as provenance.

---

## Part B — the transfer factor

### K, and why

Run at K = 2, 4, 6 and 8 (192, 96, 64 and 48-session windows) rather than one
choice, because the answer turned out to depend on K, which is itself the result.

### The same-window control does not work, and the reason is structural

The brief anticipated this — *"if the same-window baseline is itself far from 1,
say so; it bounds how much this design can resolve"* — and it is worse than far
from 1. Same-window control medians at n=86:

| K | build window | control build | control median @ far=0.001 | "transfer factor" @ n=86, far=0.001 |
|---:|---:|---:|---:|---:|
| 2 | 192 | 96 | **18.92** | 0.07 |
| 4 | 96 | 48 | 0.80 | 1.51 |
| 6 | 64 | 32 | 1.13 | 1.35 |
| 8 | 48 | 24 | **4.08** | 0.22 |

Pooled over the whole grid the control's p90 runs 30–69 and its max 165–1842. A
number that swings from 1.51 to 0.22 with a free parameter is not a measurement,
and picking the K that gives the answer you like is the same failure Task 27
refused for block length.

The structural reason: at build-window size S the control builds on **S/2**
sessions (half a window) while the cross pairs build on **S**. The control is
always noisier than the thing it calibrates. Making it size-matched is not
possible on this pool — a no-transfer control at build size S needs 2S sessions
from one regime, and 2S contiguous sessions *are* two adjacent windows, which is
a cross pair.

### So ask distance instead, which needs no control

If regime transfer drives the ratio it should grow with the separation between
build and check windows. If build-sample error drives it, distance is irrelevant.
Both are measured at identical build size, so the comparison is clean.

K = 6, median ratio pooled over the whole (n, far) grid:

| distance | 1 | 2 | 3 | 4 | 5 |
|---|---:|---:|---:|---:|---:|
| median ratio | 1.06 | 1.09 | 0.97 | 1.00 | 1.08 |
| p90 | 28.91 | 58.43 | 61.16 | 4.07 | 24.51 |
| pairs | 10 | 8 | 6 | 4 | 2 |

**Slope of the median against distance: −0.005 per window.** Flat. The per-cell
numbers do swing — 30.12 at distance 2, 1.45 at distance 4, 18.93 at distance 5 —
but on 8, 4 and 2 pairs, and the non-monotonicity (2 > 3 > 4 < 5) is the
signature of noise, not of separation in time.

### What this means

**There is no measurable regime-transfer signal in the 384-session training
pool.** Task 27's 3.12 was one draw from a distribution whose median is ≈1.0 at
every distance and whose spread is enormous at every distance including
adjacent windows. It is consistent with build-sample estimation error at 192
sessions, and it should not be used as an inflation factor.

**That is not the same as the FAR column being fine.** The real problem is
larger and simpler than transfer: **tail-quantile estimation from a few hundred
sessions is unreliable at the alphas the gate operates at.** A p90 ratio of 4 to
61 at *distance 1* is not a regime story, it is a sample-size story. At
far = 0.0005 the estimate rests on the extreme tail of a 384-value pool that has
been resampled, and no amount of resampling adds information the pool does not
contain.

The committed table is built on all 384 sessions, twice the build size of
anything measured here, so its tail estimate is better than any of these — and
**by construction it cannot be verified against held-out data from the same
pool**, because there is no pool left over. That is the honest position: this
diagnostic can neither certify nor condemn the committed table.

### So I did not choose an inflation policy

Item 3 asked for one, pre-registered and written into `metric.md`. Choosing a
quantile of a distribution whose location moves with K, and whose only
size-matched read is flat, would be pre-registering a number with nothing behind
it — the appearance of rigour rather than rigour. Task 27's own rule applies:
*if something needs the threshold moved to appear correct, report that instead.*

**The per-candidate randomization null is the answer, and the brief already says
so.** It is conditional on the evaluation window's regime by construction, so it
sidesteps transfer entirely, and — more importantly given the above — it
calibrates against the candidate's own return series rather than against a tail
quantile estimated from 384 pooled sessions. It was finding 5's fix; on this
evidence it is finding 4's fix too, and it is the thing to build rather than an
inflation factor.

### What `metric.md` should say, once someone acts on this

Three sentences, and none of them is an inflation factor:

1. The FAR column is calibrated on the 384-session training pool and is
   **marginal over that period**, not conditional on the evaluation window.
2. Held-out checks on sub-windows of that pool show median realized FAR ≈
   promised with **no dependence on temporal distance**, and a tail that is wide
   at every distance — the limiting factor is the size of the pool, not regime
   change.
3. A validation or holdout period more extreme than anything in the pool is
   outside what any of this measures.

Task 27 item 2 asked for the restatement and made it conditional on Part B's
conclusion. This is the conclusion; the wording is left for the task that does
items 2, 3 and 4.

### Power cost

Not computed, because no inflation is proposed. The current operating thresholds
after the Part A re-tabulation, for whatever policy is adopted later to quote its
cost against:

| n | far = 0.001 | far = 0.01 |
|---:|---:|---:|
| 73 | +0.606598 | +0.402762 |
| 86 | +0.533631 | +0.357753 |

All four are unchanged from the pre-Part-A table beyond 1e-14.
