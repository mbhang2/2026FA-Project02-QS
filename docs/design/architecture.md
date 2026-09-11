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
                        │  blotter (one row per fill)
                        v
        src/core/scoring/*    per-session returns -> Sortino -> ScoreTuple
                        │
                        v
        run.py: RunRecord + score + diagnostics -> runs/ledger.jsonl (append)
                                                -> runs/sources/<digest>.py
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
4. **Every fill** becomes a `BlotterRow`. The blotter's `digest()` is a SHA-256
   over rounded row tuples: same data, same strategy, same seed → byte-identical
   blotter.
5. **The scorer** turns the blotter plus the split's session calendar into a
   `ScoreTuple`. Sessions the strategy sat out still count.
6. **`run.py` appends exactly one ledger row per invocation**, crashes and
   abandoned runs included, tagged with what triggered it, and copies the
   strategy source to `runs/sources/<digest>.py` so the trial can actually be
   re-run later.

---

## 3. `src/core/data/` — where data enters, and where the split is enforced

The only way data reaches the simulator. A dataset is not just a feed: it owns
the chronological split, the embargo and the holdout lock, so those are
*enforced* rather than requested.

| File | Responsibility |
|---|---|
| `src/core/data/dataset.py` | The `Dataset` protocol, `SplitBounds`, `Calendar`, `HoldoutLock`, `sessions_for()`, `warmup_start()`, `calendar_path()` |
| `src/core/data/synthetic.py` | `SyntheticDataset` — tick generator with a planted-signal switch |
| `src/core/data/nq.py` | `NQDataset` — the R2/Databento NQ adapter, session assembly, front-month selection, local cache |
| `src/core/data/dbn.py` | DBN-under-zstd reading rules, in one place |

**`dataset.py`.** `Calendar.__post_init__` refuses splits that are not
chronological or that do not honour the embargo, so a bad split fails at
construction rather than producing plausible numbers. `HoldoutLock` is one-way
and cannot be re-locked — a flag that can be toggled back is a flag someone
toggles back — and `unlock_holdout()` requires a reason for the record.
`calendar_path(name)` derives `config/calendars/<dataset>.yaml` from
`Dataset.name` so the adapter, its error message and the file a human is meant to
write cannot drift to different names.

**`synthetic.py`.** Built before the first real adapter deliberately: two
implementations from day one is what keeps the `Dataset` interface honest, and it
makes null and power calibration ordinary runs instead of special cases.
`signal_strength = 0` is pure noise and the gate must accept essentially nothing;
`signal_strength > 0` plants an edge of known size. It generates *ticks*, not
bars, so synthetic data exercises the same aggregation path as a real feed.

**`nq.py`.** Everything specific to this bucket: sessions assembled from the two
UTC-day objects each one straddles, front-month selection among outrights by
session volume, and a local `.npz` session cache. It reads the committed
`data/manifest.json` for its session list rather than re-deriving one — the
manifest is ground truth and the adapter cross-checks against it. `digest()`
pins the actual bytes from the manifest's per-object ETags, so a ledger row names
the data it ran on.

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

**Ordering within a tick** is fixed and is the thing most worth knowing: resolve
resting orders → deliver fill callbacks → deliver the tick event if subscribed →
deliver **one** bar callback carrying every bar that closed at this timestamp →
drain lifecycle events. One bar callback rather than one per timeframe, because
otherwise the ordering between a 5m and a 1m close is a hidden variable that
changes results and is recorded nowhere.

**T+1.** Anything submitted while handling the callback at sequence *S* is
eligible strictly after *S*. Without it a strategy fills at the close of the bar
it just saw, which is a clean one-bar lookahead producing beautiful and entirely
fake results.

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
and the tail is afforded by pooling per-session returns across runs and
bootstrapping N of them, because the expensive part is what a session looks like
and the cheap part is the sampling distribution over N of them.

**The reference strategy lives here, not in `src/strategies/`.** It used to be
the agent's file, which meant the power curve measured the metric, the threshold
*and* whatever that file happened to contain, with no way to separate them.

Reasoning: `docs/design/metric.md`.

---

## 6. `src/core/types.py` and `src/strategies/`

**`src/core/types.py`** holds every shared value type: `Ts`, `Side`, `Split`,
`OrderType`, `TimeInForce`, `OrderStatus`, `LifecycleKind`, `Tick`, `Bar`,
`BarEvent`, `TickEvent`, `FillEvent`, `LifecycleEvent`, `Order`, `BracketSpec`,
`Submit`/`Modify`/`Cancel`, `Blotter`/`BlotterRow`, `ScoreTuple`,
`DataCapabilities`, `Subscription`, and the `LookaheadError` / `HoldoutLocked`
exceptions. Immutable and dataset-agnostic. It sits at the top of `src/core/`
rather than inside one subpackage because all three import it and none owns it.

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
- **Reproducibility.** `git_sha` alone does not identify a trial, because the
  agent edits and runs before anything is committed. The ledger row carries a
  content hash of the strategy source, and `store_source()` copies the source to
  `runs/sources/<digest>.py` — content-addressed, so a hundred runs of the same
  strategy cost one copy. A digest identifies a trial; only the source reproduces
  one, and the rejected trials are exactly what N counts.
- **The optional fill-model spread.** A second full run under the opposite fill
  model, opt-in per profile, because a candidate that collapses between the two
  is telling you where its edge comes from.
- **`warmup_sessions` against the calendar embargo.** Warmup reaches backward
  from the first session of the split and the embargo is the gap in front of it,
  so warmup within the embargo reaches only into embargoed sessions. Checked once
  the dataset is built — the earliest point both numbers exist — and stood down
  for a dataset whose calendar is not pre-registered yet.

---

## 8. `scripts/` — build-time

Each produces something committed. None is importable from `src/core/`.

| Script | Produces | Consumed by |
|---|---|---|
| `scripts/inventory.py` | `data/manifest.json`, `docs/data-inventory.md` | `src/core/data/nq.py`, `tests/test_data_integrity.py`, `scripts/gen_agent_data.py` |
| `scripts/nq_contracts.py` | `data/nq-contract-selection.json` | `src/core/data/nq.py`, `tests/test_nq_adapter.py` |
| `scripts/tabulate_null.py` | `data/calibration/empirical-null.json` — the pooled null and its bootstrap tail | `src/core/scoring/calibration/null.py` |
| `scripts/calibrate.py` | the two-sided calibration curves (`--json` writes them under `data/calibration/`) | `docs/design/metric.md` |
| `scripts/gen_context_api.py` | `docs/design/context-api.md` | the research agent |
| `scripts/gen_agent_data.py` | `docs/agent/data.md` | the research agent |
| `scripts/probe_dbn.py` | nothing — a metadata/hash/compare CLI | a human debugging the bucket |
| `scripts/nq_smoke.py` | nothing — an end-to-end adapter smoke run | a human |
| `scripts/replay.py` | nothing — replays a ledger row from its stored source | a human auditing a trial |
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
| `tests/test_boundary.py` | The context boundary: immutability by introspection, subscription-id resolution, the protocol against the engine, warmup against the embargo |
| `tests/test_architecture_doc.py` | That this document names every module |
| `tests/test_examples.py` | That every worked example in `docs/agent/strategy-api.md` is byte-identical to a file in `tests/examples/` and actually runs |
| `tests/test_live_bucket.py` | The bucket itself, when credentials are present |

`tests/golden/pre_context_refactor.json` is the acceptance artefact for
behaviour-neutral refactors: eight cases, each pinning a blotter digest, row
count, fill count, bar count and final position.
