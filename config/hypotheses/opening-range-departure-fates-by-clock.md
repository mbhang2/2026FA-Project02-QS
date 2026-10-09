# When price leaves the opening range, does the move hold or does the range win?

Written 2026-10-04, before the campaign that uses it was registered. This is the fifth campaign family this project has pointed at the opening range and the first to leave the direction entirely to the strategy. The question is unchanged; what this revision adds is what the last rule's evidence says about *when* a departure is decided and *where in the calendar and the clock it pays*.

## The question

Call the high and low of the first part of the regular-hours session the opening range. Price leaves it, usually more than once, on almost every session.

**When it leaves, does the move hold, or does price come back inside and the range win?**

Two accounts are common and they predict opposite things. **The move holds:** the opening is where overnight information is repriced and the day's participants arrive, so a move out of the range is the day choosing a direction. **The range wins:** the opening is thin and full of orders that had to be done at the open whatever the price, so leaving the range is that flow exhausting itself and price returns to where the size actually is.

**Which one happens is yours to find out, and so is which side to trade.** A rule that only ever buys breakouts and a rule that only ever sells them are both answers. So is a rule that does one in some conditions and the other in others, provided the condition is stated in the rule and not chosen after looking.

## What the last rule did, and what this revision has to answer

The rule traded 362 times across 384 sessions, and more sessions finished down than up — 235 of them. The profit is carried by a right tail: skew 1.679, kurtosis 7.99, the top 5% of trades supply 39% of the profit, and the best session is 0.05 of the total. That shape is what an opening-range mechanism should look like if it is real at all: most departures go nowhere, a few go a long way. A tail is not by itself an objection.

Two things in the concentration report are. Three months — 2025-04 at 1161.55, 2025-11 at 896.8, 2024-08 at 720.15 — together account for about 2778 of the 2840.6 net, so the result leans on part of the calendar. And the by-hour report puts the money at 16 UTC (2992.1) and 17 UTC (3757.95) and the losses at 14 UTC (-2454.1) and 15 UTC (-2503.4): drop hour 16 alone and the total goes to -152, drop hour 17 alone and it goes to -917. Whatever the rule found, it found it inside one two-hour stretch of the clock while trading other hours and losing in them. Two of the four auditors did not run, so what they were asked remains open here.

## The three fates

A departure from the range has three possible fates and the question must say so. **The move holds:** price runs a stated distance in the direction of the departure. **The range wins:** price returns inside the range. **Neither:** the horizon runs out with neither having happened. The third case is counted separately and reported on its own, never folded into the first two.

This follows openalex-W7214001983 (also listed as openalex-W7214046383; it is one paper), which found that the distance to a level ahead changes how often a trade times out far more than it changes which side wins among trades that are decided. Applied here: a nearby confirmed swing level outside the range may chiefly decide whether a departure is ever settled. State the horizon in the rule. Report decided-one-way, decided-the-other, and undetermined as three counts. Keep the paper's own limit as well: it does not separate a level that blocks price from a stretch that was merely quiet, and neither should we.

## The clock: when the decision arrives

The previous framing read money arriving early as the opening speaking. The by-hour report says otherwise: the settled part of the outcome sits a few hours into the regular session, and the first hours lose. Two readings fit, and the question must not choose between them silently. One: a departure needs time to be settled, and the hours that pay are the ones in which the day's move either runs or comes back — which the move-holds account predicts if repricing takes a session to work out. Two: the rule is simply exposed to a stretch of the day and the opening range is incidental.

So the rule must record how long after the regular-hours open each departure began and how long it took to be decided, and the three fates must be reported by time band of the session as well as in total. The answer must also survive the loss of any one clock hour or any one month: if the two accounts can only be told apart inside a single two-hour stretch, that is a statement about the clock, and it should be written down as one rather than read as a statement about the opening.

## Sharpening what is measured

How far price must go before it has "left" the range should be measured against trailing realized volatility computed from prior days of this instrument, not against a fixed number of points (doi-10.2139/ssrn.7364204, which switches a fade on and off by volatility state; we take its measurement, not its switching). This makes "leaving" comparable across calm and loud sessions. It is a definition, not a regime model.

## Why it might be true, and why it might not

It might be true in one direction or the other because the opening is the one moment whose participants are structurally different from the rest: the orders that must be done at the open are not price-sensitive, and what they do to price has to either persist or unwind.

It might not be true because four campaigns in this family have pointed here and none survived. The honest prior is that there is nothing here.

## What is yours to choose

How long the opening range is. How leaving it is defined, in volatility-scaled terms. Which direction to take and under what condition. Entry, exit, stop, target and size. The horizon at which an undecided departure is called undecided. Which part of the session to act in, and why.

## What is NOT yours to choose

**It must condition on the opening range.** A rule that would behave the same way with the range deleted from it is answering a different question.

**It must act on the ordinary run of sessions.** A rule that acts on a handful of sessions out of 384 cannot tell us what the opening range does.

**It must be the regular-hours open.** The weekly Globex reopen is a different opening with different participants.

**It must not let an undecided departure speak for either account.**

**It must not buy its answer by dropping a clock hour it traded and lost in.** If the rule wants to act in only part of the session, say so before it acts and connect that part to the opening-range mechanism.

## What would make the answer interesting

A direction that is consistent across the ordinary run of sessions, that survives being filled at worse prices than the ones that generated the signal, and whose decided trades show the same thing whichever way the time-outs are handled and whichever single clock hour or single month is set aside. Either answer is worth having: that the move holds, or that the range wins. What is not worth having is a result that cannot be read because too little happened, or one that is really a measurement of the exit convention or of the clock.

## What this is not asking for

Not a multi-stage confirmation architecture; simpler entry logic has consistently done better here. Not the overnight or Globex session. Not a model that switches between the two accounts by fitting which is which. Not a search across instruments.

## Why this one

The question survived the last attempt; what changed is that the outcome is settled in a narrow stretch of the clock and a narrow part of the calendar. The additions are narrow: three fates, counted by time band and by month, and volatility-scaled leaving.
