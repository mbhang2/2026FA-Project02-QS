# When price leaves the opening range, does the move hold or does the range win?

Refined again after the campaign that ran against the second version of this question. The question is Michael's; this file states it. It is still the same question, and this version tightens what the previous two campaigns resolved badly.

## The question

Call the high and low of the first part of the regular-hours session the opening range. Price leaves it, usually more than once, on almost every session.

**When it leaves, does the move hold, or does price come back inside and the range win?**

Two accounts are common and they predict opposite things. **The move holds:** the opening is where overnight information is repriced and the day's participants arrive, so a move out of the range is the day choosing a direction. **The range wins:** the opening is thin and full of orders that had to be done at the open whatever the price, so leaving the range is that flow exhausting itself and price returns to where the size actually is.

**Which one happens is yours to find out, and so is which side to trade.** A rule that only buys breakouts and a rule that only sells them are both answers. So is a rule that does one in some stated conditions and the other in others.

## What the last campaign's strategy showed, and what this revision demands

The rule acted: 4,528 trades over 384 sessions, 253 of which had any exposure at all, and 196 of those lost money. Net 274.65 index points on a gross of 1,187.75, with commission alone taking 913.1 — roughly 77% of gross. Per-trade gross edge is 0.262 points against 0.202 points of commission per round trip. NQ's minimum tick is 0.25 points, and the fills were modelled with no spread at all. So the first demand is economic: **the rule must clear a round-trip cost that names a spread on top of commission, not commission alone.** An answer that survives zero-spread fills and dies at one tick of spread is not an answer. The second demand is that the exit do what the rule says: the reported holding period was close to 834 seconds against a stated ten-minute deadline. Report realized holding against the stated deadline, and let the reader see whether the deadline binds.

The money arrived in a few places again, in different places than last time. Hour 15 UTC contributed 938.2 points and hour 16 contributed 609.75; 2024-11 contributed 876.55 and 2025-01 contributed 846.10 while 2025-06 lost 975.2; Thursday alone contributed 1,082.6 while Monday lost 706.9 and Friday lost 1,011.2; the best nineteen or so sessions contributed about 583% of net, meaning the ordinary run of sessions lost money. **The answer must be readable in the ordinary session and across the ordinary run of the calendar.** If the mechanism is expected to bite only at certain hours or on certain days, the rule states that up front and says why the opening range is expected to matter there. An opening range is an object of the session's first part; a position whose money arrives hours later owes an account of what it held and why that is still an answer about the range. What is not wanted is a total carried by two months, two hours, one weekday, or a handful of sessions while everything ordinary does the opposite.

## Sharpening what stays loose

**How large a move counts as leaving.** State it: measured against the width of the opening range, or against how much this market has been moving recently. The latter is worth doing — a band built from trailing realized volatility over the days before the session, never from anything forward-looking (doi-10.2139/ssrn.7364204, adapting its band from implied to historical realized volatility). Conditioning on that level is permitted as a stated condition; what stays off-limits is a model that learns which regime is which.

**How an exit resolves.** Every position gets a stated deadline, and the report separates three outcomes: price came back inside the range, price ran to a stated target, and the deadline simply expired. They are different events and the answer should say which one produced the profit. Carry the distance to the nearest confirmed swing level ahead of the entry as a stated condition (openalex-W7214001983): across its layers that distance moved how often a trade was decided at all, from 0.3774 down to 0.1058, while the win rate among decided trades moved only from 0.4906 to 0.4661. In other words a level ahead changes whether the exit resolves, not which side it resolves to — so if the fade-back-to-the-range outcome looks strong only where few trades expire, that is the level, not the range. That paper's own open question belongs here too: a nearby swing level also marks a stretch of small movement, and it did not separate the two. Note also that its numbers carry no costs and are not trading results.

## Why it might be true, and why it might not

It might be true because the opening is the one moment of the session whose participants are structurally different from the rest: the orders that must be done at the open are not price-sensitive, and whatever they do to price has to either persist or unwind.

It might not be true because this project has asked questions in this family and none of them survived. The honest prior is that there is nothing here. Two open questions to carry inside the revision: the concentration above is not explained by one weekday or the weekly reopen, so it must be explained by when this mechanism actually bites, or the mechanism is smaller than it looks; and the previous rule's profit was smaller than a plausible spread, so whatever effect exists may be too small to trade at all.

## What is yours to choose

How long the opening range is. How leaving it is defined, and against what. Which direction, and under what stated condition. Entry, exit, stop, target, size, the spread assumed on every fill, and the deadline at which a position is called unresolved. Whether to act on every session or only on stated ones.

## What is NOT yours to choose

**It must condition on the opening range.** A rule that would behave the same way with the range deleted is answering a different question.

**It must act on the ordinary run of sessions.** A rule that acts on a handful of sessions cannot tell us what the opening range does.

**It must be the regular-hours open.** Not the weekly Globex reopen.

**It must not be carried by its extremes,** and it must clear its stated round-trip cost.

## What would make the answer interesting

A direction consistent across the ordinary sessions and ordinary hours, surviving fills worse than the ones that generated the signal, whose typical session looks like its total rather than opposing it. Either answer is worth having: that the move holds, or that the range wins.

## What this is not asking for

- Not a multi-stage confirmation architecture; simpler entry logic has done better here.
- Not the overnight or Globex session, and not the weekly reopen.
- Not a regime model that switches between the two accounts by learning which regime is which.
- Not a search across instruments. One instrument, this data.

## Papers considered and not used

arXiv 2607.20093v1 still offers nothing to add: daily-bar retail signal families on other instruments, and this question is about one intraday event on one market. openalex-W7214046383 is the same work as openalex-W7214001983 and is used once.

This revision is not much longer than its last version; the extra length buys the spread demand and the three-way exit decomposition, which the previous candidates resolved by assuming both away.
