# BTC absorption reversal: when aggressive flow stops moving price, the trend turns

Written 2026-11-22, before the campaign that uses it was registered. No strategy has
been run on this question.

## The claim

**After a directional move on the BTC perpetual, when aggressive flow in that direction
is absorbed, price is more likely to reverse than to continue.**

Work in delta: on each bar, the volume bought by aggressors minus the volume sold by
aggressors, available as `bar.delta`, `buy_volume` and `sell_volume`.

**Absorption is delta and price disagreeing at the end of a move.** After price has
risen, a bar prints unusually large positive delta and price fails to extend: it does
not take out the recent high, or it closes back down. Someone is filling that buying
without letting price move. The mirror case comes after a fall: unusually large
negative delta that fails to make a new low.

A long entry follows absorbed selling after a fall. A short entry follows absorbed
buying after a rise.

**This names a direction.** A strategy that trades with the absorbed flow is testing
the opposite claim and is not an implementation of this one.

## Why it might be true, and why it might not

The case for it is that a large passive participant can stop a move by absorbing the
aggressive side. Once late aggressors are trapped, their exits can add to the turn.

The case against it is that heavy flow without movement may only mean that displayed
liquidity was large at that moment. Strong trends also pause often, so a failed bar can
be a rest rather than a turn.

## What is yours to choose

**The prior move.** Define it over a window you name, using only data already seen.

**"Unusually large" delta.** BTC volume differs widely across hours and years, so
normalise against recent flow or recent total volume.

**"Fails to extend".** The prior bar's extreme, a recent extreme and a close back inside
the range are different rules. Choose one and hold it within a strategy.

**Confirmation.** You may enter on the absorption bar or wait for later flow in the new
direction. Each choice is a different strategy.

**Entry, exit and holding.** These are part of the claim. Choose market or resting limit
orders deliberately, because they face different fills.

## How each trade must end

**Each trade opens from flat and closes back to flat within the same UTC day.** Do not
reverse directly from long to short or from short to long. If a new signal opposes an
open position, close the position first and treat the new entry as a separate trade.

Trade on most days. A rule that fires only on a few unusual days cannot be evaluated
here.

## The conditions it must survive

Each side pays a fee of 5 basis points of notional, so a round trip costs 10 basis points
before spread and depth. Funding is charged on positions held across funding times.
Orders reach the market 100 milliseconds after the decision.

Fills use the OKX top-five order book. A market order pays through the visible levels and
is refused if it needs more than five. A resting limit order joins behind the displayed
size and fills only when trades reach it. **Your strategy does not see the book.** Here,
absorption is inferred from trades and bars.

## What would make the answer interesting

**That it survives its costs.** A reversal that pays before fees and not after them is
not a tradable effect here.

**That it is not one hour of the day**, and **that it is present across ordinary
sessions** rather than only on the few days with the largest moves.

## What this is not asking for

- **Not a book-based signal.** Use trades and bars.
- **Not a multi-instrument study.** BTC perpetual only.
- **Not a regime model.** Answer the simple version first.
- **Not a continuation strategy.** See the claim.
