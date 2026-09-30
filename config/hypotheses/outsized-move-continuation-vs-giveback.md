# Does an outsized move inside a session continue, or give it back?

Written 2026-09-16; revised after the first strategy built against this question was audited.

## The question

Within a single trading session, after price has made a move that is large relative to how that session has been moving so far, what happens over the rest of the session? Two accounts are common and they predict opposite things:

- **Continuation.** A large move reflects information arriving, or participants repositioning, and the flow has further to run. Later prices keep going the same way.
- **Give-back.** A large move reflects a temporary imbalance -- someone had to trade, and paid for immediacy. Price drifts back toward where it came from.

Both are plausible, both are widely claimed, and they cannot both describe this instrument at this time scale. Which one does?

## What "large" should mean here

Deliberately not fixed. "Large" has to be measured against something the session itself supplies -- its own realised range so far, the typical size of its moves, the time of day -- because a fixed number of points is a different question on a quiet day than on a busy one, and the answer would be about the number rather than about the market.

Choosing that measure well is part of the work. Different choices are different strategies and each is its own proposal.

## What the first attempt revealed, and what this revision therefore demands

A strategy written against the original framing earned most of its money in a narrow slice of its own activity. One month contributed roughly a quarter of all profit. One session contributed more than an eighth. Two hours of the trading day contributed roughly two-fifths of all profit between them, while several other hours lost money, and one weekday contributed less than a tenth of the best weekday's total. The distribution of per-session outcomes was heavily right-skewed, with a small number of sessions dominating the total.

This pattern is not itself an answer to the question. But it tells us what the next proposal must do if the answer is to be believed as a property of the mechanism rather than as a property of a few sessions:

- **State where in the session the mechanism is claimed to operate.** If the effect is expected to be concentrated in particular hours, the question must say which hours and why the mechanism predicts that. The original framing asked about "inside a session" without saying whether the claim was meant to hold across the whole session or only in part of it. That ambiguity is now closed: the revision must say.

- **State whether the claim is meant to hold across the full run of sessions.** The original framing said the answer would be interesting only if the effect is there across the ordinary run of sessions. The first strategy's results were consistent with the opposite -- a small number of sessions doing most of the work. The revision therefore requires the strategy to report, as part of its own evidence, how much of its total outcome comes from its best single session and its best single month, and to say whether that concentration is consistent with the mechanism it proposes. A mechanism that predicts the effect should be spread across sessions cannot be supported by evidence in which it is not.

- **Keep the direction consistent.** The weekday and hour concentration in the first strategy's results is not evidence for either account until the mechanism says where and when it should operate. If the mechanism cannot say, the concentration is a warning that the result may be about particular sessions rather than about the move.

## What would make the answer interesting

That the direction is consistent. A story that only works on the largest handful of moves in the sample is a story about a handful of moves. What would be worth knowing is whether the effect is there across the ordinary run of sessions, in one direction, and survives being traded rather than merely observed -- entry, exit and holding are part of the claim, not details to be added later.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a regime model that switches between the two accounts. If the answer is "it depends", that is a later question and a harder one; answer the simple version first.
- Not a fast-execution idea. The claim should survive being filled at prices that are worse than the ones that generated the signal.
- Not an effect discovered by peeking at where the first strategy made money and then redefining the question around those hours. If the revision narrows the session window, the narrowing must be justified by the mechanism -- by what the continuation or give-back account predicts about when flows arrive or when imbalances unwind -- not by the first strategy's profit by hour.

## Why this one

It is simple enough to state in a sentence, it has a real direction to be wrong in, and it is not one of the hypothesis families this project has already falsified on this data -- those are listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md` and re-exploring them spends trials on a known answer.

## Open question carried forward

The first strategy's results were concentrated in hours and months for which the original question offered no explanation. Whether that concentration reflects the mechanism or the sample is exactly what the next proposal must address, and it may turn out that the honest answer is that the effect, if it exists, is not spread across the ordinary run of sessions. That would be a real finding about the question, and it is better to say so than to dress up a few sessions as a general claim.
