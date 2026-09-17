# Task 30 — the loop runs, and the first campaign says the FAR column does not support it

The loop is built and works end to end. **The null campaign says stop.** BH's
realized error rate on known-null campaigns is **0.575 against a promised 0.05**,
and the per-trial gate fires at **10–17× its stated rate**. The brief's own rule
applies: *if BH does not control at the stated q on a known-null campaign, stop;
that is a finding about the FAR column, and it outranks the loop.*

The cause is not a bug in the correction and not a bug in the loop. It is the
single-strategy assumption that `metric.md` already records — and this is the
first thing that ever pointed hundreds of *different* strategies at a null
tabulated from one.

---

## The null campaign

200 edgeless proposals, synthetic training split, q = 0.05, pre-registered before
the first trial. `runs/campaigns/ba2e91f668d6/`.

> **This campaign cannot be replayed, and that was not known until Task 47.**
> All 200 of its rows are *unpinned*: the `code_fingerprint` each one records
> does not match the fingerprint recomputed from the tree at the commit the row
> names, which means they were run against a working tree with uncommitted
> edits. **The code state that produced these 200 numbers is not recoverable
> from history.** The rows are not deleted and they still replay *from stored
> source* — every strategy's text is in `sources/` — but what harness they ran
> under cannot be established, so a replay cannot be checked against them.
>
> **The conclusion survives, on the 40-campaign replication below.** That is the
> measurement the finding actually rests on: 2,000 trials over 40 independent
> data realizations, 23/40 campaigns with a BH discovery against a promised
> 0.05. The single campaign here is an illustration of a result established
> elsewhere, and its numbers should be read as such rather than as evidence in
> their own right. Had the finding rested on this campaign alone it would now be
> unsupported.
>
> Nobody knew because `scripts/ledger_check.py` only ever looked at
> `runs/ledger.jsonl`, and campaign trials are written to the campaign's own
> ledger and never to that one. Since Task 47 the standard invocation covers
> every ledger in the repository, which is how this surfaced.

Budget of 200 because at far = 0.001 it predicts on the order of 0.2 per-trial
acceptances — the smallest budget where "expect about zero" is a checkable
prediction rather than a truism — and because the uncorrected best-of-200 is
clearly inflated at that size.

```
N (trials)    200      harness failures (not trials) 0
outcomes      {'completed': 200}
wall          266.1s

PER-TRIAL GATE, uncorrected
  accepts at far <= 0.001        3            <- expected ~0.2
  best score                     +0.1629
  its nominal FAR                0.0005
  candidates at far <= q         29

CORRECTED
  Benjamini-Hochberg (FDR, q=0.05)   2 discoveries    <- expected 0
  Holm (FWER, q=0.05)                0 discoveries
  expected max of N under the null   -0.0086
  best score minus that bar          +0.1715   clears: True
```

**The headline contrast the project has been building toward, and it works.** A
reader given only the uncorrected line would say: *the best of 200 candidates
scored +0.1629 at a nominal false-acceptance rate of 0.0005* — one in two
thousand, a discovery. Every one of those 200 candidates has no edge by
construction. Holm says zero. That is the demonstration, and it is worth having.

**But BH said 2, and it should have said 0.** One campaign cannot distinguish
"the column is anti-conservative" from "this campaign landed in the 5% where BH
fires anyway", so it was measured properly.

## Measured over 40 campaigns, each on its own data realization

The 200 trials of one campaign share a single synthetic seed — one price path,
one draw from the null, seen 200 correlated ways. Varying the thing the campaign
holds fixed is the only way to read the rate.

`fdr_calibration.py`, 40 campaigns × 50 edgeless trials = 2,000 trials:

```
  campaigns with any BH discovery     23/40 = 0.575   (q promises <= 0.05)
  campaigns with any Holm discovery   20/40 = 0.500
  campaigns with any uncorrected hit  40/40 = 1.000   <- what no correction looks like

  per-trial accepts at far <= 0.001   35/2000 = 0.01750   (promised 0.001)
  ratio measured/promised             17.50x
```

Under the global null — every candidate edgeless, true here by construction —
BH's FDR reduces to the family-wise error rate, so `P(any discovery) ≤ q`. 0.575
is not a bad-luck reading of 0.05.

**BH DOES NOT CONTROL.** And the fault is upstream of it: a procedure that takes
p-values cannot manufacture validity its inputs do not have.

## Why: the null is one strategy's, quantified

Two explanations with very different consequences — the FAR column is broken for
everyone, or the table is right about the strategy it was built from and wrong
about others. `why_it_fails.py` settles it by running the **reference strategy**,
the one the null was tabulated from, over the same 40 dataset seeds:

| | n | accepts at far ≤ 0.001 | rate | vs promised |
|---|---:|---:|---:|---:|
| the **reference** strategy | 40 | 0 | 0.00000 | **0.00×** |
| the null generator's variants | 400 | 4 | 0.01000 | **10.00×** |

| | per-session return sd | downside deviation | trades |
|---|---:|---:|---:|
| the tabulated pool | 2.7095 | 2.2960 | — |
| the reference strategy | 2.4946 | 2.1602 | 636 |
| the variants | 2.8458 | 2.4996 | 548 |

**The table is right about the strategy it was built from and wrong about the
variants.** The variants trade less often and have wider dispersion; their
per-session return distributions are not the reference's, and a quantile of the
reference's distribution is not a quantile of theirs.

This is *exactly* the assumption recorded in `metric.md` under "The null is one
strategy's, and that assumption is now visible". What is new is the number, and
that it is not a footnote-sized effect: at the gate's operating point it is a
factor of 10 to 17, and it takes the correction's realized error rate from 0.05
to 0.575.

It is also the first time the assumption could be measured, because measuring it
requires a campaign of many different strategies, which is the thing this task
built.

## What this means

**The per-candidate randomization null is now blocking, not merely intended.**
Task 28 concluded it was the principled fix for the tail-estimation weakness and
for regime transfer. This adds the reason that outranks both: without it, the
correction the whole project is built to deliver does not control, and no amount
of care in the loop changes that. A conditional null calibrated on the
candidate's own return series has no single-strategy assumption to violate.

**The loop itself is finished and correct** — see below — and should not be
re-litigated when the null is fixed. What must not happen is a real campaign
being run and reported against the current column.

## The loop, verified

Everything the brief asked for, working:

- **Trial driver.** One call, one isolated subprocess run under a committed
  profile, one ledger row. 1.33 s per trial.
- **Crash accounting.** A generator with `crash_fraction=1.0` over 6 trials gives
  `{'crashed': 6}`, N = 6, and 0 BH discoveries — a crashed trial is a trial and
  enters the correction at p = 1.
- **`harness_failed` excluded and visible.** Not in `TRIAL_OUTCOMES`, counted
  separately, reported separately.
- **Pre-registration.** Editing the record's budget from 200 to 20 after the fact
  is refused: `does not hash to what it says: stored 8d260f8f0ba5, recomputed …`.
  So is editing q from 0.05 to 0.2, and so is editing the stopping rule.
- **`ledger_check.py`** passes over the campaign's 200 rows: N = 200, 0
  superseded, 0 environment-drifted.
- **The correction** reproduces Benjamini & Hochberg (1995) Table 1 — 4 BH
  discoveries and 3 Holm — and Holm is asserted never more generous than BH over
  200 random vectors.
- **Training split only.** Asserted against `config/run.yaml`, and against the
  `split` field of every row a campaign produces.

**Wall clock: 266 s for 200 trials, 1.33 s each.** An eight-hour overnight run is
about **21,600 trials**. That is far more than the multiple-testing budget can
absorb — at N = 21,600 the BH critical value for the best trial is
q/N ≈ 2.3 × 10⁻⁶, well past the table's 0.0005 floor, so the tabulated grid
cannot express a p-value small enough to be a discovery. **Throughput is not the
constraint; the resolution of the FAR column is**, and that is another argument
for the per-candidate null, which produces a p-value per candidate rather than a
lookup against a fixed grid.

## Not anticipated by the brief

1. **One campaign cannot answer the question it was asked to answer.** The
   brief's check — run one null campaign, expect zero BH discoveries — is
   underpowered by construction: BH is *supposed* to fire 5% of the time under
   the global null, and 200 trials on one dataset seed are one draw, not 200. The
   answer needed 40 campaigns on 40 data realizations. The single-campaign
   version would have read "2 discoveries, slightly unlucky" and passed.
2. **The grid floor caps how large a campaign can usefully be.** With the far
   grid bottoming at 0.0005, no trial in a campaign of more than 1,000 can clear
   its own BH critical value on the strength of its p-value alone.
3. **The uncorrected/corrected contrast is stronger than expected.** 40 of 40
   campaigns produce an uncorrected "hit"; Holm produces zero in 20 of 40 even
   with a broken column. The demonstration survives the finding.
