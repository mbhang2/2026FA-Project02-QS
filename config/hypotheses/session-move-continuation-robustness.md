# Does an outsized move inside a session continue, or give it back?

Written 2026-09-16, before the campaign that uses it was registered. Revised after the first strategy was examined.

## The question

Within a single trading session, after price has made a move that is large relative to how that session has been moving so far, what happens over the rest of the session? Two accounts are common and they predict opposite things:

- **Continuation.** A large move reflects information arriving, or participants repositioning, and the flow has further to run. Later prices keep going the same way.
- **Give-back.** A large move reflects a temporary imbalance -- someone had to trade, and paid for immediacy. Price drifts back toward where it came from.

Both are plausible, both are widely claimed, and they cannot both describe this instrument at this time scale. Which one does?

## What "large" should mean here

Deliberately not fixed. "Large" has to be measured against something the session itself supplies -- its own realised range so far, the typical size of its moves, the time of day -- because a fixed number of points is a different question on a quiet day than on a busy one, and the answer would be about the number rather than about the market.

Choosing that measure well is part of the work. Different choices are different strategies and each is its own proposal.

## What the first strategy showed, and what the revision must address

The first strategy written against this question produced results that were not spread across the market's ordinary run. Most of what it earned came from a small number of sessions: roughly four-fifths of its total came from Mondays and Thursdays, with Tuesdays and Sundays losing; more than half came from two hours of the day, 14 UTC and 11 UTC, with several other hours negative; and two months, February and April 2025, accounted for most of the total, with April alone the largest month by a wide margin. Most sessions it traded were losing sessions, with the gains concentrated in a minority.

This pattern is not what the question asked for. A claim that only works on particular weekdays, particular hours, or a couple of months is not a claim about what an outsized move does inside a session generally -- it is a claim about those particular times, and the question said the answer would be interesting only if it was consistent across the ordinary run of sessions, in one direction.

The revision therefore requires the strategy to state, before trading, where its edge is expected to live across the session's calendar: which weekdays, which hours, and whether it expects the effect to be present in every month or only in some. If the effect is expected to be uniform, the strategy must say so and the results must show it. If the effect is expected to be concentrated -- for example, only in the hours around a particular session phase -- the strategy must say which hours and why, and the question narrows to that claim. A strategy that finds its edge concentrated after the fact, without having said where it would be, has not answered this question.

The same applies to the size of the move. The question asks about moves that are large relative to the session so far. A strategy that only ever trades the most extreme handful of moves in the sample is answering a question about that handful, not about the ordinary run of large moves. The revision requires the strategy to state what fraction of sessions it expects to trade, and to show that its answer does not depend on a small number of unusually large moves.

## What would make the answer interesting

That the direction is consistent. A story that only works on the largest handful of moves in the sample is a story about a handful of moves. What would be worth knowing is whether the effect is there across the ordinary run of sessions, in one direction, and survives being traded rather than merely observed -- entry, exit and holding are part of the claim, not details to be added later.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a regime model that switches between the two accounts. If the answer is "it depends", that is a later question and a harder one; answer the simple version first.
- Not a fast-execution idea. The claim should survive being filled at prices that are worse than the ones that generated the signal.

## Why this one

It is simple enough to state in a sentence, it has a real direction to be wrong in, and it is not one of the hypothesis families this project has already falsified on this data -- those are listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md` and re-exploring them spends trials on a known answer.

## Papers considered

I considered 2607.20093v1, 2608.00885v1, and 2609.13825v1. None of them bears on this question. 2607.20093v1 is about whether retail signal families work at all on daily bars; this question is about a single intraday mechanism and is not one of those families. 2608.00885v1 is a microstructure mean-reversion model at the scale of seconds; this question is about moves large relative to a session, a different time scale and a different mechanism. 2609.13825v1 is about auction-market-theory features for reinforcement learning on single-name equities; this question is about a directional claim on one futures instrument, and the feature set is not relevant to measuring an outsized move.
