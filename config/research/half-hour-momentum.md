# Half-hour market intraday momentum: does the first 30 minutes predict the last 30?

## The question

Replicate the "market intraday momentum" effect on NQ futures: test whether the
return over the first 30 minutes of the trading day predicts the return over
the last 30 minutes. The campaign that will answer this runs on the
pre-registered NQ train split with a sign-flip null; the hypothesis you draft
must be a concrete, falsifiable rule that a strategy can express and the
harness can score. The effect, as documented in the equity-momentum
literature, is a same-day continuation: after the first half-hour's return,
the final half-hour tends on average to move the same way. Whether that
continues to hold on real NQ trades at this data's granularity, net of the
spread and commission the harness charges, is the question this campaign
answers.

## What to look for in the papers

- The canonical intraday-momentum result (first half-hour return predicting the
  last half-hour return) and how it is defined: the exact return windows, the
  direction claimed, and the horizon over which it is reported to hold.
- Any U.S. index-futures evidence for or against the effect specifically, and
  the dataset and period it was measured on.
- How the papers handle frictions: whether the effect is reported gross or net
  of costs, and at what trade size. Entry and exit are part of the claim, not
  details added later.
- The likely economic mechanism and any published reason it should have
  weakened or strengthened over time.
- Open questions the papers leave unanswered at intraday horizons on index
  futures, so the draft is additive rather than a re-derivation.

## Constraints on the draft hypothesis

- One instrument: NQ, this data. No cross-instrument search.
- One direction. If the truth is "it depends on the regime", that is a later,
  harder question; answer the simple version first.
- It must survive trading: fills are at the limit or worse, commission is
  charged per side, and the action cap binds the number of trades per session.
- "First 30 minutes" and "last 30 minutes" must be defined against the
  session's own clock, not the wall clock — this data anchors bars to the
  session open, and a strategy cannot know about other sessions' opens.
- Nothing about how an answer would be judged. The papers may speak freely
  about statistics; the draft must not.
- The draft must not re-propose a hypothesis family this project has already
  falsified on this data -- those are listed under *Falsified hypothesis
  families* in `docs/datasets/nq-es-qqq.md`.