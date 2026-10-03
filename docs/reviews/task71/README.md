# Task 71 — how often does the feasibility screen reject a paper its body would keep?

**Pre-registered before anything ran.** Written 2026-10-01. Nothing below is
adjusted in response to a result; the results section is appended, not edited in.

## 1. The gap, stated precisely

Stage 1 screens on the **abstract**. Bodies are fetched only for papers the
screen *keeps* (`fetch_fulltext.py --survivors`). So:

```
infeasible   261        judged on abstracts alone, no body ever fetched
adaptable     20
direct         3
with body     27
```

We already know the screen errs. The full-text re-screen of the 27 survivors
moved **5 of 27 verdicts (19%), every single one toward the abstract having been
too generous.** That measures one direction.

**The other direction is structurally unmeasurable.** A paper rejected on its
abstract never gets a body, so nothing can ever discover that its body would have
kept it. The 261 are a set we have no mechanism for being wrong about.

## 2. Why it matters now, and not as housekeeping

**Zero of 284 papers rate high on the campaign axis.** Four rate high on the gate
axis, and those are methodology papers. The refinement loop is handed three
papers per iteration and cites none of them, because none of them proposes
anything about what the market does.

That has two readings and nothing currently separates them:

- **A finding.** Our single-instrument, single-dataset constraint really does
  eliminate essentially every signal paper, and the pipeline is correctly
  reporting an empty shelf.
- **A bug.** The screen rejects adaptable signal papers on abstracts that do not
  mention the one detail making them adaptable, and the shelf is not empty — we
  are looking at it through a filter that discards the interesting half.

This measurement is what distinguishes them.

## 3. The design — a pilot, with its stopping rule fixed now

Fetching and re-screening all 261 bodies is roughly **7.8M tokens** if every body
hits the 120,000-character cap. That is affordable but not free, and a pilot
answers the question at a twentieth of the cost if the rate is low.

| | |
|---|---|
| pilot size | **40** papers currently marked `infeasible` |
| sampling | a **seeded** random sample (`random.Random(71)`), not the first 40 — the registry is in arXiv fetch order, which correlates with search term and date |
| body fetch | `fetch_fulltext.py --infeasible`, the same HTML → LaTeX → PDF ladder survivors get |
| re-screen | `screen_papers.py --rescreen`, the same prompt and model stage 1 used, with the body in place of the abstract |
| what is recorded | every verdict change, in both directions, with the first-pass reason preserved (`feasibility_first_pass`) |

**The stopping rule, fixed before the pilot runs.** Fetch and re-screen all 261
if **either**:

1. **2 or more of 40** flip from `infeasible` to `direct` or `adaptable`, or
2. **any single flip is a signal paper** — one proposing something about market
   behaviour rather than about methodology — because that is the specific thing
   the empty campaign axis is missing, and one of those is worth more than five
   methodology papers.

Otherwise the 261 stay as they are and **"the shelf is empty" is recorded as a
finding** rather than a suspicion.

**What 40 can resolve.** At 0 of 40, the one-sided 95% bound on the true
false-negative rate is **7.2%** — about 19 papers among 261. So a null pilot does
*not* prove the screen is clean; it bounds the damage. Said now so that a null
result is not later read as a clean bill of health.

## 4. Predictions, recorded before running

1. **The flip rate will be low — I expect 0 to 3 of 40.** The argument is the
   earlier re-screen's direction: if abstracts are systematically *too generous*,
   then a paper rejected despite a generous abstract is probably genuinely
   infeasible. The 261 were rejected through a lenient filter.
2. **Whatever flips will be `adaptable`, not `direct`.** A body changes a verdict
   by revealing that a method is instrument-agnostic, which is the definition of
   adaptable.
3. **The flips will be methodology papers, not signal papers**, for the same
   reason the survivors were: our constraint bites hardest on cross-sectional
   signal work, and no amount of full text makes a 500-stock study runnable on
   one future.
4. **If prediction 3 is wrong, that is the most valuable outcome here**, because
   it means the empty campaign axis is an artefact of the screen and the
   retrieval side has been failing silently rather than reporting an honest
   emptiness.

## 5. What this measurement cannot say

It cannot tell us whether a paper that survives screening is *useful*. Screening
asks "could this be run on NQ trades data at all", and the campaign axis asks
"does this bear on the question we are chasing". A paper can pass the first and be
worthless to the second, and 20 of our 23 survivors are exactly that. **Fixing
the screen does not fix the ranking**, and if the pilot flips nothing, the ranking
criterion is the next thing to look at rather than the last.

## 6. How to run it

```
.venv/bin/python scripts/fetch_fulltext.py --infeasible --limit 40
.venv/bin/python scripts/screen_papers.py --rescreen --limit 40
```

Offline except for arXiv and the gateway. No market data, no trial, no look.

---

## 7. Results — the screen rejects about 1 in 13 papers it should keep

Pilot complete: **3 of 40** originally-infeasible papers flipped to feasible when
judged on their body instead of their abstract.

| | |
|---|---|
| false-negative rate | **7.5%**, Wilson 95% CI **[2.6%, 19.9%]** |
| applied to the 261 rejected | **7 to 52** wrongly rejected, point estimate **20** |

Predictions 1 and 2 held: the rate is low (3 of 40, inside the 0–3 I predicted),
and every flip landed on `adaptable` rather than `direct`.

**Prediction 3 failed, which section 4 called the most valuable outcome.** One of
the three flips is a signal paper, not a methodology paper:

| id | flipped to | what it is |
|---|---|---|
| 2605.01176v4 | adaptable | decision-induced ranking, prediction inflation and **excessive turnover** in SPO portfolios |
| 2608.03088v1 | adaptable | goodness-of-fit for ergodic Markov processes |
| **2609.13825v1** | **adaptable** | **ViperQ — Auction Market Theory features for order-flow pattern recognition** |

## 8. The finding: the screen rejected a paper that produced five campaigns

`2609.13825v1` is **ViperQ**, and `config/hypotheses/auction-market-features.md`
was distilled from it by a human. That hypothesis ran **six campaigns** —
`nq-ml-01` through `nq-ml-06`.

The screen marked it **infeasible** on its abstract:

> "requires testing on individual equities (TSLA, NVDA) with tick-level
> microstructure data including Volume Point of..."

On its body:

> "the paper's core claim — that Auction Market Theory features (VPOC, Value Area,
> CVD, tape velocity) drive profitable RL trading — **can be tested**"

So this is not an estimated false-negative rate with no instance behind it. **The
pipeline rejected a paper that a human then read, distilled, and spent six
campaigns on.** That is a demonstrated, consequential miss, and it is the single
strongest piece of evidence in this review.

## 9. And the binding constraint is not the screen — it is the ranker

Fixing the screen did not move the thing we actually care about:

```
survivors            23 -> 25
campaign-axis high    0 ->  0
```

All three flipped papers were then ranked **campaign=low**, ViperQ included. Its
recorded reason is worth quoting, because it names the failure exactly:

> "The paper proposes a reinforcement learning system with a deep policy network
> trained end-to-end... This project explicitly defers deep networks (§9) and
> reward-shaping questions (§8), and the adapted claim still requires training
> machinery (PPO agent, prospect-theory reward tuning) we do not have. **No
> actionable edge emerges that a strategy could implement without that
> infrastructure.**"

Every word of that is true about ViperQ's *method*. And the human who read the
same paper wrote, in the hypothesis's own second heading:

> **"The feature set is borrowed. The method is not."**

The hypothesis tests the AMT *inputs* with a supervised model and a plain
forward-return target, **precisely because** RL is deferred. The ranker treated
"the method is out of scope" as "the paper is irrelevant"; the human separated the
features from the method in one sentence.

**That is why 0 of 284 rate high on the campaign axis.** Nearly every paper's
*method* is out of our scope — RL, deep networks, cross-sectional equity panels,
options surfaces — while many papers' *features and mechanisms* are perfectly
testable on one future. The ranking criterion asks whether the paper's approach is
implementable. It should ask what, if anything, in the paper is separable and
testable here.

So: **"the shelf is empty" is NOT a finding. It is a ranking artefact**, and we
have a counter-example that cost six campaigns to produce by hand.

## 10. What this changes about the plan

The pre-registered stopping rule is met twice over — 3 flips, one of them a signal
paper — so the full 261-paper sweep is warranted. **But it should not be run
first.** Fetching 221 more bodies is roughly 6M tokens and would hand the ranker
more survivors to rate `low`. The constraint has moved one stage downstream.

Order, revised on the evidence and stated as a change rather than as the plan all
along:

1. **Fix the campaign-axis criterion**, with ViperQ as the test case: a prompt
   that rates it `low` is wrong, and one that rates it usable is at least not
   wrong in that instance.
2. **Then** sweep the remaining 221 bodies, so the new survivors meet a criterion
   that can actually pass them.
3. Re-rank the whole corpus under the fixed criterion.

## 11. One defect found in the tooling while running this

`screen_papers.py --rescreen` selected any screened paper holding a body,
including papers whose verdict **already came from that body**. Its own comment
forbids exactly this — *"Re-running the same input expecting a different answer is
not a re-screen, it is a re-roll"* — and the selection did not implement it. So
`--rescreen --limit 40` spent 19 of its 40 slots re-asking settled questions, and
the first pass of this measurement covered 21 papers rather than 40. Fixed:
papers already judged on full text are skipped unless `--force-rescreen` is passed,
which is named a re-roll in its own help text.

---

## 12. The criterion, fixed — and the first campaign-axis `high`

The clause that did the damage, quoted in full as it stood:

> **Requiring machinery we do not have**, even if stage 1 judged the data
> adaptable — a paper needing a fitted regime model, a trained network or a
> calibration we cannot perform is a project, not a campaign.

Every word true of ViperQ **as a whole**, and the prompt never asked whether a
*part* of a paper is separable from its apparatus. Almost every paper in this
corpus proposes a method we cannot run, so judging by method rates the corpus
`low` — which is exactly what it did, 284 times.

Two changes to `config/prompts/paper-rank.md`:

1. A new first step on axis 1: **separate the ideas from the apparatus.** Ask
   what survives when the method is removed — the features or state it computes,
   the mechanism it claims, the condition under which it claims it — and judge
   that. ViperQ is the worked example, **named as a teaching example out loud**,
   because using one paper as a test case risks fitting the criterion to it and
   hiding that would be worse than doing it.
2. The machinery clause now requires **both halves**: machinery we lack *and*
   nothing separable once it is removed. A paper is a project rather than a
   campaign only when the edge **is** the trained network.

With the guard against the opposite failure stated in the prompt itself:
*separability is not a licence to rate everything high* — if what survives is a
model, an estimator or a stylised fact, criterion 1 still makes it `low`.

### The result, both directions

```
campaign axis   before {low: 25}
                after  {low: 21, medium: 3, high: 1}
```

**21 of 25 stayed low, so the criterion is not vacuous**, and four labels moved:

| paper | now | why it moved |
|---|---|---|
| 2607.20093v1 — *Retail Trader's Ruin* | **high** | its three-gate acceptance framework is separable from the signal families it tests |
| 2605.11423v3 — VVG classifier, MNQ | medium | a day-classifier usable as a conditioning variable, on our instrument |
| 2609.06085v1 — explainable deep learning | medium | lagged signed volume as a saturating impact mechanism, separable from the Shapley apparatus |
| **2609.13825v1 — ViperQ** | **medium** | the test case. "The AMT feature set is separable from the PPO apparatus" |

ViperQ's new reason is sharper than the one I would have written, and it is why
it is `medium` rather than `high`:

> "the paper proposes no mechanism that would change how a strategy *acts* — it
> is a feature engineering contribution, not a claim about when or why those
> features predict returns."

That is a better judgement than the human's, which simply took the features.

### The first high is a paper about the gate

`2607.20093v1`, *Retail Trader's Ruin: An Anatomy of Popular Signal Failure*,
rates **high on both axes** and carries a concrete `protocol_edit`:

> "(1) statistical gate uses hierarchical Benjamini-Yekutieli correction for
> multiplicity rather than Bonferroni; (2) economic gate requires net return
> after transaction costs to exceed a pre-registered threshold (not just
> positive); (3) survival under leverage..."

Which is this project's own deliverable, arriving through the retrieval pipeline,
on the first run of a criterion that can see it. Whether to adopt any of it is a
separate question and a human one — gate (2) in particular is a threshold, and
this project does not fit thresholds to make a curve look better.

### What is still not fixed

**Impact remains zero.** No refined hypothesis has cited a paper yet, and the
loop has not run since the criterion changed. A criterion that can rate a paper
`high` is a precondition for the retrieval side mattering, not evidence that it
does. The next session is what tests that.

The 221 unfetched bodies are also still unfetched. Section 10's order stands, and
step 1 of it is now done.

---

## 13. The full sweep — the pilot held

Step 2 of section 10, run 2026-10-02 after the criterion was fixed. Bodies
fetched for every remaining rejected paper, then re-screened on full text.

```
bodies on disk        67  ->  282 of 284   (201 html, 11 pdf, 3 latex, 2 unavailable)
judged on full text   48  ->  281
survivors             25  ->  36
```

**14 papers flipped `infeasible` → feasible across the whole corpus**, a
false-negative rate of **14/259 = 5.4%** against the pilot's 7.5% with
CI [2.6%, 19.9%]. The pilot predicted the sweep, which is the result a pilot is
supposed to produce.

### The campaign axis, before and after a day's work

```
this morning   25 survivors   {low: 25}                   zero above low
now            36 survivors   {low: 31, medium: 4, high: 1}
```

| | | |
|---|---|---|
| **high** | 2607.20093v1 | *Retail Trader's Ruin* — a three-gate acceptance framework |
| medium | 2605.11423v3 | VVG classifier, regime identification on MNQ |
| medium | **2608.00885v1** | **Optimal Trading of Microstructure Mean Reversion** |
| medium | 2609.06085v1 | lagged signed volume as a saturating impact mechanism |
| medium | 2609.13825v1 | ViperQ — the paper the screen originally threw away |

`2608.00885v1` is the one worth noting: a **signal paper recovered from the
reject pile** by the sweep and then rated usable by the fixed criterion. Both
halves of today's work were needed to surface it — the screen would have kept it
buried, and the old ranker would have rated it `low` for needing Hasbrouck or
Kalman filtering it does not have. Its separable claim is a band around an
estimated latent efficient price, which is testable here.

## 14. And a second inversion in the ranking criterion

Found while looking at why relevance was still mostly `low`.

Criterion 2 said: *"Each hypothesis states what it was not asking. Those
deferrals are **the open questions**, written down at the time, and a paper that
speaks to one is worth more."* The ranker was handed that list under the heading
**"Set aside deliberately -- the open questions"**.

They are not open questions. Read them:

- *"No resting-liquidity data, no depth, no bid-ask."* — a permanent constraint
  on the feed.
- *"One instrument, this data."* — the same.
- *"Not reinforcement learning (§8 defers it)."* — a decision that stands.
- *"Not a regime model -- that is a later and harder question."* — likewise.

**So the criterion rewarded papers for proposing exactly what we had ruled out.**
It had already happened: `2605.11423v3` was rated `medium` partly for *"directly
addresses the deferred question 'Not a regime model'"* — a line that says do not
build a regime model yet.

Criterion 2 now splits them into three kinds — hard constraints on the data,
deferred by choice, and genuinely unresolved — and says only the third raises a
score. The context block is relabelled from "the open questions" to "what each
hypothesis said it was NOT asking", with the instruction to read them as
exclusions.

**What is still missing is the third kind.** Nothing in the pipeline records a
question we asked and could not answer. The committee's `CANNOT_TELL` verdicts
and the robustness seat's unexplained concentration are exactly that, and they
reach no one. That is the next thing to build on the retrieval side, and it is
the only one of the three kinds a paper can actually help with.
