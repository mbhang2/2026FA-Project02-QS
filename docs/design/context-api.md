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

#### `ctx.subscriptions() -> 'tuple[Subscription, ...]'`

_(no docstring)_

#### `ctx.ticks()`

Every train tick, in order.

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
- **`ctx.ts`** — Nanoseconds since the epoch, UTC.
- **`ctx.unrealized_pnl`**
- **`ctx.working_order_ids`**
- **`ctx.working_orders`** — Read-only copies, in the order the engine holds them.
  Copies rather than the engine's own `Order` objects: assigning to one of
  those worked, and bypassed the queue, the cap, the T+1 rule and the run
  record.

### Calls

#### `ctx.buy(size: 'int' = 1, **kw) -> 'int | None'`

Submit a buy. Returns the order id, or None if the action cap dropped it.

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

- **`ctx.fill`**

## `LifecycleContext`

*`on_lifecycle(ctx)` — accept, reject, expiry, session*

### Reads

- **`ctx.event`**

---

Calls are queued: the context records the intent, the callback returns, and the engine applies the queue. Order methods return the id the order will have, or `None` if the action cap dropped it. A context is valid only during its own callback.
