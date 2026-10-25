# Does the shape of the options surface say anything about where the index goes next?

You can see the whole options surface at every bar. The question is whether its
shape carries information about the index's next move.

## What you can see

`ctx.options()` returns the entire cross-section at the current instant, as a
tuple of quotes. Each quote carries:

- `expiry_session` — which listed expiry it belongs to, as an index
- `strike`
- `is_call`
- `bid`, `ask` — the quoted prices
- `bid_size`, `ask_size` — how many contracts are quoted at each

There are three listed expiries, roughly two months, fourteen months and
fifty months out. Strikes run every 5 index points near the money and every 25
further out, about 300 to 370 of them per expiry. That is around two thousand
numbers per read.

The surface moves. Remaining time shrinks as the run proceeds, and the level
and the shape both move with the index.

## What is yours to decide

Everything about how you turn two thousand quotes into a decision.

Shapes people describe in options markets include: the level of premium at the
money; how much dearer downside strikes are than upside ones at the same
distance; how that difference changes between the near and far expiries; how
wide the quoted spreads are; where the quoted size is thick and where it is
thin; how any of these move from one bar to the next. None of that list is a
recommendation. It is there so the vocabulary is not the obstacle.

You decide which features to build, over what window, how to normalise them,
what threshold acts, which direction it acts in, and how long it holds.

## What is NOT yours

**You trade the index only.** `ctx.buy`, `ctx.sell` and `ctx.close` take a size
and nothing else: there is one position and it is in the index. There is no
option order. The surface is something to read, not something to trade.

So a relationship you find has to be expressed as a position in the index. If
the only thing you can say about the surface is a statement about the options
themselves, there is no way to act on it here.

**The index costs what it costs.** Commission is charged per contract on every
fill and the fill model is unfavourable — a resting limit fills only on a trade
strictly through it, so you pay the spread on entry and exit. A rule that
trades often has to beat that before it beats anything else.

**You cannot see the process that generates the surface.** Implied volatility
is observable because it is a function of the quotes. The instantaneous
variance of the underlying process is not, and nothing exposes it.

## Why it might be true

Options markets are forward-looking by construction: someone paying more for
downside protection than for upside participation is expressing something. If
that something is information about where the index is going, the shape of the
surface should lead the index. Skew has been studied as a sentiment measure for
decades on exactly that reasoning.

## Why it might not be

The same shape is also explained by things that are not forecasts. Demand for
protection is demand for insurance, and insurance is bought by people who do
not know what happens next. A skew that reflects risk aversion rather than
expectation would be stable, visible, and predict nothing. Spreads and size
reflect who is willing to make a market, which is a fact about dealers rather
than about the index.

And there is a specific trap in this data. There are two thousand numbers per
bar, which is more than enough to find a combination that fits whatever the
index happened to do. The surface being large is not the same as the surface
being informative, and a rule that reads many strikes is easier to fit than one
that reads few.

## What would make this interesting

A rule whose features are described before they are measured, which acts on a
stated shape rather than on a search across strikes, and which holds for a move
rather than turning over many times a session.

Something that trades when the shape is unusual and stands aside otherwise is
more interesting than something always in the market, provided "unusual" is
defined in advance rather than chosen afterwards.

## What this is not asking for

Not a volatility forecast: there is nothing here that pays for predicting
volatility, because you cannot trade volatility.

Not a fit of the surface to a model. Recovering parameters is not the question;
whether the shape leads the index is.

Not a scalping rule. The index's costs dominate at high turnover, and a rule
doing many round turns a session has to overcome them twice over.
