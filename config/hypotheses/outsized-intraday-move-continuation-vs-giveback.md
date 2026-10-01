# Does an outsized move inside a session continue, or give it back?

Written 2026-09-16; revised after the first attempt at an answer.

## The question

Within a single trading session, after price has made a move that is large relative to how that session has been moving so far, what happens over the rest of the session? Two accounts are common and they predict opposite things:

- **Continuation.** A large move reflects information arriving, or participants repositioning, and the flow has further to run. Later prices keep going the same way.
- **Give-back.** A large move reflects a temporary imbalance — someone had to trade, and paid for immediacy. Price drifts back toward where it came from.

Both are plausible, both are widely claimed, and they cannot both describe this instrument at this time scale. Which one does?

## What "large" should mean here

Deliberately not fixed. "Large" has to be measured against something the session itself supplies — its own realised range so far, the typical size of its moves, the time of day — because a fixed number of points is a different question on a quiet day than on a busy one, and the answer would be about the number rather than about the market.

Choosing that measure well is part of the work. Different choices are different strategies and each is its own proposal.

## What the first attempt at an answer showed

A strategy written against this question traded 2,430 times across 384 sessions and lost 610.75 index points net. The loss was not spread evenly. Every weekday was negative, and only four months were positive: 2024-11, 2025-06, 2025-11, and 2025-12. One of those months, 2025-11, contributed 53.60 points, which is more than twice the next best positive month. Removing that single month makes the overall result more negative by roughly the size of the month itself. The best single session was about 2.6 percent of total profit. The hourly pattern was no more even: hour 04 was the only hour that showed a gain of more than a few points, while most other hours lost.

This is not a picture of an effect that is present across the ordinary run of sessions. It is a picture of a strategy whose results are carried by a small number of sessions and a small number of hours, and whose losses are otherwise broad.

That does not settle the question. It sharpens it. A claim that an outsized move continues, or gives back, should not depend on a handful of sessions. If the effect is real, it should be visible in the ordinary run of sessions, not only in the largest ones. If it is only visible in the largest ones, then the question is not about outsized moves in general but about a specific kind of extreme session, and the question should say so.

## What the revision must address

A strategy written against this question must answer three things the first attempt left open:

- **Where in the day the effect lives.** The first attempt's results were strongly concentrated in specific hours, and the hours were not adjacent in a way that suggests a single mechanism. A strategy that is long in one hour and short in the next is not expressing a view about continuation or give-back. It is expressing two different views. The question asks about one mechanism. The strategy should either state why the mechanism should be present at some hours and absent at others, or restrict itself to the hours where the mechanism is claimed to operate, and say why those hours.

- **Whether the effect is a property of the session or of the month.** Only four of eighteen months were positive. The strategy should not be one that only makes money in a few months. If the mechanism is real, it should be present across the sample, not concentrated in a few months. If it is only present in a few months, then the question is not about outsized moves in general but about a specific regime, and the question should say so.

- **Whether the effect is a property of the session or of the weekday.** Every weekday was negative in the first attempt. A mechanism that is real should not be uniformly absent on every day of the week. If the mechanism is claimed to operate only on certain days, the strategy should say which days and why the mechanism would be present then and absent otherwise.

These are not requirements to make the strategy profitable. They are requirements to make the strategy an answer to the question. A strategy that is profitable only in November 2025 is not answering whether outsized moves continue or give back. It is answering whether outsized moves continued or gave back in November 2025.

## What would make the answer interesting

That the direction is consistent. A story that only works on the largest handful of moves in the sample is a story about a handful of moves. What would be worth knowing is whether the effect is there across the ordinary run of sessions, in one direction, and survives being traded rather than merely observed — entry, exit and holding are part of the claim, not details to be added later.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a regime model that switches between the two accounts. If the answer is "it depends", that is a later question and a harder one; answer the simple version first.
- Not a fast-execution idea. The claim should survive being filled at prices that are worse than the ones that generated the signal.

## Why this one

It is simple enough to state in a sentence, it has a real direction to be wrong in, and it is not one of the hypothesis families this project has already falsified on this data — those are listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md` and re-exploring them spends trials on a known answer. The first attempt at an answer did not fail because the question was wrong. It failed because the strategy did not respect the question's own standard: that the effect be present across the ordinary run of sessions, in one direction. That standard remains.
