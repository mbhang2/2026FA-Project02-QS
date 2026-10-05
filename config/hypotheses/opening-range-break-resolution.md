# When price leaves the opening range, does the move hold or does the range win?

Revised after the campaign that ran against the first version of this question. The question is Michael's; this file states it, and this version says out loud what the first version left to the strategy's taste.

## The question

Call the high and low of the first part of the regular-hours session the opening range. Price leaves it, usually more than once, on almost every session.

**When it leaves, does the move hold, or does price come back inside and the range win?**

Two accounts are common and they predict opposite things. **The move holds:** the opening is where overnight information is repriced and the day's participants arrive, so a move out of the range is the day choosing a direction. **The range wins:** the opening is thin and full of orders that had to be done at the open whatever the price, so leaving the range is that flow exhausting itself and price returns to where the size actually is.

**Which one happens is yours to find out, and so is which side to trade.** A rule that only ever buys breakouts and a rule that only ever sells them are both answers to this question. So is a rule that does one in some conditions and the other in others, provided the condition is stated in the rule and not chosen after looking.

## What the first version's strategy showed, and what this revision takes from it

The rule acted: 2,627 trades over 384 sessions. The family's old failure mode — a rule that barely traded — is not what happened this time. What happened is that the money arrived in a few places. Roughly 44% of all profit came from 2025-04 alone; hours 14-17 UTC together contributed about 60%, with 17 UTC alone near 27%; the top 5% of sessions delivered close to half the profit while 225 of 384 sessions lost money; the session-return shape is far from balanced, with skew 5.56 and kurtosis 57.5.

So the question now carries a second demand alongside the first. **The answer must be readable in the ordinary session and across the ordinary run of the calendar.** If the effect only appears in one slice of the day or one stretch of months, that slice is a condition the rule states up front, and the rule must say why the mechanism is expected to appear there. An opening range is an object of the session's first part; a candidate whose money arrives hours later owes an account of what it held and why holding it that long is still an answer about the range. What is not wanted is a total carried by a handful of extreme sessions while most sessions do something ordinary and opposite.

## Sharpening what the first version left loose

**How large a move counts as "leaving."** The first version let the strategy choose this silently. State it explicitly: whether the move out is measured against the width of the opening range itself, or against how much this market has been moving recently. The latter is worth doing — a band built from trailing realized volatility over the days before the session, not from anything forward-looking (doi-10.2139/ssrn.7364204, adapting its band from implied to historical realized volatility). Conditioning on that level is permitted as a stated condition; what remains off-limits is a model that fits which regime is which.

**How an exit resolves.** Give every position a stated deadline, and report separately the outcomes that resolved inside it from the ones that did not. In the work behind openalex-W7214001983, a level sitting ahead of the target moved whether a trade was decided at all — time-outs swung far across its layers — while the win rate among decided trades barely moved. A fade-back to the range and a drift that simply expires are different events and the answer should say which one produced the profit.

## Why it might be true, and why it might not

It might be true because the opening is the one moment of the session whose participants are structurally different from the rest: the orders that must be done at the open are not price-sensitive, and whatever they do to price has to either persist or unwind.

It might not be true because this project has asked questions in this family and none of them survived. The honest prior is that there is nothing here. The one open question worth carrying inside the revision: the concentration above cannot be explained away as a fluke of one weekday or the weekly reopen, so it must be explained by when this mechanism actually bites — or the mechanism is smaller than it looks.

## What is yours to choose

How long the opening range is. How "leaving" it is defined, and against what. Which direction to take and under what stated condition. Entry, exit, stop, target, size, and the deadline at which a position is called undecided. Whether to act on every session or only on stated ones.

## What is NOT yours to choose

**It must condition on the opening range.** A rule that would behave the same way with the range deleted from it is answering a different question.

**It must act on the ordinary run of sessions.** A rule that acts on a handful of sessions cannot tell us what the opening range does — there is nothing to measure.

**It must be the regular-hours open.** Not the weekly Globex reopen.

**It must not be carried by its extremes.** An answer whose profit depends on one month, one hour, or a few sessions is not an answer about what price does when it leaves the range.

## What would make the answer interesting

A direction consistent across the ordinary run of sessions and the ordinary hours of the session, that survives being filled at worse prices than the ones that generated the signal, and whose typical session looks like its total rather than opposing it. Either answer is worth having: that the move holds, or that the range wins.

## What this is not asking for

- Not a multi-stage confirmation architecture; simpler entry logic has consistently done better here.
- Not the overnight or Globex session, and not the weekly reopen.
- Not a regime model that switches between the two accounts by learning which regime is which.
- Not a search across instruments. One instrument, this data.

## Papers considered and not used

arXiv 2607.20093v1 was read and offers nothing to add: it concerns daily-bar retail signal families on other instruments, and this question is about one intraday event on one market.

This revision is longer than its first version; the extra length buys explicit statements of how a move is sized and how an exit resolves, which the first version left open and which the previous candidates resolved badly.
