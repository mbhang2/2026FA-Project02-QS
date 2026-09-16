# Task 39 — where the teammate directories should go. A proposal; nothing moved.

On `merge-test-michael` (`8634ec5`), which carries the L1 harness at root plus
`Gowtam/L1/` (22 files, 965 lines) and `nalin/l1-loop/` (84 files, 7,097 lines
excluding `Data/raw/`).

**No files were changed. This document is the only write.** Execution is a
separate task, after this is reviewed.

---

## 0. Two corrections to the brief, before anything else

**The brief's premise about test collection is wrong, and it changes section 2.**
It says nalin's tests "are already being collected by a root `pytest` run (580
became 604 after the merge)". They are not. The root `pyproject.toml` sets
`testpaths = ["tests"]`, and `pytest --collect-only -q` on this branch collects
**604 tests, none of them under `nalin/` or `Gowtam/`**. The 604 is the same 604
it always was: **580 passed + 24 skipped**. The merge added no tests to the root
run.

That is good news for the mapping — nothing is silently in scope — but it means
the root `pytest` scope is a decision still to be made, not one already made by
accident.

**What *is* wrong is worse, and the brief does not mention it.** Run pytest at
nalin's tests explicitly and you get 45 collected and 7 collection errors, all
`ModuleNotFoundError: No module named 'pydantic'` — an undeclared dependency at
the root. But the import that *succeeds* is the dangerous one. See §2.0.

---

## 1. Inventory

### `Gowtam/L1/` — an agentic loop driver, no harness code

| Path | What it is | Entry points / importers | Imports `src.core`? Duplicates it? |
|---|---|---|---|
| `.opencode/agent/{planner,reviewer,worker}.md` | OpenCode agent definitions — role prompts for a three-agent loop | read by `opencode_bridge/invoke.py` | no. **Byte-identical to nalin's three** |
| `README.md` | 20 lines, what the loop is | — | no |
| `loop/driver.py` (142) | the loop: plan → work → review, per candidate | top of the call graph; nothing imports it | no |
| `loop/paths.py` (25) | `candidates/<id>/` layout; `STRATEGY_FILE = "current.py"` | imported by 7 modules | no — but see §5, it *targets* `src/strategies/current.py` |
| `loop/{planner,worker,reviewer}_step.py` (43 each) | one agent turn each | `driver.py` | no |
| `loop/prompt_doc.py` (155) | assembles the prompt document | `*_step.py` | no |
| `loop/spec_digest.py` (61) | hashes the spec the loop ran against | `driver.py` | no — **conceptual** overlap with `integrity.artifact_digests` |
| `loop/state_store.py` (22) | JSON candidate state on disk | `driver.py` | no |
| `opencode_bridge/{invoke,parse}.py` (141/41) | shells out to OpenCode, parses the reply | `*_step.py` | no |
| `schemas/*.py` (5 files, 117) | pydantic models: task plan, candidate package, state, verdict | everywhere | no. **pydantic is not in `requirements.lock`** |

Import style: **bare top-level** — `from loop import paths`, `from schemas...`,
`from opencode_bridge...`. Requires `Gowtam/L1/` itself on `sys.path`. The names
`loop`, `schemas`, `opencode_bridge` are generic and unqualified.

### `nalin/l1-loop/` — a second agentic loop *plus* a second backtest engine

| Path | What it is | Entry points / importers | Imports `src.core`? Duplicates it? |
|---|---|---|---|
| `src/loop/**` (8 files, 559) | the same loop as Gowtam's, diverged | `src/loop/run.py` (argparse CLI) | no. **Duplicates `Gowtam/L1/loop/`** — see §2.1 |
| `src/opencode_bridge/**` (193) | same, diverged by 13 lines | `*_step.py` | no. Duplicates Gowtam's |
| `src/schemas/**` (133) | same pydantic models, `__init__` and `candidate_package` differ | everywhere | no. Duplicates Gowtam's |
| `src/strategy/protocol.py` (39) | `Strategy` Protocol: `generate_positions(price_history) -> Series` | `loader.py`, tests | **overlaps `src/core/types.Strategy`** — §2.2 |
| `src/strategy/{candidate,loader}.py` (132) | load a strategy module from disk | orchestrator | overlaps `RemoteStrategy`'s loading, without isolation |
| `src/strategy/reference_strategies/buy_and_hold.py` (18) | a reference strategy | tests | overlaps `calibration/reference.py` in role |
| `src/execution/simulator.py` (41) | fill at next bar's open, bps fees/slippage | orchestrator | **overlaps `src/core/engine/fills.py`** — §2.3 |
| `src/execution/portfolio.py` (56) | equity curve from positions | orchestrator | overlaps `Blotter` / `reconstruct.py` |
| `src/execution/leakage_guard.py` (33) | raises if history extends past the current bar | orchestrator | overlaps `LookaheadError` in `engine/view.py` |
| `src/metrics/**` (6 files, 249) | sharpe, vol, drawdown, CVaR, turnover, concentration, stability, decay, composite | orchestrator, tests | **overlaps `src/core/scoring/metrics.py`** — §2.4 |
| `src/splitting/windows.py` (33) | train/val/holdout from an `ExperimentSpec` | orchestrator | **overlaps `config/calendars/` pre-registration** — §2.5, the sharpest one |
| `src/config/experiment_spec.py` (70) | the experiment spec dataclass | everywhere | overlaps `config/run.yaml` profiles |
| `src/data/loader.py` (139) | CSV/parquet bar loading | orchestrator | overlaps `src/core/data/` |
| `src/data/convert_dbn.py` (108) | DBN → parquet CLI | standalone | overlaps `src/core/data/dbn.py`, `scripts/probe_dbn.py` |
| `src/data/download_r2.py` (116) | **boto3 R2 downloader; reads `secrets/r2.env`** | standalone | overlaps `src/core/data/` + `scripts/inventory.py`. §5.3 |
| `src/orchestrator/run_experiment.py` (153) | the fixed experiment engine, end to end | CLI | overlaps `run.py` in role |
| `src/packaging/package_candidate.py` (121) | bundles a candidate for review | loop | no root equivalent |
| `src/serialization/trial_log.py` (35) | writes a trial log | orchestrator | **overlaps `runs/ledger.jsonl` and `runlog.py`** — §2.6 |
| `tests/**` (17 files, 1,281) | tests for all of the above | — | not collected by the root run |
| `experiments/dummy_run.py` (41) | end-to-end smoke; `sys.path` hack in the file | standalone | experiment, not harness |
| `hypotheses/Test1.md` (3,147) | a full research hypothesis document | — | notes |
| `hypotheses/example_{mean_reversion,momentum}.md` (21 each) | hypothesis templates | — | notes |
| `fixtures/sample_candidate.json` (11) | fixture | tests | test data |
| `Data/README.md` + `Data/raw/` (4 files, 2.9 MB) | **committed market data**: an ES DBN.zst, a CSV, a parquet, a placeholder | `loader.py` | §4.3 — the case trap, plus committed binaries |
| `.opencode/agent/*.md` | identical to Gowtam's three | bridge | duplicate |
| `pyproject.toml` | its own package + pytest config, deps incl. pydantic/scipy/pyarrow | — | **a second build config in one repo** |
| `opencode.json` (3) | OpenCode config | — | — |
| `.gitignore` (30) | ignores `candidates/`, `secrets/` locally | — | — |

Import style: **`src.`-prefixed** — `from src.loop import paths`. Requires
`nalin/l1-loop/` on `sys.path`, and the package is named `src`, the same as the
harness's.

---

## 2. Overlaps. Named, not resolved.

### 2.0 The one that is not a design overlap but a live hazard: two packages named `src`

`nalin/l1-loop/src/` and the root `src/` are both regular packages (both have
`__init__.py`) with the **same top-level name**. Which one `import src` resolves
to depends on `sys.path` order. Measured on this branch:

```
nalin's path first:   src      -> nalin/l1-loop/src/__init__.py
                      src.core -> src/core/__init__.py          (!)
                      src.loop -> nalin/l1-loop/src/loop        OK

root's path first:    src      -> src/__init__.py
                      src.core -> src/core/__init__.py          OK
                      src.loop -> ModuleNotFoundError
```

The first case is a **Frankenstein package**: `src` is nalin's, `src.core` is the
harness's. It works only because the harness is installed editable and
`__editable___l1_harness_0_1_0_finder` maps the submodule explicitly, overriding
normal path resolution. On a fresh checkout without `pip install -e .`, or with
a different install layout, `src.core` disappears the moment nalin's directory
precedes the root on `sys.path`.

Why this matters more here than in most repos: `tests/test_layout.py` asserts
`src.core` never transitively imports `scripts`, and `tests/test_boundary.py`
walks module graphs to prove the strategy process cannot reach the engine. Both
reason about the identity of the `src` package. **A `src` whose identity depends
on `sys.path` order is a direct threat to the guarantees this project rests on**,
and it is true today whether or not anything moves.

This is not an overlap to arbitrate. It is a defect of the merge layout, and any
mapping has to end with exactly one importable `src`.

### 2.1 `Gowtam/L1/loop/` versus `nalin/l1-loop/src/loop/` — two forks of one loop

These are the same program. Same module names, same `.opencode` agent
definitions **byte for byte**, same pydantic schemas, same bridge. Diverged:

| module | Gowtam | nalin | differing lines |
|---|---:|---:|---:|
| `driver.py` | 142 | 131 | 49 |
| `planner_step.py` | 43 | 59 | 52 |
| `worker_step.py` | 43 | 42 | 49 |
| `reviewer_step.py` | 43 | 55 | 40 |
| `paths.py` | 25 | 23 | 6 |
| `state_store.py` | 22 | 22 | 4 |
| `opencode_bridge/invoke.py` | 141 | 152 | 13 |
| `schemas/` | 5 files | 5 files | 2 files differ |

Two differences are not cosmetic:

- Gowtam's `paths.py` sets `STRATEGY_FILE = "current.py"` and carries the comment
  *"the final file must land in `src/strategies/current.py`"*. nalin's sets
  `STRATEGY_FILE = "train.py"`. **Gowtam's loop is aimed at this harness;
  nalin's is aimed at its own engine.**
- nalin's has `src/loop/run.py` (208 lines, argparse) as a CLI entry; Gowtam's
  has no entry point at all — `driver.py` is imported by nothing.

nalin's is the larger and more finished of the two; Gowtam's is the one already
pointed at the harness's contract. **Which survives is a team call.** Adopting
Gowtam's costs writing the CLI and the tests it has none of (nalin has 17 test
files covering this code; Gowtam has zero). Adopting nalin's costs re-pointing
`paths.py` and `packaging` at `src/strategies/current.py` and deciding what
happens to the engine underneath it (§2.2–2.6). Adopting neither and keeping the
loop built in Tasks 30–36 costs the OpenCode integration, which neither
`src/core/loop/` nor `scripts/llm_campaign.py` has.

### 2.2 The `Strategy` contract — two shapes, not two implementations

| | nalin | root |
|---|---|---|
| shape | `generate_positions(price_history: DataFrame) -> Series` | `setup(ctx)`, `on_bars(ctx)`, `on_fill(ctx)`, `on_lifecycle(ctx)` |
| lookahead defence | the signature only hands history up to "now" — structural | `ctx` raises `LookaheadError` on future access; plus the process boundary |
| execution | vectorised: positions for a whole frame | event-driven: intents, resolved by a fill model |
| isolation | in-process import (`strategy/loader.py`) | separate process, module allowlist, sandbox |

These are **not reconcilable by filing**. A vectorised next-bar-position
strategy and an event-driven intent-emitting strategy are different research
objects; the metric, the fill model, the null and every calibration number in
`docs/design/metric.md` are defined on the second. Adopting the first means
re-deriving all of it. Adopting the second means nalin's `metrics/`,
`execution/`, `splitting/` and `orchestrator/` lose their consumer.

### 2.3 Fills

nalin's `simulate_fill` executes at the *next bar's open* with fees and slippage
in bps on traded notional. The harness's `unfavorable` model fills a resting
limit only on a trade strictly through it, at tick resolution, and Task 4's
whole argument is that the pessimistic model is the one the loop scores on. The
bps model is a different — and materially more optimistic — assumption. **Not a
duplicate to delete; a second modelling choice that would need its own
calibration.**

### 2.4 Metrics

nalin: `sharpe_ratio`, `annualized_volatility`, `max_drawdown`, `cvar`,
`turnover`, `pnl_concentration`, `subperiod_stability`, `decay_check`,
`composite_score` — on daily-return and equity `Series`.
Root: `sortino` (the objective), `sharpe`, `downside_deviation`, `skewness`,
`excess_kurtosis`, `max_drawdown`, `profit_concentration`, `diagnostics` — on
per-session returns derived from a reconstructed blotter.

`max_drawdown` and concentration exist on both sides with different inputs and
conventions. **`composite_score` is the important one**: it is a second
objective function. The harness's central claim is that the objective is a
single fixed scalar the agent cannot reach; a second scoring path that any code
can import is precisely the thing `metrics.py`'s import guard exists to prevent.

### 2.5 Splitting — the sharpest conflict in the merge

`src/splitting/windows.py` **derives** train/validation/holdout from an
`ExperimentSpec` at run time. The harness **pre-registers** them in
`config/calendars/nq-trades.yaml`, and `CLAUDE.md` states that the file's commit
date *is* the pre-registration, so editing it is not a code change.

A split computed from a spec at run time is not a pre-registration. These two
cannot both be true of the same project. This is the overlap that most needs a
decision before any code moves, and it is not a placement question.

### 2.6 Trial logging

`serialization/trial_log.py` (35 lines) writes a trial log. `runs/ledger.jsonl`
plus `runlog.py` is the harness's: append-only, one row per invocation including
crashes, fingerprints, isolation level, N counted from it. If both exist, "N" has
two definitions. Only one can be the denominator.

---

## 3. Proposed mapping

Rules applied: nothing enters `src/core/` in this proposal; build-time and
run-time stay separated; experiments, hypotheses and notes go outside the core;
tests follow what they test.

**The shape of the proposal is: move almost nothing yet, and rename the two
things that are actively unsafe.** The overlaps above are decisions, and filing
code into `src/core/` before they are made would launder unreviewed code through
a directory whose whole meaning is that it is reviewed and immutable.

| From | To | Why |
|---|---|---|
| `nalin/l1-loop/src/` | **`nalin/l1-loop/nalin_l1/`** (rename in place) | Ends the two-packages-named-`src` hazard (§2.0) without deciding anything else. Their imports become `from nalin_l1.loop import paths`. This is the one change that should happen regardless of how every other question is answered. |
| `nalin/l1-loop/Data/` | **`nalin/l1-loop/market_data/`** (rename in place) | §4.3. Removes the latent case collision with root `data/` before anyone proposes moving it up. Renaming in place costs nothing and cannot collide. |
| `nalin/l1-loop/tests/` | stays | It tests nalin's engine and loop, not shared code. Rule: tests follow what they test. Root `testpaths` stays `["tests"]` — deliberately, and now on the record. |
| `nalin/l1-loop/hypotheses/` | `research/hypotheses/` | Hypotheses are research notes, not harness code. A new top-level `research/` is the right home and the repo has none. `docs/` would also do; `research/` keeps prose that is *input to* experiments separate from prose that *documents* the system. |
| `nalin/l1-loop/experiments/dummy_run.py` | `research/experiments/` | Same reason. It is a smoke script with a `sys.path` hack, not a component. |
| `nalin/l1-loop/Data/README.md` | with the renamed data dir | Describes the data; travels with it. |
| `Gowtam/L1/` | **stays entirely**, pending §2.1 | Nothing imports it, it has no entry point, and it is one side of an unresolved fork. Moving it would imply it won. Its bare top-level package names (`loop`, `schemas`, `opencode_bridge`) must **not** reach the repo root — they would shadow anything with those names for good. |
| `Gowtam/L1/.opencode/` and `nalin/l1-loop/.opencode/` | stay until §2.1 is decided, then one copy | Byte-identical. Two copies of one contract is exactly the drift `agent-patterns/` was reviewed for in Task 35. |
| `nalin/l1-loop/pyproject.toml` | stays | A second build config is a symptom of the fork; resolving it is §2.1's job, not a file move. |
| everything else under `nalin/l1-loop/` | stays | Blocked on §2.2–2.6. |

**Nothing is proposed for `src/core/`, `src/strategies/`, `config/`, `data/`,
`scripts/`, `run.py` or root `tests/`.** Per the brief's first rule, any such
proposal would be flagged for explicit review; this mapping makes none.

---

## 4. What each move would break

### 4.1 `src/core/**`, `run.py` — **nothing proposed here**

For the record, since a later task will be tempted: a move here changes
`code_fingerprint`, which is a hash over `src/core/**/*.py` and `run.py` **by
path**. Every committed ledger row records the fingerprint of the code that
produced it, and `scripts/ledger_check.py` re-derives it at the row's commit to
answer "was this row produced by code still in history". Move a file and every
historical row's fingerprint stops matching HEAD.

Re-establishing provenance would mean either re-running every committed trial
(≈ 1,100 rows across Tasks 36–38, including a 7.1-hour calibration) or recording
a path-translation table that `ledger_check` consults — which makes the
fingerprint a function of a mapping file, and a fingerprint with an exception
list is not a fingerprint. **Not worth doing. Leave `src/core/` alone.**

### 4.2 `config/**`, `data/calibration/**` and the other `ARTIFACT_GLOBS` paths — **nothing proposed here**

`artifact_digests()` covers 19 paths by glob: `config/*.yaml`,
`config/calendars/*.yaml`, `config/prompts/*.md`, `data/calibration/*.json`,
four other `data/*.json`, `src/strategies/current.py`. A missing file is recorded
as `"absent"` rather than skipped, precisely so a delete-and-recreate is visible.
Moving one changes `artifact_fingerprint` for every row that recorded it, and a
run whose artifacts moved mid-flight is recorded as
`invalidated_artifacts_mutated`.

Same conclusion, same reason. **Nothing from either teammate directory should be
proposed for these paths**, and if something is later, it is a re-calibration,
not a move.

### 4.3 The case trap, checked as instructed — and there is a second one

`nalin/l1-loop/Data/` and root `data/` differ only in case. Today there is **no
collision**, because `Data/` is nested: `git ls-files` shows no case-colliding
top-level names on this branch. The trap is real but latent — it fires only if
`Data/` is proposed for the root.

The proposal is to rename it **in place** to `market_data/`, which removes the
trap permanently and costs one `git mv` plus one path in `loader.py`. On a
case-insensitive filesystem a rename that differs only in case needs the
two-step `git mv Data tmp && git mv tmp market_data`; renaming to a different
word avoids that too.

**The second one the brief does not mention.** Both `paths.py` files reference
`PROJECT_ROOT / "Docs" / ...` — capital D, against the repo's lowercase `docs/`.
It is currently harmless because `PROJECT_ROOT` is the teammate directory and
neither has a `Docs/`. But `ls -d Docs` at the repo root **succeeds on this
machine**, resolving to `docs/` by case-insensitivity. If either loop is ever
re-rooted at the repo, that reference silently reads the real `docs/` on macOS
and fails on Linux CI. Fix the string, not the filesystem.

### 4.4 Everything actually proposed — imports, test paths, doc references only

| move | breaks |
|---|---|
| `src/` → `nalin_l1/` | every `from src.*` inside `nalin/l1-loop/` (35 import lines, 17 test files, `conftest.py`, `experiments/dummy_run.py`), and `[tool.setuptools.packages.find] include = ["src*"]`. Mechanical, contained, and **testable in isolation**: `pytest nalin/l1-loop/tests` before and after should collect the same 45 and error the same 7. |
| `Data/` → `market_data/` | one path constant in `src/data/loader.py`, the `Data/README.md` reference. |
| `hypotheses/`, `experiments/` → `research/` | nothing imports them; `dummy_run.py`'s `sys.path` hack needs one line changed. |

None of these touch a fingerprinted path. None changes a committed number.

---

## 5. Also reported

### 5.1 Does either expect to import from `src.core`? — No, and one is clearly aimed at it

**Neither directory contains a single reference to `src.core`.** Grep across both
trees returns nothing.

But **Gowtam's loop is written against this harness's contract**: its
`paths.py` sets `STRATEGY_FILE = "current.py"` and carries the comment *"the
final file must land in `src/strategies/current.py`"*. It assumes the interface
the harness already enforces — one agent-writable file at a fixed path — without
importing anything. That is the cheapest possible coupling and it is the right
one: a loop that writes the file and invokes `run.py` needs no import at all.

nalin's assumes its own engine throughout and would need §2.2–2.6 resolved before
it could import anything of the harness's.

So the answer to "does this layout work at all" is: **yes for Gowtam's loop,
which needs only a path convention; not yet for nalin's, which needs a decision
about which engine the project has.**

### 5.2 What should not move, or should be deleted rather than filed

- **The duplicated `.opencode/agent/*.md`** — three files, byte-identical across
  both trees. One copy survives §2.1; the other should be *deleted*, not filed.
- **`nalin/l1-loop/src/schemas/` vs `Gowtam/L1/schemas/`** — same models, two
  files differing. Same disposition.
- **`Data/raw/placeholder.csv`** (241 lines) — named placeholder, alongside real
  data. Delete unless something depends on it.
- **`Gowtam/L1/loop/driver.py`** has no entry point and nothing imports it.
  Whether that is "unfinished" or "superseded" is Gowtam's call, not a filing
  decision.
- **`Data/raw/glbx-mdp3-20250703.trades.dbn.zst`** (2.9 MB) and the two derived
  files are **committed market data**. The root repo deliberately commits only
  *fingerprints* of bucket objects (`data/manifest.json`) and gitignores
  `data/cache/`. A 2.9 MB binary in git is a policy difference worth a decision:
  it is one day of ES, it is already in the bucket, and it is in history now
  either way.

### 5.3 Anything touching `runs/`, `secrets/` or credentials

- **`nalin/l1-loop/src/data/download_r2.py`** reads
  `nalin/l1-loop/secrets/r2.env` and uses `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT` via boto3. It **prints
  guidance, never values** — checked line by line. nalin's `.gitignore` ignores
  `secrets/`, so the path is covered locally; the root `.gitignore` covers
  `secrets/` too. **If this file ever moves, its three-`parent` walk to
  `secrets/` moves with it and will silently point somewhere else.**
- **Nothing in either tree references `runs/`.** nalin writes to `candidates/`
  (gitignored in its own `.gitignore`, **not** in the root's — worth adding if
  the loop is ever run from the root).
- No credential value appears in either tree.

---

## 6. What this mapping cannot decide

Each of these is a team call, and each blocks files this proposal leaves in
place:

1. **Which loop.** Gowtam's, nalin's, or the one in `src/core/loop/` +
   `scripts/llm_campaign.py`. They are three implementations of one idea; only
   the third has a gate, a ledger and a correction behind it, and only the first
   two speak OpenCode.
2. **Which engine, and therefore which `Strategy` contract** (§2.2). Vectorised
   next-bar positions, or event-driven intents. Every calibration number the
   project has is defined on the second.
3. **Pre-registered splits or derived splits** (§2.5). These cannot coexist.
4. **One objective or two** (§2.4). `composite_score` is a second scoring path,
   and a scoring path any code can import is what the import guard exists to
   stop.
5. **One trial log or two** (§2.6). "N" needs exactly one definition.
6. **Whether committed market data is repo policy** (§5.2).
7. **Root `pytest` scope.** Currently `tests/` only, by configuration rather than
   by accident. If nalin's tests should run in CI they need `pydantic`, `scipy`
   and `pyarrow` added to the root lockfile, and the `src` rename in §3 first.

**The one thing that should not wait for any of these** is the `src` rename. It
is a correctness fix for a hazard that exists today, it decides nothing, and it
is the only proposal here I would make regardless of how every question above is
answered.
