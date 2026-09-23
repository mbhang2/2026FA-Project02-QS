# Intraday momentum: does a large move keep going?

## The question

On real NQ index-futures trades, does an intraday price move that is large
relative to that session's own recent behavior keep going over the next bars
(momentum), or does it give the move back (reversal)? The campaign that will
answer this runs on the pre-registered NQ train split with a sign-flip null; the
hypothesis you draft must be a concrete, falsifiable rule that a strategy can
express and that the harness can score.

## What to look for in the papers

- Whether short-horizon intraday momentum or reversal is documented in equity
  index futures specifically, and under what holding horizon the effect holds.
- How the papers operationalize "large": against the session's realised range,
  typical bar size, or time of day. A definition the session itself supplies is
  preferred over a fixed number of points.
- Whether effects are reported before or after costs; entry and exit are part of
  the claim, not details added later.
- Open questions the papers leave unanswered at intraday horizons on index
  futures, so the draft is additive rather than a re-derivation.

## Constraints on the draft hypothesis

- One instrument: NQ, this data. No cross-instrument search.
- One direction. If the truth is "it depends on the regime", that is a later,
  harder question; answer the simple version first.
- It must survive trading: fills are at the limit or worse, commission is
  charged per side, and the action cap binds the number of trades per session.
- Nothing about how an answer would be judged. The papers may speak freely
  about statistics; the draft must not.
- The draft must not re-propose a hypothesis family this project has already
  falsified on this data -- those are listed under *Falsified hypothesis
  families* in `docs/datasets/nq-es-qqq.md`.