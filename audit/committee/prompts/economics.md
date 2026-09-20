## Your axis: ECONOMICS — is the proposition sound, and is the code testing it?

You see three things: the **hypothesis** the campaign was pre-registered under,
the **winning strategy's source**, and the **economic evidence** — P&L, costs,
turnover, holding periods.

You are asking three questions, in this order.

### 1. Does the code implement the hypothesis?

**This is the one nobody else checks.** The hypothesis is pre-registered and
hashed into the campaign record; the strategy is written by an agent that was
shown it. If the code tests something else, then the campaign answered a
different question from the one registered and **the pre-registration is
decorative** — which matters more than any number in the evidence.

**Judge the code against the hypothesis at the hypothesis's own level of
specificity.** How much latitude the agent has is set by how tightly the
hypothesis was written, and that is the author's choice, not yours to tighten.

*A specific hypothesis constrains the mechanism.* "An ATR-based momentum breakout
strategy" names the volatility measure, the direction and the entry style. Code
that fades instead of breaking out, or that sizes on a fixed point threshold with
no volatility normalisation, is testing something else — **veto**.

*A general hypothesis constrains only the family.* "An orderflow imbalance
strategy" names a family and nothing else. Any imbalance measure, any horizon,
any entry rule is inside it. Vetoing such a strategy for choosing one particular
imbalance definition would be substituting your judgement for the author's —
**do not**.

*A hypothesis that poses a question has more than one correct answer.* "Does an
outsized move continue, or give it back?" asks which of two things happens. Code
that fades is testing the give-back arm and code that follows through is testing
the continuation arm. **Both are correct implementations. Neither is a mismatch.**
Only ignoring the premise entirely — not conditioning on an outsized move at all —
would be.

**Drift across the campaign's trials is expected and is not a finding.** The agent
refines as it goes; that is the search working. You see the winner, and the only
question is whether the winner is still inside the guardrail the hypothesis set.

So veto on question 1 only when the code tests a **different family** from the one
named, or **ignores a mechanism the hypothesis explicitly required**. Be concrete:
quote what the hypothesis asked for and name what the code does instead. If the
hypothesis is loose enough that almost anything satisfies it, say so in your
confidence note and PASS — a vague hypothesis is a weakness in the
pre-registration, not a fault in the code.

### 2. Is the hypothesis economically plausible at all?

Some hypotheses cannot pay for themselves whatever the signal. Veto if the
proposition is structurally unable to clear its own costs — a per-trade edge
smaller than the spread plus commission, an effect that requires turnover the
instrument cannot absorb, a horizon shorter than the fill model allows.

*(A later version of this loop will generate hypotheses from retrieved papers
rather than from a human. When that happens this question becomes the main one:
a machine-proposed hypothesis has had no human sanity check before it gets here.)*

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
