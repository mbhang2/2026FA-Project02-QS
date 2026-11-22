# BTC aggressive flow continues: cumulative volume delta predicts the next move

Written 2026-11-22, before the campaign that uses it was registered, and before
any strategy was run on the OKX BTC perpetual.

## The claim

**When aggressive buying outweighs aggressive selling on the BTC perpetual, price
keeps rising for a while, and when aggressive selling outweighs buying, price keeps
falling.**

Concretely: over a window you choose, add up the volume bought by aggressors and
subtract the volume sold by aggressors. That running difference is cumulative
volume delta (CVD), and each bar carries its parts as `bar.delta`, `buy_volume` and
`sell_volume`. The claim is that an unusually large positive CVD is followed by
higher prices, and an unusually large negative CVD by lower prices, over the next
comparable window.

**This names a direction.** A strategy that fades large flow is testing the opposite
claim and is not an implementation of this one. If continuation is wrong here, the
campaign should say so rather than quietly rotating to the other side.

## Why it might be true, and why it might not

The case for it is price impact. A participant who needs more than the displayed
liquidity keeps trading, each trade moves the price, and a large order arrives over
time rather than all at once.

The case against it is that impact can be temporary. Liquidity providers are paid
to absorb pressure, and price can drift back when the pressure stops. Crypto flow
can also be dominated by liquidations, which may exhaust themselves quickly.

## What is yours to choose

**How to measure "large".** BTC volume differs widely across hours and years, so a
fixed size means different things at different times. Normalise against something
the data already seen supplies, such as recent flow or recent total volume.

**The horizon.** Name the horizon you are testing and hold it within a strategy.

**Entry, exit and holding.** These are part of the claim. Choose market or resting
limit orders deliberately, because they face different fills.

## The conditions it must survive

Each side pays a fee of 5 basis points of notional, so a round trip costs 10 basis
points before spread and depth. Funding is charged on positions held across funding
times. Orders reach the market 100 milliseconds after the decision.

Fills use the OKX top-five order book. A market order pays through the visible
levels and is refused if it needs more than five. A resting limit order joins behind
the displayed size and fills only when trades reach it. **Your strategy does not see
the book.** It affects your fills and is not an input to your signal.

## What would make the answer interesting

**That it survives its costs.** Flow signals can fire often. An effect that pays
before fees and not after them is not a tradable effect here.

**That it is not one hour of the day**, and **that it is present across ordinary
sessions** rather than only on the few days with the largest moves.

## What this is not asking for

- **Not a book-based signal.** Use trades and bars.
- **Not a multi-instrument study.** BTC perpetual only.
- **Not a regime model.** Answer the simple version first.
- **Not a fade.** See the claim.
