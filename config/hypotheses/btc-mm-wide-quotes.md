# BTC market making away from the touch: quote wide enough that price tends to come back

Written 2026-11-26, before the campaign that uses it was registered. Drafted for Michael's approval of
its wording. No strategy has run against this question.

## The claim

**On OKX BTC-USDT perpetuals, can a market maker that rests one-lot quotes several ticks away from the
best bid and ask on both sides earn money after fees, because price tends to come back after moving
that far?**

It chooses how far from the touch to quote, may quote wider when the market is volatile and closer when
it is calm, and leans its quotes away from the position it holds, within a 0.10 BTC limit.

**This is market making.** Most of the return should come from resting orders that others trade
against, not from crossing the spread to get in or out.

## Why it might be true, and why it might not

The case for it is that each fill earns the distance from the mid, which is many times one tick, and
that short moves of a few dollars in BTC are often partly reversed. A quote that waits for such a move
is filled near its far end.

The case against it is that a resting order away from the touch is filled mostly when price is moving
toward it, and a move that reaches the quote may keep going. Fills are also far fewer, so the spread
earned on each one has to cover the moves that do not come back.

## What is yours to choose

**How far from the touch to quote**, in ticks or as a fraction of recent movement, and whether the
bid and ask distances differ.

**How volatility sets the distance.** Measure recent movement from trades or the book, keep it in your
own memory, and quote wider when it is high and closer when it is low.

**How inventory leans the quotes**, and whether to cross the spread to reduce a position near the
limit, which pays the taker fee.

**When to requote.** Requoting moves an order to the back of the line at its new price, and a quote far
from the touch does not need to follow every change of the best price.

## The conditions it must survive

Resting maker fills pay 0 basis points. Taker fills pay 2 basis points of notional. These fees are an
assumed professional tier. Funding is charged on positions held across funding times. Orders and cancels
reach the market 1 millisecond after the decision.

Fills happen only on trades. A resting order joins behind the displayed size at its price once that
price becomes the best bid or ask, and moves forward only as trades print there. Your own orders do not
move the book. An order that would take the position, plus same-side working orders, past 1,000 lots
(0.10 BTC) is not sent.

You are not told about fills directly. Infer them from your position and from which order ids are still
working.

## What would make the answer interesting

**That it earns money after all costs**, including inventory marked to market and funding.

**That it holds across ordinary sessions**, including trending days, rather than only on calm days.

**That the distance matters**, so quoting away from the touch does better than quoting at it.

## What this is not asking for

- **Not quoting at the touch.** The question is about resting quotes away from the best bid and ask.
- **Not a directional strategy.** Do not take positions by crossing the spread on a signal and hold
  them for the move.
- **Not a multi-instrument study.** BTC perpetual only.
