## Your job: two separate judgements about this paper

Stage 1 already decided this paper's claim *could* be tested on our data. You are
deciding whether it is *worth* our attention — on **two independent axes**, and a
paper can score high on one and low on the other. Most will be low on both.

**Axis 1 — `relevance`: is it worth a campaign?** Does it offer a claim a strategy
could act on, and would it change the question we ask next?

**Axis 2 — `gate_relevance`: does it bear on how we JUDGE a result?** This project's
deliverable is the research loop and its keep-or-discard gate, not a profitable
strategy. A paper that tells us nothing to trade but something about *evaluation* is
valuable here, and axis 1 is built to score it low. Axis 2 is where it counts.

Judge the axes separately. Do not let a low score on one pull the other down, and do
not average them.

---

## Axis 1 — `relevance`

### First, separate the paper's ideas from its apparatus

**Almost every paper in this corpus proposes a method we cannot run** —
reinforcement learning, a deep network, a cross-sectional panel of 500 equities,
an options surface. Judging those papers by whether their *method* is runnable
here rates the whole corpus `low`, which is what happened: **0 of 284 papers rated
high on this axis**, and one of the zeros had already produced six campaigns by
hand.

So before anything else, ask what survives when the apparatus is removed:

- the **features or state** it computes from market data;
- the **mechanism** it claims drives returns;
- the **condition** under which it claims the effect appears.

If any of those is computable from one instrument's trades and bars, **the paper
has an actionable claim even though its method is out of scope.** Judge that part,
and say in your reason which part you judged.

**A worked example, from this corpus, and it is why this section exists.**
`2609.13825v1` (ViperQ) trains two PPO agents against a prospect-theory reward.
Reinforcement learning is deferred here and we cannot tune a shaped reward, so the
paper's *method* is out of scope and nothing about that is arguable. But its
Auction Market Theory features — volume point of control, value area, cumulative
delta, tape velocity — are computable from trades on a single future. A human read
this paper, wrote *"the feature set is borrowed; the method is not"*, and ran six
campaigns on the features with a plain forward-return target. **Ranked on its
method it is `low`. Ranked on what is separable it is at least `medium`.** The
second reading is the right one, and the first one is the mistake this section
exists to stop.

**Separability is not a licence to rate everything high.** If what survives the
removal is a model, an estimator, a risk measure or a stylised fact, criterion 1
below still applies and the answer is still `low`. The question is whether
something a strategy could *act on* survives — not whether anything at all
survives.

### Then, the three things that make a paper worth a campaign

**1. There is a claim a strategy could act on.**
Stage 1 passes papers that are *feasible*, which is not the same as *testable*. A
long-memory GARCH variant can be fitted to our data — that makes it feasible — and
it proposes no edge to trade, which makes it worthless here. A paper whose
contribution is a model, an estimator, a risk measure, a theoretical result or a
description of stylised facts is **low** relevance however elegant it is. Ask: what
would the strategy *do* differently on Tuesday morning?

**2. It speaks to something we have set aside or not resolved.**
Each hypothesis states what it was not asking. Those deferrals are the open
questions, written down at the time, and a paper that speaks to one is worth more
than a paper on a topic we have never touched.

**3. It would change the question, not just confirm it.**
The best answer names a concrete edit to the next hypothesis: a condition to add, a
horizon to name, a state to split on, a measurement to replace. A paper that would
leave the next hypothesis identical is not worth a campaign.

### What lowers relevance

- **A falsified family.** The list is below. Re-exploring one spends trials on a
  known answer; if a paper argues a falsified family works for a reason we did not
  test, say that explicitly rather than ranking it high by default.
- **A question we just answered.** Repeating a closed question is not a new campaign.
- **Requiring machinery we do not have, *and* nothing separable left once it is
  removed.** Both halves are required. A paper needing a fitted regime model, a
  trained network or a calibration we cannot perform is a project rather than a
  campaign **only when its claim is inseparable from that apparatus** — when the
  edge *is* the trained network. If its features, its mechanism or its
  conditioning survive the removal, rank on those instead and say so.

  This clause used to have only its first half, and it is what rated ViperQ
  `low`: every word of the recorded reason — *"the adapted claim still requires
  training machinery (PPO agent, prospect-theory reward tuning) we do not have"* —
  was true about the method and irrelevant to the features.

### What does NOT raise relevance

**Do not rank a paper higher because it resembles something that scored well here.**
You are given outcomes so you do not repeat closed questions, not so you can steer
toward what has worked. Every score in this project so far is a rejection, and
picking papers by resemblance to past scores would be selection on data that has
already been spent.

---

## Axis 2 — `gate_relevance`: does it bear on how we judge a result?

**Why this axis exists.** Axis 1 was the only one for the first 224 papers and it
rated **none** of them high. Reading the survivors showed why, and it was not the
corpus: a systematic falsification study on five-minute bars of the *same index we
trade*, with its own acceptance criteria — t-statistic ≥ 2.0, ≥ 30 trades per fold,
positive net after friction, stability across years — scored **low**, correctly,
because it offers no edge to act on. That paper is among the most useful in the
corpus for what this project actually builds. Axis 1 could not say so. This axis can.

### What raises `gate_relevance`

- **An acceptance protocol we can compare ours against.** Thresholds, walk-forward
  schemes, minimum-trade rules, stability requirements across years or folds. Ours
  are pre-registered and their error rates measured; a second design is evidence
  about ours.
- **A multiple-testing, overfitting or backtest-overfitting method.** Corrections,
  haircuts, deflated ratios, how to count trials, how to price the search itself.
- **A null construction or significance procedure**, especially one that is cheap,
  or that is calibrated rather than assumed.
- **A measurement of out-of-sample decay** — how much of an in-sample result
  survives, and what predicts the part that does not.
- **A negative result on our instrument or something close to it.** This is the
  clearest case and the one axis 1 is structurally blind to. A credible "this family
  does not work on NQ-like data, and here is the protocol that establishes it" tells
  us where **not** to spend trials, and `docs/datasets/nq-es-qqq.md` keeps a
  falsified-families list for exactly that purpose. Trials not spent on a known
  answer are the cheapest trials in the project.
- **Evidence about the proposer or the loop itself** — how an automated search
  behaves, how it overfits, what it converges to.

### What does NOT raise it

- **Elegance, novelty, or a model with no evaluation contribution.** A better
  estimator of something is axis 1's problem, not this axis's.
- **A method needing machinery we do not have.** Same rule as axis 1: a procedure we
  cannot run is a project, not a change to the gate.
- **Agreement with what we already do.** A paper that would leave our protocol
  identical is `low`. The question is what it would *change* — a threshold to
  measure, a check to add, a claim of ours it contradicts.
- **Resemblance to anything that scored well here.** The warning above applies to
  both axes without exception.

`high` on this axis means: **it names a concrete change to the gate, the accounting,
or the falsified-families list.** If you cannot name that change, it is not high.

---

### Return exactly this JSON and nothing else

```json
{
  "relevance": "high" | "medium" | "low",
  "reason": "one or two sentences naming the binding consideration",
  "acts_on": "what a strategy would do differently, or null if nothing",
  "hypothesis_edit": "the concrete change to the next hypothesis, or null",
  "speaks_to_deferral": "which set-aside question it addresses, or null",

  "gate_relevance": "high" | "medium" | "low",
  "gate_reason": "one or two sentences naming the binding consideration",
  "bears_on": "gate" | "accounting" | "falsified_family" | "loop" | null,
  "protocol_edit": "the concrete change to the gate, the accounting or the falsified-families list, or null"
}
```

`high` should be rare **on both axes**. Most feasible papers are worth neither a
campaign nor a change to how we judge one, and saying so is the useful answer — a
campaign is fifty looks at the data and there is a budget, and the gate's error
rates were measured against the protocol as it stands.
