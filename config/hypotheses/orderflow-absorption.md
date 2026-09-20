# Does absorption on the 1-minute bar mark a turn?

Written 2026-09-20, before the campaign that uses it was registered.

## The question

Work in **delta** — signed trade flow. For a 1-minute bar, delta is the volume
that traded on the offer minus the volume that traded on the bid, using the
aggressor side the feed reports. Trades whose aggressor is unknown are counted in
volume and in neither side; ignore them.

**Absorption** is delta and price disagreeing. A bar prints a large positive
delta — buyers are lifting the offer hard — and price does not go with it: the
bar fails to take out the prior bar's high, or closes below it, or closes red
outright. Someone is filling that buying without moving. The mirror case is a
large negative delta that fails to make a new low.

The claim under test: **absorption marks a turn.** After a bar where aggressive
flow is absorbed, price is more likely to move against that flow than with it.

## The second half: initiation

Absorption on its own says a push failed. What may make it tradeable is what
comes next — **initiation**, flow in the opposite direction that is efficient
rather than absorbed:

- delta strongly negative, and
- the bar closes near its low, and
- the candle agrees with the delta rather than fighting it

Delta-efficiency is the idea doing the work: the same delta that moved nothing on
the absorption bar now moves price cleanly. Absorption says the old direction is
done; initiation says the new one has started.

Whether to require initiation before entering, how soon after the absorption bar
it must arrive, and what "strongly" and "near the low" mean are open. Different
answers are different strategies and each is its own proposal.

## What "large" should mean here

Not fixed. Delta has to be measured against something the session supplies —
its own recent delta range, the bar's volume, the typical bar of the day — because
a fixed contract count is a different question on a quiet morning than on a
payrolls release. Choosing that normalisation well is part of the work.

The same applies to "fails to go higher": prior bar's high, prior *n* bars' high,
or the session high so far are all defensible and are not the same rule.

## What would make the answer interesting

That it survives costs. Absorption-and-initiation is a two-bar pattern, which
means it can fire often, and an effect that only pays before commission is not an
effect. Entry, exit and holding are part of the claim.

And that it is not one hour of the day. Order flow around the cash open behaves
differently from the overnight session, and a result that lives entirely in one
window is a statement about that window.

## What this is not asking for

- Not a book- or quote-based measure. The feed here is trades with an aggressor
  side; there is no resting-liquidity data.
- Not a multi-instrument study. One instrument, this data.
- Not a regime model. If absorption works in some conditions and not others, that
  is a later and harder question.

## Why this one

Absorption and initiation are standard order-flow vocabulary and the data supports
them directly — the aggressor side is in the feed rather than inferred from an
uptick rule. It is not one of the hypothesis families already falsified on this
data, which are listed under *Falsified hypothesis families* in
`docs/datasets/nq-es-qqq.md`.
