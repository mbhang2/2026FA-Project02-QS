# Does an outsized move inside a session continue, or give it back?

Written 2026-09-16, before the campaign that uses it was registered.

## The question

Within a single trading session, after price has made a move that is large
relative to how that session has been moving so far, what happens over the rest
of the session? Two accounts are common and they predict opposite things:

- **Continuation.** A large move reflects information arriving, or participants
  repositioning, and the flow has further to run. Later prices keep going the
  same way.
- **Give-back.** A large move reflects a temporary imbalance -- someone had to
  trade, and paid for immediacy. Price drifts back toward where it came from.

Both are plausible, both are widely claimed, and they cannot both describe this
instrument at this time scale. Which one does?

## What "large" should mean here

Deliberately not fixed. "Large" has to be measured against something the session
itself supplies -- its own realised range so far, the typical size of its moves,
the time of day -- because a fixed number of points is a different question on a
quiet day than on a busy one, and the answer would be about the number rather
than about the market.

Choosing that measure well is part of the work. Different choices are different
strategies and each is its own proposal.

## What would make the answer interesting

That the direction is consistent. A story that only works on the largest handful
of moves in the sample is a story about a handful of moves. What would be worth
knowing is whether the effect is there across the ordinary run of sessions, in
one direction, and survives being traded rather than merely observed -- entry,
exit and holding are part of the claim, not details to be added later.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a regime model that switches between the two accounts. If the answer is
  "it depends", that is a later question and a harder one; answer the simple
  version first.
- Not a fast-execution idea. The claim should survive being filled at prices
  that are worse than the ones that generated the signal.

## Why this one

It is simple enough to state in a sentence, it has a real direction to be
wrong in, and it is not one of the hypothesis families this project has already
falsified on this data -- those are listed under *Falsified hypothesis families*
in `docs/datasets/nq-es-qqq.md` and re-exploring them spends trials on a known
answer.
