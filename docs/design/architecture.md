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
| `scripts/check_agent_paths.py` | nothing — the CI write-path allowlist | CI |

The two generators both take `--check` and both run in CI, so a doc that has
gone stale fails the build instead of rotting quietly. This document is checked
the same way, by `tests/test_architecture_doc.py`: every module under `src/` must
be named here, so a new module nobody documented breaks the build.

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

**In-process containment is not won, and the fill log does not win it.** The log
removes the *score* from a strategy's reach. It does not remove the interpreter.
A strategy still shares an address space with the engine, and the honest boundary
is a separate process with the fills crossing it as data. Everything here is the
distance to that, measured.

Open findings from the second adversarial pass, none of them closed by Task 21:

| # | Severity | What |
|---|---|---|
| 2 | critical | `_SealedDataset.__getattr__` forwards a **bound method of the inner dataset**, so `ctx._session_bounds.__self__` — and `gc.get_referents` straight through the seal — is the raw dataset: seed, calendar, `unlock_holdout` and a holdout tick, all reachable from `setup()`. The reachability walk records it as the single entry in `KNOWN_LIVE_ESCAPES` rather than exempting it. |
| 3 | critical | `ctx._queue.cap` and `ctx._queue.actions` are mutable. 200 injected submits past a cap of 64, `action_cap_hits: 0`, a 50,000-contract position, and no validation fired. |

Finding 6 is **closed**. The check no longer executes strategy code, so its
answer is a property of the source text. `docs/reviews/task23/oracle_after.py`
runs the original binary search against it on two strategies whose peak realized
P&L differs by 4.39: both searches return the same 18 answers and converge to the
same interval, because the channel carries nothing.

Finding 7 is **closed** as far as detection goes — the artifact fingerprint above
covers the null tables, `config/` and the strategy source, and a mid-run write to
a calibration table now invalidates the trial. The *writability* is unchanged and
deliberately so: nothing in-process can take the write away.

Findings 4 and 5 are calibration, not containment, and are open too: the
"no anti-conservatism" claim from Task 19 is split-direction dependent (worst
ratio 2.64 the other way round), and the null is 9–24× anti-conservative for
selective strategies, where exposure is a free choice the strategy makes.
