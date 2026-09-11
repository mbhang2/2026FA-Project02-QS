# Writing a strategy

Everything you can do, in the order you need it. You write one file,
`src/strategies/current.py`; nothing else in the repo is yours to change.

Read this with `docs/agent/data.md`, which is generated from the manifest and
says what data exists, what fields it has and what they mean. This document says
what you can *do* with it. Where the two overlap — bar fields, tick fields, the
three values of `side` — `docs/agent/data.md` is the authority and is regenerated
whenever the data changes.

`docs/design/context-api.md` is the generated reference for every attribute and
method on `ctx`. When this document and that one disagree, that one is right: it
comes from the code.

---

## 1. Start here

A complete, working strategy. Four methods, one subscription, one order.

```python
class Strategy:
    def setup(self, ctx):
        ctx.subscribe("5m")

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        history = ctx.history("5m")
        if len(history) < 1:
            return
        last = history[-1]
        if last.close > last.open:
            ctx.buy(size=1, stop_offset=0.75, target_offset=0.75, tag="entry")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
```

What each line is doing:

- `ctx.subscribe("5m")` — declare the data you want, once, in `setup()`. There is
  no way to subscribe later.
- `ctx.actionable` — `False` during warmup sessions. The bars are real; the
  orders would not be.
- `ctx.history("5m")` — the bars that have **closed**. `history[-1]` is the one
  that just closed. There is nothing after it to read.
- `ctx.buy(...)` — records an intent. It does not fill here; see §6.
- `stop_offset` / `target_offset` — a bracket. The engine arms the stop and the
  target at the instant your entry fills, not at your next callback.

`run.py` looks for a class named `Strategy` (or `STRATEGY`, or
`MomentumStrategy`) in `src/strategies/current.py` and instantiates it with no
arguments. Give every parameter a default.

---

## 2. The contract

| Method | When it fires | Required |
|---|---|---|
| `setup(ctx)` | once, before the walk | **yes** |
| `on_bars(ctx)` | one or more subscribed bars closed at this instant | **yes**, if you subscribe to any bars |
| `on_fill(ctx)` | one of your orders filled | **yes** |
| `on_lifecycle(ctx)` | an order was accepted, rejected, cancelled or expired; a session opened or closed | **yes** |
| `on_tick(ctx)` | a trade occurred | only if you `subscribe("tick")` |

`on_fill` and `on_lifecycle` fire whether or not you care — a session open is a
lifecycle event and every run has one — so define them even if they do nothing.
`on_tick` is the exception: the engine checks for it only when you subscribed to
ticks, and refuses the run at wiring time if it is missing.

**`ctx` is different in each callback and is valid only inside it.** Stashing it
on `self` and reading it later raises `ContextExpired`, because that would be
reading state from an instant you are not standing in. Read what you need while
the callback runs; keep the *values* on `self`.

### `setup(ctx)` — the only place with bulk data

`setup` sees the **training split in bulk**. That is not a lookahead hole: the
training split is, by construction, entirely before the split you are scored on.
It is the place to fit parameters.

| Call | Gives you |
|---|---|
| `ctx.subscribe(interval, emit_empty=False)` | declares a subscription, returns a `Subscription` — keep it |
| `ctx.ticks()` | every training tick, in order, as a tuple |
| `ctx.train_bars("5m")` | the training split aggregated at any interval, through the same code the live walk uses |
| `ctx.dataset_name` | e.g. `"nq-trades:NQ"` |

`setup()` **returns nothing**. Returning a list of subscriptions is the old shape
and is refused with a message rather than silently ignored.

You cannot place orders in `setup()`. There is no market instant to place them
at.

### The run-time callbacks

`on_bars`, `on_tick`, `on_fill` and `on_lifecycle` all receive a context with the
same reads and the same order methods, plus one extra each:

| Callback | Extra |
|---|---|
| `on_bars` | `ctx.closed` — every `Bar` that closed at this instant; `ctx.closed_ids()` — their subscription ids |
| `on_tick` | `ctx.tick` — the trade event |
| `on_fill` | `ctx.fill` — the fill |
| `on_lifecycle` | `ctx.event` — the lifecycle event |

---

## 3. Subscriptions

Declared in `setup()`, once each.

```python
def setup(self, ctx):
    self.fast = ctx.subscribe("5m")          # keep what it returns
    ctx.subscribe("30s")
    ctx.subscribe("1h", emit_empty=True)
    ctx.subscribe("tick")
```

**Time bars** take any interval: `"250ms"`, `"30s"`, `"5m"`, `"1h"`, `"1d"`, or a
positive integer of nanoseconds. Bars are anchored to the **session open**, not
to the wall clock — a grid anchored at midnight would put the first bar of a
session in the middle of an interval. Empty intervals are skipped unless
`emit_empty=True`, which emits a bar carrying the previous close forward at zero
volume and `tick_count == 0`.

Bar interval is close to free. Thirty-second bars cost about the same as
five-minute ones, because the walk stops at boundaries either way.

**Tick events** — `ctx.subscribe("tick")` — deliver every trade to `on_tick`.
This is what volume bars, dollar bars, tick imbalance, cumulative delta and every
order-flow feature are built from. It is the most expensive thing you can do: it
disables the engine's event-driven jump, because every tick must now be
delivered, and measured on real NQ sessions a tick subscriber runs about **5×
slower** than the same strategy on bars alone. That cost lands on the wall-clock
budget, where it is visible. Subscribe to ticks when you need them, not by
default.

**Subscription ids are canonical**, not the string you typed: `"5m"` becomes
`time:300000000000`, and `"tick"` stays `"tick"`. `ctx.history()` and
`ctx.bars[...]` accept either the human string or the `Subscription` object, but
`ctx.closed_ids()` returns canonical ids — so to ask "did my 5m bar close?",
compare against the `sub_id` of the subscription you kept:

```python
if self.fast.sub_id in ctx.closed_ids():
    ...
```

Subscribing to the same interval twice returns the same subscription. Subscribing
to it twice with different `emit_empty` raises.

**Several bars closing at the same instant arrive in one callback.** If a 5m and a
1m bar both close at 09:35 you get a single `on_bars` with both in `ctx.closed`.
This is deliberate: separate callbacks would make the ordering between them a
hidden variable that changes your results and is recorded nowhere.

---

## 4. What you can read

### Market state

| Read | What it is |
|---|---|
| `ctx.ts` | nanoseconds since the epoch, UTC |
| `ctx.now` | the same instant as a timezone-aware `datetime` |
| `ctx.session` | session index within the dataset, counting from 0 |
| `ctx.actionable` | `False` during warmup: bars are real, orders are ignored |
| `ctx.last_price` | the most recent trade price |
| `ctx.history(sub)` | closed bars for a subscription, oldest first |
| `ctx.bars[sub]` | the most recent closed bar — shorthand for `ctx.history(sub)[-1]` |
| `ctx.ticks()` | every trade event so far — **only populated if you subscribed to ticks** |

`ctx.history(...)` returns a `HistoryView`. It supports `len()`, iteration,
indexing (`view[0]`, `view[-1]`), slicing, and `view.last(k)` for the `k` most
recent oldest-first. Short history is a legitimate state — check `len()` rather
than catching an exception.

Bar and tick field names and meanings are in `docs/agent/data.md`. The short
version: a bar carries `open_ts` and `close_ts` and you are called at `close_ts`;
a tick carries `side`, whose three values are buy, sell and **unknown**, and
unknown arrives as `None`.

### Your own position

| Read | What it is |
|---|---|
| `ctx.position` | signed contracts; `0` when flat |
| `ctx.average_entry_price` | average price of the open position |
| `ctx.realized_pnl` | realized P&L **net of commission** |
| `ctx.unrealized_pnl` | mark-to-market on the open position, `0.0` when flat |
| `ctx.equity` | `realized_pnl + unrealized_pnl` |
| `ctx.working_order_ids` | ids of every order that can still do something, sorted |
| `ctx.working_orders` | the `Order` objects themselves |
| `ctx.fills` | fills since the previous callback |

You are allowed to know how you are doing — you need it for risk logic and you
would have it live. You are not allowed to know how you are being *judged*: there
is no score, no metric, no split identity and no seed on `ctx`, and
`src.core.scoring` refuses to be imported from a strategy.

`ctx.working_orders` gives you the live order objects; read `order_id`,
`order_type`, `side`, `size`, `filled_size`, `remaining`, `limit_price`,
`stop_price`, `tif`, `status`, `reduce_only`, `parent_id`, `oco_group`.

**Change an order with `ctx.modify()`, never by assigning to its attributes.**
These are the engine's own objects and assigning to one *works* — which is
precisely the problem. It skips the intent queue, the action cap and the T+1
rule, and nothing about it reaches the run record, so the trial is no longer the
trial that was logged. This is the one rule in this document that the simulator
does not enforce for you; treat what comes back from `ctx.working_orders` as read
only.

---

## 5. What you can do

Every one of these **queues** an intent. Nothing happens until your callback
returns.

### Orders

```python
ctx.buy(size=1, **kw)      # returns the order id, or None if the cap dropped it
ctx.sell(size=1, **kw)
ctx.close(**kw)            # flatten with a market order; raises if flat
ctx.modify(order_id, limit=None, trigger=None, size=None)
ctx.cancel(order_id)
ctx.cancel_all()
```

`buy` and `sell` take the same keywords:

| Keyword | Meaning |
|---|---|
| `size` | positive whole contracts; use `sell()` rather than a negative size |
| `type` | `"market"` (default), `"limit"`, `"stop"`, `"stop_limit"` |
| `limit` | limit price — required for `"limit"` and `"stop_limit"` |
| `trigger` | stop trigger price — required for `"stop"` and `"stop_limit"` |
| `stop` / `target` | bracket levels as **absolute prices** |
| `stop_offset` / `target_offset` | bracket levels as **positive distances** from the fill |
| `tif` | `"DAY"` (default), `"GTC"`, `"GTD"`, `"IOC"`, `"FOK"` |
| `expire_ts` | required for `"GTD"`; nanoseconds UTC |
| `reduce_only` | the order may only reduce an existing position |
| `tag` | free text; lands in the blotter row |

`trigger` and `stop` are different things and the error message says so:
`trigger` is where a stop order fires, `stop` is the protective stop of a
bracket.

### Brackets

Give `stop`/`target` **or** `stop_offset`/`target_offset`, never both. Absolute
prices are usually what you mean — "stop below that swing low" is a level — but a
distance is the only form available before a market order's fill price is known.

The engine arms both legs as a reduce-only OCO pair **at the instant the entry
fills**. When one fills the other is cancelled before any callback runs, so the
sibling can never resolve against the same trade. This is the single most
valuable thing the engine does for you: a strategy that places its own protection
on the next bar callback leaves the position naked for the rest of the interval,
and that gap is where a large historical artefact lived.

Absolute bracket levels are validated against the entry reference — the limit
price if you gave one, otherwise the last trade price — so a stop above the entry
of a long is refused at the call.

### Time in force

| TIF | Behaviour |
|---|---|
| `DAY` | rests until the session closes, then expires (default) |
| `GTC` | rests until filled or cancelled — survives the session close |
| `GTD` | rests until `expire_ts`; checked mid-session, not only at the close |
| `IOC` | fills what it can against its first eligible tick, the rest cancels |
| `FOK` | fills in full against its first eligible tick or cancels |

**A position is not flattened at the session close.** DAY orders die; GTC and
unexpired GTD orders live on, brackets included, and so does the position. If you
want to be flat overnight, say so. The end-of-session position is recorded in the
run record, so an unintended hold is visible.

### Lifecycle events

`ctx.event.kind` is one of:

| Kind | Meaning |
|---|---|
| `session_open` | a session began |
| `session_close` | a session ended |
| `order_accepted` | the order exists and is working (also carries the `detail` when a reduce-only size was clamped) |
| `order_rejected` | it was refused — read `ctx.event.detail` |
| `cancel_confirmed` | your cancel took effect, or an IOC/FOK remainder was pulled |
| `day_expired` | a DAY order died at the session close |
| `gtd_expired` | a GTD order reached `expire_ts` |
| `oco_cancelled` | the sibling of a filled bracket leg |

`ctx.event.order_id` is the order it concerns, or `None` for session events.
**Watch these if you hold order ids**: an id whose order has expired is stale, and
`ctx.cancel()` on it raises.

### The action cap

**64 queued actions per callback.** Past it, calls are dropped, `buy()`/`sell()`
return `None`, and the count reaches the run record as `action_cap_hits` and
`actions_dropped`. It exists to turn a fill-modify-fill-modify ping-pong bug into
a counted number instead of a mystery timeout.

Treat `None` from `buy()`/`sell()` as "no order exists" rather than assuming an
id.

---

## 6. The timing rules

Four rules. Everything else in the engine follows from them.

**1. An order submitted at instant *T* is eligible from the next tick, never at
*T*.** You cannot fill against the trade that closed the bar you just read.
Without this rule a strategy fills at the close of the bar it just saw, which is
a clean one-bar lookahead that produces beautiful, entirely fake results.

**2. A fill is its own callback, and it carries present-instant state only.**
Never a partially formed bar. If you want finer intra-bar state, subscribe to a
finer timeframe — one rule instead of two. Fills are delivered *before* the tick
event and *before* the bar closes at the same instant, so you learn about your own
fill before being asked to react to the trade that caused it.

**3. `setup()` sees the training split in bulk; every run-time callback sees the
current instant and the past, and nothing else.** `ctx.history(...)` holds only
closed observations, and reaching at or beyond the cursor raises
`LookaheadError`. There is no API that returns a future bar, and that is the
point: a prompt telling you to avoid lookahead does not prevent lookahead, an
interface that cannot express it does.

**4. Warmup reaches backward; decisions never reach forward.** The harness may
serve some sessions before the split's first scored session so your features can
spin up. During those, callbacks fire and `ctx.actionable` is `False`: orders are
ignored and counted into `warmup_actions_ignored` rather than silently vanishing.
Check `ctx.actionable` before acting.

Within a single tick the order is fixed: resolve resting orders → fill callbacks
→ tick event → one bar callback with every bar that closed → lifecycle events.

### How much history you have

There is no rule that refuses a long lookback — nothing raises. What bounds you
is simply that history starts empty and fills up: at your first actionable bar
you hold only the bars from the warmup sessions the harness configured, and the
number of warmup sessions is set by the run profile, not by you. A feature
needing twenty sessions of context will be undefined for a while.

So **check `len(ctx.history(...))` and return early**, rather than assuming depth.
A strategy that raises on its first bar is a crashed trial, and a crashed trial
still counts.

---

## 7. What is not available

You will reach for these. They do not exist.

- **No bid, no ask, no BBO, no quotes.** The price source is trades only. That
  rules out spread, mid, microprice and quoted-depth imbalance.
- **No order book.** No queue position, no depth, no book pressure, no
  cancel/replace flow.
- **No vendor bars.** Bars are built from trades by the engine, so the session
  and anchoring conventions live in reviewable code.
- **No score, no metric, no null.** `src.core.scoring.scorer`,
  `src.core.scoring.metrics` and `src.core.scoring.calibration` raise
  `ImportError` when imported from anything under `src.strategies`. You can know
  your P&L; you cannot know the number you are graded on.
- **No split identity and no seed.** Nothing on `ctx` says whether this is
  train, validation or holdout.
- **No data beyond the current instant**, including no future bar, no full
  session you are in the middle of, and no next tick.
- **No evaluation outside the logged path.** Every invocation of `run.py`
  appends exactly one ledger row, crashes included. An agent-triggered run is
  validation-only with a fixed profile; `--split`, `--fill-model`, `--seed`,
  `--unlock-holdout` and `--profile` are refused. Running fifty backtests and
  presenting the best as one candidate is the failure this design exists to
  prevent, so the count is kept where you cannot reach it.

Import what you need from `src.core.types` — `Side`, `OrderType`,
`TimeInForce`, `NS_PER_SECOND` and the rest are all available and are the right
way to compare enum values.

---

## 8. What will make a run fail

Searchable, because a failure message is the fastest way to learn an interface.
All of these are raised at the call that caused them, not swallowed — a silent
no-op is a trial that looks like a result.

### Wiring

| Message | Cause |
|---|---|
| `setup() declared no subscriptions; call ctx.subscribe('5m')` | `setup()` subscribed to nothing |
| `setup() returns nothing now; declare subscriptions with ctx.subscribe('5m') instead of returning them` | you returned a list of subscriptions |
| `the strategy subscribed to ticks but has no on_tick(ctx) callback` | `subscribe("tick")` without `on_tick` |
| `cannot read '5min' as an interval; use e.g. '30s', '5m', '1h', '1d'` | unparseable interval |
| `interval must be positive, got -1` | a non-positive integer interval |
| `time:300000000000 already subscribed with emit_empty=False` | same interval, two different `emit_empty` |
| `train_bars() aggregates; for raw train events use ctx.ticks()` | `ctx.train_bars("tick")` |

### Using the context wrongly

`ContextExpired` and `ContextError` unless noted.

| Message | Cause |
|---|---|
| `this context belonged to an earlier callback. Read what you need while the callback is running, or keep it on self.` | you stored `ctx` and used it later |
| `the setup context is only valid during setup(); subscribe there, and keep anything you fitted on self.` | same, for the setup context |
| `no subscription 'time:60000000000'; declare it in setup() (have: [...])` | `ctx.history()` or `ctx.bars[...]` for something you never subscribed to — a **`KeyError`**, because you asked for a key that does not exist |
| `no bar has closed yet for 'time:300000000000'; check len(ctx.history(...))` | `ctx.bars[...]` before the first close — reachable from `on_fill`, `on_tick` and `on_lifecycle` (the session open fires before any bar), never from `on_bars` |

### Reaching forward — `LookaheadError`

| Message | Cause |
|---|---|
| `index 12 is at or beyond the current instant (12 closed observations; the newest readable is 11)` | `history[len(history)]` |
| `slice stop 13 reaches past the current instant (12 closed observations)` | a slice whose stop runs past the cursor |
| `index -20 reaches before the start of history (12 available)` | reaching back further than history goes |

### Malformed orders — `ContextError`

| Message | Cause |
|---|---|
| `size must be a positive whole number of contracts, got -1; use sell() rather than a negative size` | negative or fractional size |
| `a limit order needs limit=<price>` | `type="limit"` with no `limit` |
| `a stop order needs trigger=<price>; stop= is the protective stop of a bracket, which is a different thing` | `type="stop"` with no `trigger` |
| `a market order cannot carry a limit price` | `type="market"` plus `limit=` |
| `give bracket levels either as prices (stop=, target=) or as distances (stop_offset=, target_offset=), not both` | mixing the two bracket forms |
| `stop 101.0 must be below the entry 100.0 for a long` | bracket level on the wrong side |
| `target 99.0 must be above the entry 100.0 for a long` | same, for the target |
| `stop_offset is a positive distance from the fill price, got -1` | a negative offset |
| `a GTD order needs expire_ts=<nanoseconds>` | `tif="GTD"` with no expiry |
| `close() with no position open` | `ctx.close()` while flat |
| `no order 42` | an id that was never issued |
| `cannot cancel order 42: it is already filled` | acting on an order that is done |
| `cannot modify order 42: it is cancelled` | same, for modify |
| `modify() with nothing to change` | `ctx.modify(oid)` with no keyword |

### Rejections that do not raise

These arrive as an `order_rejected` lifecycle event with a `detail`, because the
order was well-formed when you wrote it and only became impossible later:

| Detail | Cause |
|---|---|
| `reduce_only with no position open` | reduce-only while flat |
| `reduce_only buy would increase a position of 2` | reduce-only on the wrong side |
| `reduce_only size clamped from 5 to 2` | accepted, not rejected — the size was reduced to the position |
| `cancel for an order that is not working` | the order had already filled or expired when the queue was applied |
| `modify for an order that is not working` | same, for modify |
| `cannot resize to 1: 1 already filled` | a modify shrinking below what is already filled |

### The budget

`abandoned after 121.4s against a 120s budget (4,096,000 ticks in)` —
`BudgetExceeded`. The run is **abandoned, not crashed**: nothing went wrong
except that the strategy was too slow, and the distinction is recorded. It still
counts as a trial. The clock is checked every few thousand ticks *and* every few
dozen callbacks, so a strategy that is slow inside a callback cannot overshoot
unnoticed.

The usual causes, in order of likelihood: recomputing a feature over the whole
history on every bar instead of updating it incrementally; subscribing to ticks
without needing them; an unbounded structure that grows across sessions.

### The holdout

`the holdout is not served until it is explicitly unlocked, once. Call
dataset.unlock_holdout(reason) and expect the reason to appear in the run
record.` — `HoldoutLocked`. You cannot do this, and should not try. The run is
recorded as `refused`.

---

## 9. Worked examples

Every example below is a real file under `tests/examples/`, run in CI on the
synthetic dataset. If one of them stops working, the build fails. Copy them.

### 1. Fixed time entry

The shape, with nothing else in it: enter on the third bar of every session and
let the bracket do the exit.

```python
# tests/examples/01_fixed_time_entry.py
"""Enter long on the third bar of every session, with a fixed bracket.

The simplest thing that is still a strategy: no feature, no state beyond
"which bar of this session is this". It exists to show the shape -- subscribe
in setup(), act in on_bars(), let the bracket handle the exit.
"""


class Strategy:
    def __init__(self, interval="5m", entry_bar=3, size=1,
                 stop_offset=0.75, target_offset=0.75):
        self.interval = interval
        self.entry_bar = entry_bar
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self._session = None
        self._bars_this_session = 0

    def setup(self, ctx):
        # Declared once, before the walk. There is no way to subscribe later.
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
        self._bars_this_session += 1

        # Warmup sessions deliver real bars but refuse orders. Checking keeps
        # the count of ignored actions in the run record at zero.
        if not ctx.actionable:
            return
        if self._bars_this_session != self.entry_bar:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # A market buy. The bracket is armed by the engine at the instant the
        # entry fills, not at the next callback -- which is the difference
        # between a protected position and an unprotected one.
        ctx.buy(
            size=self.size,
            stop_offset=self.stop_offset,
            target_offset=self.target_offset,
            tag="entry",
        )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
```

### 2. Feature-based entry, bracket in offsets

A breakout of the last N closed bars, sized off the measured range rather than a
constant. This is the first example that reads history, and it shows the pattern
for it: take a window, check its length, never index past the end.

```python
# tests/examples/02_breakout_with_bracket.py
"""Breakout of the last N closed bars, with the bracket given as distances.

Shows the two things most feature code needs: reading history without reaching
past the current instant, and sizing a bracket off a measured range rather than
a constant.
"""


class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.0,
                 target_mult=1.5):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        # `history` holds only closed bars. `history[-1]` is the one that just
        # closed; `history[len(history)]` raises LookaheadError rather than
        # returning something plausible. Short history is legitimate during
        # warmup, so check the length instead of catching.
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last, prior = window[-1], window[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if last.close > high:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="breakout-long")
        elif last.close < low:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="breakout-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
```

### 3. Multi-timeframe

Five-minute entries filtered by the previous session's daily bar. Two
subscriptions, so "did *my* bar close?" becomes a real question — and the answer
is to keep what `ctx.subscribe()` returned and compare `sub_id` against
`ctx.closed_ids()`.

```python
# tests/examples/03_multi_timeframe.py
"""Five-minute entries, filtered by the previous session's daily bar.

Two subscriptions. When both close at the same instant they arrive in **one**
callback, so "did my fast bar close?" is a real question -- and the way to ask
it is to keep what `ctx.subscribe()` returned. Subscription ids are canonical
strings (`time:300000000000`), not the text you typed.
"""


class Strategy:
    def __init__(self, fast="5m", slow="1d", lookback=6, size=1,
                 stop_offset=1.0, target_offset=1.5):
        self.fast = fast
        self.slow = slow
        self.lookback = lookback
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.fast_sub = None
        self.slow_sub = None

    def setup(self, ctx):
        # Keep the Subscription objects: they carry the canonical id, and every
        # read method accepts one directly.
        self.fast_sub = ctx.subscribe(self.fast)
        # A daily subscription on this data closes once per session -- a session
        # is shorter than a day, and any bar still open at the session close is
        # flushed there.
        self.slow_sub = ctx.subscribe(self.slow)

    def on_bars(self, ctx):
        # Act only when the fast bar closed. The slow one closing is a session
        # boundary, not a decision point.
        if self.fast_sub.sub_id not in ctx.closed_ids():
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.slow_sub)
        if not len(daily):
            return                      # no completed session yet
        prev = daily[-1]
        bias = prev.close - prev.open
        if bias == 0:
            return

        fast = ctx.history(self.fast_sub)
        if len(fast) < self.lookback:
            return
        window = fast.last(self.lookback)
        rising = window[-1].close > window[0].close

        # Take only the fast signal that agrees with the last completed session.
        if bias > 0 and rising:
            ctx.buy(size=self.size, stop_offset=self.stop_offset,
                    target_offset=self.target_offset, tag="with-trend-long")
        elif bias < 0 and not rising:
            ctx.sell(size=self.size, stop_offset=self.stop_offset,
                     target_offset=self.target_offset, tag="with-trend-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
```

### 4. Managing a position from `on_fill`

A resting limit entry with hand-rolled protection: cancel the entry if it goes
stale, arm a stop the moment it fills, trail it, and flatten on time. Note where
each decision lives, and note the gap the bracket in examples 1–3 was closing for
you.

```python
# tests/examples/04_manage_from_on_fill.py
"""A resting limit entry, managed by hand: cancel, arm, trail, close.

Everything the bracket does for you in the earlier examples, done explicitly --
because sooner or later a rule does not fit a bracket. Note where each decision
lives: the entry is placed on a bar, the protection is armed on the *fill*, and
the exit is a bar decision again.

The gap this example is really about: between placing an entry and its fill you
are unprotected, and `on_fill` is the first instant you can do anything. A
bracket closes that gap inside the engine; managing it yourself does not.
"""


class Strategy:
    def __init__(self, interval="5m", size=1, offset=0.5, stop=1.0,
                 trail=0.5, patience=3, max_hold=8):
        self.interval = interval
        self.size = size
        self.offset = offset        # how far below the market to rest the entry
        self.stop = stop            # initial protective distance
        self.trail = trail          # tighten the stop by this much per bar held
        self.patience = patience    # bars to wait for the entry before pulling it
        self.max_hold = max_hold    # bars to hold before flattening
        self.entry_id = None
        self.stop_id = None
        self.entry_price = None
        self.age = 0

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
        self.age += 1

        if ctx.position:
            self._manage_open_position(ctx)
            return

        if self.entry_id is not None:
            # Still waiting on the resting entry. Pull it if it has gone stale:
            # a limit that never fills is not free, it is an exposure to a
            # move you no longer have a view on.
            if self.age > self.patience:
                ctx.cancel(self.entry_id)
                self.entry_id = None
            return

        # Flat, nothing working: rest a new bid under the market.
        self.entry_id = ctx.buy(
            size=self.size,
            type="limit",
            limit=ctx.last_price - self.offset,
            tif="DAY",            # cancelled at the session close, not carried
            tag="limit-entry",
        )
        # buy() returns None when the action cap dropped the call. Treat that as
        # "no order exists" rather than assuming an id.
        self.age = 0

    def _manage_open_position(self, ctx):
        if self.age >= self.max_hold:
            if self.stop_id is not None:
                ctx.cancel(self.stop_id)   # cancel before flattening
                self.stop_id = None
            ctx.close()                    # market, reduce_only, tagged "close"
            return
        if self.stop_id is not None:
            # Trail the protective stop upward, never down.
            new_stop = ctx.last_price - self.stop + self.trail * self.age
            ctx.modify(self.stop_id, trigger=min(new_stop, ctx.last_price - 0.25))

    def on_fill(self, ctx):
        fill = ctx.fill
        if fill.order_id == self.entry_id:
            # The first instant protection can exist. reduce_only so it can
            # only ever close this position, never open a new one.
            self.entry_id = None
            self.entry_price = fill.price
            self.age = 0
            self.stop_id = ctx.sell(
                size=fill.size,
                type="stop",
                trigger=fill.price - self.stop,
                tif="GTC",         # survives the session close, like the position
                reduce_only=True,
                tag="protective-stop",
            )
        elif fill.order_id == self.stop_id or fill.position == 0:
            self.stop_id = None
            self.entry_price = None
            self.age = 0

    def on_lifecycle(self, ctx):
        ev = ctx.event
        # A DAY order dies at the session close. If the entry went with it, the
        # id is stale and reusing it would raise on the next cancel().
        if ev.order_id is not None and ev.order_id == self.entry_id:
            if str(ev.kind) in ("day_expired", "gtd_expired", "cancel_confirmed",
                                "oco_cancelled", "order_rejected"):
                self.entry_id = None
        if ev.order_id is not None and ev.order_id == self.stop_id:
            if str(ev.kind) in ("day_expired", "gtd_expired", "cancel_confirmed",
                                "oco_cancelled", "order_rejected"):
                self.stop_id = None
```

### 5. A tick-subscribed strategy

Signed-volume imbalance built from raw trades, decided on bars. This is the
expensive one — the event-driven walk is off and every trade is a callback — and
it is the example that shows the `side is None` branch, which you must write.

```python
# tests/examples/05_tick_imbalance.py
"""Signed-volume imbalance from raw ticks, decided on bars.

Subscribing to ticks is the only way to build order-flow features, and it is
the most expensive thing a strategy can do: every trade becomes a callback, the
event-driven walk is disabled because every tick must be delivered, and a tick
subscriber runs roughly 5x slower than the same strategy on bars alone. That
cost is charged to the wall-clock budget on purpose -- it is a real cost and it
should be visible.

**The `side is None` branch is not defensive programming.** The feed's aggressor
flag has three values -- bid-initiated, ask-initiated, and *unknown* -- and the
adapter maps unknown to `None`. It is rare (roughly one tick in 300,000 on the
NQ data) and that is exactly why folding it into one side never shows up in a
test and biases every order-flow number you compute. Decide what unknown means
and say so.
"""

from collections import deque

from src.core.types import Side


class Strategy:
    def __init__(self, interval="5m", window=200, threshold=0.25, size=1,
                 stop_offset=1.0, target_offset=1.5):
        self.interval = interval
        self.window = window
        self.threshold = threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.signed = deque(maxlen=window)
        self.unknown_side_ticks = 0

    def setup(self, ctx):
        ctx.subscribe(self.interval)
        ctx.subscribe("tick")

    def on_tick(self, ctx):
        t = ctx.tick
        if t.side is None:
            # Unknown aggressor. This strategy contributes nothing rather than
            # guessing -- and counts it, so the choice is visible afterwards
            # instead of being invisible in an aggregate.
            self.unknown_side_ticks += 1
            self.signed.append(0.0)
        elif t.side is Side.BUY:
            self.signed.append(float(t.size))
        else:
            self.signed.append(-float(t.size))

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        if len(self.signed) < self.window:
            return

        total = sum(abs(v) for v in self.signed)
        if total <= 0:
            return
        imbalance = sum(self.signed) / total

        if imbalance > self.threshold:
            ctx.buy(size=self.size, stop_offset=self.stop_offset,
                    target_offset=self.target_offset, tag="imbalance-long")
        elif imbalance < -self.threshold:
            ctx.sell(size=self.size, stop_offset=self.stop_offset,
                     target_offset=self.target_offset, tag="imbalance-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
```

---

## 10. Before you submit

- Every parameter has a default, and the class is named `Strategy`.
- `setup()` subscribes and returns nothing.
- `on_fill` and `on_lifecycle` exist, even if they are `pass`.
- `on_tick` exists if and only if you subscribed to ticks.
- Every `ctx.history(...)` read checks `len()` first.
- `ctx.actionable` is checked before every order.
- Order ids from `buy()`/`sell()` are checked for `None`.
- Stale order ids are cleared in `on_lifecycle`.
- If you touch `tick.side`, the `None` branch is written and says what it means.
- Nothing recomputes the whole history on every callback.
