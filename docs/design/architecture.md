# Architecture

A map of the system for someone joining it or reviewing it. What each part is
responsible for, what depends on it, and where the reasoning lives.

**This is a map, not a duplicate.** The *why* behind the simulator's semantics is
`docs/design/simulator-interface.md`; the *why* behind the score is
`docs/design/metric.md`; the dataset's own invariants are
`docs/datasets/nq-es-qqq.md`. This document says what exists and how it fits
together, and links to those rather than restating them.

Audience note, because the repo has four: `CLAUDE.md` is for an agent editing
this repo, `docs/data-inventory.md` is for a human auditing the bucket,
`docs/agent/data.md` and `docs/agent/strategy-api.md` are for the research agent
writing strategies, and this file is for you.

---

## 1. What the system is

An automated research loop. An agent proposes a change to a trading strategy, a
backtest runs, a score comes out, and the change is kept or discarded. **The
deliverable is the loop and its keep/discard gate, not a profitable strategy.**
Everything below exists to make that gate honest: the trial count is real, the
data the trial saw is pinned, and the number it is judged by is not reachable
from the code being judged.

Three boundaries do all the load-bearing work. Everything else is implementation.

### Boundary 1 — the write path is a whitelist

The agent may write **only** under `src/strategies/`. Not because everything else
lives somewhere special, but because `scripts/check_agent_paths.py` allows
`src/strategies/` and refuses every other path in the diff. CI runs it on
branches the loop pushes (identified by the `agent/` prefix) and `--force-check`
runs it anywhere. Tool permissions in the agent config are the other half; the CI
check is the half that actually holds, because configs drift and get copied
wrong.

A consequence worth stating plainly: **the directory layout is free to optimize
for legibility.** Gathering immutable code into one tree to make it immutable
would be a blacklist, and blacklists need maintaining.

### Boundary 2 — the objective is unreachable from the strategy

`src/core/scoring/metrics.py`, `src/core/scoring/scorer.py` and
`src/core/scoring/calibration/__init__.py` each refuse to be imported from
anything under `src.strategies`, by walking the import stack at module import
time and raising `ImportError` naming the rule. The guard travels with the file,
so moving it does not weaken it. A strategy that can compute its own objective
can optimize a number it controls; one that can read the null it is judged
against can be written to clear it.

`src/core/scoring/__init__.py` deliberately re-exports **nothing**. Importing a
submodule imports its parent package first, so a convenience re-export there
would fire the metric's guard on the way to `calibration` and report the wrong
prohibition.

The context facade (§4) is the same boundary from the other side: it exposes
position, P&L and equity — a strategy needs those for risk logic and has them in
live trading — and exposes no score, no split identity, no seed, and no data
beyond the current instant.

### Boundary 3 — build-time produces, run-time consumes

`scripts/` produce committed artifacts. `src/core/` consumes them during a trial.
Nothing under `src/core/` may import anything under `scripts/`, and
`tests/test_layout.py` asserts it two ways — statically over the source, and by
importing all of `src.core` in a fresh interpreter and checking `sys.modules` by
file location, which catches a `sys.path` insertion followed by a bare
`import inventory`.

This is what makes the committed artifacts authoritative. If `inventory.py` were
reachable from a trial, the trial could re-derive coverage and disagree with the
manifest it is measured against. If `tabulate_null.py` were, a trial could
tabulate its own null — marking its own homework.

The dependency is directional, not a wall: scripts import `src.core` freely, and
several do. `scripts/inventory.py` reads DBN through `src/core/data/dbn.py` on
purpose, so the manifest and the adapter cannot disagree about how bytes decode.

---

## 2. One trial, end to end

```
config/run.yaml ──┐
                  ├─> run.py: pick profile, build dataset, import strategy
data/manifest.json┘
                        │
                        v
        src/core/data/*       the split, the embargo, the holdout lock,
                              session assembly, the tick stream, digest()
                        │  ticks
                        v
        src/core/engine/*     resolve resting orders -> fills -> tick event
                              -> bar closes -> lifecycle; queued intents
                              applied after each callback
                        │  fill log (append-only, one record per fill)
                        v
        engine/reconstruct.py reconstruct(fills) -> blotter, as a pure function
                        │  blotter (one row per fill)
                        v
        src/core/scoring/*    per-session returns -> Sortino -> ScoreTuple
                        │
                        v
        run.py: RunRecord + score + diagnostics -> runs/ledger.jsonl (append)
                                                -> runs/sources/<digest>.py
                                                -> runs/fills/<digest>.jsonl.gz
```

In order:

1. **`run.py`** loads `config/run.yaml`, resolves a profile (`harness` or
   `agent`), and builds the dataset with the profile's seed. It imports the
   strategy by name from `src/strategies/current.py` — never accepts one as an
   argument.
2. **The dataset** serves the split it was asked for. `Split.HOLDOUT` raises
   `HoldoutLocked` unless it has been unlocked once, with a reason, which lands
   in the run record.
3. **`Engine.run()`** calls `setup(ctx)` with the training split in bulk, reads
   back the declared subscriptions, then walks the served split — warmup sessions
   first, where callbacks fire but orders are inert and counted.
4. **Every fill is appended to the run's fill log**, in one place, as a frozen
   `FillRecord`. The log lives on the run and nothing reachable from a strategy
   holds a reference to it. `reconstruct(fills)` then rebuilds the blotter from
   the log alone — a pure function of the fills, taking no engine state. The
   blotter's `digest()` is a SHA-256 over rounded row tuples: same data, same
   strategy, same seed → byte-identical blotter.
5. **The scorer** turns *the reconstructed* blotter plus the split's session
   calendar into a `ScoreTuple`. Sessions the strategy sat out still count. The
   engine's running accumulator is kept, compared against the reconstruction, and
   used for nothing else; a disagreement invalidates the trial.
6. **`run.py` appends exactly one ledger row per invocation**, crashes and
   abandoned runs included, tagged with what triggered it and what it was *for*
   (`purpose`: trial, smoke or replay), and copies the source of whatever
   actually ran to `runs/sources/<digest>.py` so the trial can be re-run later.
   N is the count of rows with `purpose == "trial"` — a filter over the ledger,
   never a shorter ledger.

---

## 3. `src/core/data/` — where data enters, and where the split is enforced

The only way data reaches the simulator. A dataset is not just a feed: it owns
the chronological split, the embargo and the holdout lock, so those are
*enforced* rather than requested.

| File | Responsibility |
|---|---|
| `src/core/data/dataset.py` | The `Dataset` protocol, `SplitBounds`, `Calendar`, `HoldoutLock`, `sessions_for()`, `warmup_start()`, `calendar_path()`, `load_calendar()`, `CalendarNotWritten` |
| `src/core/data/synthetic.py` | `SyntheticDataset` — irregular tick generator with a planted-signal switch |
| `src/core/data/nq.py` | `NQDataset` — the R2/Databento NQ adapter, session assembly, early closes, front-month selection, local cache |
| `src/core/data/dbn.py` | DBN-under-zstd reading rules, in one place |

**`dataset.py`.** `Calendar.__post_init__` refuses splits that are not
chronological or that do not honour the embargo, so a bad split fails at
construction rather than producing plausible numbers. `HoldoutLock` is one-way
and cannot be re-locked — a flag that can be toggled back is a flag someone
toggles back — and `unlock_holdout()` requires a reason for the record.
`calendar_path(name)` derives `config/calendars/<dataset>.yaml` from
`Dataset.name` so the adapter, its error message and the file a human is meant to
write cannot drift to different names. `load_calendar()` maps that file's
**dates** onto session positions against whichever session list the dataset
publishes — dates rather than counts is what lets a future GEX-using dataset take
547 sessions over the same periods the NQ trades dataset takes 564 over, aligned
rather than one constraining the other. A missing file raises `CalendarNotWritten`,
its own exception type so a caller can tell "the human has not decided" from "this
is broken".

NQ's calendar is committed: `config/calendars/nq-trades.yaml`, 384 / 86 / 73 with
an embargo of 10, and **the file's commit date is the pre-registration date** —
there is deliberately no date field inside it, because a date written into a file
can disagree with the commit that introduced it.

**`synthetic.py`.** Built before the first real adapter deliberately: two
implementations from day one is what keeps the `Dataset` interface honest, and it
makes null and power calibration ordinary runs instead of special cases.
`signal_strength = 0` is pure noise and the gate must accept essentially nothing;
`signal_strength > 0` plants an edge of known size. It generates *ticks*, not
bars, so synthetic data exercises the same aggregation path as a real feed.

**It is irregular by default, and that is the point.** Three real-data defects
were invisible here for the same reason: the generator was uniform in exactly the
ways markets are not. Sessions all the same length hid a strategy that could not
handle a half day; every interval having ticks hid a session length learned as a
bar count; sessions starting at the same price hid overnight holds realising the
previous day's drift. Each cost a task to find on real data. It now has variable
session lengths with early closes, runs of intervals with no trade (thinness tied
to volatility, so the calm days are the thin ones), volatility clustering across
sessions, and continuous prices across boundaries. Each is independently
toggleable — which earned its keep immediately, since the payoff-shape defect
Task 17 found was produced by no single irregularity but by their interaction.
`uniform=True` opts back into the old world, and the golden blotter digests pin it
so they move only when behaviour does.

**`nq.py`.** Everything specific to this bucket: sessions assembled from the two
UTC-day objects each one straddles, front-month selection among outrights by
session volume, and a local `.npz` session cache. It reads the committed
`data/manifest.json` for its session list rather than re-deriving one — the
manifest is ground truth and the adapter cross-checks against it. `digest()`
pins the actual bytes from the manifest's per-object ETags, so a ledger row names
the data it ran on.

**Early closes reach the session bounds.** `session_bounds` computed 17:00 CT to
16:00 CT from the date alone, so on a half day a strategy reading
`ctx.session_end` was told it had four hours it did not have — which stranded a
position on 12 of 384 training sessions and cost the null pool 25 sessions to a
workaround. It reads `data/nq-session-closes.json` now, built by
`scripts/session_closes.py` from the exchange calendar and cross-checked against
the observed last trade. All 384 end flat.

**`Tick.session` here is a Gregorian ordinal, not a position**, deliberately: a
position means something different depending on how many sessions the spec
trimmed to. `Calendar` counts by position, so `scored_sessions()` and
`warmup_sessions()` translate, and that is the one seam between the two id
spaces. A position handed to `session_bounds()` yields a date in the year 2 AD
and per-session returns keyed to sessions that do not exist — wrong in a way
nothing downstream could notice.

**`dbn.py`.** The rules that bite: no footer, two prefixes are zstd-compressed
twice, never seek past a header by fixed offset (v1 metadata is 2,438 bytes and
v3 is 3,272), and symbol identity comes from the header and never the path. The
adapter and `scripts/inventory.py` both read through it; they used to have
separate copies, which is exactly the thing that drifts until one is quietly
wrong.

Detail and reasoning: `docs/datasets/nq-es-qqq.md`, `docs/data-inventory.md`.

---

## 4. `src/core/engine/` — the walk

The immutable side of the split. What a strategy can see and do, and when.

| File | Responsibility |
|---|---|
| `src/core/engine/engine.py` | The event loop, order lifecycle, brackets/OCO, the event-driven walk, `RunRecord` |
| `src/core/engine/context.py` | The strategy-facing facade, the intent queue, interval parsing |
| `src/core/engine/bars.py` | `BarBuilder` — session-anchored time-bar aggregation |
| `src/core/engine/fills.py` | `FavorableFill` / `UnfavorableFill`, capability checking |
| `src/core/engine/view.py` | `HistoryView` / `BarHistory` — the read surface that cannot reach forward |
| `src/core/engine/reconstruct.py` | `reconstruct(fills)` — the blotter the fill log implies, as a pure function; `log_digest` |

**Ordering within a tick** is fixed and is the thing most worth knowing: resolve
resting orders → deliver fill callbacks → deliver the tick event if subscribed →
deliver **one bar callback per close**, each carrying every bar that closed at
that instant → drain lifecycle events. One callback rather than one per
timeframe, because otherwise the ordering between a 5m and a 1m close at the same
moment is a hidden variable that changes results and is recorded nowhere.

The rule is about closes, not ticks: one tick after a quiet stretch closes
several intervals at once, and those did not close together, so they arrive as
separate events in close order. On thin data `ctx.ts` therefore runs ahead of
`close_ts` — you learn about a bar when the next trade arrives, because the
engine cannot call you when nothing happens. A session's tail bar is stamped with
the session close rather than the interval boundary it never reached.

**T+1.** Anything submitted while handling the callback at sequence *S* is
eligible strictly after *S*. Without it a strategy fills at the close of the bar
it just saw, which is a clean one-bar lookahead producing beautiful and entirely
fake results.

**Nothing reaches the engine or the dataset from a context.** A context holds an
`EnginePort` — a frozen record of callables, each bound to the narrowest object
that can answer it — and no engine, no dataset, no blotter. It used to hold both
as `ctx._engine` and `ctx._dataset`, and adversarial review used them to read the
split and the seed, call `unlock_holdout`, pull a holdout tick and mutate engine
state. `tests/test_boundary.py` had been told to skip underscore names, so it
never looked: the test encoded the same assumption the code did.

The replacement test walks everything — private attributes, the `__self__` of
bound methods, `__closure__` cells — and it found two more paths after the
refactor was written, which is the argument for walking rather than reasoning.
One exemption remains and is named: `setup()` streams the training split lazily,
so something in it must be able to produce those ticks, and in-process that means
holding a reference. It holds the *sealed* dataset, which has no `unlock_holdout`
and no `holdout_lock`, and the lock still refuses any split but the one served.

**The score is computed from a fill log, not from a running total.** The second
adversarial pass reached `_RunState` through the closure cell of a bound port
callable and set `avg_price` before flattening. Sortino went from −0.2547 to
+4.2089 and significance from −1.611 to +26.619, with a genuine 1,255-row
blotter, a stable digest, a replay that reproduced it exactly, and an untouched
scoring fingerprint. Every downstream check passed, because every downstream
check was reading a number the strategy had already written to.

The fix is not another guard on `avg_price`. It is that the score no longer reads
anything a strategy can write:

- every fill is appended, in **one** place, to an append-only log of frozen
  `FillRecord`s — `(seq, ts, session, order_id, side, size, price, liquidity,
  tag)`, facts rather than derivations;
- `reconstruct(fills, commission_per_contract=…)` rebuilds position, average
  price and realized P&L from that log as a **pure function**, taking no engine
  state, and returns a `Blotter` so nothing downstream changes shape;
- the scorer consumes that reconstruction. The engine's accumulator survives as a
  diagnostic and is compared against it on every run; a disagreement raises and
  the row is written as `invalidated_fill_log_mismatch`, with both numbers;
- the log's digest goes in the ledger row and the log itself is stored at
  `runs/fills/<digest>.jsonl.gz`, so `scripts/replay.py` **recomputes** the P&L
  from the fills instead of re-running the mutation and agreeing with itself.

The mutation still succeeds. `avg_price` is still wrong afterwards. That is the
point: blocking one attribute is a race against whoever finds the next one, and
the reconstruction does not care which one they find.
`tests/test_boundary.py` runs the reproduction unmodified and asserts the score
is **unchanged**.

**Where the engine still reads mutable state to make a decision.** Enumerated,
not fixed — these decide behaviour, not the score, and changing them is an engine
semantics change that Task 21 was scoped out of:

| Site | What it reads | What it decides |
|---|---|---|
| `engine.py` `_clamp_reduce_only` | `self.position` | rejects a reduce-only order with no position, rejects one that would increase exposure, clamps size to `abs(pos)` |
| `engine.py` `_apply_fill` (revalidation) | `self.position` | re-checks a resting reduce-only order at fill time and cancels it if the position has since shrunk or flipped |
| `engine.py` `_apply_fill` (arithmetic) | `self.position`, `self._avg_price` | the realized amount booked on the closing portion, and the new average price |
| `engine.py` `_end_session` | `self.position` | the `end_of_session_positions` entry, which is how an unintended overnight hold becomes visible |
| `engine.py` `_close_out` / `RunRecord` | `self.position`, `self.last_price` | `blotter.final_position` and `final_price` |
| `context.py` `close()` | `_port.position()` | the side and size of the flattening order, and the `ContextError` when flat |
| `context.py` `position`, `average_entry_price`, `realized_pnl`, `unrealized_pnl`, `equity` | `_RunState` | what the strategy is told about itself |

A strategy that corrupts `_RunState` can therefore still change *what the engine
does* — reject an order it should have accepted, clamp a size, mis-report its own
equity to itself. It can no longer change *what it is scored*. Those are different
problems and only the second one is a false discovery.

**Import guards are defence in depth, not a boundary.** `run.py` imports the
scorer before it loads the strategy, so the module is cached and the
module-level stack guard never runs again; the static source check is bypassable
with `__import__` or `sys.modules`. The threat was never reading — the metric is
public, it is in `metric.md` — it is *writing*: a strategy that reaches the
module can replace a function in it. So the scoring code is fingerprinted at
import and again after scoring, and any difference invalidates the trial.
`src/core/scoring/integrity.py`. The real boundary is a separate process.

**Nothing mutable crosses the context boundary.** Every accessor returns a
primitive, a tuple, a frozen dataclass or a read-only view — `ctx.working_orders`
hands back frozen `OrderView` copies, never the engine's `Order` objects, because
assigning to one of those used to work and skipped the queue, the cap, the T+1
rule and the run record. `tests/test_boundary.py` asserts it over the whole
surface by introspection rather than accessor by accessor.

**One resolver for subscription ids.** `"5m"`, `"300s"`, the `Subscription` and
the canonical `time:300000000000` all name the same thing, and an id that was
never subscribed raises rather than reading as absent. `ctx.bars` holds the bars
that closed *in this callback*, so `if "5m" in ctx.bars:` is both the obvious
expression and the correct one.

**The strategy is validated against `types.Strategy` before the walk.** One error
listing every missing or malformed callback, at wiring time. The protocol is
load-bearing: a test reads the engine's source and asserts the methods it
declares are exactly the ones the engine calls.

**The intent queue** (`context.py`). A callback's `ctx.buy(...)` records intent
and returns the id the order *will* have; the engine applies the queue after the
callback returns. The action cap — 64 per callback — is checked at queue time, so
an order past the cap never consumes an id. During warmup the queue is inert:
nothing is recorded, no id is issued, and the calls are counted into
`warmup_actions_ignored` rather than silently vanishing.

**Brackets are armed by the engine at the instant the entry fills**, as an OCO
pair of reduce-only orders. Strategy-managed exits are where the historical fade
artefact lived: an entry filled at 12:30:05 that waits for the 12:35 callback to
place protection is unprotected for five minutes.

**The event-driven walk.** Between two bar closes with no resting order
reachable, nothing can happen, so the walk jumps to
`min(next bar boundary, first tick that could reach any resting level)`. It is a
pure optimization, and the safety property is one-sided: the price conditions
used to find the next candidate are deliberately *weaker* than the fill model's,
so the walk may stop where nothing happens and cannot skip where something would
have. `Engine(event_walk=False)` steps every tick and `tests/test_event_walk.py`
asserts the two produce identical blotters. A tick subscription disables the jump
entirely — every tick must be delivered, and that cost belongs in the wall-clock
budget where it is visible.

**`view.py` is the lookahead guard.** Make the bug unrepresentable rather than
documented: `HistoryView` holds only closed observations and raises
`LookaheadError` on any index or slice that reaches at or beyond the cursor. A
prompt asking an agent to avoid lookahead does not prevent lookahead; an API that
cannot express it does.

**`fills.py`.** Two trade-based models differing only in the price comparison.
The loop scores on `unfavorable` (fill only on a trade strictly through the
limit) because whichever model the agent is scored against is the one it learns
to exploit; `favorable` is diagnostic, and the spread between them tells you
whether a candidate's edge lives in the optimism. `check_capabilities()` refuses
a model the dataset cannot support at wiring time rather than mid-run.

Reasoning and the decisions behind each rule:
`docs/design/simulator-interface.md`. The strategy-author view of the same
surface: `docs/agent/strategy-api.md`.

---

## 5. `src/core/scoring/` — the number the loop acts on

| File | Responsibility |
|---|---|
| `src/core/scoring/scorer.py` | `score()` / `score_diagnostics()` — the fixed surface the loop calls |
| `src/core/scoring/metrics.py` | Per-session returns, Sortino, Sharpe, shape moments, `Diagnostics` |
| `src/core/scoring/calibration/__init__.py` | The calibration rig's public surface, and its import guard |
| `src/core/scoring/calibration/reference.py` | `ReferenceStrategy` — the strategy the rig measures with |
| `src/core/scoring/calibration/null.py` | The empirical null, `threshold_for()` / `far_for()` |
| `src/core/scoring/calibration/_sweep.py` | Trial running and the planted-signal power sweep |
| `src/core/scoring/integrity.py` | Fingerprints the scoring code, so a trial whose metric changed underneath it is invalidated rather than believed |

**The metric is Sortino on per-session returns**, MAR = 0, downside deviation
over all N observations, every session in the split counted including flat ones,
against a constant notional base. Below `MIN_SESSIONS = 30` the trial is
rejected rather than scored; below `MIN_DOWNSIDE_SESSIONS = 5` losing sessions it
falls back to Sharpe, which is always defined, and the fallback carries its own
name so it is calibrated against its own null.

**The null is measured, not assumed.** `significance = ratio × √N` is exact for
Sharpe and an approximation for Sortino, and at the 30-session floor the
parametric rule fired on 6% of pure noise against the ~0.1% a *t* of 3.0 implies.
So the null is simulated: no-signal data, reference strategy, metric, repeat —
and the tail is afforded by pooling per-session returns and resampling N of them,
because the expensive part is what a session looks like and the cheap part is the
sampling distribution over N of them.

**One null per dataset, and the table says which.** A threshold measured on one
return-generating process says nothing about another, and the first real run
proved it: a Sortino of +0.0076 over 86 sessions — a significance of 0.07, which
is nothing — read as a 0.18% false-acceptance rate against the synthetic table
and is 41% against the null measured on NQ's own training sessions. The run
completed, the number was plausible, and it meant nothing. Every table now
carries a `dataset` tag; `threshold_for` / `far_for` raise `WrongNullTable` when
asked about a dataset the table was not measured on, and `FileNotFoundError`
naming the script when no table exists for it.

**Real sessions are not exchangeable, so the resample is a block bootstrap** —
stationary, Politis & Romano, with the block length *measured* from the pool
rather than chosen. The returns themselves are near-uncorrelated; their absolute
values are not, so the length is measured over both series and taken from
whichever persists longer. NQ's is 7 sessions, the synthetic generator's is 3.
Block length 1 reproduces the i.i.d. resample exactly, to the same integers from
the same generator state, so the old behaviour is a special case of the new code
path rather than a parallel one.

Tables live at `data/calibration/empirical-null.json` (synthetic) and
`empirical-null-nq-trades.json`, each beside a `-verification.json` recording
promised-versus-measured false acceptance on sessions the table was not built
from.

**The reference strategy lives here, not in `src/strategies/`.** It used to be
the agent's file, which meant the power curve measured the metric, the threshold
*and* whatever that file happened to contain, with no way to separate them.

Reasoning: `docs/design/metric.md`.

---

## 5c. `src/core/engine/remote/` — the strategy runs in its own process

Findings 1, 2 and 3 of the second adversarial pass were one mistake three times:
strategy code and engine code shared an address space, so `__self__`,
`__closure__` and `gc.get_referents` eventually reached anything. Task 21 made
the *score* unreachable, which was the urgent half. This is the rest.

| File | Responsibility |
|---|---|
| `src/core/engine/remote/protocol.py` | Length-prefixed JSON frames, the action whitelist, and generic encoders over `dataclasses.fields` |
| `src/core/engine/remote/adapter.py` | `RemoteStrategy` — a `Strategy` the engine cannot tell apart; snapshots, id blocks, the five failure kinds, per-platform isolation |
| `src/core/engine/remote/child.py` | The strategy host: `RemoteContext`, the rolling `Window`, the setup pulls |
| `src/core/engine/remote/_surface.py` | GENERATED by `scripts/gen_remote_surface.py`. The context surface, classified |

**`engine.py` does not change.** The adapter *is* a `Strategy`, so the engine runs
a subprocess-backed one without knowing. That is the property the Task 24 spike
was built to demonstrate and the reason this refactor is small.

**One round trip per callback, and no pulls in a callback.** Task 24 measured
why: nothing bounds how many times a strategy calls `ctx.history()`, the action
cap bounds orders and there is no read cap, and a cost whose multiplier is chosen
by the untrusted side is not a cost anyone can plan around. Ten pulls per
callback is 63% of a trial. So each callback carries a pushed snapshot, and the
history window is maintained **incrementally** — a rolling window is append-only,
so the parent sends only the bars the child has not seen. Re-sending the window
every callback was measured at 9× the cost for identical semantics.

Measured end to end on the real NQ validation split: **45.3 s in process, 49.3 s
isolated** — +8.8%, byte-identical blotter, 46,433 messages, 69 MB. The
`sandbox-exec` wrapper adds 0.4 s.

**The surface is generated, not remembered.** `BarContext` has 28 public names
and the spike implemented 12; it worked, because the reference strategy reads 12,
and `sub_id` reads like a scalar and is a method. `scripts/gen_remote_surface.py`
classifies every public member from `context.py` by rule — a property is pushed,
a method returning `None` or `int | None` is an action, anything else is a pull —
and **anything it cannot classify fails the build**. It found one immediately:
`SetupContext.ticks()` had no return annotation.

**`setup()` is the exception and serves pulls**, because it happens once. Bulk
training data is capped at 256 MB. The numbers behind the cap, measured on the
384-session NQ training split: `ctx.ticks()` is 180.5 M events and **8.0 GB** on
the wire; `ctx.train_bars("5m")` is 105,984 bars and **12.7 MB**; `train_bars("1m")`
is 62 MB. A ratio of 633×. The 22 GiB the spike named was an artifact of what the
API hands over, not of what a strategy needs — so the cap admits every bar
interval anyone would plausibly fit on and refuses the raw tick stream *with the
number*. What it costs in expressiveness is real and worth naming: a strategy
that wants to fit volume bars, dollar bars or tick-imbalance features on training
data cannot. It can still subscribe to ticks for the live walk.

**Order ids** come from a block pre-allocated per callback, sized to the action
cap so exhausting it *is* hitting the cap rather than a second failure mode with
its own message. Ids unused in a callback are retired, never reused: the fill log
is keyed on order id and an id meaning two orders would make it unreadable.

### Five failure kinds, and why the fifth is separate

| kind | outcome | counts toward N |
|---|---|---|
| `strategy_raised` | `crashed` | yes — the child's traceback is preserved verbatim |
| `strategy_timed_out` | `crashed` | yes |
| `strategy_exceeded_resources` | `crashed` | yes — SIGXCPU, or killed |
| `strategy_violated_protocol` | `crashed` | yes — a malformed frame or an action outside the whitelist |
| `harness_failed` | `harness_failed` | **no** |

A harness bug recorded as a crashed trial inflates N, and N is the denominator of
every corrected p-value here, so "the strategy broke" and "we broke" must never
be the same row. The traceback matters for the same kind of reason: an agent
debugging against a truncated traceback is debugging against fiction, and every
error message in `docs/agent/strategy-api.md` would become a lie.

### The boundary is a data boundary, not only a process boundary

Neither adversarial pass reported this, and it has been reachable since the first
line of strategy code. The strategy child is ordinary Python; unless something
stops it, it can open a socket. A strategy that fetches real NQ prices for the
evaluation dates from any public source and trades on them produces a spectacular
result with a genuine blotter, a stable `dataset_digest`, a clean scoring
fingerprint and a **replay that reproduces**, because the replay fetches them
again. It never touches the engine, the dataset or the scorer, so everything
built in Tasks 21 through 25 is orthogonal to it.

Both passes reasoned about the object graph, because both briefs framed the
boundary as an in-process one. The reachability walk is a good tool that answers
exactly one question, and the question it does not answer is where the data came
from. **The entire point of the pre-registered split is that a strategy cannot
see certain prices; an open socket makes the calendar decorative.**

Network is denied in the child on both platforms — an empty network namespace on
Linux, the `sandbox-exec` profile on macOS — and verified by probe rather than by
configuration. Three probes, because they fail in different places: DNS, a raw
TCP connect to a literal IP with no name resolution involved, and `urllib` the
way a strategy would actually write it. A configuration that denies one and not
the others is worse than none, because it reads as protection.

Nothing legitimate in the child needs the network — it receives everything over
the pipe — and a test asserts the child's modules import no socket library, so
that stays true rather than being remembered.

### Isolated on Linux, best-effort on macOS

Not one claim meaning two things. Every run record carries `isolation` — level,
platform, filesystem, network, memory ceiling, CPU ceiling — because a row that
says `best-effort` is a row a reader can discount and a row that says nothing
lies by omission.

| | Linux | macOS |
|---|---|---|
| filesystem | read-only bind mount in an unprivileged user namespace | `sandbox-exec` profile (`config/strategy.sb`), **deprecated for a decade** |
| network | denied | denied by the same profile |
| memory ceiling | `RLIMIT_AS` | **unavailable** — `RLIMIT_AS`/`DATA`/`RSS` cannot be set at all |
| CPU ceiling | `RLIMIT_CPU` | `RLIMIT_CPU` |
| level | `isolated` | `best-effort` |

Task 24 measured the macOS gaps rather than inferring them: `setrlimit` raises
`ValueError: current limit exceeds maximum limit` for every memory limit with
either hard value, and the sandbox needs three interpreter paths whitelisted by
literal because the profile applies before the exec that starts the child.
**Scored campaigns should run on Linux.** macOS stays supported for development.

Task 26 stood a Linux runner up and verified it rather than asserting it —
`docs/setup/LINUX_RUNNER.md` is how to reproduce one, and
`docs/reviews/task26/verify_isolation.py` is the check. Every item holds on
Linux under bubblewrap: goldens byte-identical (both sets, windows 1 and 512),
the escape walk clean over 21,198 objects inside the child, writes to `config/`,
`runs/`, `data/calibration/` and the strategy's own source all refused with
`EROFS`, all three network probes unreachable, and **`RLIMIT_AS` actually binds**
— a strategy allocating without bound gets `MemoryError`, which is the one thing
macOS cannot do.

One correction to the spike, which said the mechanism needs no privileges: the
*run* needs none, but on Ubuntu 24.04 *getting there* needs one root step.
Unprivileged user namespaces are restricted by AppArmor and `bubblewrap` ships
without a profile, so `bwrap` fails with `loopback: Failed RTM_NEWADDR` or
`setting up uid map: Permission denied` — neither of which mentions AppArmor.
The profile is three lines and is in the setup doc.

Cost, same real NQ validation split and the same blotter digest on both:

| | in process | isolated | overhead |
|---|---:|---:|---:|
| macOS 15.0, M1 Pro | 45.3 s | 49.3 s | +8.8% |
| Linux VM, 4 vCPU, bubblewrap | 61.0 s | 68.2 s | +11.8% |

Per callback the boundary costs 82 µs on macOS and 152 µs on Linux — bubblewrap's
namespace setup plus a virtualised pipe — and next to tick decoding on real data
it is invisible either way.

### Scores are bit-identical across platforms, since Task 28

Task 26 found the same trial scoring differently on macOS and Linux and blamed
numpy's pairwise summation. Task 28 measured it instead, and the attribution was
wrong twice over: numpy is not in that code path at all, and **every input was
already bit-identical** — all 426 row prices, every per-row realized amount and
commission, every per-session return.

The divergence was `sum()`. **CPython 3.12 uses Neumaier compensated summation
for floats and 3.11 does not**, so the identical list of commissions summed to
4.259999999999954 on one interpreter and 4.26 on the other, and that propagated
into `net_pnl`, Sortino, Sharpe, skew, kurtosis and significance. The blotter
digest never showed it because `BlotterRow.as_row()` rounds to ten places before
hashing — "the digests match" was a weaker statement than it read as, and that is
worth remembering about any digest that rounds.

Money is now summed with `math.fsum` — exactly rounded, so the same answer on
every interpreter and platform rather than on the ones that happen to compensate
— in `Blotter.realized_pnl`, `Blotter.commission`, `session_returns`, and every
statistic in `metrics.py`. Verified rather than argued: `run.py` on the same
profile on both machines gives an identical blotter digest, an identical fill-log
digest, and every score and diagnostic field identical to the last bit, with
`net_pnl` landing on exactly −48.86 where it used to be −48.85999999999994 on one
of them.

**Integer ticks were not needed and are not the fix.** The Task 28 brief proposed
representing money in integer ticks on the grounds that P&L is exactly
representable — true, and both datasets do sit on a 0.25 grid — but the measured
problem was aggregation, not the per-fill arithmetic, and retyping the engine
would have changed per-row values while leaving `sum()` in place. What integer
ticks would still buy is *exactness* of intermediate values rather than agreement
between them: a running average entry price is not exactly representable, so a
per-row realized amount is off in the last ULP even when both machines agree on
which ULP. That is a real but separate improvement, priced separately.

The null tables were re-tabulated once on Linux afterwards, with the platform and
arithmetic mode recorded in `provenance.arithmetic`. The pool moved in 361 of 384
elements by at most 9.1e-13, and **the worst threshold movement was 7.6e-15** —
rounding, as expected, and nothing beyond it.

### Every digest, and what it actually establishes

`Blotter.digest()` rounds to ten places before hashing. That is why the platform
divergence hid for two tasks — and it was the statement carrying every "no golden
digest moved" assurance from Task 21 through Task 28. Audited in Task 29 rather
than waiting to be bitten a third time:

| digest | rounds? | establishes | where the claim is made |
|---|---|---|---|
| `Blotter.digest()` | **yes, 10 dp** | agreement to ten places | its own docstring, and `BlotterRow.as_row` |
| `Blotter.exact_digest()` | no — floats as `float.hex` | **identity** | added in Task 29; the golden sets pin it |
| `engine.fill_log_digest()` / `reconstruct.log_digest` | **yes, 10 dp** on price | agreement to ten places | `FillRecord.as_row` |
| `SyntheticDataset.digest()` | no (spec fields, `json` repr) | identity of the generator's configuration, truncated to 16 hex | its docstring |
| `NQDataset.digest()` | no (ETags and dates) | identity of the bytes served, truncated to 16 hex | its docstring |
| `NQDataset._session_digest` | no (ETags) | cache identity, truncated to 12 hex | its docstring |
| `integrity.artifact_digests` | no (file bytes) | identity | §"the fingerprint covers the files" |
| `integrity.code_fingerprint` | no (file bytes) | identity | same |
| `integrity.environment_fingerprint` / `lockfile_digest` | no | identity | same |
| `runlog.strategy_digest` | no (file bytes), truncated to 16 hex | identity | its docstring |
| `integrity.fingerprint()` | **used to truncate constants at 400 chars** | now identity — the constant is hashed rather than clipped | fixed in Task 29 |

Two of these needed changing and both were changed at the claim rather than at
the value:

- **The rounding digests keep rounding.** Removing it would move every committed
  golden digest for no gain, now that the arithmetic agrees across platforms.
  What changed is that they no longer describe themselves as establishing
  identity, and `exact_digest` exists beside them for callers that need it.
- **`integrity.fingerprint` stopped truncating.** It rendered each constant with
  `repr` and clipped to 400 characters, so the threshold grids — precisely the
  long constants worth protecting — were fingerprinted by their first 400
  characters and no further. The constant is hashed now.

The truncations to 12 and 16 hex characters are collision space, not precision,
and are left alone: 16 hex is 64 bits, and these identify artifacts rather than
authenticate them.

**The golden sets now pin full precision.** Each of the sixteen cases carries
`exact_digest` and `score_exact` — every score field as its exact hex form —
alongside the rounded digest it always had. Pinning that is only possible because
Task 28 made the arithmetic agree; before it, the assertion would have failed on
whichever platform did not write the file. Verified passing on both.

### The child's modules are an allowlist

Twice an `__init__` re-export has quietly widened this boundary, and both times a
graph walk caught it because someone happened to run one.
`CHILD_MODULE_ALLOWLIST` is the version that fails by name on the next import:
the strategy process may have `src`, `src.core`, `src.core.types` and
`src.strategies` loaded, and nothing else. `src.core.engine.engine`,
`src.core.engine.context`, `src.core.data`, `src.core.scoring.*` and
`src.core.runlog` are named as forbidden separately, so the failure says *what*
leaked rather than only that something did.

### One in-process exception, bound to a digest

Calibration runs tens of thousands of engines against the committed reference to
measure the apparatus; paying process spawn on each is not sensible and the
reference is not what this distrusts. So `RunTicket.for_calibration` is the only
in-process route, and it **checks the source digest against the committed
reference** rather than trusting the caller's intent — a calibration ticket for
any other strategy is refused. An exception bound to an intention is not an
exception, it is a loophole with a comment.

---

## 5b. `src/core/runlog.py` and `src/core/checks/` — accounting, and the free check

Two Task 23 modules. Both exist because a rule that everyone remembers is not a
rule.

**`src/core/runlog.py` — no engine without a ledger row.** `Engine.__init__`
requires a `RunTicket`, and there are exactly three ways to mint one:
`LedgerRun`, a context manager that appends exactly one row on exit whatever
happened inside it; `RunTicket.for_calibration(reason)`, for the null tabulation,
which runs tens of thousands of engines against the *immutable reference
strategy* and gets one row for the tabulation rather than one per engine; and
`RunTicket.for_test`, which a test asserts is called nowhere under `src/` or
`scripts/`. `tests/test_accounting.py` walks the tree for `Engine(...)` call
sites, so a fourth route cannot appear quietly. It also holds the ledger
helpers — `append_ledger`, `store_source`, `store_fill_log`, `strategy_digest` —
which used to live in `run.py`: the thing that authorises an engine and the thing
that writes the row are now one module rather than two habits.

The third ticket kind was not anticipated by the brief and is called out rather
than folded into one of the other two. Calibration is not an evaluation of a
candidate, and ledgering each of its engines would drown the record it exists to
keep.

**This stops accidents, not attackers.** Anything running in this process can
reach the mint sentinel and forge a ticket, exactly as it can reach anything else
in the address space. What it stops is the failure that actually happens: a
legitimate entry point added without a thought for the ledger.

**`src/core/checks/` — the free check executes nothing.** Adversarial review's
finding 6: `scripts/agent_check.py` reported pass/fail and no number, and
pass/fail *is* a number if you ask enough times. A strategy that raises when its
own P&L crosses a threshold turns the exit code into one bit, and a binary search
over the threshold recovers the rest — 18 probes gave peak realized P&L to
±0.0002 with **zero trials counted**. It does not forge a trial; it performs
trials the system has agreed not to look at.

Narrowing the channel does not fix it: any output that depends on market data can
carry a score. So the check does not execute strategy code, and that rules out
`import` too, because a module's top level runs on import and a probe would live
there.

| File | Responsibility |
|---|---|
| `src/core/checks/static.py` | `compile()` for syntax; an AST pass for module-level inertness, the strategy class, the callbacks and their signatures, `setup()` returning subscriptions, and tick subscription without `on_tick` |
| `src/core/checks/typecheck.py` | mypy against `src.core.types.Strategy`, through a generated conformance file. Static: mypy parses and infers, it does not import what it analyses |

The two layers catch different things and neither alone is the check. The AST
layer sees a missing callback; the type checker sees a return type the Protocol
forbids and a constructor `load_strategy()` cannot call. `tests/test_accounting.py`
asserts that neither module contains a call that could execute the file.

**What the agent loses, stated plainly.** A runtime error is no longer free. A
`TypeError` on the first bar, an unsubscribed id, an order the engine rejects —
these surface in a scored run, which is a trial with `outcome: crashed`,
`score: null`, and a row that counts toward N. That is intended. The project's
claim is that N includes crashed and abandoned runs, and a free crash check is a
mechanism for making crashes not count: the same hole in a friendlier shape.

---

## 5d. `src/core/loop/` and `src/core/scoring/correction.py` — the loop, and the
correction it exists for

**The project's stated contribution is the correction over the trial ledger, and
until Task 30 nothing consumed the FAR column.** Every system in the reference
bibliography accepts a change when a single scalar improves and none corrects
across hundreds of nightly trials. This is the part that was missing.

| File | Responsibility |
|---|---|
| `src/core/scoring/correction.py` | Benjamini-Hochberg, Holm, and the DSR-shaped `deflation_view`. Checked against Benjamini & Hochberg (1995) Table 1, whose published answers are 4 and 3 |
| `src/core/loop/agents.py` | The stub proposal side: `NullGenerator` (edgeless variants, deterministic) and `ReplayList` (a fixed sequence) |
| `src/core/loop/campaign.py` | `CampaignRecord` (pre-registered and hashed), `run_campaign`, `summarise`, and `CampaignResult.unaccounted` |
| `src/core/loop/llm.py` | The real proposal side (Task 36): `LLMAgent`, the same one `propose(history)` method the stubs implement, plus the prompt assembly, the history whitelist, the bounded static-repair loop and the blocked-proposal path. Runs in the driver, which has network; nothing here is reachable from the strategy process, which does not |

**A stub agent first, with no LLM in it.** A correction cannot be checked against
a campaign whose ground truth is unknown, so the null generator proposes
strategies that have no edge by construction and the known answer is zero real
discoveries. It also means the machinery is debugged against something
deterministic.

**Then a real one (Task 36), and it found two things the stub could not.** The
four Task 31 decisions — what the agent sees, how validation is spent, the
stopping rule, what is reproducible — are settled and written into
`docs/design/loop.md` and into every campaign record. What the stub had been
hiding: a strategy that failed to *load* raised past `run.py`'s handlers and
produced a ledger row with no outcome at all, which `summarise` counted as
neither a trial nor a harness failure and **dropped from N**; and
`remote.child.Window` was missing `last(k)`, which the documentation promises
and two CI-checked worked examples use, because those examples were being run
in process. Both are fixed, and both now have a check that fails by name. A
stub that always emits valid Python and never reads the documentation finds
neither.

**The campaign record is written and hashed before the first trial** — budget,
stopping rule, q, the correction set, the stub's seed — and re-hashed at the end.
A budget or a q chosen after seeing results is the failure this whole project
exists to prevent, and building the loop without that guard would have been an
embarrassing place to commit it. Verified: editing the budget from 200 to 20, or
q from 0.05 to 0.2, is refused with the stored and recomputed digests side by
side.

**N is the count of a campaign's trial rows, unfiltered.** Accepted, rejected and
crashed alike; a crashed trial enters the correction at the worst possible
p-value rather than being dropped, because dropping it would shrink the
denominator by exactly the trials that did not work. The single exception is
`harness_failed`, which is not a trial: Task 25 gave it its own outcome for this
reason, and it is counted separately and reported separately.

**Campaigns run on the training split only** (`config/run.yaml`, profile
`campaign`). A campaign that searched on validation would consume the split it is
eventually judged against.

**What the agent sees is deliberately unanswered.** `propose(history)` is passed
the history and the stub ignores it, with `del history` in the body so the
omission is explicit. Showing prior trials and their scores is what makes this
research rather than random search, and it is also what makes trials correlated,
which changes what raw N means. That is a decision to argue for, not to inherit
from whoever wrote the stub.

**Read `docs/reviews/task30/README.md` before quoting any campaign's numbers.**
The first campaign ever run found that the FAR column does not support the
correction: BH's realized error rate on known-null campaigns is 0.575 against a
promised 0.05, because the null is one strategy's and a campaign points hundreds
of *different* strategies at it.

---

## 5e. `src/core/scoring/randomization/` — a null that knows which candidate it is

Task 30 measured what a single-strategy null costs: BH fired in 23 of 40
known-null campaigns against a promised 0.05, because the table was tabulated
from one reference strategy and a campaign points hundreds of different ones at
it. The reference gets 0/40; its variants get 10×.

| File | Responsibility |
|---|---|
| `src/core/scoring/randomization/rotation.py` | `RotatedDataset` — circular rotation of each session's price path against the clock |
| `src/core/scoring/randomization/candidate.py` | `candidate_null` — the candidate against R rotations of its own data, with the resolution floor reported as a floor |
| `src/core/scoring/randomization/profile_null.py` | The cheap alternative: a null indexed by behavioural profile rather than by one strategy |
| `src/core/scoring/randomization/hybrid.py` | `choose_null` — bucket when covered, rotation when not, keyed on the profile and never on the score |

**The null hypothesis, stated before the design.** *This candidate, with its
trading behaviour unchanged, has no edge.* So the randomization must preserve the
behavioural profile and destroy the relationship between decisions and subsequent
price. Rotation preserves the multiset of within-session increments, the
timestamps, the tick count and each session's net move; it destroys the alignment
between where in the session a move happened and what the clock said. Measured
profile drift across 60 candidates: median 2.6–7.5% per axis, p90 ≤ 23.8%.

**Rotating by whole sessions was ruled out before implementing it.** Sortino is
computed over per-session returns, so permuting sessions leaves it unchanged.

**The resolution floor is the first thing about an empirical null.** R rotations
cannot resolve a p-value below `1/(R+1)`, and that floor is a field, never a
value substituted for one that could not be measured. Task 30's two BH
discoveries were partly manufactured by exactly that substitution: the grid
bottomed at 0.0005 and two trials tied there, at the rank-2 BH critical value.

**The profile index is not dispersion**, despite dispersion being Task 30's
headline number. Sortino is `mean / downside_deviation` and both scale with the
series, so the ratio is scale-invariant and a dispersion index would index on
something the metric cannot see. The axes are the scale-invariant ones where the
variants actually spread: **active fraction** (0.343–1.000 across 24 variants,
reference 1.000) and **downside fraction** (0.162–0.752, reference 0.590).

**Candidate nulls run behind the process boundary**, which is the cost the design
did not price: isolation is 5.7× per run, so R = 99 is 100 s per candidate and a
200-candidate campaign is 5.5 hours. `RunTicket.for_null_calibration` is the
route — it runs arbitrary generated strategies so it checks no digest, and in
exchange it forbids in-process execution, asserted by `tests/test_accounting.py`.

### The hybrid: bucket when covered, rotation when not

Task 32's gate controls and **21.8% of candidates never reach it** — a thin
bucket means no null, p = 1, and no possibility of discovery. That gap is in the
tails of profile space, so once an agent is hill-climbing, novel profiles are
silently unacceptable and **the calibration's coverage becomes an implicit prior
over strategy space, imposed by nobody and documented nowhere.**

`choose_null` routes on coverage alone. *Mixing two valid procedures is valid
when the choice between them is independent of the outcome, and invalid the
moment it is not* — so the rule is enforced by the signature: it takes a return
series and a table, and has no parameter through which a score could reach it.

Under the conditional guarantee the profile table is a **per-campaign step**
built on the campaign's own data, not a committed artifact. `profile-null.json`
remains as a Task 31 screening artifact, pooled across realizations.

### The guarantee is conditional, and the operating null is the cheap one

Task 32 settled what the gate claims before paying for calibration, and the
answer changed which null is needed. **The guarantee is conditional on the
observed training data**: given these sessions, the probability that a campaign
produces any false discovery is at most q, with the randomness over strategy
generation and the randomization rather than over which market happened.

Under that framing the between-realization variance that sank the pooled profile
null in Task 31 does not arise — production has one realization, and a null built
on it conditions on that by construction. Measured over 400 independent campaigns
on one realization: **FWER 0.045, 95% CI [0.029, 0.070]**, against 0.575 in Task
30 and 0.115 under the marginal framing.

So the **profile-indexed null is the operating null** and rotation is the
fallback, priced at 87 minutes for a 200-candidate campaign at R = 99 on eight
cores. `docs/design/metric.md` carries the guarantee sentence and
`docs/reviews/task32/README.md` the measurement.

### The correction's resampling bias, and the campaign-size result

`max_of_n` resamples each trial's null with replacement, so the simulated maximum
cannot exceed the largest draw that null contains: a finite null truncates its own
upper tail and the test is **liberal**. Measured over 4,000 campaigns per cell on
known normal nulls: **+0.021 at R = 20, +0.011 at R = 50, inside noise from
R = 150.** The adopted remedy is **R ≥ 150** — the only option that costs money
rather than an assumption. A smoothed-tail variant was implemented and measured to
**overcorrect** (−0.022 where the bias was +0.015); it is kept, defaulted off, and
documented as rejected. `resolution_cap()` reports the bias beside the floor.

**Detection is monotone in N** up to the limit of what the measurement can see.
The interior optimum one would expect is a BH property — `q/N` shrinks linearly —
and `max_of_n`'s penalty grows like √(2 log N), so more trials always help. The
recommendation is "as large as affordable, at least 400", with the stated
assumption that at π = 0.01 and a weak edge no tested N detects anything.

### The correction changed shape

`N ≤ q(R+1)`. BH rejects the smallest p only if it beats `q/N`, and an empirical
null resolves no finer than `1/(R+1)`. At q = 0.05 with R = 1,000 that is N ≤ 50,
against 21,600 trials overnight. Four orders of magnitude, and no null closes it.

So `correction.max_of_n` simulates the null distribution of the **best-of-N**
directly: B replicates, one null draw per trial from that trial's own null, take
the maximum. N becomes a parameter of the simulation rather than a divisor, and
the resolution that matters is B, which is free — B = 10,000 puts the floor at
1e-4 for any N. It controls FWER, which is the question the project asks: did the
loop fool itself. `step_down_max` is the Westfall–Young step-down for per-trial
discoveries. Drawing each trial independently overstates the spread when trials
are correlated, so the test errs conservative.

**Never amortize rotations into one child process.** A candidate that persisted
across rotations could recognise repeated session sets and *sandbag* -- trade
badly on what it identifies as null runs, inflating its own null so its real
score looks exceptional. A fresh child per rotation is what the 5.7x isolation
multiplier buys. Amortizing is the obvious optimisation and it destroys the test.

**Named for the third adversarial pass, not solved here:** rotation preserves
timestamps, session labels, tick counts, the increment multiset and each
session's net move, so a candidate that measures autocorrelation can tell rotated
data from real. One that can detect it is being null-tested can sandbag even with
fresh children per rotation. That is the next place to look.

**Read `docs/reviews/task31/README.md` and `docs/reviews/task32/README.md` for
what was and was not achieved.**

---

## 6. `src/core/types.py` and `src/strategies/`

**`src/core/types.py`** holds every shared value type: `Ts`, `Side`, `Split`,
`OrderType`, `TimeInForce`, `OrderStatus`, `LifecycleKind`, `Tick`, `Bar`,
`BarEvent`, `TickEvent`, `FillEvent`, `LifecycleEvent`, `Order`, `OrderView`,
`BracketSpec`, `Submit`/`Modify`/`Cancel`, `Blotter`/`BlotterRow`, `ScoreTuple`,
`DataCapabilities`, `Subscription`, the `Strategy` / `TickStrategy` protocols, and
the `LookaheadError` / `HoldoutLocked` exceptions. Immutable and dataset-agnostic.
It sits at the top of `src/core/` rather than inside one subpackage because all
three import it and none owns it.

`Order` is the engine's own mutable record; `OrderView` is the frozen copy a
strategy is handed, and the two are separate because handing out the former let a
strategy change an order without going through the queue, the cap or the record.
The `Strategy` protocol is load-bearing rather than decorative: the engine
validates against it before the walk and a test asserts the methods it declares
are exactly the ones the engine calls — it carried a signature that had been
wrong for four tasks because nothing imported it.

**`src/strategies/current.py`** is the only writable tree — the agent's file.
`run.py` imports it by name and looks for `STRATEGY`, `Strategy` or
`MomentumStrategy`. Its current contents are a momentum placeholder that is also
the shape the worked examples in `docs/agent/strategy-api.md` follow.

**`src/strategies/ported_buy_and_hold.py`** and
**`src/strategies/ported_momentum.py`** are Task 40's ports of
`nalin_l1.strategy.reference_strategies.BuyAndHold` and
`nalin_l1.strategy.candidate.Momentum` from a vectorised next-bar-position API
onto this event-driven one. They are **apparatus, not proposals**: the loop
writes `current.py` and never these, nothing runs them but
`tests/test_ported_strategies.py`, and no ledger row references them. They are
here because `src/strategies/` is where a strategy lives and parking them
elsewhere would make the tree lie about what they are.

Each carries its translation notes in its own docstring, and one of them is a
finding about this API rather than about the port: the source applies
`.shift(1)` to every signal by hand, because a pandas frame hands a strategy the
bar it is deciding on and the defence has to be written each time. Here
`ctx.history()` contains only closed bars and `history[len(history)]` raises,
so the shift is in the type — and porting it literally would lag every decision
by two bars. *The shape does the work the discipline used to do.*

---

## 7. `run.py` — the immutable entry point

`src/strategies/current.py` is a library; this runs it. **Run configuration is
immutable for the same reason the scorer is:** whoever chooses the split, the
fill model and the seed can point a run at the holdout, select favorable fills,
or retry until a lucky seed lands — none of which require touching the scorer,
and all of which are cheaper ways to fake a result. Everything comes from
`config/run.yaml`.

What it owns:

- **Profiles.** `harness` is what the loop runs. `agent` is what the agent may
  run itself — validation only, holdout unreachable, and `--split`,
  `--fill-model`, `--seed`, `--unlock-holdout` and `--profile` are refused by
  name with a message saying to change the strategy, not the conditions it is
  measured under.
- **The ledger.** Exactly one append per invocation, whatever the outcome:
  completed, `abandoned_on_budget`, `refused` (holdout locked), or `crashed`. An
  agent that could run fifty exploratory backtests and present the best as one
  candidate makes N fiction, in the one place nobody would look.
- **No evaluation condition is a command-line argument.** No `--dataset`,
  `--split`, `--seed`, `--fill-model` or `--trigger`: they were overridable with
  an agent-forbidden-list in front of them, two names were missing from the list,
  and the supplied-argument check tested truthiness so `--seed 0` read as "not
  supplied". A strategy could evaluate itself against the planted-signal dataset,
  retry on seeds, and write the result to a ledger nobody counts. Adding names to
  a list is a race against whoever adds the next flag. Everything comes from a
  committed profile; `--purpose`, `--ledger` and `--unlock-holdout` need
  `--human`, and provenance comes from which entry point ran.
- **The agent never invokes a scored run.** `scripts/agent_check.py` is its only
  entry point: it compiles the strategy, walks a small committed dataset, and
  returns pass or fail and **no number**. A check that yields no score cannot be
  used for selection — nothing to optimise against, nothing to retry for, nothing
  to forge.
- **`purpose`** — `trial`, `smoke`, `replay` or `check`. N is a *filter over the ledger*,
  never a shorter ledger: a run that was not a candidate gets classified rather
  than deleted afterwards, because a record edited by the party being measured
  against it is not a record. An agent-triggered run cannot set it.
- **Reproducibility.** `git_sha` alone does not identify a trial, because the
  agent edits and runs before anything is committed. The ledger row carries a
  content hash of the strategy source, and `store_source()` copies the source to
  `runs/sources/<digest>.py` — content-addressed, so a hundred runs of the same
  strategy cost one copy. A digest identifies a trial; only the source reproduces
  one, and the rejected trials are exactly what N counts. The fill log goes
  beside it at `runs/fills/<digest>.jsonl.gz`, so replay recomputes the P&L
  instead of re-observing it.
- **Five fingerprints now, five questions.** The three below, plus
  `environment_fingerprint` (what was actually importable when the run happened,
  from `importlib.metadata`) and `lockfile_digest` (what `requirements.lock`
  declares). numpy and pandas versions change numbers and no digest over the
  repo's own files can see that; the pair says which environment was promised and
  which was used. `scripts/ledger_check.py` reports a row whose environment
  differs from today's as **environment-drifted**, which is not superseded — the
  row may be perfectly good and simply will not replay bit-identically.
- **Three fingerprints, three questions.** `scoring_fingerprint` is the bytecode
  of the loaded scoring modules and is the only one that can see a monkeypatch.
  `artifacts` is the pre-run digest of every file a result depends on — null
  tables, calendars, profiles, the manifest, the strategy source — so the row
  states which versions produced it, and it is re-taken after scoring and
  compared. `code_fingerprint` hashes the engine, data and scoring sources on
  disk and is the one that can be recomputed at an arbitrary commit, which is how
  `scripts/ledger_check.py` asks whether a row's code is still in history. None
  substitutes for another.
- **The optional fill-model spread.** A second full run under the opposite fill
  model, opt-in per profile, because a candidate that collapses between the two
  is telling you where its edge comes from.
- **`warmup_sessions` against the calendar embargo.** Warmup reaches backward
  from the first session of the split and the embargo is the gap in front of it,
  so warmup within the embargo reaches only into embargoed sessions. Checked once
  the dataset is built — the earliest point both numbers exist — and stood down
  for a dataset whose calendar is not pre-registered yet.

### The fingerprint covers the files, not only the modules

Adversarial review's finding 7: `integrity.fingerprint()` hashes scoring
*modules*, and rewriting `data/calibration/empirical-null.json` moves every
accept/reject decision in the harness without changing one byte of bytecode. The
fingerprint reported everything as unchanged, which is worse than not having one,
because the row then carries an assurance it did not earn.

`integrity.artifact_digests()` closes that. It hashes `config/*.yaml`,
`config/calendars/*.yaml`, `data/calibration/*.json`, the manifest, the contract
selection, the session closes, the DBN equivalence record and
`src/strategies/current.py` — taken at `run.py` import, before `load_strategy()`,
and taken again after scoring. Any difference raises `ArtifactsMutated` and the
row is written as `invalidated_artifacts_mutated` with both digests. The path
list is a tuple constant inside the scoring package, which a strategy cannot
import: a set assembled by the caller is a set the caller can shorten, and the
caller closest to the incentive is the one running the strategy.

**This is detection, not prevention.** A strategy can still write to any of those
paths — they are ordinary files on a shared filesystem. What it cannot do is
write to them and have the result stand. Prevention is a separate process with a
read-only mount, and that is a later task.

Deliberately outside the set, each for a reason rather than by omission: `raw/`
in the bucket (immutable by policy, read-only by credential, and the manifest is
its fingerprint); `data/cache/` (derived and content-keyed, and rewriting it
moves `dataset_digest`, which is already in the row); `runs/` (output, not input
— `scripts/ledger_check.py` is its integrity check); `docs/` (a result does not
depend on prose); and **the environment**, which is a real gap — numpy and pandas
versions change numbers and the honest fix is a lockfile, not a hash.

### The ledger has its own integrity requirements

It is the p-value column. False-discovery control over it assumes every row is a
calibrated number from a run someone could re-run, so a row nobody can reproduce
is not a p-value and carrying it silently overcounts N.
`scripts/ledger_check.py` asks three questions per row — is its `git_sha` still
reachable from HEAD, does it say what produced it, and (with `--replay`) does it
reproduce its recorded blotter digest — and prints honest N with supersessions
applied.

**A row that fails is superseded, never deleted.** Deleting it loses the fact
that a trial happened, which is the one thing the ledger cannot afford to lose;
leaving it unmarked corrupts the column. So supersessions live in their own
append-only file, `runs/supersessions.jsonl`, one entry per row naming it by
index *and* blotter digest, with a reason. The ledger itself is never rewritten.
Nothing is superseded today: all four committed rows reproduce.

---

## 8. `scripts/` — build-time

Each produces something committed. None is importable from `src/core/`.

| Script | Produces | Consumed by |
|---|---|---|
| `scripts/inventory.py` | `data/manifest.json`, `docs/data-inventory.md` | `src/core/data/nq.py`, `tests/test_data_integrity.py`, `scripts/gen_agent_data.py` |
| `scripts/nq_contracts.py` | `data/nq-contract-selection.json` | `src/core/data/nq.py`, `tests/test_nq_adapter.py` |
| `scripts/session_closes.py` | `data/nq-session-closes.json` — when each early-closing session actually ends | `src/core/data/nq.py`, `tests/test_nq_adapter.py` |
| `scripts/agent_check.py` | nothing — the research agent's only entry point; pass/fail and **no number** | the research agent |
| `scripts/tabulate_null.py` | `data/calibration/empirical-null*.json` — one per dataset, plus its held-out verification | `src/core/scoring/calibration/null.py` |
| `scripts/calibrate.py` | the two-sided calibration curves (`--json` writes them under `data/calibration/`) | `docs/design/metric.md` |
| `scripts/gen_context_api.py` | `docs/design/context-api.md` | the research agent |
| `scripts/gen_agent_data.py` | `docs/agent/data.md` | the research agent |
| `scripts/probe_dbn.py` | nothing — a metadata/hash/compare CLI | a human debugging the bucket |
| `scripts/nq_smoke.py` | nothing — an end-to-end adapter smoke run | a human |
| `scripts/replay.py` | nothing — replays a ledger row from its stored source and **recomputes** its P&L from the stored fill log | a human auditing a trial |
| `scripts/ledger_check.py` | nothing — per-row verdict on whether a ledger row is reproducible, and honest N with supersessions applied | a human, before a campaign; CI |
| `scripts/lock_env.py` | `requirements.lock` — the full transitive set, pinned | `integrity.lockfile_digest()`, CI (`--check`) |
| `scripts/gen_remote_surface.py` | `src/core/engine/remote/_surface.py` — the context surface, classified | the remote adapter and child; CI (`--check`) |
| `scripts/tabulate_profile_null.py` | `data/calibration/profile-null.json` — the null indexed by behavioural profile | `randomization.profile_null`, the campaign summary |
| `scripts/campaign.py` | `runs/campaigns/<id>/` — the pre-registered record, the campaign's ledger, and the corrected summary | a human running a campaign |
| `scripts/llm_campaign.py` | `runs/campaigns/task36/` — the same, for a model-proposed campaign and its history-withheld control arm, in four resumable stages | a human running a campaign; `docs/reviews/task36/` |
| `scripts/campaign_summary.py` | `runs/campaigns/<id>/<arm>/tier2-summary.json` — the artifact tier 3 reads: the pre-registered record's digest, N as the ledger defines it, the gate's verdict and its simulated bar, the best candidate's source, the per-trial diagnostics, the provenance block, and the ledger rows by reference. `--check` rebuilds and compares | `daksh/audit_agent/tier3.py`; `docs/design/tier-contract.md` |
| `scripts/positive_control.py` | `runs/campaigns/task37/` — the same loop against planted signal at two strengths, with the prompt template digest asserted equal to Task 36's so the comparison means something. A sibling rather than a flag on the above, because that script is the record of what Task 36 ran | a human running a campaign; `docs/reviews/task37/` |
| `config/prompts/strategy-agent.md` | not a script — listed here because it is the other **input artifact** a campaign depends on. Hashed into the campaign record and into `ARTIFACT_GLOBS`, so a mid-run edit invalidates the trial the way a rewritten null table does | `src/core/loop/llm.py` |
| `scripts/check_agent_paths.py` | nothing — the CI write-path allowlist | CI |

The two generators both take `--check` and both run in CI, so a doc that has
gone stale fails the build instead of rotting quietly. This document is checked
the same way, by `tests/test_architecture_doc.py`: every module under `src/` must
be named here, so a new module nobody documented breaks the build.

---

## 8b. `Gowtam/L1/`, `nalin/l1-loop/`, `research/` — beside the harness, not in it

Three top-level trees that are **not** part of the harness and are not on the
path a scored run takes. They arrived with the `merge-test-michael` merge and
Task 40 made them safe to sit next to; it did not integrate them, and this
section exists so the map does not stop at the parts that were already mapped.

| tree | what it is | imports `src.core`? | on the scored path? |
|---|---|---|---|
| `Gowtam/L1/` | An OpenCode agentic loop: `loop/` (driver, planner/worker/reviewer steps, prompt assembly, spec digest, state store), `opencode_bridge/`, pydantic `schemas/`, and three agent definitions under `.opencode/`. 18 Python files. | no | no — nothing imports it and it has no entry point |
| `nalin/l1-loop/` | A second agentic loop **plus a second backtest engine**: `nalin_l1/{loop,opencode_bridge,schemas}` (the same loop, forked and diverged), and `{strategy,execution,metrics,splitting,config,data,orchestrator,packaging,serialization}` — a vectorised next-bar-position engine with its own metric set, fill model and trial log. 68 Python files, 17 test modules, its own `pyproject.toml`, and `market_data/raw/` (one ES session, committed). | no | no |
| `daksh/audit_agent/` | An **audit agent**, and **tier 3** of the three-tier gate since Task 43 — `tier3.py` reads `tier2-summary.json` and emits `tier3-verdict.json`, importing nothing from `src.core`. Its verdict type has three members and no fourth, so PROMOTE is unrepresentable rather than forbidden by policy. Also: its own backtest engine (`engine/{backtest,data,metrics,stats,leaks,store}.py`), five reference strategies including a deliberate `lookahead_cheat`, a `facade.py`, an OpenCode agent definition and two skills, its own `tests/`, and seven design documents of its own. 37 files. Arrived on a branch with **no shared history** — a root commit, merged with `--allow-unrelated-histories` — so nothing in it was ever based on the harness it now sits beside. Unassessed: it postdates the Task 41 overlap review and has not been through one. | no | no |
| `research/` (`research/hypotheses`, `research/experiments`) | Hypotheses and one-off experiment scripts. Prose that is *input to* experiments, as against `docs/`, which describes the system that runs them. | no | no |

**Neither teammate tree contains a single reference to `src.core`.** The coupling
that exists is a convention, not an import: `Gowtam/L1/loop/paths.py` sets
`STRATEGY_FILE = "current.py"` and says in a comment that the file it builds must
land in `src/strategies/current.py`. That is the cheapest possible integration
and the right one — a loop that writes the file and invokes `run.py` needs no
import at all.

**What Task 40 changed in them, and what it deliberately did not.** Changed: one
importable `src` (nalin's package was renamed, because two top-level packages of
that name resolved by `sys.path` order and `src.core` disappeared on a fresh
checkout); the `Data/` and `Docs/` case traps; the split, which was derived from
a spec at run time and is now read from a committed calendar; and the trial log,
which now declares that it is not a trial-ledger row and is not counted in N.
Not changed: **which of the two forked loops survives, which fill model, which
metric set, which objective.** Those are decisions the invariants do not cover
and a teammate would reasonably disagree with. `docs/reviews/task40/README.md`
has the full account, and `docs/plans/task39-teammate-directory-mapping.md` the
overlaps that remain open.

A fourth tree, `daksh/audit_agent/`, arrived later on an unrelated history and
was outside the Task 41 role assignment. **Task 42 reviewed it**: its role is the
committee — frozen gates plus a read-only critic that cannot override a FAIL —
and its gates audit empirically what this harness enforces structurally. What
that review found, in `docs/reviews/task42/`: its `n_trials` is a count of files
in a directory rather than this project's N, its DSR bar is biased high by a
measured +0.017 to +0.036 in Sharpe units against the harness's simulation, its
`research/audit_db.json` records audits rather than trials, and **its three demo
experiments were never committed** — all eight of its own tests fail on missing
data, on this branch and on the commit it arrived from.

**Two things about these trees that are true and easy to forget.** They carry
their own `pyproject.toml`, `pytest` configuration and dependency set — seven of
nalin's test modules need `pydantic`, which is not in this repo's lockfile, and
installing it would drift `environment_fingerprint` on every committed ledger
row. And the root `pytest` run does not collect them: `testpaths = ["tests"]`, by
configuration rather than by accident.

---

## 9. Tests

Offline tests assert against committed artifacts and run in CI; live tests re-read
the bucket and skip when `R2_*` is absent.

| File | What it pins |
|---|---|
| `tests/test_simulator.py` | The end-to-end null/power behaviour of the gate, and the path allowlist |
| `tests/test_simulator_rules.py` | The spec decisions — empty bars, session boundaries, OCO, holdout lock, embargo, action cap |
| `tests/test_context_and_run.py` | The context surface, queued intents, the ledger, and the **golden blotter digests** |
| `tests/test_task7_interface.py` | Tick subscriptions, `reduce_only`, the budget, replay from stored source |
| `tests/test_event_walk.py` | Event walk against full walk, blotter row for row |
| `tests/test_metrics.py` | The metric's conventions and its import guard |
| `tests/test_calibration_null.py` | The empirical null and its import guard |
| `tests/test_nq_adapter.py` | The NQ adapter against the committed manifest |
| `tests/test_data_integrity.py` | Coverage counts, corrections, anomalies |
| `tests/test_layout.py` | The build-time / run-time boundary |
| `tests/test_boundary.py` | The context boundary: immutability by introspection, subscription-id resolution, the protocol against the engine, warmup against the embargo, the reachability walk, and the finding-1 reproduction asserting the score does not move |
| `tests/test_architecture_doc.py` | That this document names every module |
| `tests/test_randomization.py` | What rotation preserves and destroys, the resolution floor, the max-of-N correction, and that a thin profile bucket is refused rather than answered |
| `tests/test_loop.py` | The correction against a published worked example, the pre-registration hash, and the accounting rules a campaign could most easily cheat |
| `tests/test_ledger_integrity.py` | That every committed row names a commit still in history and reproduces, that supersession narrows N without deleting anything, and that a stored source with a relative import still loads |
| `tests/test_examples.py` | That every worked example in `docs/agent/strategy-api.md` is byte-identical to a file in `tests/examples/` and actually runs |
| `tests/test_agent_docs.py` | That the two documents the research agent reads agree with each other, and that the generated one is current |
| `tests/test_live_bucket.py` | The bucket itself, when credentials are present |

`tests/golden/pre_context_refactor.json` is the acceptance artefact for
behaviour-neutral refactors: eight cases, each pinning a blotter digest, row
count, fill count, bar count and final position.

`tests/golden/irregular_generator.json` is the same artefact for the irregular
generator, which is the half that actually finds things. Both sets are now
asserted twice: once against `engine.blotter` and once against
`engine.reconstructed()`, which is what says Task 21 changed where the number
comes from and not the number.

---

## 10. What is not fixed

Written down because the alternative is a document that reads like a finished
system. Each of these has been demonstrated, not hypothesised.

The second adversarial pass, as it stands now. The rows were written when every
one of them was open; the status column is the part that has moved.
`docs/reviews/second-pass/` holds the script that demonstrated each, and every
script still runs.

| # | Severity | What | Status |
|---|---|---|---|
| 1 | critical | `ctx._port.session_bounds.__closure__[0].cell_contents` reaches a mutable `_RunState`; writing `avg_price` took Sortino −0.2547 → **+4.2089** | **closed, Task 21** — the score is re-derived from the fill log |
| 2 | critical | `_SealedDataset.__getattr__` forwards a bound method, so `ctx._session_bounds.__self__` is the raw dataset: seed, calendar, `unlock_holdout`, holdout ticks | **closed, Task 25** — structurally; there is no dataset in the strategy's process |
| 3 | critical | `ctx._queue.cap` and `.actions` are mutable. 200 submits past a cap of 64, `action_cap_hits: 0`, a 50,000-contract position | **closed, Task 25** — the cap is enforced on the engine side of the boundary |
| 4 | high | Task 19's "no anti-conservatism" claim is split-direction dependent; worst ratio 2.64 the other way round | **open** |
| 5 | high | the null is 9–24× anti-conservative for selective strategies, where exposure is a free choice the strategy makes | **open** |
| 6 | high | `scripts/agent_check.py` is an oracle: 18 probes recovered peak realized P&L to ±0.0002 with **0 trials counted** | **closed, Task 23** — the check executes nothing |
| 7 | medium-high | null tables, `config/`, the ledger and the strategy source are writable, and the scoring fingerprint does not change when a null table is rewritten | **detection closed, Task 22; prevention closed on Linux, Task 26; best-effort on macOS** |

Findings 4 and 5 are calibration rather than containment, and both are about the
null being the wrong null for a selective strategy. Tasks 31 through 33 changed
what the operating null *is* — per-candidate and conditional rather than one
strategy's — which is the same territory, and the FWER numbers there are measured
on campaigns rather than on a single strategy. Neither finding has been re-run
against the current null, so neither is closed.

Named for a third pass, and not yet attempted: **a candidate that measures
autocorrelation can detect rotated data and sandbag its own null.** The rotation
null runs a fresh child per rotation precisely so a candidate cannot carry state
between them, but nothing stops a single run from noticing that its input has a
seam in it.

**Findings 2 and 3 are closed by Task 25, structurally.** The strategy runs in
its own process, so there is no dataset in its address space to forward through
`_SealedDataset.__getattr__` and no intent queue to reach: the action cap is
enforced on the engine side of a boundary the child cannot cross. The
`gc.get_referents` walk, re-run *inside* the strategy process over 20,794
objects, finds no engine, dataset, blotter, lock, fill record or queue, and the
only harness modules loaded there are `src.core` and `src.core.types`.

`KNOWN_LIVE_ESCAPES` still has its one entry and that is deliberate. The escape
is real on the **in-process path**, which since Task 25 may run nothing but the
committed calibration reference — checked by digest, not by intention. Emptying
the list would claim the in-process context stopped leaking, which is false; what
changed is that nothing this project distrusts runs there.

**Finding 7's prevention half is closed on Linux and best-effort on macOS.** The
Task 22 artifact fingerprint stays and is now the second layer rather than the
only one. On a platform where the sandbox holds, a strategy that rewrites a null
table gets `PermissionError` and the detection never has to fire; where it does
not, detection still catches it. `tests/test_context_and_run.py` asserts the
property both share — no score, a row either way — and then each mechanism where
it applies.

Findings 1 and 6 were already closed, by Tasks 21 and 23. Findings 4 and 5 are
unaffected by Task 25 because they are calibration rather than containment — no
process boundary makes a null the right null.

**Process isolation was costed before it was built, and the estimate held.**
`docs/reviews/task24/` is the spike: measurements, a throwaway prototype that
produced a byte-identical blotter through a pipe, and a go recommendation at
**6.6% of a full 564-session NQ trial**. Measured after the build, the overhead
came in at +8.8% on macOS and +11.8% on Linux — the same order, and invisible
next to tick decoding. The spike's structural claim held too: findings 2 and 3
closed by there being nothing to reach, not by a patch, and the strategy process
needs nothing from the harness but `src/core/types.py`.

**In-process containment was never won; it was made irrelevant.** This is where
Task 21 left the argument: the fill log removes the *score* from a strategy's
reach but not the interpreter, and a strategy sharing an address space with the
engine is a boundary that holds by inventory rather than by construction. Task 25
moved the fills across a pipe instead, and the only thing still running in
process is the committed calibration reference, admitted by digest.

Finding 6 is **closed**. The check no longer executes strategy code, so its
answer is a property of the source text. `docs/reviews/task23/oracle_after.py`
runs the original binary search against it on two strategies whose peak realized
P&L differs by 4.39: both searches return the same 18 answers and converge to the
same interval, because the channel carries nothing.

Finding 7 is **closed** as far as detection goes — the artifact fingerprint above
covers the null tables, `config/` and the strategy source, and a mid-run write to
a calibration table now invalidates the trial. The *writability* is unchanged and
deliberately so: nothing in-process can take the write away.
