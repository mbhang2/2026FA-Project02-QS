# Task 36 — the first campaign with a model proposing, and its control

Two campaigns of 100 trials each on the synthetic training split, identical but
for one thing: whether the agent is shown its own prior trials and their scores.
Both pre-registered and hashed before either ran, both committed before the
first trial. `claude-haiku-4-5` at temperature 0.7, prompt template digest
`94c0d8df…`, q = 0.05, fixed budget, no early stop.

Reproduce:

    python scripts/llm_campaign.py --stage register   # refuses to overwrite
    python scripts/llm_campaign.py --stage run
    python scripts/llm_campaign.py --stage nulls
    python scripts/llm_campaign.py --stage report

`resolution.py` is the power calculation, and it ran **before** the campaign.
Records, ledgers, fill logs, sources, nulls and the report are committed under
`runs/campaigns/task36/`.

---

## The headline

**An agent shown its own scores learns, on a dataset with no edge in it, and the
gate rejects it anyway.**

| | history | control |
|---|---:|---:|
| N (trials) | 100 | 100 |
| completed / crashed | 99 / 1 | 99 / 1 |
| harness failures, unaccounted | 0, 0 | 0, 0 |
| mean score | **+0.2292** | **−0.2018** |
| sd | 0.1760 | 0.1539 |
| best | +0.4222 | +0.3061 |
| trend, Spearman rho | **+0.283** | −0.139 |
| trend, permutation p | **0.0031** | 0.942 |
| **gate: max-of-N p** | **0.686** | 0.266 |
| **accepted** | **no** | no |
| rotation share | **98.0%** | 52.0% |

Between arms: a shift of **+1.586 SD**, rank-sum p = **0.0001**. The design
resolves 0.4 SD at 80% power, so this is not a close call.

Three things follow, and the third is the one that matters.

**1. It is not random search with a language model attached.** That was the
question the brief posed, and the answer is no. The history arm's scores rise
with trial index — rho +0.283, permutation p 0.0031 — and the control arm's do
not (rho −0.139, p 0.942, which is the correct behaviour for an agent told
nothing). The arms differ by 1.59 SD in the mean. The agent is using the
feedback.

**2. What it learned is worthless, because there was nothing to learn.** The
campaign profile is `signal_strength: 0.0` — 105 sessions of a pure random walk.
There is no edge in that data, so a strategy that scores +0.42 on it has fitted
the realization. **The trajectory measures how fast this agent overfits, not how
well it researches**, and the honest reading of "history helps" here is "history
lets it climb noise faster". A campaign with a planted signal would separate the
two; this one cannot, and that is a property of the dataset, not of the
measurement.

**3. Every uncorrected way of reading this campaign reports a discovery. The
gate does not.**

| how you read the history arm | what it says |
|---|---|
| the best score, uncorrected | +0.4222 at a nominal false-acceptance rate of **0.0005** |
| Benjamini–Hochberg over the tabulated FAR column | **97 discoveries of 99** |
| Holm over the same column | **70 discoveries** |
| the DSR-shaped deflation view | best beats the expected max under the null by **+0.4536**; `clears_selection_bar: True` |
| **the gate — max-of-N over per-candidate conditional nulls** | **p = 0.686. Nothing accepted.** |

The control arm is the same story at a smaller size: 4 BH discoveries, 2 Holm,
the deflation view clears its bar by +0.3376, and the gate says p = 0.266.

That gap is the project's entire claim, and this is the first time it has been
demonstrated against an adversary that was actually trying. The tabulated column
is one strategy's null and these candidates are not that strategy; the deflation
view compares against an expected maximum computed from the same wrong null. The
conditional null asks a different question — *what does **this** candidate score
on rotations of its own data* — and the answer is that the best-of-100 under
that null sits at +0.8011 at q = 0.05, nearly twice the +0.4222 observed.

---

## Rotation share: the cost is not known in advance, and now there is a number

Task 33 measured 23.5% at no signal and 55.2% at planted strength 0.50, and
recorded the share as a leading indicator: candidates exploiting an edge trade
unusually, land in thin profile buckets, and take the expensive path.

With a real agent: **98.0% in the history arm and 52.0% in the control arm.**

The committed `data/calibration/profile-null.json` was tabulated from
`NullGenerator` — momentum on the last closed bar, bracketed, with jittered
parameters. An LLM writes Donchian breakouts with ATR-scaled brackets,
multi-timeframe filters, session-time gating and volatility regimes, and almost
none of that lands in a bucket the table covers. So the table is nearly useless
against the thing it now has to price, and the campaign paid 22,500 isolated
rotated runs (~57 minutes at 7.7/s on nine workers) to get nulls the bucket path
was supposed to make cheap.

Two consequences worth stating plainly:

- **The bill scales with how unlike the calibration set the agent is**, not with
  how good it is. A stub campaign of 100 costs ~23 rotation candidates; this one
  cost 98.
- **Task 33's coverage-hole argument is stronger than it looked.** It warned that
  the gate's blind spot sits in the tails of profile space, "so the loop drifts
  toward the region the gate can see". At 98% fallback there is almost no
  bucket-visible region left, and had the hybrid not existed, 98 of 99
  candidates would have entered the correction at p = 1 and the campaign would
  have reported nothing — while looking like it had a working gate.

---

## The trajectory, in full

Running maximum at trials 1 / 10 / 25 / 50 / 75 / 99:

```
history   +0.306  +0.306  +0.403  +0.408  +0.408  +0.422
control   -0.292  +0.035  +0.306  +0.306  +0.306  +0.306
```

First half vs second half of the mean: history +0.2093 → +0.2488; control
−0.1714 → −0.2317.

The running maxima are close and the *means* are not, which is worth noticing.
The history arm's advantage is that it stops producing bad candidates, not that
it finds a spectacular one — its whole distribution moves up 1.59 SD while its
maximum moves +0.116 past the control's. **Selection acts on the maximum, so an
agent that lifts its mean is buying very little of what the gate charges for and
a great deal of what a naive reader would call improvement.**

This is also why the running-max curve is reported and not tested. See below.

## T2: failures

First half 0.02, second half 0.00, in both arms. One blocked proposal each (an
API call that failed), and no runtime crashes after trial 50 in either. There is
nothing to compare: at 1 failure per 100 the split has no resolution, and the
pre-registered test returns what it should — nothing.

Worth recording because it did not happen: **zero repair attempts across 200
proposals.** Every reply parsed and passed `compile()`, the AST pass and mypy
against the protocol on the first try. The bounded-repair machinery was built,
pre-registered, and never fired.

---

## The statistic that was rejected, before it could be reported

`resolution.py` measured three candidate statistics on simulated data at
N = 100, and the obvious one failed.

| statistic | size at 0 effect | detection at 1.5 SD trend |
|---|---:|---:|
| area under the normalised running-max curve | 0.070 | **0.088** |
| Spearman rho(index, score) | 0.047 | 0.997 (0.873 at 1.0 SD) |

The running-max AUC is nearly powerless: normalising by the range reduces the
curve to "what fraction of the campaign was spent at the top", which is
dominated by where one order statistic happened to land. It is the natural
reading of "does the running maximum climb faster", it is what the brief asks to
see, and it cannot answer the question.

So the running maximum is reported and Spearman rho is tested, and both the
rejected statistic and its numbers are in `resolution.json` — a pre-registration
that only shows the statistic that worked is not a pre-registration.

**What this design cannot resolve**, stated before it ran and unchanged after:
*"the history arm's best beat the control arm's"* is one draw against one draw.
+0.4222 versus +0.3061 is not evidence of anything, and no campaign size fixes
it — only more campaigns would, and they were not in this budget.

---

## Reproduction: decision (d), demonstrated

Every scored candidate in both arms was re-run from its stored source as a side
effect of recovering session returns for the routing. **198 of 198 reproduced
their score exactly** — not to a tolerance, to the float.

That is the claim in `docs/design/loop.md` under decision (d), and it is the half
that is true. The other half is also true and is not softened anywhere: a
*campaign* is not reproducible, because the model is not. What is recorded
instead is in the campaign record.

## The record did not move

Both records hash to what they hashed to before the first trial:
`beeb0a2a58b3e184` and `3b279f349e69f50d`, unchanged. Zero unaccounted rows and
zero harness failures across 200 trials.

---

## Cost

| | history | control |
|---|---:|---:|
| API calls | 101 | 101 |
| repair attempts | 0 | 0 |
| blocked proposals | 1 | 1 |
| prompt tokens | 2,182,054 | 1,898,983 |
| completion tokens | 296,092 | 469,440 |
| API latency | 47 min | 59 min |
| wall clock, trials | 49 min | 12 min |
| wall clock, nulls | 32 min | 20 min |

At `claude-haiku-4-5` list prices and the cache hit rates the endpoint reported
(~16k of ~19k prompt tokens served from cache per call), the two campaigns come
to roughly **$6**. The control arm's wall clock is a quarter of the history
arm's because its proposals were generated six at a time — valid precisely
because it is shown no history, so nothing about proposal *i* can depend on
proposal *j*.

Per trial, across both arms: about **$0.03**, 32 seconds of API latency, 18
seconds of scored run, and 113 seconds of per-candidate null.

**The null dominates, and it will get worse.** 22,500 rotated runs against 200
scored trials is 113 null runs per trial, and that ratio is set by the rotation
share, which is set by how unlike the calibration set the agent is. A campaign
at Task 34's recommended N = 400 would cost about four times this, most of it in
rotations.

---

## Two defects this campaign found, and one it caused

Both of the first two were invisible to thirty-five tasks of stub campaigns.
They are described in `docs/design/loop.md` and `docs/design/architecture.md`;
summarised here because "what the first real agent found" is the interesting
list.

**1. A strategy that failed to load left N without leaving a trace.** The
strategy process was started in `run.py:main()`, one level outside `_walk`'s
handlers, so a syntax error or a missing `Strategy` class raised past all of
them. `LedgerRun` wrote its row from an empty record — no outcome, no error —
and `summarise` counted it as neither a trial nor a harness failure. The
denominator shrinking by exactly the proposals that failed is the cheapest
attack there is on honest N, and the stub never produced one because it always
emits valid Python. Fixed, plus `CampaignResult.unaccounted` and a `summarise`
that raises rather than reporting a field nobody reads.

**2. `remote.child.Window` had no `last(k)`.** `strategy-api.md` documents it,
two worked examples use it, `HistoryView` implements it — and
`tests/test_examples.py` ran those examples through `Engine.for_test`, the
**in-process** path. The check that claims the examples work against the current
engine was checking the engine no trial uses. The model read the document, wrote
`history.last(n)`, and crashed; since Task 23 that costs a trial. In the first
two-trial smoke, *both* trials died this way. Fixed, the examples now also run
through `RemoteStrategy`, and a test asserts `Window`'s surface covers
`HistoryView`'s by name.

**3. Self-inflicted: mypy is not thread-safe, and the control arm ran six
threads.** `type_check` uses mypy's Python API, which keeps process-global
state; concurrent calls produced `INTERNAL ERROR ... _hashlib.pyi:26`, which
lands in stderr, which `type_check` reports as a finding, which sends the agent
away to repair a file that was fine. A harness failure wearing a candidate
failure's clothes, on the one path whose job is to keep those apart. Serialised
with a lock. **The control arm's first attempt was discarded** and its log is
kept as `runs/campaigns/task36/control-attempt1-discarded.log`: a run thrown
away because the harness broke is not a result thrown away because it looked
wrong, and the difference is only legible if the discarded log is in the record.

---

## What the brief did not anticipate

- **The rotation share against a real agent is ~98%, not ~24%.** The profile
  table is calibrated on stub strategies and an LLM does not write stub
  strategies. The hybrid's fallback is not a fallback here; it is the mechanism.
  Either the table gets rebuilt from LLM-authored candidates — which makes it a
  function of the model, and a new model invalidates it — or the rotation cost
  is the standing cost of a campaign. Neither is obviously right and this task
  does not decide it.
- **The agent needed to be told what it was running on.** The shipped documents
  describe the NQ adapter; the campaign is synthetic. Without a short factual
  statement of the run's conditions, the first proposals reasoned about GEX,
  overnight gaps and CME session structure that do not exist in the data. That
  statement is now part of the prompt, it is facts only, and it never says
  whether there is an edge. It is also a recorded field, because it shaped the
  campaign.
- **`deflation_view` clears its bar in both arms.** It is documented as a
  diagnostic and not the gate, and this is the first evidence of what that
  distinction is worth: the DSR-shaped view compares the observed maximum
  against an expected maximum derived from the same single-strategy null the
  tabulated column uses, so it inherits the same error. Both arms "clear the
  selection bar" while the gate rejects both. It should probably be reported
  next to the gate rather than under `uncorrected`, so nobody quotes it alone.
- **Zero repair attempts in 200 proposals.** The bounded-repair path, its safety
  argument and its accounting were all built and none of it ran. The static
  check is not the binding constraint on this model; the runtime is. Worth
  knowing before anyone spends effort making repair cleverer.
- **The means separate and the maxima barely do.** 1.59 SD of mean shift buys
  +0.116 of maximum. Selection charges for the maximum. A reader watching mean
  score climb would conclude the loop is working; the gate is indifferent to
  almost all of that motion.
