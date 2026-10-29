# Context API reference

Everything a strategy can read or do. Generated from `src/core/engine/context.py` by `scripts/gen_context_api.py` — edit the code, not this file.

What a strategy is handed, and the only surface it can act through.

A strategy calls `ctx.buy(...)`; the call is **queued**, the callback returns,
and the engine applies the queue. The facade is immutable code, so nothing is
given up by making it convenient: the recorded intent list is exactly what the
strategy used to return, and T+1 eligibility, the action cap and auditability are
untouched.

It is convenient on purpose. An LLM writes hundreds of these files, and
`ctx.buy(...)` is the idiom in backtrader, zipline, QuantConnect and freqtrade,
with a great deal of it in training data; assembling lists of dataclasses is not.
A method can also reject `size=0`, or a stop on the wrong side of the entry, with
a message naming the rule at the moment of the mistake. Every malformed action
was a wasted trial and a wasted entry in N.

**The boundary is the objective function, not P&L.** A strategy may know how it
is doing -- it needs position P&L for risk logic, it has it in live trading, and
it could reconstruct it from its own fills anyway. It may not know how it is
being judged: nothing here exposes the score, anything the scorer computes, the
split identity, the seed, or any data beyond the current instant.

## Errors

**`ContextError`** — A strategy asked for something the simulator will not do.

Raised at the call, naming the rule, rather than accepted and silently
dropped -- a silent no-op is a trial that looks like a result.

**`ContextExpired`** — The context was used outside the callback it belongs to.

Stashing `ctx` in `setup()` and reading it later would let a strategy read
state from an instant it is not standing in.

## `SetupContext`

*`setup(ctx)` — once per run, before the walk*

What `setup()` sees: the train split in bulk, and the subscription call.

Bulk is the point -- a strategy fitting parameters needs the whole training
period at once, and there is no lookahead risk in data that is, by
construction, entirely before the split it will be scored on.

**Bulk, but not materialised.** `train_ticks` is a factory returning a fresh
iterator, not a list. The engine used to build the list eagerly and this
class copied it again, which cost nothing on the synthetic generator's 68,000
training ticks and was fatal on the first real run: the NQ training split is
384 sessions of roughly 321,000 trades, so a run that never looked at a
training tick still tried to hold about 247 million of them twice, on the
order of 22 GiB, before `setup()` was called. A strategy that wants the ticks
still gets them and still pays for them; one that does not pays nothing.

### Reads

- **`ctx.dataset_name`**

### Calls

#### `ctx.subscribe(interval: 'str | int', *, emit_empty: 'bool' = False) -> 'Subscription'`

Declare a subscription.

`ctx.subscribe("5m")` for time bars: anchored to the session open,
stamped with their open timestamp, delivered on close. Empty intervals
are skipped unless `emit_empty=True`, which carries the previous close
forward at zero volume.

`ctx.subscribe("tick")` for every trade event, which is what volume
bars, dollar bars, tick imbalance and any order-flow feature need. It is
expensive -- hundreds of thousands of callbacks a session -- and the
wall-clock budget is what makes that an honest cost rather than a hidden
one.

`ctx.subscribe("quote")` for every top-of-book update. Refused by the
engine on a dataset that has no quotes, which the NQ adapter does not.

#### `ctx.subscriptions() -> 'tuple[Subscription, ...]'`

_(no docstring)_

#### `ctx.ticks() -> 'tuple[Tick, ...]'`

Every train tick, in order.

Materialises the whole training split, which on a real dataset is
hundreds of millions of events -- 384 NQ sessions at roughly 321,000
trades each. Nothing stops you; it is your memory and your wall-clock
budget. `train_bars()` streams the same data through the aggregator
instead, and is what you want unless you are building something the bar
builder cannot express.

#### `ctx.train_bars(interval: 'str | int | Subscription') -> 'tuple[Bar, ...]'`

Aggregate the train split at an arbitrary interval, through the same
code path the live walk uses.

## `BaseContext`

*Shared by every run-time callback*

Reads and writes shared by the three run-time callbacks.

### Reads

- **`ctx.actionable`** — False during warmup: the bars are real, the orders would not be.
- **`ctx.average_entry_price`**
- **`ctx.bars`** — The bars that closed **in this callback**, addressed by subscription.
  `if "5m" in ctx.bars:` is the question a multi-timeframe strategy
  actually has, and it is now the expression that answers it. It used to
  mean "the most recent closed bar", which made the obvious membership test
  true whenever any 5m bar had ever closed -- and made the obvious
  `"5m" in ctx.closed_ids()` silently false, because ids are canonical.
  A strategy that never trades and says nothing about why is the worst
  failure mode there is.
  For the most recent closed bar, whenever it closed, use
  `ctx.history("5m")[-1]`.
- **`ctx.closed`** — Every bar that closed at this instant, in one callback.
  Empty in every callback but `on_bars`.
- **`ctx.equity`**
- **`ctx.fills`** — Fills since the previous callback.
- **`ctx.last_price`**
- **`ctx.now`**
- **`ctx.position`**
- **`ctx.realized_pnl`**
- **`ctx.session`**
- **`ctx.session_end`** — When this session closes, in nanoseconds UTC.
  **Not lookahead.** An exchange publishes its calendar in advance, so the
  close is known at the open; it is a fact about the schedule, not about
  the data. Nothing here says anything about what happens between now and
  then.
  It exists because without it a strategy cannot be reliably flat at a
  boundary. Every alternative is a guess at the session's length from what
  it has already seen, and every such guess is wrong on the first session
  shorter than anything before it -- a half day, which arrives without
  warning and is exactly when being caught long overnight matters. The
  calibration apparatus guessed from a bar count, which on real data
  measured activity rather than length and collapsed its trading window to
  a third of the session for most of a 384-session run.
  `ctx.session_end - ctx.ts` is the time left. Deciding what margin to
  leave is yours; knowing the deadline should not have been.
- **`ctx.session_start`** — When this session opened, in nanoseconds UTC.
- **`ctx.ts`** — Nanoseconds since the epoch, UTC.
- **`ctx.unrealized_pnl`**
- **`ctx.working_order_ids`**
- **`ctx.working_orders`** — Read-only copies, in the order the engine holds them.
  Copies rather than the engine's own `Order` objects: assigning to one of
  those worked, and bypassed the queue, the cap, the T+1 rule and the run
  record.

### Calls

#### `ctx.assets_held() -> 'tuple[str, ...]'`

Every asset you have a POSITION in, default first. Empty when flat.

A strategy holding several legs needs to be able to ask what it is
carrying without remembering what it opened -- so "held" is a non-zero
position, and a book that exists and is flat is not one. It read "every
asset with an open book" and returned the default asset unconditionally,
which made `if ctx.assets_held(): return` -- the obvious way to write
"do nothing while carrying something" -- always true.

#### `ctx.average_entry_price_in(asset: 'str') -> 'float'`

_(no docstring)_

#### `ctx.buy(size: 'int' = 1, **kw) -> 'int | None'`

Submit a buy. Returns the order id, or None if the action cap dropped it.

`asset=` names what to trade and defaults to the one instrument a
single-asset run has, so every strategy written before assets existed
means exactly what it meant. `ctx.buy(1)` and
`ctx.buy(1, asset="SPX_C_7850_DEC26")` are different books.

#### `ctx.cancel(order_id: 'int') -> 'None'`

_(no docstring)_

#### `ctx.cancel_all() -> 'None'`

_(no docstring)_

#### `ctx.close(**kw) -> 'int | None'`

Flatten the current position with a market order.

#### `ctx.closed_ids() -> 'tuple[str, ...]'`

_(no docstring)_

#### `ctx.find_orders(*, tag: 'str | None' = None, order_type: 'str | OrderType | None' = None, side: 'str | Side | None' = None, parent_id: 'int | None' = None) -> 'tuple[OrderView, ...]'`

Working orders matching every filter given. Empty tuple if none do.

`ctx.find_orders(tag="protective-stop")` is the intended way to get hold
of an order you placed earlier, and `parent_id=` is how you reach the two
legs of a bracket the engine armed for you. Tracking ids by hand across
lifecycle events also works, and is exactly where a strategy holding an
id through a DAY expiry crashes.

#### `ctx.history(sub: 'str | Subscription') -> 'HistoryView[Bar]'`

Closed bars for a subscription, oldest first.

Takes whatever you subscribed with (`"5m"`), the canonical id, an
equivalent spelling (`"300s"`), or the `Subscription` itself. An id you
never subscribed to raises `ContextError`.

Reaching at or beyond the current instant raises `LookaheadError` -- the
newest readable element is the bar that just closed.

#### `ctx.modify(order_id: 'int', *, limit: 'float | None' = None, trigger: 'float | None' = None, size: 'int | None' = None) -> 'None'`

_(no docstring)_

#### `ctx.options() -> "'tuple[OptionQuote, ...]'"`

The options surface as of this instant, as a tuple of quotes.

**Why the whole cross-section and not a stream.** An arbitrage is a
statement about several quotes at once -- parity needs a call and a put,
a butterfly needs three strikes, a calendar needs two expiries -- so a
feed that delivered them one at a time would make the task reassembly
rather than detection. `ctx.history(...)` is the right shape for a
scalar series and the wrong shape for a surface.

**There is no look-ahead to refuse here, and that is a property of the
construction rather than a check.** The surface is a deterministic
function of the underlying price, and the price it is built from is the
last trade at or before this instant -- the same number `ctx.last_price`
returns. There is no future quote in the dataset to leak, so unlike
`history()` this method needs no `LookaheadError`: a strategy cannot
ask for a surface the engine has not reached, because the surface is
computed from what the engine has already seen.

Raises `ContextError` when the dataset has no options channel, naming
the capability, rather than returning an empty tuple that would read as
"no quotes right now".

#### `ctx.position_in(asset: 'str') -> 'int'`

The position in one asset. `ctx.position` is this for the default.

A companion rather than an argument on the property: `ctx.position` is
read by 65 call sites and by every one of the 3,447 committed
strategies, and a property cannot take one anyway. The pattern is the
same for the three below.

#### `ctx.realized_pnl_in(asset: 'str') -> 'float'`

_(no docstring)_

#### `ctx.sell(size: 'int' = 1, **kw) -> 'int | None'`

_(no docstring)_

#### `ctx.sub_id(sub: 'str | Subscription') -> 'str'`

The canonical id of a subscription you declared.

`ctx.sub_id("5m")` -> `"time:300000000000"`. Ids are canonical, not the
text you typed, and an unknown one raises rather than quietly not
matching -- so this is here for the rare code that needs the canonical
form, not as something every strategy has to remember to call.

#### `ctx.ticks() -> 'HistoryView[TickEvent]'`

Every trade event seen so far, oldest first.

Only populated when `setup()` subscribed to ticks -- the engine does not
pay to retain them otherwise. Reaching at or beyond the current instant
raises, exactly as for bars.

## `BarContext`

*`on_bars(ctx)` — one or more subscribed bars closed*

Delivered when one or more subscribed bars close at this timestamp.

`ctx.closed`, `ctx.closed_ids()` and `ctx.bars` are on the base context and
are empty everywhere else, so the same expression reads correctly in every
callback rather than existing in one of them.

## `FillContext`

*`on_fill(ctx)` — an order filled*

Delivered on a fill. Present-instant state only, never a partial bar.

### Reads

- **`ctx.average_entry_price`**
- **`ctx.fill`**
- **`ctx.position`**
- **`ctx.realized_pnl`**

## `LifecycleContext`

*`on_lifecycle(ctx)` — accept, reject, expiry, session*

### Reads

- **`ctx.event`**

## `QuoteContext`

*`on_quote(ctx)` — a top-of-book update, when subscribed*

Delivered for every top-of-book update, when the strategy subscribed.

Quotes are not trades. Nothing here is a fill, and a bar is not closed by
one. The dataset that has no quotes refuses the subscription before the
walk, so this context is never built there.

### Reads

- **`ctx.quote`** — The top of book at this instant.

---

Calls are queued: the context records the intent, the callback returns, and the engine applies the queue. Order methods return the id the order will have, or `None` if the action cap dropped it. A context is valid only during its own callback.
