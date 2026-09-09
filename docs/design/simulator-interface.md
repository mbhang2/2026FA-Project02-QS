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

The strategy **never** computes P&L and **never** imports the scorer. It emits
timestamped intents; the simulator decides what happened. Both halves of that are
enforced, not requested: `src/core/scorer.py` raises on import from `src.strategies.*`,
and a CI path check makes `src/strategies/**` the only writable tree.

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

The strategy declares subscriptions in `setup()`. MVP supports **time bars only** at
arbitrary intervals (1s, 30s, 1m, 5m, daily…). Tick, volume and dollar bars come later —
the subscription type is a tagged union so they slot in without touching the event loop.

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
3. **If this tick's timestamp crosses one or more subscribed bar boundaries**, close
   those bars and deliver **one** `BarEvent` carrying every bar that closed at that
   timestamp. Actionable.

Bar closure is triggered by the first tick with `ts >= boundary`; bar contents are ticks
in `[start, boundary)`. The triggering tick belongs to the *next* bar.

**One callback per timestamp, never one per timeframe.** If a 5m and a 1m bar both close
at 09:35 and they arrive as two callbacks, the ordering between them becomes a hidden
variable that affects results and is recorded nowhere. `BarEvent.bars` is ordered by
interval ascending, then by subscription id, so iteration order is stable.

## Timing rules — the ones that prevent fake alpha

**Actions submitted at tick T are eligible from tick T+1**, strictly after. This applies
to both bar callbacks and fill callbacks. Without it a strategy fills at the close of the
bar it just saw, which is a clean one-bar lookahead producing beautiful, entirely fake
results. Every order carries `eligible_from_seq`, and the fill resolver skips orders
whose eligibility has not been reached. Asserted and tested.

**A strategy cannot read at or beyond its current index.** History is served through a
view whose `__getitem__` raises `LookaheadError` for any position at or past the cursor.
The most recent readable element is the bar that just closed.

**[decided] Cap actions per tick at 64**, and log every hit in the run record. A
ping-pong bug — fill, modify, fill, modify — otherwise hangs the run and the wall-clock
budget kills it with no record of why. A ceiling turns a mystery timeout into a ledger
row. Actions beyond the cap are dropped, counted, and reported; the run does not fail.

**Fill callbacks carry present-instant state only**: fill price and size, order id,
timestamp, resulting position, working orders, last trade price. Never a
partially-formed bar. A strategy wanting finer intra-bar state subscribes to a finer
timeframe — one rule instead of two.

## Actions

On any callback the strategy returns zero or more of: `Submit`, `Modify`, `Cancel`.
Orders have persistent ids, assigned by the simulator and returned on
`OrderAccepted`.

Order types: `market`, `limit`, `stop`, `stop_limit`, and **native bracket / OCO**.
Brackets matter — entry with stop and target attached, armed atomically by the simulator
at fill time. Otherwise a strategy filled at 12:30:05 sits unprotected until 12:35, and
exits managed by strategy code is exactly where the historical fade artefact lived. When
one leg of an OCO fills, the sibling is cancelled in the same instant, before any
callback is delivered.

Time in force: `IOC`, `FOK`, `GTC`, `GTD`, `DAY`.

**[decided] Resting orders at a session boundary.** At session close: `DAY` orders are
cancelled and reported as `LifecycleKind.DAY_EXPIRED`; `GTD` orders are cancelled if
their expiry is at or before the close; `GTC` and unexpired `GTD` survive into the next
session, including bracket legs, whose parent position also survives. `IOC` and `FOK`
never rest. Positions are **not** flattened at the boundary — a strategy that wants to
be flat overnight must say so, and the run record carries the end-of-session position so
that an unintended overnight hold is visible rather than silent.

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
dataset-specific. Lives in `src/core/scorer.py`.

**[open] The metric.** It must be invariant to turnover, leverage, universe size, *and
decision frequency* — candidates may subscribe to different timeframes and produce trade
counts differing by orders of magnitude. For MVP the tuple is a placeholder: net P&L
after costs, trade count, and a vol-normalized per-trade return with its t-statistic.
The interface is fixed even though the metric is not.

Two guards, both required and both implemented:

- CI check on changed paths — `src/strategies/**` is the only writable tree
- `src.strategies.*` cannot import `src.core.scorer`, enforced at import time and
  asserted statically over the source tree

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
    def setup(self, ctx: SetupContext) -> Sequence[Subscription]: ...
    def on_bars(self, ev: BarEvent, ctx: RunContext) -> Sequence[Action]: ...
    def on_fill(self, ev: FillEvent, ctx: RunContext) -> Sequence[Action]: ...
    def on_lifecycle(self, ev: LifecycleEvent, ctx: RunContext) -> Sequence[Action]: ...

class FillModel(Protocol):
    requires: DataCapabilities
    def resolve(self, order: Order, tick: Tick) -> Fill | None: ...
```

`setup()` receives the train split in bulk and returns the subscription set. The three
callbacks are the only actionable surface; all return actions, and returning nothing is
the common case.
