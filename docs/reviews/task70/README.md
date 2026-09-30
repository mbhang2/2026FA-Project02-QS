# Task 70 — does an adaptive chooser break the L2 gate's error control?

**Pre-registered before anything ran.** Written 2026-09-30. Nothing below is
adjusted in response to a result; if the measurement contradicts an expectation
stated here, the expectation is what was wrong. The results section is appended,
not edited in.

## 1. Why this is due now, and not later

`docs/design/pre-registration.md` §4 argues that a session's M campaigns may be
corrected over as a family of M, and it attaches a caveat to its own argument:

> if a future L1.5 reallocates budget across hypothesis families based on
> results, campaigns become adaptively selected and this argument must be
> re-checked **before that exists, not after**.

That L1.5 now exists. `scripts/record_outcome.py` writes a concluded campaign's
verdicts onto the row of the paper whose distillation produced its hypothesis,
and `scripts/rank_papers.py` reads the registry when it ranks which paper to
distil next. So the chooser is wired, results reach it, and §4's own condition
for re-checking has been met. This is the re-check.

## 2. The question, stated so that it can fail

**Does a chooser that picks campaign k+1's hypothesis family from campaigns
1..k's *train* results inflate the L2 gate's false-positive rate above
q = 0.05?**

The gate under test is `max_of_m` as `scripts/l2_gate.py` calls it: rule (a),
the best winner's validation Sortino above zero, and rule (b), the best-of-M
maximum against each winner's own validation null. Rule (b) is the conjunct
§4's argument is actually about; rule (a) is an extra requirement that can only
lower the rate. **Both are reported separately** so that a change in rule (b)
cannot hide behind rule (a)'s filtering.

## 3. The parameter everything turns on

Let **rho** be the correlation, for one candidate, between its statistic on
train and its statistic on validation. Three mechanisms that make rho non-zero
are worth naming in advance, because the answer below is a statement about rho
and nothing else:

1. **An embargo too short for the data's own persistence.** NQ's embargo is 10
   sessions and it was chosen without measuring the autocorrelation it is meant
   to break.
2. **An artifact that spans the split boundary** — a fill-model bias, a
   contract-roll convention, a calendar rule. Not an edge, and present in both
   splits, which is precisely the case the sign-flip null cannot see.
3. **Weight reuse.** A fitted champion carried into validation carries
   parameters fitted on train.

Under the strict null rho = 0: nothing directional exists on validation, the
chooser can learn nothing about validation from train, and every acceptance is
a false positive.

## 4. The three arms

Every arm runs on data with **no edge anywhere**, so every L2 pass is a false
positive and can be counted.

| | arm F — fixed | arm A — adaptive on train | arm V — adaptive on validation |
|---|---|---|---|
| family for campaign k | a fresh family, chosen with no reference to any result | k = 1 fresh; k >= 2 continues the family with the best **train** winner so far with probability 0.8, else a fresh family | same rule on the best **validation** winner so far |
| champion carried across campaigns | no | yes, within a family | yes, within a family |
| what arm V is for | — | — | **a positive control.** If the forbidden arm does not inflate, this simulation cannot detect inflation at all, and arm A's null result would be indistinguishable from an underpowered one |

Arm V is what §4 forbids and it is included for exactly one reason: a null
result in arm A means something only if the instrument can register the effect
when the effect is certainly there.

## 5. Constants, fixed now

| | |
|---|---|
| campaigns per session, M | **6** — the size of the only real family we have (`nq-ml-01` .. `nq-ml-06`) |
| trials per campaign, N | **20** — what the `nq-ml` campaigns ran |
| families available | **12** |
| exploit probability | **0.8** |
| sessions per cell, S | **2,000** |
| rho grid | **0.0, 0.1, 0.2, 0.3, 0.5, 0.8** |
| validation null draws per winner, R | **200** — above the R >= 150 operating rule, so `max_of_n`'s measured liberal bias (+0.021 at R = 20, nothing distinguishable past 150) is not in the answer |
| L2 resamples, b | **2,000** |
| q | **0.05** |
| seeds | session i of a cell uses `max_of_m(seed=700_000 + i)`; candidates come from one `default_rng` per cell, seeded from (arm, rho) |

**The effect size this can resolve, stated before it runs.** At a pass rate near
0.05 with S = 2,000, the binomial standard error is 0.0049, so the 95% interval
is about +/- 0.0095. **This measurement can resolve 0.07 from 0.05. It cannot
resolve 0.055 from 0.05.** A difference between arms smaller than one percentage
point is not a result here, and will not be reported as one.

## 6. The model, and the one place it simplifies

A candidate's train statistic is a standard normal draw. A campaign's winner is
the maximum over its pool — N draws in arm F, the family's accumulated draws in
arms A and V, which is what carrying a champion means. The winner's validation
statistic is

    v = rho * t + sqrt(1 - rho^2) * z,        z ~ N(0, 1)

so v has unit variance at every rho, and the only thing rho changes is how much
of the train selection survives into validation.

**The winner's validation null is R draws from N(0, 1).** That is the
simplification, and it is a statement about what a sign-flip null can see: it
resamples the candidate's own round turns, so it keeps the magnitude
distribution and destroys the directional information. Under the strict null the
observed value is exchangeable with those draws. Under rho > 0 the observed
value carries a persistent component the null cannot reproduce, and the null
stays centred at zero — which is the whole mechanism under test.

What this therefore **cannot** say: anything about how a real null's shape — heavier
tails, a scale that differs candidate to candidate — moves the rate. Every arm
here shares one null shape, so the comparison between arms is clean and the
absolute level is a property of this model. Task 57 is the measurement that
speaks to null shape.

## 7. Predictions, recorded before running

1. **At rho = 0, all three arms sit at or below q on rule (b).** The chooser
   sees train; validation is independent of train; selection cannot bias what it
   is independent of. An excess here would be a leak in the implementation, not
   a statistical finding.
2. **At rho > 0, arm F inflates too.** Best-of-N on train is already selection,
   and L2 corrects for M, not for M x N. This is not a fact about the chooser
   and should not be attributed to one.
3. **Arm A inflates more than arm F at every rho > 0**, because carrying the
   champion makes the exploited family's pool larger than N while the correction
   stays at M.
4. **Arm A lands between F and V.** If it lands at V, the chooser's use of train
   is as damaging as using validation outright, and §4's train/validation
   distinction buys nothing.

## 8. The second, smaller measurement: what is rho actually?

Section 3 makes everything a function of rho, so the number matters. The six
`nq-ml` campaigns each have a train winner and a validation score, which is a
sample of six paired points. Pearson r over those six is reported **with its
sample size and the plain statement that at n = 6 the interval spans nearly
everything**. It goes in the record because an assumed rho is worse than a
badly-estimated one, not because six points settle anything.

## 9. How to run it

```
.venv/bin/python docs/reviews/task70/adaptive_selection.py --out docs/reviews/task70/results.json
```

Offline, no market data, no campaign. Writes `results.json` and prints the table
appended in section 10.

---

## 10. Results — the pre-registered grid

`adaptive_selection.py`, 3 arms x 6 rho x 2,000 sessions, 11.4 seconds.
`results.json` is the full record. Rates are **rule (b)** — the conjunct §4's
argument is about — with Wilson 95% intervals.

| rho | arm F fixed | arm A adaptive on train | arm V adaptive on validation |
|---|---|---|---|
| 0.00 | 0.0435 [0.0354, 0.0533] | 0.0500 [0.0413, 0.0604] | 0.0500 [0.0413, 0.0604] |
| 0.10 | 0.0790 [0.0680, 0.0916] | 0.0885 [0.0768, 0.1017] | 0.0815 [0.0703, 0.0943] |
| 0.20 | 0.1265 [0.1126, 0.1418] | 0.1440 [0.1293, 0.1601] | 0.1350 [0.1207, 0.1507] |
| 0.30 | 0.1710 [0.1551, 0.1881] | 0.1960 [0.1792, 0.2140] | 0.2020 [0.1850, 0.2202] |
| 0.50 | 0.2785 [0.2593, 0.2986] | **0.4065** [0.3852, 0.4282] | 0.3905 [0.3693, 0.4121] |
| 0.80 | 0.5015 [0.4796, 0.5234] | **0.6685** [0.6476, 0.6888] | 0.6540 [0.6329, 0.6745] |

**Prediction 1 holds. At rho = 0 all three arms sit at nominal** — 0.0435, 0.0500,
0.0500, every interval covering 0.05. The chooser sees train, validation is
independent of train, and selection cannot bias what it is independent of. There
is no implementation leak.

**Prediction 2 holds, and it is the bigger finding. The chooser is not the
problem; rho is.** At rho = 0.2 the fixed arm — *no chooser at all* — already
rejects at 0.1265, two and a half times nominal. Best-of-N on train is selection,
and the L2 gate corrects for M campaigns, not for the M x N candidates the
winners were selected from. **Nothing in the protocol corrects a validation
statistic for the fact that its candidate was chosen as the best of N on train.**
Under independence that is free. It is not free otherwise.

**Prediction 3 holds but is small, and is resolvable only at rho >= 0.5.** Arm A
minus arm F is +0.010, +0.018, +0.025, +0.128, +0.167 across the grid. Against the
+/- 0.0095 resolution fixed in section 5, the intervals separate cleanly only at
rho = 0.5 and 0.8. **At rho <= 0.3 this measurement cannot say that adaptive
selection costs anything.**

**Prediction 4 fails, and the failure is the answer to §4's question.** Arm A does
not land between F and V. It lands *on* V, and at rho = 0.5 and 0.8 slightly above
it, the intervals overlapping throughout. **Choosing the next hypothesis from
train results is as damaging as choosing it from validation results**, in the only
regime where either is damaging.

Why, mechanically: arm A picks the family with the best train maximum, and the
train maximum is what the *pool* grows toward — so arm A concentrates trials in
the family whose pool will keep producing extremes. Arm V picks by the validation
statistic, which at rho < 1 is a noisier proxy for the same pool. Selecting on the
thing that drives the pool beats selecting on a noisy shadow of it.

**So §4's train/validation distinction, as a protection for the *chooser*, buys
nothing measurable here.** What it protects is a different thing and still
protects it: a human deciding whether to stop, which is what §4's optional-stopping
argument is actually about.

## 11. Results — the empirical rho, and why the table cannot be collapsed

Six paired points, the same winner's primary on train and on validation:

| campaign | train | validation |
|---|---|---|
| nq-flowcont-01 | -0.2966 | -0.7867 |
| nq-flowcont-5m | -0.1370 | -0.2179 |
| nq-ml-02 | +1.7807 | -0.1845 |
| nq-ml-03 | +0.2898 | -0.0557 |
| nq-orderflow-01 | -0.0125 | -0.1516 |
| nq-orderflow-02 | -0.4232 | -0.4655 |

**r = 0.446, n = 6, 95% CI [-0.573, +0.923].**

The interval is the result. It covers rho = 0, where the gate is exact, and rho =
0.8, where the gate rejects at fourteen times its nominal rate. **We do not know
which regime we are in, and the point estimate sits where the fixed arm alone runs
at 0.28.** Six points cannot settle this and were never going to.

What it would take: rho is a property of the split and the embargo, so it is
measurable without spending trials — score the *same* candidate on train and on
validation many times over. The obvious source of candidates is the ones already
in the six campaign ledgers, which have train scores; scoring a sample of the
non-winners on validation would give rho at n in the hundreds. **That spends the
validation split on candidates nobody proposed for promotion**, which §4 does not
obviously permit and is a decision for a human, not a consequence of this
measurement.

## 12. POST HOC — the raw maximum is valid but blind

**Decided after seeing section 10, and labelled so.** The pre-registered
simulation gives every campaign the same null shape, so it cannot see this.
`heterogeneous_nulls.py`, 4,000 families per cell, `heterogeneous-nulls.json`.

What prompted it: the six real validation nulls have **standard deviations
differing by 20x** (0.0108 to 0.2214) and **means differing by 0.9 Sortino**
(-0.8934 to -0.0143), because a sign-flip null inherits its own candidate's cost
drag and turnover. `max_of_m` takes the maximum over **raw** Sortino across those.

| effect planted in | delta, own-null sds | raw rejects | standardised rejects |
|---|---|---|---|
| nothing (strict null) | — | 0.0565 [0.0498, 0.0641] | 0.0580 [0.0512, 0.0657] |
| the tight-null campaign | 0 | 0.0565 | 0.0580 |
| the tight-null campaign | 2 | 0.0558 | **0.3887** |
| the tight-null campaign | 4 | 0.0510 | **0.9475** |
| the tight-null campaign | **8** | **0.0505** | **1.0000** |
| the wide-null campaign | 0 | 0.0620 | 0.0622 |
| the wide-null campaign | 2 | 0.6252 | 0.3905 |
| the wide-null campaign | 4 | 0.9912 | 0.9485 |
| the wide-null campaign | 8 | 1.0000 | 1.0000 |

The two `delta = 0` rows are the strict null re-measured under each cell's own
seed, which is why the tight-null row reproduces it exactly and the wide-null row
lands 0.005 away. Nothing is planted in either.

**Both statistics are valid.** Under the strict null, 0.0565 and 0.0580 against
q = 0.05. The excess is `max_of_n`'s own documented resampling bias at finite R,
not a property of either statistic.

**The raw maximum is blind.** An effect of **eight of its own null standard
deviations** in the tight-null campaign moves the raw rejection rate to 0.0505 —
indistinguishable from no effect at all. The standardised maximum rejects every
time.

**Which campaign can even be "the best of M", under the strict null:**

| | flowcont-01 | flowcont-5m | ml-02 | ml-03 | orderflow-01 | orderflow-02 |
|---|---|---|---|---|---|---|
| raw | **0.0000** | 0.1212 | 0.5212 | 0.3573 | **0.0000** | **0.0003** |
| standardised | 0.1678 | 0.1610 | 0.1708 | 0.1775 | 0.1643 | 0.1588 |

**Two of our six campaigns were named the best of M zero times in 4,000
families, and a third was named once** (`nq-orderflow-02`, 0.0003 — one family in
four thousand). They are not unlikely to win; they are effectively ineligible,
because their nulls are too narrow for their raw Sortino to top the family
maximum. The standardised statistic gives each campaign its 1/6.

**And this is live, not hypothetical.** Re-running the real L2 gate:

```
    nq-flowcont-01    obs -0.7867  null -0.7724 +/- 0.0108    -1.32 sd
    nq-flowcont-5m    obs -0.2179  null -0.1740 +/- 0.1267    -0.35 sd
    nq-ml-02          obs -0.1845  null -0.0143 +/- 0.2214    -0.77 sd
    nq-ml-03          obs -0.0557  null -0.0703 +/- 0.1454    +0.10 sd
    nq-orderflow-01   obs -0.1516  null -0.8934 +/- 0.0416   +17.83 sd
    nq-orderflow-02   obs -0.4655  null -0.4075 +/- 0.0733    -0.79 sd

  raw          : best nq-ml-03        p = 0.7321   reject = False
  standardised : best nq-orderflow-01 p = 0.0001   reject = True
```

**`nq-orderflow-01` is +17.8 standard deviations above its own validation null,
and the gate gave it a zero percent chance of being the family's best.** The
recorded p of 0.7321 was a statement about `nq-ml-03` at +0.10 sd.

**What the trade-off costs, stated plainly.** Where the effect really is in the
widest-null campaign, the raw maximum has *more* power at a small effect — 0.625
against 0.391 at two own-null sds. That is not a virtue. It is the raw maximum
behaving as a single-campaign test on the widest-null campaign, paying no
multiplicity because the other five cannot win. It buys power on one campaign by
discarding five.

## 13. What was changed in the code, and what was deliberately not

**Changed.** `max_of_m` now always reports `per_campaign` — each campaign's own
null mean, null sd, and distance above it in sds — plus a `standardised_view`, and
sets `disagreement` when the standardised statistic rejects and the raw one does
not. `scripts/l2_gate.py` prints all of it. None of this existed before, which is
why +17.8 sd sat in a file nobody could see.

**Not changed: the verdict.** `statistic="raw"` remains the default and the L2
verdict is still **FAIL**, because rule (a) fails — every one of the six
validation Sortinos is below zero. **Flipping the default statistic is a
pre-registration decision**, and it is drafted as Amendment 10, NOT IN FORCE, for
a human to sign or refuse. Changing the statistic that decides a gate, on the
strength of a measurement made by the party being measured, is precisely the move
this project's pre-registration exists to prevent.

**One defect fixed on the way.** `max_of_m` raised `ValueError: The truth value of
an array with more than one element is ambiguous` on a numpy null sample, because
`sample or ()` is not a length test. `round_turn.null_sample` returns a list so
production never reached it; this simulation reached it on its first run.

## 14. The finding that is about the strategy, not the gate

Separate from everything above, and the most interesting thing here.

`nq-orderflow-01`'s winner scores **-0.1516** on validation while its own
sign-flip null averages **-0.8934**. The sign-flip null destroys directional
information and keeps magnitudes, so the gap means the candidate's *signs* carry
real information on a split it was never fitted to — and it still loses money,
because commission and the idle charge are larger than what the signs are worth.

That is rule (a)'s exact purpose, arriving in the data rather than in an argument:
**§3b's "the sign-flip null tests directional skill, not profitability" is no
longer a hypothetical.** We have a candidate with directional skill on validation
and no profit. The gate refuses it, correctly. What it says about the research
question is that the obstacle on this data is costs, not signal — which is a
different sentence from "we found nothing", and it is the one the evidence
supports.

## 15. Amendment 10 was signed — 2026-09-30

Section 13 left the statistic to a human. Michael signed Amendment 10 the same
day, with the objection in section "The argument against signing it" **standing
rather than answered**: the statistic was chosen after seeing which campaigns it
favours, and that stays true. The signature rests on three checkable facts, and
it does not extend past them — if any stops being true, the question reopens:

1. the direction was fixed by a simulation under a strict null, not by our
   results;
2. **no claim has ever been made from the L2 gate** — it has returned FAIL once
   and gated nothing;
3. **the change cannot flip the current verdict**, because rule (a) fails under
   either statistic.

**The reading under the new statistic, and it is still FAIL:**

```
  statistic: standardised  (Amendment 10: IN FORCE from 2026-09-30)

  M                6
  best campaign    nq-orderflow-01
  observed best    -0.1516
  p                0.0001   (q = 0.05)
  rule (a) Sortino > 0 on the best   False
  rule (b) max_of_m p <= q           True
  VERDICT          FAIL

  standardised statistic [IN FORCE]:   best nq-orderflow-01 at +17.83 sd, p = 0.0001
  raw statistic          [diagnostic]: best nq-ml-03 at -0.0557 Sortino, p = 0.7321
```

**Rule (b) now rejects and rule (a) is the only thing refusing.** That is the
honest state of the project: on validation we have a candidate whose direction is
17.8 standard deviations better than its own round turns reshuffled, and which
still loses money. The gate says no, on the profitability floor, which is exactly
what that floor was written for.

`runs/l2-verdict-raw-statistic.json` keeps the previous reading, as the amendment
requires — the record shows the gate re-run under a different statistic and what
each one said.

### Two things the signing changed in code, beyond the statistic

**`scripts/prereg.py` now answers "is amendment N in force", and it found a bug
in the old answer.** `holdout.py` asked whether "IN FORCE" was present and "NOT IN
FORCE" absent. **"REGISTERED, NOT YET IN FORCE"** satisfies both halves — it
contains the first string and not the second — and Amendments 4 and 7 are written
exactly that way. The check guarding the single irrecoverable look would have
unlocked on a status line that says the opposite in plain English. The rule is now
positional: a status line counts only if it *begins* with "IN FORCE", and an
amendment with no status line at all is an unconditional dated one.

**The disagreement is now recorded in both directions.** It previously fired only
when the standardised statistic rejected and the raw one did not, which left the
opposite case — the raw maximum rejecting alone, where its effectively
single-campaign behaviour flatters a wide-null campaign — unrecorded.

### What is now the only thing between here and the holdout

Two refusals, down from three:

- **Amendment 9 is not in force.** §8 requires the pass rule be written before
  the look, and it has not been signed.
- **L2 is FAIL.** §8 opens the holdout only if L2 accepts.

The disagreement refusal no longer fires, because the document has chosen.

## 16. RETRACTION — the +17.8 sd finding was a bug, 2026-09-30

**Sections 12 and 14 above are wrong where they treat `nq-orderflow-01` as
evidence, and section 14's conclusion is withdrawn entirely.** The sections are
left standing rather than edited, because the record of what was believed and why
is worth more than a tidy document.

**What was actually true.** `nq-orderflow-01`'s champion made **two trades in 86
validation sessions**. Its validation score was produced before Amendment 3's
idle charge reached `run.py`'s scoring path — its run record carries no
`primary_uncharged` field, which dates it — while its validation null was rebuilt
months later with the current scorer. So the observed was **-0.1516 on the Sharpe
fallback** and every possible null draw was **a Sortino around -0.87**: a
different objective *and* a different metric.

Three independent things were wrong at once, and each alone would have been enough:

| | |
|---|---|
| the null and the observed measured different objectives | one carried the idle charge, the other did not |
| they measured different **metrics** | Sharpe against Sortino, which are not comparable at any value |
| the null had a support of **four values** | 2 round turns → 2² assignments. The honest p floor was 0.25; the machinery reported 0.0001 |

**Corrected, `nq-orderflow-01` sits at +1.15 sd and the family's p is 0.6036.**
Both statistics now agree on FAIL and there is no disagreement between them.

**What survives.** Section 10's measurement is untouched — it ran on simulated
families under a strict null with well-behaved nulls, and the raw maximum really
is valid but blind. Amendment 10's *reasoning* stands. What does not survive is
the live example that made it feel urgent, and **section 14's "directional skill
without profitability" is withdrawn**: two trades is not directional skill.

**What it changed in the code.**

- `round_turn.identity_primary` / `identity_from_blotter` — the **no-flip draw**,
  which is the observed's place in a sign-flip null's support. If re-deriving it
  disagrees with the stored score, in value or in metric, the two were computed
  under different rules and no p from them means anything.
- `l2_gate.py` now **recomputes the observed in the same process as the null**,
  from the same fill log, and records `rescored` when that differs from the stored
  number. Recomputing is not a second look: §5's "once per campaign" is about
  reading the split, and a score is a pure function of a fill log, a calendar and
  a scorer, all three already on disk.
- Amendment 9 gained the **activity floor** — rule (ii) is not evaluable below
  `ceil(log2(1/q)) = 5` round turns — and the same no-flip guard, so the holdout
  cannot inherit any of this.

**The general lesson, which is the part worth keeping.** This gate builds nulls
**now** for runs scored **then**. Any change to the metric between those two
moments breaks comparability silently, and this document's own warning —
"changing it now invalidates every calibration number in the repo" — is exactly
that hazard. A number recomputed from stored inputs is trustworthy; a number
*stored* under one version of the rules and compared against one computed under
another is not, and nothing about it looks wrong from the outside. It looked like
a discovery.
