# Backtest simulator — interface specification

The contract between the **immutable side** (data access, split enforcement, event loop,
order lifecycle, fills, blotter, scoring) and the **mutable side** (features, strategy
logic, order decisions). Committed before the implementation and implemented against;
it is also the coordination artifact with the loop owner, who builds against these
types.

Scope is the MVP. Out of scope, deliberately: latency modelling, the N-contracts-through
queue proxy, quote-based fills, tick/volume/dollar bars, the indicator library,
partial-fill size modelling beyond the simple case, and the NQ adapter (Task 4).

Status markers: **[decided]** · **[open]**

## Roles

| Immutable | Mutable (the agent) |
|---|---|
| raw data access, split enforcement | feature computation |
| bar aggregation on request | strategy logic |
| event loop and ordering | order decisions |
| order lifecycle and fill resolution | — |
| blotter construction | — |
| scoring | — |

The strategy acts through a **context object** passed into each callback. It does
not return actions and it does not decide what happened: it calls `ctx.buy(...)`,
the call is queued, the callback returns, and the simulator applies the queue.

### The boundary is the objective function, not P&L

An earlier version of this spec said "strategies emit intents, never P&L". That
was imprecise, and the imprecision cost something: a strategy needs position P&L
for risk logic, it has it in live trading, and it could reconstruct it from its
own fills anyway. Hiding it was theatre.

**The strategy may know how it is doing. It may not know how it is being
judged.** `ctx` exposes realized and unrealized P&L; it exposes nothing the
scorer computes. `src.core.scoring.scorer` and `src.core.scoring.metrics` remain unreachable from
`src.strategies`, enforced at import time and by a static check, and a CI path
check makes `src/strategies/**` the only writable tree.

Not exposed, deliberately: the score or anything the scorer computes, the split
identity, the seed, and any data beyond the current instant.

## Splits

Three, chronological, never random. Random splits leak: adjacent observations are
correlated, so a randomly held-out point sits between two training points and is nearly
free to predict.

- **train** — served in bulk to `setup()`
- **validation** — streamed; the loop scores on this
- **holdout** — the simulator refuses to serve it until explicitly unlocked, once

An **embargo gap** sits between splits, at least as long as the longest feature lookback
plus the longest holding period. The dataset declares it; the simulator enforces it.

**Warmup reaches backward across a boundary; decisions never reach forward.** At the
first validation bar the strategy needs lookback history, and that history legitimately
comes from before the validation start. Starting cold instead would waste
warmup-length validation data, which is the scarce resource. Concretely: the engine
replays `warmup` periods before the served split to fill bar history, and the strategy
receives no actionable callback during that replay.

**[decided] Holdout unlock is one-way and recorded.** `Dataset.unlock_holdout(reason)`
sets a flag that cannot be cleared within a process, and the reason lands in the run
record. Serving holdout without it raises `HoldoutLocked`.

## Subscriptions

The strategy declares subscriptions in `setup()`. Two kinds:

**`ctx.subscribe("5m")` — time bars** at arbitrary intervals (1s, 30s, 1m, 5m,
daily…).

**`ctx.subscribe("tick")` — every trade event**: price, size, aggressor side,
timestamp. Added in Task 7, because until then the agent could only subscribe to
time bars and the aggregation choice was still being made for it. Volume bars,
dollar bars, tick-imbalance bars, cumulative delta and every order-flow feature
need the events themselves. Aggressor side is `None` where the feed does not
say, which a strategy must handle rather than assume.

It is **expensive by design**. A few hundred thousand trades a session is tens of
millions of callbacks across a split, and the wall-clock budget is what
disciplines it: a strategy wanting tick granularity pays for it in trials, which
is an honest trade rather than a hidden one.

**This was an API break, not an addition.** `Subscription.interval_ns` was a
required positive integer; it is now optional, because a tick stream has nothing
to bucket by and inventing an interval for it would have been the kind of lie
that costs a coordinated break later. Volume and dollar bars fit the existing
`kind` tag without further change.

- Bars are **anchored to the session open**, not wall-clock.
- A bar is **stamped with its open timestamp** but **delivered on close**. A bar opening
  12:30 is delivered at 12:35.
- Bars carry **both** `open_ts` and `close_ts`. Stamping by open while delivering at
  close is where a downstream join silently grabs future data; having both present means
  nothing has to infer which it holds.
- Multiple simultaneous subscriptions are supported.

**[decided] Empty bars are skipped by default.** A bar interval containing zero ticks
produces no bar and no callback. Per-subscription `emit_empty=True` instead emits a bar
with zero volume, `open=high=low=close` carried from the previous close, and
`tick_count=0`, for strategies that want a regular grid. `emit_empty` cannot synthesise
a bar before the first tick of the session — there is no previous close to carry — so
leading empty intervals are skipped under either setting. The flag is recorded per
subscription in the run record.

## Event loop

The simulator walks ticks. At each tick, in this order:

1. **Resolve resting orders** against the tick via the fill model → `FillEvent`s.
2. **Deliver fill callbacks.** Actionable.
3. **Deliver the tick event**, if subscribed. Actionable.
4. **If this tick's timestamp crosses one or more subscribed bar boundaries**, close
   those bars and deliver **one** `BarEvent` carrying every bar that closed at that
   timestamp. Actionable.

Fills first because a strategy should learn about its own fill before being asked
to react to the trade that caused it. The tick before the bars because the tick
that closes a bar belongs to the *next* bar — delivering it after the close would
report it out of order.

Bar closure is triggered by the first tick with `ts >= boundary`; bar contents are ticks
in `[start, boundary)`. The triggering tick belongs to the *next* bar.

**One callback per bar close, never one per timeframe.** If a 5m and a 1m bar both close
at 09:35 and they arrive as two callbacks, the ordering between them becomes a hidden
variable that affects results and is recorded nowhere. `BarEvent.bars` is ordered by
interval ascending, then by subscription id, so iteration order is stable.

The rule is about *closes*, not about ticks, and the distinction is not academic. One
tick arriving after a quiet stretch closes several intervals at once, and those
intervals did not close at the same instant — they arrive as separate events, oldest
close first, each stamped with its own `close_ts`. Lumping them into one event labelled
with the tick's timestamp made `ctx.bars` claim two closes at one moment and put a bar
in history alongside one that closed a minute later. Invisible until Task 17 gave the
synthetic generator empty intervals.

So `BarEvent.ts` is the bars' close and `ctx.ts` is when you were told, and on thin data
those differ: **you learn about a bar when the next trade arrives**, because the engine
is tick-driven and cannot call you when nothing happens. A strategy that needs a
heartbeat regardless subscribes with `emit_empty=True`.

**A session's tail bar is stamped with the session close, not the interval boundary it
never reached.** It is truncated by definition. Stamping it at the boundary put a
`close_ts` in the future on the one bar per session where a position is most likely
still open — delivered at the close, claiming to close up to a full interval later.

## Timing rules — the ones that prevent fake alpha

**Actions submitted at tick T are eligible from tick T+1**, strictly after. This applies
to both bar callbacks and fill callbacks. Without it a strategy fills at the close of the
bar it just saw, which is a clean one-bar lookahead producing beautiful, entirely fake
results. Every order carries `eligible_from_seq`, and the fill resolver skips orders
whose eligibility has not been reached. Asserted and tested.

**A strategy cannot read at or beyond its current index.** History is served through a
view whose `__getitem__` raises `LookaheadError` for any position at or past the cursor.
The most recent readable element is the bar that just closed.

**[decided] The wall-clock budget is enforced, and an abandoned run is a trial.**
`wall_clock_budget_s` comes from the run profile. The action cap protects against
a ping-pong bug; it does not protect against an O(n²) feature recomputation,
which is the likelier failure once a strategy computes features from raw ticks. A
nightly loop of hundreds of runs becomes a nightly loop of three if one strategy
can spin. On overrun the run terminates and emits a ledger record with outcome
`abandoned_on_budget` — not `crashed`, because nothing went wrong except that the
strategy was too slow, and the distinction matters when reading the ledger. The
clock is checked every 4,096 ticks *and* every 32 callbacks, since a slow
strategy is slow in callbacks.

**[decided] `reduce_only` is enforced.** It was accepted, carried on the order and
checked nowhere: a strategy passing it on an oversized order got a position flip
and no complaint. It is now clamped to the current position, rejected when flat,
and rejected when it would increase exposure. A parameter that is accepted and
silently ignored is worse than an absent one, because the strategy believes it
took effect — the same sweep fixed GTD orders, which expired only at a session
boundary and so rested through any mid-session expiry.

**[decided] Cap actions per tick at 64**, and log every hit in the run record. A
ping-pong bug — fill, modify, fill, modify — otherwise hangs the run and the wall-clock
budget kills it with no record of why. A ceiling turns a mystery timeout into a ledger
row. Actions beyond the cap are dropped, counted, and reported; the run does not fail.

**Fill callbacks carry present-instant state only**: fill price and size, order id,
timestamp, resulting position, working orders, last trade price. Never a
partially-formed bar. A strategy wanting finer intra-bar state subscribes to a finer
timeframe — one rule instead of two.

## The context API

On any callback the strategy calls methods on `ctx`. **Calls are queued, not
applied**: the context records the intent, the callback returns, and the engine
applies the queue. Nothing about the guarantees changes — the recorded intent
list is exactly what the strategy used to return, so T+1 eligibility, the action
cap and auditability are untouched.

Why a facade rather than returned structs. An LLM writes hundreds of these files,
and `ctx.buy(...)` is the idiom in backtrader, zipline, QuantConnect and
freqtrade — there is a great deal of it in training data, and assembling lists of
dataclasses is not. One class with docstrings autocompletes and can be dumped as
a reference the agent reads; a set of struct definitions cannot. And a method can
reject `size=0` or a stop on the wrong side of the entry with a specific message
at the moment of the mistake. Every malformed action was a wasted trial and a
wasted entry in N; this converts some of them into corrected ones.

```python
class Strategy:
    def setup(self, ctx: SetupContext) -> None:
        ctx.subscribe("5m")
        self.model = fit(ctx.train_bars("5m"))

    def on_bars(self, ctx: BarContext) -> None:
        if "5m" not in ctx.bars:
            return
        bar = ctx.bars["5m"]
        if self.signal(bar) > 0 and not ctx.position:
            self.oid = ctx.buy(size=1, stop=bar.low - 25, target=bar.close + 100)

    def on_fill(self, ctx: FillContext) -> None: ...
    def on_lifecycle(self, ctx: LifecycleContext) -> None: ...
```

**Reads.** `ctx.bars` (the bars that closed *in this callback*, by subscription —
empty in every other callback), `ctx.history(sub)` (a `HistoryView` that raises on
reaching forward), `ctx.position`, `ctx.working_orders` (frozen `OrderView` copies,
never the engine's own objects), `ctx.find_orders(tag=...)`, `ctx.fills` (since the
previous callback), `ctx.last_price`, `ctx.ts` / `ctx.now`, `ctx.session`,
`ctx.session_start` / `ctx.session_end`, `ctx.realized_pnl`, `ctx.unrealized_pnl`,
`ctx.equity`.

**`ctx.session_end` is not lookahead.** An exchange publishes its calendar in advance,
so the close is known at the open; it says nothing about what happens between now and
then. It exists because without it a strategy cannot be reliably flat at a boundary —
every alternative guesses the session's length from what it has already seen, and every
such guess is wrong on the first session shorter than anything before it. That is a half
day, which arrives without warning and is exactly when being caught long overnight
matters. The calibration apparatus guessed twice, from a bar count and then from a
learned duration, and both were wrong on real data.

Two things it cannot do. You still need a callback to act on, and a session whose last
minutes hold no trades gives you nothing to exit on and nothing to exit *against*. And
an order queued on a bar close is eligible from the next tick, so leave a margin.

Every accessor returns a primitive, a tuple, a frozen dataclass or a read-only
view; `tests/test_boundary.py` walks the whole context surface by introspection
and asserts it, so the next accessor added is covered without anyone remembering.
`ctx.working_orders` used to hand back the engine's live `Order` objects, and
assigning to one took effect — skipping the queue, the cap and the T+1 rule, and
reaching the run record nowhere.

**Naming a subscription.** `"5m"`, `"300s"`, the `Subscription`, or the canonical
`time:300000000000` all resolve to the same thing, through one resolver. An id
that was never subscribed raises and names the ones that exist, rather than
reading as absent — a strategy that silently never trades is the worst failure
mode available.

**Writes.** `ctx.buy(...)`, `ctx.sell(...)`, `ctx.close()`, `ctx.modify(oid, ...)`,
`ctx.cancel(oid)`. Order methods **return the order id** so modify and cancel have
something to reference; the id is allocated when the call is queued, not when it
is applied.

Order types: `market`, `limit`, `stop`, `stop_limit`, and **native bracket /
OCO**. Brackets matter — entry with stop and target attached, armed atomically by
the simulator at fill time. Otherwise a strategy filled at 12:30:05 sits
unprotected until 12:35, and exits managed by strategy code is exactly where the
historical fade artefact lived. When one leg of an OCO fills, the sibling is
cancelled in the same instant, before any callback is delivered.

Bracket levels may be given as **absolute prices** (`stop=`, `target=`) or as
**distances from the fill** (`stop_offset=`, `target_offset=`). Mixing the two
forms on one order is rejected.

**Prefer offsets on a market order.** They resolve at the fill, so there is no
side to get wrong. Absolute levels on a market order are checked against the last
trade price at submission, which is the best estimate available then and still
permits the bracket to arm on the wrong side after a T+1 fill at a different
price. **Known limitation:** in that case the simulator arms the level anyway and
the position is stopped out on the next tick. That is punitive, and deliberately
so — the test for an ambiguity is whether it favours the strategy, and this one
does not. Absolute levels are the right form for a limit entry, where the entry
price is known at submission.

Time in force: `IOC`, `FOK`, `GTC`, `GTD`, `DAY`.

**[decided] The context is valid only during its own callback.** A strategy can
stash `ctx` in `setup()` and reach for it later; doing so raises
`ContextExpired`. Otherwise a strategy could hold a context across the walk and
read state from an instant it is not standing in.

**[decided] Validation raises with a specific message**, at the call, naming the
rule: a non-zero size, a stop below the entry for a long and above it for a
short, a target on the opposite side, a limit price on a limit order, a known
subscription, an order that is still working before a modify or cancel.
Silent no-ops are the failure mode this replaces.

**[decided] The action cap is checked when a call is queued**, not when the queue
is applied, so an order dropped past the cap never consumes an id. Drops are
counted and reported exactly as before.

## Lifecycle events
## Lifecycle events

Delivered on the same actionable channel as fills: `ORDER_ACCEPTED`, `ORDER_REJECTED`,
`GTD_EXPIRED`, `DAY_EXPIRED`, `CANCEL_CONFIRMED`, `SESSION_OPEN`, `SESSION_CLOSE`.

## Fill models

Two, both trade-based for MVP (the first real dataset is `trades` only — no book, no
BBO):

- **favorable** — fill if a trade occurs at the limit price or better
- **unfavorable** — fill only if a trade occurs strictly through the limit price

**The loop scores on `unfavorable`.** Whichever model the agent is scored against is the
one it learns to exploit; optimizing under favorable and checking under unfavorable
systematically selects for strategies whose edge lives in the optimism. Favorable is
diagnostic, and the **spread between the two modes** is itself informative — a candidate
that collapses between them is telling you where its edge comes from. The spread is
exposed as a first-class output and is a natural committee input later.

Quote-based branches slot in when `tbbo` arrives: a `Dataset` declares
`DataCapabilities(has_trades, has_quotes, has_book)` and a fill model declares what it
requires. Requiring a capability the dataset lacks raises at wiring time, not mid-run.

**Record the fill model and the data capabilities in every run's output.** If fills
silently vary with available data, two ledger rows are not measuring the same thing.

## Scorer

`score(blotter, *, period) -> ScoreTuple`. Blotter in, score tuple out, nothing
dataset-specific. Lives in `src/core/scoring/scorer.py`.

**[open] The metric.** It must be invariant to turnover, leverage, universe size, *and
decision frequency* — candidates may subscribe to different timeframes and produce trade
counts differing by orders of magnitude. For MVP the tuple is a placeholder: net P&L
after costs, trade count, and a vol-normalized per-trade return with its t-statistic.
The interface is fixed even though the metric is not.

Two guards, both required and both implemented:

- CI check on changed paths — `src/strategies/**` is the only writable tree
- `src.strategies.*` cannot import `src.core.scoring.scorer`, enforced at import time and
  asserted statically over the source tree

## The run entry point

`src/strategies/current.py` is a library — no `main()`, nothing that runs itself.
`run.py` runs it, and `run.py` is immutable:

```
run.py                                    <- immutable
  reads config: dataset, split, fill model, seed, wall-clock budget
  imports src.strategies.current
  Engine(...).run()
  score(blotter)                          <- the harness calls it, never the strategy
  appends the run record to runs/ledger.jsonl
```

**[decided] Run configuration is immutable, for the same reason the scorer is.**
Whoever chooses the split, the fill model and the seed can point a run at the
holdout, select favorable fills, or retry until a lucky seed lands. None of those
require touching the scorer and all three are cheaper ways to fake a result.
Config comes from `config/`, never from the strategy and never from arguments the
agent controls.

**[decided] The strategy source is stored beside the ledger row.** `strategy_digest`
identifies a trial; it does not reproduce one. To re-run trial 47 you need its
source, and at that moment the source was uncommitted and is now gone.
`runs/sources/<digest>.py` is content-addressed, so a hundred runs of one
strategy cost one copy, and `scripts/replay.py <ledger> <row> --check` reproduces
the blotter digest from it. Without this, "every trial is reproducible" held only
for accepted trials that survived to a commit — and the rejected ones are exactly
what N counts.

**[decided] The agent may run evaluations, and every one counts.** An agent that
cannot execute what it writes ships syntax errors into the loop, so it gets a
fixed-config entry point: validation split only, no meaningful arguments, holdout
unreachable. **Every invocation emits a ledger record**, whatever triggered it. If
an agent can run fifty exploratory backtests, pick the best and present it as one
candidate, then N is fiction and the multiple-testing argument collapses
silently, in the one place nobody would look. Each record carries `trigger`
(`harness` or `agent`) so the distinction is visible without being exploitable.

This constrains the loop harness as much as the simulator, and it belongs in the
spec its owner reads.

## The walk is event-driven

The engine does not step every tick. Between two bar closes, with no resting
order reachable, nothing can happen: no fill, no callback, no action. The only
work in that span is bar aggregation, and that folds in one operation.

So the walk jumps to `min(next bar boundary, first tick that could reach any
resting level)`. Both are computed by search rather than iteration.

**It is a pure optimization and is tested as one.** `Engine(event_walk=False)`
steps every tick, and `tests/test_event_walk.py` runs each scenario both ways and
asserts identical blotters row for row -- including the cases the jump is most
likely to get wrong: several resting triggers between the same pair of closes, a
bracket arming while its sibling triggers, a stop-limit arming, a trigger landing
exactly on a boundary, a GTD expiry mid-span, and a session where the strategy is
never flat.

The safety property is one-sided. The price conditions used to find the next
candidate are deliberately **weaker** than the fill model's -- `<=` where
`unfavorable` needs `<`, and a stop-limit's trigger rather than its limit. The
walk may therefore stop somewhere nothing happens; it cannot skip somewhere
something would have. The fill model remains the only thing that decides a fill.

`RunRecord.ticks_stepped` against `RunRecord.ticks` reports how much was skipped,
so the speedup is a measurement rather than a claim.

**A tick subscription disables it.** Every tick must be delivered, and that is
correct: the cost of tick granularity belongs in the wall-clock budget where it
is visible, not hidden. Measured on real NQ sessions, a tick subscriber runs
**5.1x slower** than the same strategy on bars alone. Bar interval, by contrast,
is nearly free -- 30-second bars cost the same as five-minute ones, because the
walk stops at boundaries and fills either way.

## Determinism

Same data, same strategy, same seed → **byte-identical blotter**. The honest-N argument
depends on any trial being reproducible. Enforced by: seeded generators threaded
explicitly, no reliance on set or dict iteration order for anything that reaches output,
and stable sort keys wherever events are ordered.

## Per-run record

Every run emits, for the ledger: dataset name and `digest()`, split served, fill model,
data capabilities, seed, git SHA, wall-clock, subscription set, action-cap hits, and
outcome including crashes and abandonment. Written as one JSON object; the loop owner
appends it to the trial ledger.

## Types

```python
class Dataset(Protocol):
    name: str
    def capabilities(self) -> DataCapabilities: ...
    def sessions(self) -> Sequence[SessionId]: ...
    def calendar(self) -> Calendar: ...          # pre-registered train/val/holdout
    def ticks(self, split, *, warmup=0) -> Iterator[Tick]: ...
    def digest(self) -> str: ...                 # pins the exact data a run used

class Strategy(Protocol):
    def setup(self, ctx: SetupContext) -> None: ...
    def on_bars(self, ctx: BarContext) -> None: ...
    def on_fill(self, ctx: FillContext) -> None: ...
    def on_lifecycle(self, ctx: LifecycleContext) -> None: ...

class FillModel(Protocol):
    requires: DataCapabilities
    def resolve(self, order: Order, tick: Tick) -> Fill | None: ...
```

`setup()` receives the train split in bulk and returns the subscription set. The three
callbacks are the only actionable surface; all return actions, and returning nothing is
the common case.
