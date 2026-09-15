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
Status brought current 2026-09-14, after Task 34; the plan itself is unchanged, and the
sections that moved are the build-order status, the tree, and the open questions.
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
   planted signal of known strength. Both halves are now measured, not promised: a
   known-null campaign's family-wise error rate is 0.058, CI [0.039, 0.085], at q = 0.05,
   and detection reaches 0.90 at N = 200 for a planted-signal prevalence of 0.05 and
   strength 0.30. `docs/design/metric.md` states what that guarantee is conditional on.

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

Steps 1–6 need no market data and are not blocked by anything. Status is as of Task 34.

1. **[done]** **Dataset interface + synthetic implementation.** Parameterizable: pure
   noise, or a planted signal of specified strength.
2. **[done]** **Scorer**, in a file the agent's tooling cannot open. Enforced by tool
   permissions *and* a CI check on changed paths — the CI check is the one that holds.
   Since Task 25 there is a third mechanism the first two do not depend on: the scorer
   is not in the strategy's process.
3. **[done]** **Trial ledger**, before the loop that fills it. Append-only per run:
   global N, git SHA, dataset name and digest, hypothesis family, wall-clock, score
   tuple, outcome including crashes and abandonment. If it does not exist before run 1,
   N is a reconstruction.
4. **[done]** **Engine + fill model interface.** Walks periods, calls the strategy,
   resolves intents, emits a blotter.
5. **[done]** **Loop harness.** Propose → evaluate → keep or discard → log. Serial,
   wall-clock budgeted. *The accept rule did not stay dumb:* a single scalar compared to
   a per-run threshold produced a family-wise error rate of 0.575 on a known-null
   campaign, so the gate is now a correction over the campaign's trials. That is the
   project's contribution and it arrived here.
6. **[done]** **Walking skeleton on synthetic**, end to end, with the dumbest possible
   strategy. Then null and power calibration. It was not "just running it" — the first
   honest null needed a per-candidate conditional construction (Tasks 31–33) and the
   first power curve is Task 33.
7. **[open]** **Committee as a second accept tier.** Cheap scalar keeps/resets inside
   the loop; promotion runs four auditors in parallel — leakage, statistics, economics,
   robustness — any one can veto. Plus purged/embargoed CV, DSR with global N, PBO via
   CSCV, Harvey t > 3.0. Nothing of this exists yet.
8. **[done]** **First real dataset adapter** and its pre-registered calendar.
9. **[open]** **First real run**, holdout still closed. Everything measured so far is on
   the training split or on synthetic data; validation has not been spent.

Build step 6 as a thin vertical slice rather than completing each layer first. The
interfaces are the expensive part to change, and only an end-to-end run reveals whether
they are right. *In hindsight this was right about the interfaces and wrong about the
cost of step 6:* the walking skeleton was cheap and the gate behind it took five tasks
to make honest.

### What the vertical slice cost that the plan did not anticipate

Recorded here rather than in the dataset document, because none of it is dataset-specific
and a second harness would pay it again.

- **The strategy needs its own process, not just an import guard.** Two adversarial
  passes reached the dataset and the intent queue through the object graph from inside
  `setup()`. A boundary maintained by inventory fails silently when someone adds a
  re-export; a boundary that is a process fails by the module not being there.
- **A check that runs the agent's code is an oracle even if it reports no number.**
  Pass/fail is one bit, and eighteen probes binary-searching a threshold recovered peak
  P&L with zero trials counted. The agent-facing check now executes nothing.
- **The null has to be conditional on the candidate.** One null per dataset is a null
  for one strategy's trading profile, and a selective candidate is 9–24× off it.
- **Every number needs a stated resolution before it is measured.** Three confident
  attributions in this project were wrong when checked, and each was caught by a
  measurement designed to be able to resolve the effect it was claiming.

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
config/run.yaml                  the run profile: dataset, split, seed, fill model,
                                 history window, action cap, budgets
scripts/                         build-time; produces committed artifacts
src/core/                        run-time; never editable
  types.py     shared value types — the one module the strategy process loads
  runlog.py    RunTicket, LedgerRun, the append-only ledger
  checks/      static analysis; what the agent-facing check runs instead of the code
  data/        dataset protocol, adapters, cache access
  engine/      event loop, bars, orders, fill models, context
    reconstruct.py   P&L re-derived from the fill log — the score's source of truth
    remote/          the strategy's own process: frames, adapter, child, isolation
  scoring/     metrics.py, scorer.py, calibration/, randomization/, correction.py,
               integrity.py — none importable by strategies, none loaded in the
               strategy's process
  loop/        the campaign driver, the proposing-agent protocol, the gate summary
src/strategies/current.py        THE ONLY THING THE AGENT WRITES
runs/                            trial ledger, fill logs, campaign records; all
                                 append-only JSONL, all committed
data/                            committed artifacts;  data/cache/ gitignored
tests/                           offline in CI; live tests skip without credentials
docs/agent/                      the RESEARCH agent's context
  data.md                          generated; what it may subscribe to
  strategy-api.md                  how to write a strategy; examples run in CI
docs/design/                     architecture, simulator interface, metric,
                                 generated context API
docs/plans/                      this document
docs/datasets/                   one file per adapter
docs/setup/                      onboarding, and how to stand up the Linux runner
docs/tasks/                      task briefs, versioned alongside their outputs
docs/reviews/                    the script behind every measured number, with the
                                 replication count and the resolution it can claim
```

## Open questions

**[decided] The metric.** Sortino on per-session returns, MAR 0, with an observation
floor and a Sharpe fallback that carries its own name and its own null. It passed a
stated screening criterion for invariance to turnover, leverage and universe size;
`docs/design/metric.md` has the screen and the exemption it needed.

**[decided] Python for the strategy layer.** The whole harness is Python, the strategy
protocol is a Python `Protocol`, and the agent-facing check is mypy against it.

**[open] How holdout is budgeted across an agent's iterations** — for the mentors. Still
untouched, and now the main thing standing between the harness and a real result.

**[open] What the proposing agent sees.** `propose(history)` takes the campaign history
and the stub implementations delete it unread, which is the conservative default and not
a decision. An agent that sees past scores can hill-climb the gate; one that sees nothing
cannot learn. Deferred from Task 31 and still deferred.

**[open] The stopping rule.** A campaign currently runs to a fixed N. Stopping when a
discovery appears is optional stopping and breaks the correction; stopping on budget is
honest but arbitrary.

**[open] Campaign reproducibility under a real LLM agent.** Every number in the repo
comes from a deterministic stub proposer. A sampling agent makes the candidate set itself
a random variable, and nothing has been measured about what that does to the guarantee.
