# BTC market making: shift the quotes with order flow, so a trend does not run you over

Written 2026-11-25, before the campaign that uses it was registered, in wording Michael
approved. No market-making strategy has run on the real OKX book.

## The claim

**On OKX BTC-USDT perpetuals, can a market maker that quotes one lot at the best bid and ask earn
money after fees by shifting its quotes with order flow?**

When recent aggressive buying outweighs selling and the visible book leans toward the bid, it keeps
its bid at the touch and moves its ask back from the best ask or removes it, and the reverse when
selling dominates. It also leans its quotes away from the position it holds, within a 0.10 BTC
limit.

Work with two things together:

- **Order flow:** aggressive buy volume minus aggressive sell volume over a recent window, as a
  running total you keep yourself. Every trade event tells you its aggressor side and size.
- **The visible book:** how its size is split between the bid and ask, at the touch or across the
  five visible levels.

**This is market making.** Most of the return should come from resting orders that others trade
against, not from predicting direction and crossing the spread to get in or out.

## Why it might be true, and why it might not

The case for it is that adverse selection is the main cost of quoting. A quote that steps back from
the side facing one-way flow is filled less often just before price moves against it, while still
earning the spread when flow is balanced.

The case against it is that the spread is usually one tick, so each fill earns very little. Your
order also joins behind everyone already quoting at that price, so it may fill mostly when price is
about to move through it. Order flow and book imbalance may also lag the moves they are meant to
anticipate.

## What is yours to choose

**The order-flow window and how it fades.** Keep it in your own memory, because each call sees only
the current event.

**How book imbalance is measured.** The touch alone, several levels, or a mix.

**How the two signals combine, and how far to move a quote.** When to keep a side at the touch, how
many ticks to move it back, and when to remove it.

**How inventory leans the quotes.** How strongly your position pushes quotes away from it, and
whether to cross the spread to reduce it near the limit, which pays the taker fee.

**When to requote.** Book updates arrive every millisecond the book changes, and trades between them.
Requoting too often costs you your place in line.

## The conditions it must survive

Resting maker fills pay 0 basis points. Taker fills pay 2 basis points of notional. These fees are an
assumed professional tier. Funding is charged on positions held across funding times. Orders and
cancels reach the market 1 millisecond after the decision.

Fills happen only on trades. A resting order joins behind the displayed size at its price and moves
forward only as trades print there. Your own orders do not move the book. An order that would take
the position, plus same-side working orders, past 1,000 lots (0.10 BTC) is not sent.

You are not told about fills directly. Infer them from your position and from which order ids are
still working.

## What would make the answer interesting

**That it earns money after all costs**, including inventory marked to market and funding, and not
only before them.

**That it holds across ordinary sessions**, including trending days, rather than only on quiet days.

**That the order-flow shift matters**, so it does better than quoting both sides at the touch with
inventory leaning alone.

## What this is not asking for

- **Not a directional strategy.** Do not take positions by crossing the spread on a signal and hold
  them for the move.
- **Not a multi-instrument study.** BTC perpetual only.
- **Not a regime model.** Answer the simple version first.
