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
| `on_fill` | `ctx.fill` — the fill; fields listed below |
| `on_lifecycle` | `ctx.event` — the lifecycle event |

---

## 3. Subscriptions

Declared in `setup()`, once each.

```python
def setup(self, ctx):
    ctx.subscribe("5m")
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

**Name a subscription however you subscribed to it.** `"5m"`, the equivalent
`"300s"`, the `Subscription` object, or the canonical id `time:300000000000` —
every entry point that takes a subscription resolves all four to the same thing.
An id you never subscribed to **raises** with a message listing the ones that
exist; it does not quietly read as absent. `ctx.sub_id("5m")` returns the
canonical form for the rare code that wants it.

Subscribing to the same interval twice returns the same subscription. Subscribing
to it twice with different `emit_empty` raises.

**Several bars closing at the same instant arrive in one callback.** If a 5m and a
1m bar both close at 09:35 you get a single `on_bars` with both in `ctx.closed`.
This is deliberate: separate callbacks would make the ordering between them a
hidden variable that changes your results and is recorded nowhere.

So "did *my* bar close?" is a real question, and `ctx.bars` answers it:

```python
def on_bars(self, ctx):
    if "5m" not in ctx.bars:
        return                    # the 1m bar closed, not the 5m one
    bar = ctx.bars["5m"]          # the bar that closed just now
    history = ctx.history("5m")   # every 5m bar that has closed, ever
```

---

## 4. What you can read

### Market state

| Read | What it is |
|---|---|
| `ctx.ts` | nanoseconds since the epoch, UTC |
| `ctx.now` | the same instant as a timezone-aware `datetime` |
| `ctx.session` | session index within the dataset, counting from 0 |
| `ctx.session_start` | when this session opened, nanoseconds UTC |
| `ctx.session_end` | when this session **closes**, nanoseconds UTC — `ctx.session_end - ctx.ts` is the time you have left |
| `ctx.actionable` | `False` during warmup: bars are real, orders are ignored |
| `ctx.last_price` | the most recent trade price |
| `ctx.history(sub)` | every closed bar for a subscription, oldest first |
| `ctx.bars` | the bars that closed **in this callback**; `"5m" in ctx.bars`, `ctx.bars["5m"]`, `ctx.bars.get("5m")` |
| `ctx.closed` | the same bars as a tuple; `ctx.closed_ids()` for their canonical ids |
| `ctx.sub_id(sub)` | the canonical id of a subscription you declared |
| `ctx.ticks()` | every trade event so far — **only populated if you subscribed to ticks** |

`ctx.history(...)` returns a `HistoryView`. It supports `len()`, iteration,
indexing (`view[0]`, `view[-1]`), slicing, and `view.last(k)` for the `k` most
recent oldest-first. Slices and `last()` come back as tuples.

**`len()` is not how much you can read.** It is the true number of bars that have
closed; the engine pushes only the last N of them, N from the profile. So
`len(h)` can say 384 while `h[0]` raises `WindowTooShort`, and iterating the
whole view raises for the same reason. This is the single most common way a
trial crashes — 25 of 162 crashed trials across the session campaigns, more than
any other cause.

Use `last(k)`, which is bounded by what exists and returns fewer than `k`
rather than raising:

```python
# WRONG -- len() passes, the loop crashes
h = ctx.history("5m")
if len(h) >= 200:
    highs = [b.high for b in h]          # WindowTooShort
    first = h[0]                         # WindowTooShort

# RIGHT -- ask for what you need, take what you get
recent = ctx.history("5m").last(200)     # a tuple, 0..200 long
if len(recent) < 200:
    return                               # warmup; a legitimate state
highs = [b.high for b in recent]
```

If you need more history than the window holds, accumulate it on `self` as it
arrives in `on_bars`. Nothing gives you back a bar the engine has already
dropped.

`ctx.bars` is empty outside `on_bars`: nothing closed at the instant of a fill,
a tick or a session open. For the most recent close whenever it happened, use
`ctx.history("5m")[-1]`.

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
| `ctx.working_orders` | read-only `OrderView` copies of those orders |
| `ctx.find_orders(...)` | the working orders matching `tag=`, `order_type=`, `side=`, `parent_id=` |
| `ctx.fills` | fields as `ctx.fill` below; fills since the previous callback |

**What a fill carries, and what it does not.** `ctx.fill` in `on_fill` — and each
element of `ctx.fills` — has exactly these fields:

| Field | Meaning |
|---|---|
| `ts` | when it filled |
| `seq` | sequence number within the instant |
| `order_id` | the id of the order that filled |
| `side` | buy or sell |
| `price` | the fill price |
| `size` | contracts filled |
| `position` | your signed position *after* this fill |
| `last_trade_price` | the market's last trade price |
| `working_order_ids` | ids of every order that can still do something |

**There is no `tag` on a fill.** `tag` lives on the *order*, so `fill.tag` raises
`AttributeError` and the trial is gone — measured on 2026-10-15, 2 of the first 4
trials of a campaign. The order's tag is one hop away:

```python
def on_fill(self, ctx):
    mine = ctx.find_orders(tag="entry")          # by tag, among working orders
    if ctx.fill.order_id in {o.id for o in mine}:
        ...
```

Or keep your own mapping from `order_id` to meaning as the ids come back from
`ctx.buy()` / `ctx.sell()`. A fill tells you *which order* filled, not what you
called it.

You are allowed to know how you are doing — you need it for risk logic and you
would have it live. You are not allowed to know how you are being *judged*: there
is no score, no metric, no split identity and no seed on `ctx`, and
`src.core.scoring` refuses to be imported from a strategy.

An `OrderView` is a frozen copy, never the engine's own object, and carries
`id`, `tag`, `side`, `size`, `filled`, `remaining`, `order_type`, `limit_price`,
`stop_price`, `tif`, `parent_id` and `status`. Assigning to one raises. Change an
order with `ctx.modify()` — the only route that goes through the queue, the
action cap and the T+1 rule, and the only one the run record sees.

**Find your own orders by tag rather than by an id you stored.** A stored id goes
stale the moment the order expires — a DAY order dies at the session close — and
acting on a stale id raises:

```python
stops = ctx.find_orders(tag="protective-stop")
if stops:
    ctx.modify(stops[0].id, trigger=new_level)

legs = ctx.find_orders(parent_id=entry_id)   # both halves of a bracket
```

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

ctx.find_orders(tag=..., order_type=..., side=..., parent_id=...)   # read
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

Give `stop`/`target` **or** `stop_offset`/`target_offset`, never both.

**Which side a bracket leg must sit on, and this is validated:**

| | `stop` | `target` |
|---|---|---|
| after `buy()` (long) | **below** the entry | **above** the entry |
| after `sell()` (short) | **above** the entry | **below** the entry |

Breaking that raises `ContextError` and the trial is gone. Four of the ten
crashes of this kind in this project put a short's stop *below* its entry, which
is the long-side arrangement.

**`stop`/`target` are checked against the actual FILL, not against the price you
computed from.** That is the trap, and it is the other six: a market order does
not fill at the last trade price, so an absolute stop derived from the price you
could see lands on the wrong side the moment the fill slips one tick. Measured
here as `stop 20375.75 must be below the entry 20375.5 for a long` — a quarter
of a point out — and once as a stop exactly equal to the entry.

So for a **market** entry use the offsets, which are measured from the fill and
cannot invert:

```python
ctx.buy(size=1, stop_offset=4.0, target_offset=6.0)   # safe: distances
```

Absolute levels are right when the level is the point — "stop below that swing
low" — and then you want a `limit` entry, so the fill price is one you chose
rather than one you were given. `stop_offset` and `target_offset` are positive
distances in both directions; a zero or negative offset raises.

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

**Knowing when the session ends is not lookahead.** An exchange publishes its
calendar in advance, so `ctx.session_end` is a fact about the schedule, not about
the data — it says nothing about what happens between now and then. Use it: a
strategy that guesses the session's length from what it has already seen is wrong
on the first short session it meets, which is a half day, which is exactly when
being caught long overnight costs something.

Two things it cannot do for you. You still need a callback to act on — a session
whose last twenty minutes hold no trades gives you nothing to exit on, and
nothing to exit *against*. And an order queued on a bar close is eligible from
the next tick, so leave a margin: `ctx.subscribe(interval, emit_empty=True)` gives
you a callback every interval whether or not anything traded, which is the
difference between exiting late and not exiting.

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
- **No network, and no filesystem outside your own source.** Your process runs
  under a sandbox — bubblewrap on Linux, `sandbox-exec` on macOS — with the
  repository mounted read-only and egress denied. A socket does not open; a write
  to `config/`, `runs/`, `data/` or your own file is refused. This is not a style
  rule. A strategy that fetched the real prices for the evaluation dates would
  produce a genuine blotter, a stable dataset digest, a clean fingerprint and a
  replay that reproduces — every other defence in the harness is orthogonal to it,
  which is why this one is a kernel boundary rather than a check.
- **No data beyond the current instant**, including no future bar, no full
  session you are in the middle of, and no next tick.
- **No execution outside the logged path.** `python scripts/agent_check.py` is
  your entry point and it **does not run your strategy**. It reads the file:
  `compile()` for syntax, an AST pass for structure, and mypy against the
  `Strategy` protocol. It tells you pass or fail on properties of the source
  text.

  This is stricter than it used to be, and the reason is worth knowing rather
  than working around. The check used to walk a small dataset and report pass or
  fail with no number, on the reasoning that a check yielding no score could not
  be used for selection. It could: pass/fail is one bit, and a strategy that
  raises when its own P&L crosses a threshold turns the exit code into that bit.
  Eighteen probes binary-searching the threshold recovered peak realized P&L to
  ±0.0002 with **zero trials counted**. Narrowing the channel does not fix that —
  any answer that depends on market data can carry a score — so the answer no
  longer depends on market data at all.

  Scoring happens on the loop driver's path, which you do not invoke, with the
  dataset, split, seed and fill model read from a committed profile rather than
  from any argument. Every invocation of either path appends exactly one ledger
  row, crashes included.

Import what you need from `src.core.types` — `Side`, `OrderType`,
`TimeInForce`, `NS_PER_SECOND` and the rest are all available and are the right
way to compare enum values.

---

## 7a. Your code runs in its own process

Since Task 25 the harness starts a separate process, hands it your file, and
talks to it over a pipe. Everything in section 4 and section 5 still works and
the callbacks are unchanged — the engine cannot tell the difference, and the
golden digests are byte-identical either way. Four things follow that you can
observe:

**History is a window, and reaching past it raises.** Each callback carries the
last N bars per subscription, N declared by the profile (512 by default).

**You cannot change that number and you will be told if you reach past it.** The
window is a committed profile setting and `run.py` takes no arguments, so
`WindowTooShort` is not something to work around -- it means ask for less, or keep
what you need on `self` as bars arrive and read it from there. `ctx.history("5m")`
sliced to the last few hundred is inside the window; iterating the whole of it on
a long session is not.
`len(ctx.history("5m"))` is still the true total — the count is exact — but
`ctx.history("5m")[0]` on a run that has seen 4,000 bars raises
`WindowTooShort` rather than returning something. If you need more than the
window, keep it on `self` as it arrives; that is cheaper than the window anyway,
because you keep the one number you wanted instead of every bar.

**`setup()` bulk data is capped at 256 MB.** `ctx.train_bars("5m")` over the full
NQ training split is 12.7 MB and `ctx.train_bars("1m")` is 62 MB, so bar
intervals are fine. `ctx.ticks()` in `setup()` is 8.0 GB on that split and will
be refused with the number. Fitting on training *bars* works; fitting on raw
training *ticks* does not, and that is a real loss — volume bars, dollar bars and
tick-imbalance features cannot be fitted in-sample. You can still subscribe to
ticks for the live walk.

**Your traceback survives.** A `ValueError` in `on_bars` arrives in the run
record with the child's traceback intact, naming your file and your line. This is
deliberate: a truncated traceback would make every error in section 8 a lie.

**Order ids still come back from `ctx.buy()`.** They are pre-allocated per
callback from a block the size of the action cap, so the call returns
synchronously as it always did. Running out of the block *is* hitting the action
cap — the same event, not a second one.

You cannot reach the engine, the dataset or the scorer from your process, because
they are not in it. The only harness modules loaded beside your file are
`src.core` and `src.core.types`, and that is an allowlist that fails by name —
`src.core.engine.engine`, `src.core.data`, `src.core.scoring.*` and
`src.core.runlog` are each named as forbidden, so an accidental import says which
one leaked rather than only that something did.

---

## 7b. Your iteration loop, and what it costs

**A runtime error now costs a trial.** The static check catches a syntax error, a
missing or misspelled callback, a wrong signature, an async callback, `setup()`
returning subscriptions, a subscription to ticks with no `on_tick`, work done at
module level, and anything mypy can see against the protocol — a return type the
protocol forbids, a constructor the harness cannot call with no arguments.

It cannot catch anything that only happens while the data is moving:

- a `TypeError` or `AttributeError` on the first bar
- `ContextError` from an unsubscribed subscription id
- a malformed order the engine rejects
- `LookaheadError`
- a strategy that runs but never trades
- exceeding the action cap or the wall-clock budget

Those surface in a scored run. A scored run that crashes is a trial:
`outcome: crashed`, `score: null`, and a row that counts toward N exactly like a
completed one.

**That is intended, not a regression.** The project's whole claim is that N
includes crashed and abandoned runs, because a false-discovery rate computed over
only the runs that finished is a false-discovery rate over a filtered sample. A
free crash check is a mechanism for making crashes not count — the same hole in a
friendlier shape.

So the economics of your loop changed. Reading the interface carefully is now
cheaper than trying something and seeing. Worked examples in section 9 are run in
CI, so they are known to work against the current engine; starting from one and
changing it is the cheapest path to a strategy that runs. `docs/design/context-api.md`
is the generated per-attribute reference and it is exhaustive — if an attribute is
not there, it does not exist, and finding that out from the reference is free
while finding it out from a `ContextError` is not.

---

## 7c. Fitted strategies — when your parameters come from the data

Everything above assumes you choose your numbers and write them down. A **fitted**
strategy computes them from data instead: a threshold from a percentile, a
regression's coefficients, a model's weights.

**You may only write one if the campaign registered it.** A proposal defining
`fit()` under a campaign whose `allow_fitted_proposals` is not `true` is refused
before your file is written and before the run starts — it never reaches the
data. If you are not told the campaign is a fitted one, write an ordinary
strategy.

### The three extra methods

| Method | When it fires | Notes |
|---|---|---|
| `fit(self)` | once, at the edge of the fit window | **no `ctx`**, and you cannot trade from it |
| `save_weights(self) -> bytes` | when the harness records the run | return your parameters |
| `load_weights(self, blob: bytes)` | when a run is given weights to load | takes them back |

`fit()` is the only one the engine calls during the walk. It takes no context
because there is no instant to stand in — it happens *between* sessions.

### How you get the training data: you already have it

There is no array handed to you. **The fit window is warmup, generalised.** The
engine walks the first `fit_sessions` sessions of the split exactly as it walks
any other — `on_bars` fires, you accumulate whatever you need on `self` — and
your orders are **ignored**, the way they are during warmup. When the engine
reaches the edge of that window it calls `fit()` once, and everything after is
scored.

```python
# tests/examples/06_fitted_threshold.py
"""A fitted strategy: the threshold comes from the fit window, not from me.

Accumulates absolute delta through the fit window the way warmup accumulates,
fits one number at its edge, and trades that number afterwards. The smallest
thing that is genuinely fitted -- no library, no training loop, no
hyperparameter.
"""


class Strategy:
    def __init__(self):
        self.seen = []
        self.threshold = None

    def setup(self, ctx):
        ctx.subscribe("5m")

    def on_bars(self, ctx):
        history = ctx.history("5m")
        if not history:
            return
        bar = history[-1]
        # Accumulate on every bar, fit window included. Orders here are ignored
        # until the window is past, the same as during warmup.
        self.seen.append(abs(bar.delta))

        if self.threshold is None or not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if abs(bar.delta) < self.threshold:
            return
        if bar.delta > 0:
            ctx.buy(size=1, stop_offset=4.0, target_offset=4.0, tag="fitted-long")
        else:
            ctx.sell(size=1, stop_offset=4.0, target_offset=4.0, tag="fitted-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass

    def fit(self):
        """One parameter, from what was accumulated. Called once, by the engine."""
        if not self.seen:
            raise RuntimeError("fit() ran with nothing accumulated")
        ordered = sorted(self.seen)
        self.threshold = float(ordered[int(0.8 * (len(ordered) - 1))])

    def save_weights(self) -> bytes:
        """The parameters, not the accumulation buffer."""
        return repr(self.threshold).encode()

    def load_weights(self, blob: bytes) -> None:
        self.threshold = float(blob)
```

### `fit()` is called exactly once, and refused afterwards

A second call raises. This is not tidiness: a campaign winner is carried to the
validation split by re-running its source there, and a `fit()` that ran again
would retrain **on the split it is about to be graded on**. Nothing would look
wrong and the number would be meaningless.

For the same reason, a run that is *given* weights loads them and never fits.

### Weights are bytes, not files

`save_weights` returns bytes and `load_weights` takes bytes. Neither takes a
path, because **your process cannot write to disk** (§7a) — the harness writes
them for you and records the digest.

Return the parameters, not your accumulation buffers. `self.seen` above has 284
sessions of data in it and none of it is the model.

### Labels and features: what to do at the edges

If you are training on "what happened after this bar", you need bars after it.
That is not cheating — it is what training *is*, and the engine has already made
it safe: at `fit()` you hold exactly the fit window and nothing beyond it, so a
label that needs a later bar **cannot be computed**. It is absent, not forbidden.

The rule that follows:

> A row whose label **or whose features** would need a bar you do not hold is
> unusable. Drop it.

That bites at both ends. The first rows of the window have no lookback, so a
20-bar feature is missing there. The last rows have no lookforward, so a
10-bar-ahead label is missing there. Drop both.

**Never turn a missing value into zero.** `fillna(0)`, `np.nan_to_num` and
several imputers will quietly convert "I do not know" into "the return was
exactly zero" — a fabricated label that teaches the model to sit still. Assert in
`fit()` that the number of usable rows is roughly what your window and horizon
imply; if it is far off, something filled them.

**At prediction time, a feature you cannot compute means do not trade.** Emit no
order rather than an order built on a zero.

### Features you can import

`src.core.features` holds libraries the engine never calls and you may use:

- `src.core.features.volume_profile.SessionProfile` — volume at price for one
  session: `vpoc()`, `value_area()`, `low_volume_nodes()`, plus
  `distance_from_vpoc()` and `value_area_position()`.

  Feed it with **`add_bar(low, high, volume)`**, not with your own spreading
  rule. It puts a bar's volume evenly across the price levels the bar touched,
  which is the construction that was measured and registered; a hand-rolled
  variant is a different feature under the same name. `add(price, size)` is
  there for a genuine per-trade source.

  Feed it from **one-second bars** unless you have a reason not to. Ticks are
  exact and do not fit the wall-clock budget -- measured at roughly five times
  the cost of `1s` for the whole split, which is over budget before your
  strategy does any work. `1s` reproduces the tick-built profile to about 3%.

  **`vpoc()` is a fragile number.** It is the argmax of a histogram, and on NQ
  the top two price levels are often nearly tied -- measured gaps of 9.8%, 5.7%
  and 0.7% of the peak on three sessions. At 0.7% it flips between levels for
  reasons that are not market structure. `value_area()` and
  `value_area_position()` are quantile-like and far steadier; prefer them, and
  prefer `distance_from_vpoc()` over the raw level.

Everything there answers from what you have added **so far**, which is what makes
it safe mid-session. Call `reset()` on a session boundary; a profile carried
across sessions is a different feature. All of it returns `None` when there is
nothing to compute, never `0.0` — handle the `None`.

The scoring modules remain unreachable (§7): you can know your P&L, never the
number you are graded on.

---

## 8. What will make a run fail

Searchable, because a failure message is the fastest way to learn an interface.
All of these are raised at the call that caused them, not swallowed — a silent
no-op is a trial that looks like a result.

### Wiring

| Message | Cause |
|---|---|
| `MyStrategy does not match the strategy contract:` followed by one line per problem — `on_fill(ctx) is missing`, `on_bars(self) does not take exactly one argument (the context)` | a callback is absent or has the wrong signature. Checked once before the walk, so all four fail the same legible way; `on_tick(ctx) is missing` is the same error when you subscribed to ticks |
| `setup() declared no subscriptions; call ctx.subscribe('5m')` | `setup()` subscribed to nothing |
| `setup() returns nothing now; declare subscriptions with ctx.subscribe('5m') instead of returning them` | you returned a list of subscriptions |
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
| `no subscription 'time:60000000000' (from '1m'); declare it in setup(). Subscribed: [...]` | any id you never subscribed to, through `ctx.history()`, `ctx.bars[...]`, `ctx.sub_id()` or `in ctx.bars` |
| `'time:300000000000' did not close in this callback; test with \`if '5m' in ctx.bars:\` first, or use ctx.history(...) for the most recent close whenever it happened` | `ctx.bars[...]` for a subscription that did not close at this instant |
| `subscriptions are not declared until setup() runs` | resolving an id before `setup()` has finished |

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

### Sent something the protocol does not allow — `strategy_violated_protocol`

Actions cross the process boundary as data and are checked against a whitelist of
operations and their keywords. `ctx.buy(1, unlock_holdout=True)` comes back as
`buy does not take ['unlock_holdout']`, the run is `outcome: crashed` with
`failure_kind: strategy_violated_protocol`, and it counts toward N like any other
crash. The whitelist is in `src/core/engine/remote/protocol.py`.

### Reached past the pushed window — `WindowTooShort`

`ctx.history(...)` carries the last N bars, N from the profile. `len()` is the
true total; an index older than the window raises, and so does iterating the
whole view. Use `view.last(k)`, which returns what exists instead of raising, and
keep anything older on `self` as it arrives. See section 4 for the two idioms
side by side — this is the most common cause of a crashed trial in this project.

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
subscriptions, so "did *my* bar close?" becomes a real question — and
`if "5m" in ctx.bars:` is the answer.

```python
# tests/examples/03_multi_timeframe.py
"""Five-minute entries, filtered by the previous session's daily bar.

Two subscriptions. When both close at the same instant they arrive in **one**
callback, so "did my fast bar close?" is a real question -- and `ctx.bars` is
the answer: it holds exactly the bars that closed in this callback, addressed by
whatever you subscribed with.

`ctx.history(...)` is the other half: the closed bars for a subscription
whenever they closed, which is what a decision reads.
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

    def setup(self, ctx):
        ctx.subscribe(self.fast)
        # A daily subscription on this data closes once per session -- a session
        # is shorter than a day, and any bar still open at the session close is
        # flushed there.
        ctx.subscribe(self.slow)

    def on_bars(self, ctx):
        # Act only when the fast bar closed. The slow one closing is a session
        # boundary, not a decision point.
        if self.fast not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.slow)
        if not len(daily):
            return                      # no completed session yet
        prev = daily[-1]
        bias = prev.close - prev.open
        if bias == 0:
            return

        fast = ctx.history(self.fast)
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
each decision lives, note the gap the bracket in examples 1–3 was closing for
you, and note that it keeps no order ids — every order is found by tag, so a DAY
expiry at the session close cannot leave it holding a stale one.

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

Orders are found by **tag** rather than by an id kept on `self`. A stored id goes
stale the moment the order expires -- a DAY order dies at the session close --
and acting on a stale id raises. `ctx.find_orders(tag=...)` asks the engine what
is working right now, so there is no stale state to get wrong.
"""

ENTRY = "limit-entry"
STOP = "protective-stop"


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

        resting = ctx.find_orders(tag=ENTRY)
        if resting:
            # Still waiting on the entry. Pull it if it has gone stale: a limit
            # that never fills is not free, it is an exposure to a move you no
            # longer have a view on.
            if self.age > self.patience:
                ctx.cancel(resting[0].id)
            return

        # Flat, nothing working: rest a new bid under the market.
        ctx.buy(
            size=self.size,
            type="limit",
            limit=ctx.last_price - self.offset,
            tif="DAY",            # cancelled at the session close, not carried
            tag=ENTRY,
        )
        self.age = 0

    def _manage_open_position(self, ctx):
        stops = ctx.find_orders(tag=STOP)
        if self.age >= self.max_hold:
            for order in stops:
                ctx.cancel(order.id)     # cancel before flattening
            ctx.close()                  # market, reduce_only, tagged "close"
            return
        if stops:
            # Trail the protective stop upward, never down.
            new_stop = ctx.last_price - self.stop + self.trail * self.age
            ctx.modify(stops[0].id, trigger=min(new_stop, ctx.last_price - 0.25))

    def on_fill(self, ctx):
        fill = ctx.fill
        if fill.position == 0:
            self.age = 0
            return
        if not ctx.find_orders(tag=STOP):
            # The first instant protection can exist. reduce_only so it can only
            # ever close this position, never open a new one.
            self.age = 0
            ctx.sell(
                size=abs(fill.position),
                type="stop",
                trigger=fill.price - self.stop,
                tif="GTC",         # survives the session close, like the position
                reduce_only=True,
                tag=STOP,
            )

    def on_lifecycle(self, ctx):
        # Nothing to clean up: no order id is kept across callbacks, so a DAY
        # expiry at the session close cannot leave this strategy holding one.
        pass
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
- Orders are found with `ctx.find_orders(tag=...)` rather than by an id kept
  across callbacks, which goes stale at the first expiry.
- If you touch `tick.side`, the `None` branch is written and says what it means.
- Nothing recomputes the whole history on every callback.
