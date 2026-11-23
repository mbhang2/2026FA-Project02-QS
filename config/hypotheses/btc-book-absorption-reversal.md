# BTC order-book absorption: when displayed liquidity holds against aggressive flow, price turns

Written 2026-11-23, before the campaign that uses it was registered. No strategy has
read the real OKX book for this question.

## The claim

**After a directional move on the BTC perpetual, when aggressive flow runs into displayed
liquidity that holds, price is more likely to reverse than to continue.**

Work with two things together: delta, which is aggressive buy volume minus aggressive sell
volume, and the displayed book from `ctx.book()`.

**Absorption is flow failing against visible size.** After price has risen, aggressive
buying is unusually large, the best ask does not move up, and displayed ask size remains
large or refills. After price has fallen, aggressive selling is unusually large, the best
bid does not move down, and displayed bid size remains large or refills.

A short entry follows absorbed buying after a rise. A long entry follows absorbed selling
after a fall.

**This names a direction.** A strategy that trades with the absorbed flow is testing the
opposite claim and is not an implementation of this one.

## Why it might be true, and why it might not

The case for it is that a large passive participant can stop a move. Once late aggressors
are trapped, their exits can add to the turn.

The case against it is that displayed size is not a commitment. It can be cancelled before
it trades, and a level that looks strong can disappear as price reaches it. Strong trends
also pause without turning.

## What is yours to choose

**The prior move.** Define it over a window you name, using only data already seen.

**"Unusually large" flow.** Normalise against recent flow or recent total volume.

**"Holds" and "refills".** You may compare the best level or several visible levels with
their recent history. Keep your own rolling state, because `ctx.book()` returns only the
current visible row.

**Confirmation, entry and exit.** You may enter at the signal or wait for confirmation.
Choose market or resting limit orders deliberately, because they face different fills.

## How each trade must end

**Each trade opens from flat and closes back to flat within the same UTC day.** Do not
reverse directly from long to short or from short to long. If a new signal opposes an open
position, close the position first and treat the new entry as a separate trade.

Trade on most days. A rule that fires only on a few unusual days cannot be evaluated here.

## The conditions it must survive

Resting maker fills pay 0 basis points. Taker fills pay 2 basis points of notional, so a
round trip using market orders costs 4 basis points before spread and depth. These fees are
an assumed professional tier. Funding is charged on positions held across funding times.
Orders reach the market 1 millisecond after the decision.

`ctx.book()` shows the top five levels from the latest row before your current millisecond.
It can return `None`, so handle that. It does not show order identities or your queue
position.

## What would make the answer interesting

**That it survives its costs.** A reversal that pays before fees and not after them is not
a tradable effect here.

**That it is not one hour of the day**, and **that it is present across ordinary sessions**
rather than only on the few days with the largest moves.

## What this is not asking for

- **Not market making.** Do not quote both sides or earn the spread as the main source of
  return.
- **Not a multi-instrument study.** BTC perpetual only.
- **Not a regime model.** Answer the simple version first.
- **Not a continuation strategy.** See the claim.
