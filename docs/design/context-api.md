# Context API reference

Everything a strategy can read or do. Generated from `src/core/context.py` by `scripts/gen_context_api.py` — edit the code, not this file.

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

Declare a bar subscription, e.g. `ctx.subscribe("5m")`.

Bars anchor to the session open, are stamped with their open timestamp
and delivered on close. Empty intervals are skipped unless
`emit_empty=True`, which carries the previous close forward at zero
volume.

#### `ctx.subscriptions() -> 'list[Subscription]'`

_(no docstring)_

#### `ctx.ticks()`

Every train tick, in order.

#### `ctx.train_bars(interval: 'str | int | Subscription') -> 'list[Bar]'`

Aggregate the train split at an arbitrary interval, through the same
code path the live walk uses.

## `BaseContext`

*Shared by every run-time callback*

Reads and writes shared by the three run-time callbacks.

### Reads

- **`ctx.actionable`** — False during warmup: the bars are real, the orders would not be.
- **`ctx.average_entry_price`**
- **`ctx.bars`** — `ctx.bars["5m"]` -- the most recent closed bar for a subscription.
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
- **`ctx.working_orders`**

### Calls

#### `ctx.buy(size: 'int' = 1, **kw) -> 'int | None'`

Submit a buy. Returns the order id, or None if the action cap dropped it.

#### `ctx.cancel(order_id: 'int') -> 'None'`

_(no docstring)_

#### `ctx.cancel_all() -> 'None'`

_(no docstring)_

#### `ctx.close(**kw) -> 'int | None'`

Flatten the current position with a market order.

#### `ctx.history(sub: 'str | Subscription') -> 'HistoryView[Bar]'`

Closed bars for a subscription, oldest first.

Reaching at or beyond the current instant raises `LookaheadError` -- the
newest readable element is the bar that just closed.

#### `ctx.modify(order_id: 'int', *, limit: 'float | None' = None, trigger: 'float | None' = None, size: 'int | None' = None) -> 'None'`

_(no docstring)_

#### `ctx.sell(size: 'int' = 1, **kw) -> 'int | None'`

_(no docstring)_

## `BarContext`

*`on_bars(ctx)` — one or more subscribed bars closed*

Delivered when one or more subscribed bars close at this timestamp.

### Reads

- **`ctx.closed`** — Every bar that closed at this timestamp, in one callback.

### Calls

#### `ctx.closed_ids() -> 'tuple[str, ...]'`

_(no docstring)_

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
