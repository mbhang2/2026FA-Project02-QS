# Aggressive flow continues: signed volume predicts the next move, same direction

Written 2026-09-21, before the campaign that uses it was registered.

## The claim

**When one side trades aggressively, price keeps going that way for a while.**

Concretely: over some interval — a minute, five minutes, a fraction of the
session — measure how much volume traded on the offer against how much traded on
the bid. That difference is signed order flow, and it is on every bar as
`bar.delta`, with `buy_volume` and `sell_volume` beside it. The claim is that a
large positive difference is followed by higher prices, and a large negative one
by lower prices, over the next comparable interval.

**This names a direction, deliberately.** It is not asking whether flow continues
or reverses; it asserts continuation and can be wrong about it. A strategy that
fades large flow is testing the opposite claim and is not an implementation of
this one. The direction was chosen from prior work on this instrument rather than
picked arbitrarily, and if it is wrong the campaign should say so plainly rather
than quietly rotating to the other side.

## Why it might be true, and why it might not

The mechanism usually offered is price impact: a participant who needs to buy
more than the resting offer can absorb keeps lifting, and each lift moves the
price. The information does not arrive all at once, so neither does the buying.

The mechanism against it is that impact is temporary. Whoever supplied liquidity
is compensated for doing so, and price drifts back once the pressure stops. Both
happen; the question is which dominates on this instrument at the horizon you
choose, and whether what is left pays for the trading.

## What is yours to choose

**How to measure "large".** A fixed contract count is a different question on a
quiet morning than on a busy one. Normalise against something the session
supplies — its own recent flow, the bar's total volume, the typical bar of the
day. Choosing well is part of the work, and different choices are different
strategies.

**The horizon.** One minute and fifteen minutes are different claims. Name the one
you are testing and hold it; a rule whose horizon moves between proposals is not
being tested, it is being fitted.

**Entry, exit, holding.** Part of the claim, not details to be added later. The
signal has to survive being filled at prices worse than the ones that generated
it.

## The unknown-aggressor rule

Some trades carry no aggressor side. They are counted in `volume` and in neither
`buy_volume` nor `sell_volume`, and `bar.unknown_volume` reports how much that
was. Do not fold them into one side to make the arithmetic tidy. They are rare
here, and a feature that treats them as buys will not fail any test you write —
it will bias every flow number you compute, permanently and invisibly.

## What would make the answer interesting

**That it survives its own costs.** Flow signals fire often, and something that
pays before commission and not after is not an effect. Commission is 0.10 index
points per side — 0.20 per round turn — and a rule that clears that on paper by a
sliver clears nothing.

**That it is not one hour of the day.** Order flow around the cash open behaves
unlike the overnight session. A result living entirely in one window is a claim
about that window, and should be reported as one.

**That it is there on ordinary sessions**, not only on the handful with the
largest moves in the sample.

## What this is not asking for

- **Not a book- or quote-based measure.** The feed is trades with an aggressor
  side. There is no resting-liquidity data, no depth, no bid-ask.
- **Not a multi-instrument study.** One instrument, this data.
- **Not a regime model.** If flow continues in some conditions and reverses in
  others, that is a later and harder question. Answer the simple version first.
- **Not a fade.** See the claim.

## Why this one

Signed order flow is the input this instrument's data supports directly — the
aggressor side is in the feed rather than inferred from an uptick rule — and
continuation under flow is among the most examined claims in market
microstructure, which makes it a real direction to be wrong in rather than a
novelty. It is not one of the families already falsified on this data; those are
listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md`.
