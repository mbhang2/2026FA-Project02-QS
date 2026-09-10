# L1 Build Plan — the Research Harness

The deliverable is the agentic research loop and its keep/discard gate, not a
profitable strategy. Quant finance is the test bed because it is a domain where a
system that fools itself still produces a number that looks like success.

**The harness is dataset-agnostic.** Datasets, fill models and scoring metrics are
interfaces. The NQ/ES/QQQ data described in `docs/datasets/nq-es-qqq.md` is the first
adapter, not an assumption. A strategy family, an instrument, or an asset class can be
swapped without touching the loop.

Nothing in this document is specific to a dataset. If something dataset-specific turns
up here, it is in the wrong file.

Rewritten 2026-09-08, splitting the v4 plan into this and the dataset adapter document.
Companion to `architecture-proposal-v1.md`, which defines L1/L1.5/L2 and the committee.

Status markers: **[decided]** · **[open]** · **[proposed]**

## What the harness guarantees

These are dataset-independent and are the actual contribution:

1. The agent proposes a change, it is evaluated, and it is kept or reverted — with
   every trial logged, including crashes and abandoned runs.
2. The scorer is code the agent cannot edit or call.
3. Splits are pre-registered before any outcome is observed; the holdout opens once.
4. **N is global** — see below.
5. Accepting a candidate requires clearing a conjunctive gate, not a single scalar.
6. The harness demonstrably rejects nothing-there data and demonstrably detects a
   planted signal of known strength.

## Interfaces

### Dataset

```python
class Dataset(Protocol):
    name: str
    def periods(self) -> Sequence[Period]: ...     # sessions, days, bars — its own unit
    def load(self, period: Period) -> View: ...    # refuses anything outside the request
    def calendar(self) -> Calendar: ...            # pre-registered train/val/holdout
    def digest(self) -> str: ...                   # pins the exact data a run used
```

`digest()` matters: every ledger row records which data version produced it, so a trial
is reproducible against the bytes it actually ran on rather than against whatever the
store holds later.

The unit is the dataset's own — a session, a trading day, a bar, a fold. The harness
does not assume sessions exist.

### Strategy

```python
class Strategy(Protocol):
    def setup(self, ctx) -> None: ...                     # once per run
    def on_period(self, view) -> Sequence[Intent]: ...    # once per period, in order
```

The `setup()` / `on_period()` split is what makes the per-period view enforceable:
anything a strategy wants to precompute has to be declared before the walk starts, so
`on_period` can be handed a view that raises on future access. (The v4 plan called this
`setup()` / `on_bar()`; the harness does not assume bars any more than it assumes
sessions, so the per-period name is the general one.)

### Fill model

```python
class FillModel(Protocol):
    def resolve(self, intents, period_data) -> Blotter: ...
```

Strategies emit **intents** — entries, exits, bracket levels — never P&L. If a strategy
can compute its own objective, it can optimize a number it controls. The fill model
turns intents into a blotter; the engine never assumes brackets, bars, or a particular
order type exist.

A fill model must **resolve** intra-bar ordering rather than silently assume it. Where
the representation cannot say which of two levels was touched first, the resolution has
to come from a finer representation or be declared unresolvable — an assumption made
quietly at this layer becomes directional bias in every result above it, and it looks
like alpha. What that costs and how one adapter plans to satisfy it is a dataset
concern, not a harness one.

### Scorer

```python
def score(blotter: Blotter, *, period: Period) -> ScoreTuple: ...
```

Blotter in, score tuple out. Nothing dataset-specific. The metric must be invariant to
turnover, leverage and universe size — those invariances are exactly what make it
portable, and their absence is what makes raw Sharpe unusable here.

## Pre-registration and N

Calendars are **per dataset**, held in a registry. Each declares train / validation /
holdout, committed before any outcome on that dataset is observed. A split chosen after
exploring the data is not a pre-registration.

**[decided] N is global across all datasets, not per dataset.** Otherwise adding a
dataset silently buys a fresh holdout and resets the multiple-testing accounting —
reintroducing the exact false-discovery hole this project exists to close. The trial
ledger spans every dataset, and any deflated-Sharpe or Harvey threshold computed
anywhere uses the global count.

This rule is pre-registered now, while it is abstract and no experiment depends on it.

## Two-sided calibration

A gate that rejects everything passes a null test trivially and is useless. Calibration
is therefore two-sided, and the synthetic dataset provides both halves:

- **Null:** no signal present. The gate must reject essentially everything. The
  false-acceptance rate at a known null is a publishable number.
- **Power:** a planted signal of known strength. The gate must accept it. Sweeping the
  strength gives a power curve — how large an edge has to be before this harness can
  see it.

Both are just dataset implementations, which is the test that the abstraction is real.

## Build order

Steps 1–6 need no market data and are not blocked by anything.

1. **Dataset interface + synthetic implementation.** Parameterizable: pure noise, or a
   planted signal of specified strength.
2. **Scorer**, in a file the agent's tooling cannot open. Enforced by tool permissions
   *and* a CI check on changed paths — the CI check is the one that holds.
3. **Trial ledger**, before the loop that fills it. Append-only per run: global N, git
   SHA, dataset name and digest, hypothesis family, wall-clock, score tuple, outcome
   including crashes and abandonment. If it does not exist before run 1, N is a
   reconstruction.
4. **Engine + fill model interface.** Walks periods, calls the strategy, resolves
   intents, emits a blotter.
5. **Loop harness.** Propose → evaluate on validation → keep or `git reset --hard` →
   log. Serial, wall-clock budgeted. Accept rule deliberately dumb at this stage:
   single scalar.
6. **Walking skeleton on synthetic**, end to end, with the dumbest possible strategy.
   Then null and power calibration — which by now is just running it.
7. **Committee as a second accept tier.** Cheap scalar keeps/resets inside the loop;
   promotion runs four auditors in parallel — leakage, statistics, economics,
   robustness — any one can veto. Plus purged/embargoed CV, DSR with global N, PBO via
   CSCV, Harvey t > 3.0.
8. **First real dataset adapter** and its pre-registered calendar.
9. **First real run**, holdout still closed.

Build step 6 as a thin vertical slice rather than completing each layer first. The
interfaces are the expensive part to change, and only an end-to-end run reveals whether
they are right.

## Design principles

**Make the bug unrepresentable rather than documented.** A prompt telling an agent to
avoid lookahead does not prevent lookahead; an API that cannot express it does. The
per-period view raises on future access; strategies cannot import the scorer;
strategies emit intents rather than P&L.

**Log what was rejected, not just what was kept.** Crashed runs, abandoned trials,
excluded data, falsified hypotheses. A process whose rejections cannot be inspected
cannot be evaluated.

**Verify cheap representations against expensive ones on a sample.** Before trusting a
compression or approximation, replay a window both ways and assert agreement.

## Permission tiers

The agent may write **only** under `src/strategies/`.

| Path | Agent may call | Agent may edit |
|---|---|---|
| `scripts/` | no | no |
| `config/` | via loader | no |
| `src/core/` (dataset, engine, ledger) | yes | no |
| `src/core/scoring/scorer.py` | no | no |
| `src/strategies/` | it is the agent's code | yes |

Enforced by tool permissions and by a CI check comparing changed paths against an
allowlist. The CI check is the one that actually holds — configs drift and get copied
wrong.

`scripts/` is **build-time**: it produces the committed artifacts (`data/manifest.json`,
the published session list, the contract-selection file, the tabulated null,
`docs/data-inventory.md`). `src/core/` is **run-time**: it consumes them. Nothing under
`src/core/` may import anything under `scripts/`, and a test asserts it — otherwise a
trial could re-derive its own coverage, or tabulate its own null and mark its own
homework.

```
config/calendars/<dataset>.yaml  immutable — pre-registered splits, one per dataset
config/corrections.yaml          historical record of corrected objects
scripts/                         build-time; produces committed artifacts
src/core/                        run-time; importable by strategies, never editable
  data/      dataset protocol, adapters, cache access
  engine/    event loop, bars, orders, fill models, context
  scoring/   metrics.py, scorer.py, calibration/ — none importable by strategies
  types.py   shared value types
src/strategies/current.py        THE ONLY THING THE AGENT WRITES
runs/                            trial ledger, append-only JSONL, committed
data/                            committed artifacts;  data/cache/ gitignored
tests/                           offline in CI; live tests skip without credentials
docs/agent/data.md               generated; what the RESEARCH agent may subscribe to
docs/design/                     simulator interface, metric, generated context API
docs/plans/                      this document
docs/datasets/                   one file per adapter
docs/setup/                      onboarding
docs/tasks/                      task briefs, versioned alongside their outputs
```

## Open questions

**[open] The metric.** Must be invariant to turnover, leverage and universe size.

**[open] Python or R for the strategy layer.** Gate implementations (`purgedcv`,
`skfolio`) are Python; Peterson authored `quantstrat`, whose indicator/signal/rule
decomposition may be the expected shape. Ask directly.

**[open] How holdout is budgeted across an agent's iterations** — for the mentors.
