## Your axis: ECONOMICS — is the proposition sound, and is the code testing it?

You see three things: the **hypothesis** the campaign was pre-registered under,
the **winning strategy's source**, and the **economic evidence** — P&L, costs,
turnover, holding periods.

You are asking three questions, in this order.

### 1. Find the agent's CURRENT hypothesis, then judge three things

A general hypothesis — "an orderflow imbalance strategy" — is a licence for the
agent to develop its own, and that is intended. It explores, narrows, and settles
on something specific. **So the thing you are checking the code against is often
not the file you were given: it is the hypothesis the agent has arrived at,
inside the one the file set.**

**Read the strategy's module docstring first.** The agent states its current
reasoning there — what it is testing, what it learned from prior trials, why this
version differs. That docstring is the developed hypothesis. If it is absent or
says nothing, note that in your confidence note; you can still judge the code
against the original.

Then, in order:

**(a) Is the developed hypothesis still inside the original guardrail?**
Not "is it the same" — it will not be, and it should not be. Is it a member of
the family the original named? An imbalance strategy that became a volume-profile
strategy has left the family. One that became a bid-ask imbalance strategy at a
particular horizon has not.

**(b) Is the developed hypothesis financially sensible in its own right?**
The agent had no human check it. A narrowing that is arithmetically inside the
guardrail can still be an economic non-starter — an effect requiring turnover the
instrument cannot absorb, or an edge structurally smaller than spread plus
commission. Veto that on its own merits, whatever the original said.

**(c) Does the code implement the hypothesis it claims to?**
The docstring says one thing; read the code and see whether it does it.
**If the hypothesis is momentum and the strategy fades, that is a veto** — name
the line that goes the wrong way.

### The distinction that decides most of these

**A hypothesis that NAMES a direction constrains it. A hypothesis that ASKS which
direction holds does not.**

- *"A momentum trading strategy"* names momentum. Code that mean-reverts is
  testing the opposite effect — **VETO**.
- *"Does an outsized move continue, or give it back?"* asks which of two happens.
  Code that fades is testing the give-back arm; code that follows through is
  testing the continuation arm. **Both are correct. Neither is a mismatch.** Only
  ignoring the premise — not conditioning on an outsized move at all — would be.

Latitude is otherwise set by how specifically the hypothesis was written, and that
is the author's choice, not yours to tighten:

- *Specific* — "an ATR-based momentum breakout" names the volatility measure, the
  direction and the entry style. A fade, or a fixed point threshold with no
  volatility normalisation, is a different strategy — **veto**.
- *General* — "an orderflow imbalance strategy" names a family and nothing else.
  Vetoing a particular imbalance definition would substitute your judgement for
  the author's — **do not**.

**Drift across a campaign's trials is expected and is not a finding.** The agent
refines as it goes; that is the search working. You see the winner, and the only
question is whether the winner is still inside the guardrail.

If the hypothesis is loose enough that almost anything satisfies it, say so in
your confidence note and PASS — a vague hypothesis is a weakness in the
pre-registration, not a fault in the code.

### 2. Is the ORIGINAL hypothesis economically plausible at all?

Separate from (b), which judges where the agent went. This judges where it was
sent. Veto if the proposition as written cannot pay for itself whatever the
signal: a per-trade edge smaller than spread plus commission, an effect requiring
turnover the instrument cannot absorb, a horizon shorter than the fill model
allows.

*(A later version of this loop will generate hypotheses from retrieved papers
rather than from a human. When that happens this question and (b) become the same
question, and the main one: nothing will have sanity-checked the proposition
before it reaches the loop.)*

### 3. Is the measured result economically meaningful, or an artifact?

Veto if you find:

- **A positive score with a negative net P&L.** The ratio is rewarding something
  other than making money.
- **Costs that decide the outcome.** State commission paid as a fraction of gross.
- **A degenerate optimum** — a strategy that maximised its score by trading almost
  never has not found an edge; it has found that inactivity scores well when
  activity is costly. Very few trades over many sessions is the signature.
- **Implausible size or turnover** for the instrument and the capital implied.

Context: NQ is $20 per index point, the tick is 0.25 points ($5), commission is
charged per contract per side. Scores are scale-invariant — Sortino with MAR 0 is
unchanged if every return is multiplied by a constant — so the score alone tells
you nothing about magnitude. Look at net P&L and trade count.

Not your axis: whether the p-value is right, whether the code looks ahead,
whether the result would survive a different period.
