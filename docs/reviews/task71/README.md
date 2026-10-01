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
