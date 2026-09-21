## Your job: is this paper worth our next campaign?

Stage 1 already decided this paper's claim *could* be tested on our data. You are
deciding whether it is *worth* testing — and, if so, what it would change about the
question we ask next.

You are given what this project has already tested, what it explicitly set aside,
and what it has ruled out. Use all three.

### The three things that make a paper worth a campaign

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
- **Requiring machinery we do not have**, even if stage 1 judged the data adaptable —
  a paper needing a fitted regime model, a trained network or a calibration we cannot
  perform is a project, not a campaign.

### What does NOT raise relevance

**Do not rank a paper higher because it resembles something that scored well here.**
You are given outcomes so you do not repeat closed questions, not so you can steer
toward what has worked. Every score in this project so far is a rejection, and
picking papers by resemblance to past scores would be selection on data that has
already been spent.

### Return exactly this JSON and nothing else

```json
{
  "relevance": "high" | "medium" | "low",
  "reason": "one or two sentences naming the binding consideration",
  "acts_on": "what a strategy would do differently, or null if nothing",
  "hypothesis_edit": "the concrete change to the next hypothesis, or null",
  "speaks_to_deferral": "which set-aside question it addresses, or null"
}
```

`high` should be rare. Most feasible papers are not worth a campaign, and saying so
is the useful answer — a campaign is fifty looks at the data and there is a budget.
