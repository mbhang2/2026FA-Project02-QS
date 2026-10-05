# When price leaves the opening range, does the move hold or does the range win?

Written 2026-10-04, before the campaign that uses it was registered. The question
is Michael's; this file states it. It is the fifth campaign family this project
has pointed at the opening range and the first to leave the direction entirely to
the strategy.

## The question

Call the high and low of the first part of the regular-hours session the opening
range. Price leaves it, usually more than once, on almost every session.

**When it leaves, does the move hold, or does price come back inside and the
range win?**

Two accounts are common and they predict opposite things. **The move holds:** the
opening is where overnight information is repriced and the day's participants
arrive, so a move out of the range is the day choosing a direction. **The range
wins:** the opening is thin and full of orders that had to be done at the open
whatever the price, so leaving the range is that flow exhausting itself and price
returns to where the size actually is.

**Which one happens is yours to find out, and so is which side to trade.** A rule
that only ever buys breakouts and a rule that only ever sells them are both
answers to this question. So is a rule that does one in some conditions and the
other in others, provided the condition is stated in the rule and not chosen
after looking.

## Why it might be true, and why it might not

It might be true in one direction or the other because the opening is the one
moment of the session whose participants are structurally different from the
rest: the orders that must be done at the open are not price-sensitive, and
whatever they do to price has to either persist or unwind.

It might not be true because this project has asked four questions in this family
and none of them survived. Two asked whether a thirty-minute break continues:
their best candidates scored +0.038 and +0.037 against a gate that put both near
the top of their own null. One asked continuation-or-failure and produced no
measurement at all. One asked whether the short side fades the move, found +0.125, and was vetoed
for drawing three quarters of its profit from Sundays and more than half from a
single month. The honest prior is that there is nothing here, and a fifth attempt
is justified only because the third produced no measurement rather than a
negative one.

## What is yours to choose

How long the opening range is. How "leaving" it is defined. Which direction to
take and under what condition. Entry, exit, stop, target and size. Whether to act
on every session or only on some.

## What is NOT yours to choose

**It must condition on the opening range.** A rule that would behave the same way
with the range deleted from it is answering a different question.

**It must act on the ordinary run of sessions, not on a handful.** This is the
constraint the family has failed on and it is part of the question, not a detail.
The third campaign's candidates traded on between twenty and thirty-nine sessions
out of three hundred and eighty-four, and a rule that acts five times in a
hundred opportunities cannot tell us what the opening range does -- there is
nothing to measure. If your rule needs four conditions to agree before it acts,
it is answering a question about those four conditions.

**It must be the regular-hours open.** The fourth campaign's best candidate
earned almost everything it earned in two hours of Sunday evening, which is the
weekly Globex open and not the session open this question is about. A rule that
trades the weekly reopen is outside the question.

## What would make the answer interesting

A direction that is consistent across the ordinary run of sessions, survives
being filled at worse prices than the ones that generated the signal, and does
not depend on one month, one weekday or one hour of the day. Either answer is
worth having: that the move holds, or that the range wins. What is not worth
having is a fifth result that cannot be read because too little happened.

## What this is not asking for

- Not a multi-stage confirmation architecture. This data has already shown those
  widen the effective stop and degrade performance, and simpler entry logic has
  consistently done better.
- Not the overnight or Globex session, and not the weekly reopen.
- Not a regime model that switches between the two accounts by fitting which
  regime is which.
- Not a search across instruments. One instrument, this data.

## Why this one

Four campaigns have pointed at the opening range and the one that asked this
exact question measured nothing, because its candidates barely traded. The
question survives; what failed was the shape of the attempt. This version leaves
the direction open, says in the question itself that a rule has to act, and rules
out the two things that have already eaten a campaign each: elaborate gating, and
the Sunday reopen.
