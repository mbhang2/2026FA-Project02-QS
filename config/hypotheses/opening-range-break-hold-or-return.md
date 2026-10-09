# When price leaves the opening range, does the move hold or does the range win?

Written 2026-10-04, before the campaign that uses it was registered. This is the fifth campaign family this project has pointed at the opening range, and the first to leave the direction entirely to the strategy. This revision keeps the question exactly as it was and adds one thing the last attempt left unmeasured: what happens to a departure that is never decided.

## The question

Call the high and low of the first part of the regular-hours session the opening range. Price leaves it, usually more than once, on almost every session.

**When it leaves, does the move hold, or does price come back inside and the range win?**

Two accounts are common and they predict opposite things. **The move holds:** the opening is where overnight information is repriced and the day's participants arrive, so a move out of the range is the day choosing a direction. **The range wins:** the opening is thin and full of orders that had to be done at the open whatever the price, so leaving the range is that flow exhausting itself and price returns to where the size actually is.

**Which one happens is yours to find out, and so is which side to trade.** A rule that only ever buys breakouts and a rule that only ever sells them are both answers. So is a rule that does one in some conditions and the other in others, provided the condition is stated in the rule and not chosen after looking.

## What a rule against the previous version did, and what that asks of this one

The rule traded 559 times across 384 sessions, so the acting constraint held: this is no longer a campaign where nothing happened. More sessions ended down than up — 212 of them — and the profit is carried by a long right tail rather than by the body of the outcome distribution, which is what an opening-range mechanism should look like if it is real at all: most departures go nowhere, a few go a long way. The single best session is 0.04 of the total, so nothing here rests on one day. Two months finished down and the strongest month is several times the weakest, so the result leans on part of the calendar rather than all of it. Tuesday and Thursday carry most of the money and Friday almost none. The earnings sit in the earliest hours of the regular session, which is where a question about the open should pay if it pays anywhere.

That last observation is the one that shapes this revision. The rule's average holding period is well under an hour, which means most of its trades ended before the session had any chance to develop — and a departure that is simply abandoned at a time limit has not been shown to hold or to lose. It has been shown to be undetermined. The previous framing let those cases count silently on one side or the other.

## The one addition: three outcomes, not two

A departure from the range has three possible fates, and the question must say so. **The move holds:** price runs a stated distance in the direction of the departure. **The range wins:** price returns inside the range. **Neither:** the horizon runs out with neither having happened. The third case must be counted separately and reported on its own, not folded into the first two.

This follows openalex-W7214001983, which found that the distance to a level ahead changes how often a trade times out far more than it changes which side wins among trades that are decided. Applied here: a nearby confirmed swing level outside the range may chiefly decide whether a departure is ever settled, and if the rule treats time-outs as range wins or as move holds, the answer this campaign produces is an artifact of its exit convention. State the horizon in the rule. Report decided-one-way, decided-the-other, and undetermined as three counts. And state, as an open question if it cannot be settled, whether a nearby level outside the range suppresses settlement by blocking price or merely marks a quiet stretch — openalex-W7214001983 explicitly does not separate those two, and neither should we pretend to.

## Sharpening one definition, from the same family of ideas

How far price must go before it has "left" the range should be measured against trailing realized volatility computed from prior days of this instrument, not against a fixed number of points (doi-10.2139/ssrn.7364204, which switches a fade on and off by volatility state; we take its measurement, not its switching). This is a way of making "leaving" comparable across calm and loud sessions, and it is a definition, not a regime model.

I considered 2607.20093v1 and took nothing from it: its five signal families are daily-bar retail constructions, and this question is about intraday event mechanics around one specific price area, so there is no piece of it that belongs here.

## Why it might be true, and why it might not

It might be true in one direction or the other because the opening is the one moment whose participants are structurally different from the rest: the orders that must be done at the open are not price-sensitive, and what they do to price has to either persist or unwind.

It might not be true because four campaigns in this family have pointed here and none survived. The honest prior is that there is nothing here; a fifth attempt is justified only because one of the four produced no measurement rather than a negative one.

## What is yours to choose

How long the opening range is. How leaving it is defined, in volatility-scaled terms. Which direction to take and under what condition. Entry, exit, stop, target and size. The horizon at which an undecided departure is called undecided. Whether to act on every session or only on some.

## What is NOT yours to choose

**It must condition on the opening range.** A rule that would behave the same way with the range deleted from it is answering a different question.

**It must act on the ordinary run of sessions.** A rule that acts on a handful of sessions out of three hundred and eighty-four cannot tell us what the opening range does. If the rule needs several conditions to agree before it acts, it is answering a question about those conditions.

**It must be the regular-hours open.** The weekly Globex reopen is a different opening with different participants and is outside the question.

**It must not let an undecided departure speak for either account.**

## What would make the answer interesting

A direction that is consistent across the ordinary run of sessions, that survives being filled at worse prices than the ones that generated the signal, that does not depend on one month, one weekday or one hour of the day, and whose decided trades show the same thing whichever way the time-outs are handled. Either answer is worth having: that the move holds, or that the range wins. What is not worth having is a result that cannot be read because too little happened, or one that is really a measurement of the exit convention.

## What this is not asking for

Not a multi-stage confirmation architecture; simpler entry logic has consistently done better here. Not the overnight or Globex session. Not a model that switches between the two accounts by fitting which is which. Not a search across instruments.

## Why this one

The question survived the last attempt; what changed is that we now know the rule can act, and we can see that its money arrives early and in a tail. The addition is narrow and it displaces nothing: it makes the two accounts answerable rather than leaving the answer hostage to how exits were coded.
