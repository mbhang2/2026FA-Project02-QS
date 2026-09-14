# Engine + Orchestrator Upgrade — Implementation Plan

> **Document Type**: Implementation Plan (Atomic)
> **Status**: Proposal — supersedes `Docs/plan.md` (loop) and `Docs/L1_Ready.md`
> (engine) for the *next* increment only; both frozen contracts remain the
> backward-compatible baseline.
> **Two workstreams**:
> - **A — Tick-scale engine**: make the fixed orchestrator process tick-level
>   data at interactive speed (priority 1).
> - **B — Multi-session orchestrator**: replace the monolithic single-session
>   planner→worker→reviewer loop with the *document-anchored, one-task-per-session*
>   workflow used in `Docs/References/` (atomic plan + hypothesis + a
>   self-contained per-task prompt document + a fresh session per task).
> **Reference (mechanism only, not copy-pasted content)**:
> `Docs/References/Ref_atomicplan.md` and `Docs/References/Ref_prompts.md`.
> We adopt the *multi-session mechanism* — a re-pasted COMMON CONTEXT block, one
> self-contained prompt per task, "one task per session", strict role
> separation, a decision-status lifecycle, anti-hallucination rules, and
> per-session self-critique — but **re-designed as a fully agentic pipeline**.
> There is no human-in-the-loop stage; every decision (build, verify, question,
> audit, and the final PASS/VETO) is made by an agent role.

---

## 0. Data (real tick source, now present)

Raw XAUUSD ticks live at `Data/raw/Ticks/{YYYY}/{MM}/{DD}.parquet`:

- **Span**: 2020-01 → 2026 (2026 partial), ~1,595 daily files, ~255–263 files/year.
- **Schema**: `timestamp (datetime64[ns, UTC])`, `bid`, `bid_vol`, `ask`,
  `ask_vol` (all float64). ~190k ticks/day sample → ≈ **340M ticks total**.
- **Canonical price**: mid-price `(bid + ask) / 2` (project-wide convention).
- **No OHLCV exists** — the engine's canonical bar schema
  (`open/high/low/close/volume`) must be *produced* by resampling ticks. This
  makes Task A.1 the mandatory first step, not an optimization.

---

## 1. Why this plan exists (two independent failure modes)

### 1.1 Engine is O(n²) in bar count

`src/orchestrator/run_experiment.py::_run_window` loops per bar and, inside
that loop, re-slices the full history three times:

```python
for ts in window_ts:
    history = prices[prices.index < ts]               # O(n) full mask + copy per bar
    assert_no_lookahead(history, ts)                  # O(history) scan per bar
    position = strategy.generate_positions(history).iloc[-1]  # strategy recomputes O(history)
```

Three nested `O(n)` → `O(n²)`. Fine at 120 bars; **hours-to-days** at 10M–60M
ticks (5–10 years). The equity-curve builder (`src/execution/portfolio.py`) is
a second, separate Python `for` loop that should be vectorized.

### 1.2 Loop is O(steps × context) and one monolithic session

The loop threads a **single OpenCode session** per candidate, so context grows
monotonically and the **full hypothesis is re-embedded into every step's
prompt** (see `build_reviewer_prompt`: `candidate.hypothesis` is an entire
paper on `Test1.md`). This is the "exponential" slowdown. The reference project
solved the *identical* cross-session problem with a different mechanism: every
task runs in a **fresh session** fed by a **self-contained prompt + a stable
COMMON CONTEXT block** — so no session depends on accumulated context.

---

## 2. The mechanism we are adopting (from the reference)

The reference's multi-session trick, in full, is:

1. **Atomic plan** is the single source of truth. Every task is written as
   `Action / Input / Output / Acceptance` plus an optional **decision gate**.
2. **A per-task prompt document** (`Ref_prompts.md`) holds, for every task, a
   copy-paste-ready, **self-contained** prompt. Nothing in a task prompt
   depends on anything a previous session happened to remember.
3. **A COMMON CONTEXT block** is re-pasted at the top of *every* fresh session:
   data schema, locked decisions, project rules, anti-hallucination rules,
   reproducibility-metadata requirements. This is the durable memory that
   replaces accumualted conversation context.
4. **One task per session.** "Start a new session → paste COMMON CONTEXT → paste
   the task prompt → review → update PROGRESS.md."
5. **Roles are never mixed in one session**: Builder (writes code only),
   Reviewer (reads code only, never edits), Statistician (reads raw results),
   Auditor (bias/leakage/overfitting). These are the four roles that compose the
   committee — all agents, none a human.
6. **Staged, fully agentic pipeline**: planner → worker(build) → reviewer(verify)
   → engine(execute) → statistician(question) → auditor(bias/leakage/overfit) →
   committee(aggregate PASS/VETO). Each stage is an agent role; there is no
   human step anywhere in the loop.
7. **Decision-status lifecycle** written into PROGRESS.md:
   `Draft → Provisional → Locked → Deprecated`.
8. **Anti-hallucination + final self-critique** baked into every prompt.

We adopt #1–#8 as the *shape* of the new orchestrator, replacing the current
implicit single-session context with explicit, file-backed context.

---

## 3. Workstream A — Tick-scale engine (priority 1)

> **Goal**: `run_experiment` processes tick data in O(n) time and bounded
> (short-window) memory. The **planner decides the timeframe(s)** the strategy
> needs; the **engine resamples ticks → bars on-demand in short windows**; the
> strategy keeps consuming bars through the unchanged frozen `Strategy` protocol.
> No upfront "resample every frequency" grid.
>
> **Timeframe flow**: planner (reads hypothesis) → declares timeframe in the plan
> → engine resamples that timeframe on-demand per short window → strategy gets
> bars (unchanged `generate_positions`).

### Task A.0 — Timeframe declaration (planner → strategy spec)
- **Action**: The planner, when it decomposes the hypothesis, determines the
  target data timeframe(s) the strategy needs (e.g. `"1d"`, `"1m"`, `"5m"`) and
  records them in the plan and in a small `data_frequency` field carried on the
  strategy spec (not in the `Strategy` protocol method signature). Default `"1d"`
  when unspecified.
- **Why**: timeframe knowledge lives with the planner (it read the paper); the
  engine must not have to guess or pre-compute every frequency.
- **Acceptance**: a momentum hypothesis yields `data_frequency="1d"` in its plan;
  an intraday hypothesis yields whatever the planner infers; the field round-trips
  through `plan.json`.

### Task A.1 — On-demand, short-window tick resampling (engine-owned)
- **Action**: In `src/data/loader.py` add:
  - `load_ticks(year, month, day) -> pd.DataFrame` — reads one daily parquet from
    `Data/raw/Ticks/{YYYY}/{MM}/{DD}.parquet`, computes mid-price `(bid + ask) / 2`,
    returns `timestamp` (UTC) + `price` (+ vols).
  - `resample_ticks(ticks: pd.DataFrame, freq: str) -> pd.DataFrame` — vectorized
    **causal** OHLCV (`open=first`, `high=max`, `low=min`, `close=last`,
    `volume` = tick count, `closed='left'`, `label='left'`) via `pandas`/`polars`.
  - Engine (`run_experiment`): given the declared `data_frequency`, load ticks in
    **short chronological windows** (e.g. one month at a time), resample each
    window to the target frequency, append to the running bar history, and feed
    bars to the unchanged `generate_positions` (this folds naturally into the
    O(n) loop of A.2).
  - Keep `load_prices` (dummy CSV path) as-is so existing callers/tests are
    unchanged; tick loading is a parallel path.
- **Why**: raw ticks have no OHLCV, so tick→bar resampling is required to feed
  the engine. Doing it **on-demand in short windows** bounds memory (no
  ~340M-tick corpus held in RAM) and avoids materializing frequencies no one
  asked for. Resampling stays in the **engine** (not the strategy) so the causal
  no-lookahead guarantee remains structural.
- **Acceptance**: mid-price correctness on a hand-checked fixture; OHLC correct
  on a small known day; resampling is causal (a bar's `open` never uses a tick
  after the bar's start); a full day to 1m/1d is >100× faster than a per-tick
  loop; memory stays bounded across a multi-year range (never holds more than
  one short window + the running bar history).

### Task A.2 — Make the orchestration loop O(n)
- **Action**: In `run_experiment.py::_run_window`, replace the per-bar
  `prices[prices.index < ts]` re-slice with a **monotonic cursor**: window
  timestamps are sorted, so `k` only ever advances (`prices.index[k] < ts`).
  History becomes `prices.iloc[:k]` — no boolean mask, no copy, O(1) amortized.
  Replace `assert_no_lookahead(history, ts)` (a full-timeline scan) with a
  constant-time index assertion (`history.index[-1] < ts`), keeping the
  function as the documented guard API.
- **Why**: removes the dominant `O(n²)` term. Three nested `O(n)` layers become
  one `O(n)` orchestration pass plus the strategy's own cost.
- **Acceptance**: identical result to the current implementation on the dummy
  dataset (`test_determinism.py`, `test_orchestrator.py` still pass); a
  benchmark on 500k synthetic bars shows near-linear scaling.

### Task A.3 — Vectorize the equity-curve builder
- **Action**: Replace the Python loop in `src/execution/portfolio.py::build_equity_curve`
  with numpy: `delta = np.diff(prepend 0, positions)`; cost term
  `|delta| * (fee_bps + slippage_bps) / 10000`; per-bar return
  `positions * (close/open - 1) - cost`; `equity = np.cumprod(1 + net_return)`.
  Preserve the exact output columns (`equity`, `daily_return`, `position`,
  `transaction_cost`).
- **Why**: removes the second Python-level `O(n)` loop; 10–100× at intraday bar
  counts; keeps `simulate_fill` as the documented single-bar cell.
- **Acceptance**: `tests/test_execution.py` hand-computed fixtures still match
  to float tolerance; determinism holds.

### Task A.4 — Vectorized strategy fast-path ✅ DONE
- **Action**: Added an *optional* `VectorizedStrategy` protocol
  (`generate_positions_vectorized(prices) -> pd.Series`) computing the position
  series in one causal pass; the engine detects it and calls it once per window
  instead of per-bar. `assert_causal_vectorized` (leakage guard) perturbs a
  suffix and asserts earlier positions are unchanged — the active no-lookahead
  guarantee replacing the structural one the per-bar path relies on.
- **Result**: `buy_and_hold` reference strategy gained a vectorized method;
  equivalence + causality-probe tests added (`tests/test_vectorized.py`, 3 tests);
  benchmark shows per-bar is O(n²) while vectorized is O(n) (13,000× at 20k bars).

### Task A.5 — Streaming accumulator + parquet pushdown
- **Action**: `load_ticks` reads daily parquet via arrow row-group-level date
  pushdown (never a full `read_csv`); the engine's resampling pipeline appends
  each short window's bars to a running history and frees the raw tick window
  (the low-level mechanism A.1 relies on for bounded memory).
- **Why**: 2020–2026 tick data (~340M ticks) must never be held fully in RAM;
  only one short window + the running bar history are live at a time.
- **Acceptance**: processing a multi-year range never exceeds a bounded memory
  budget; a single-month slice of a multi-year corpus does not read the whole
  corpus.

### Task A.6 — Throughput benchmark + regression ✅ DONE
- **Action**: Added `experiments/benchmark_engine.py` reporting bars/sec for
  1k / 100k / 1M / 10M bars (`build_equity_curve`) plus strategy per-bar vs
  vectorized scaling and a real-tick end-to-end run; records a reproducibility
  block (seed, git hash, python/numpy/pandas versions, dataset hash). Run:
  `.venv/bin/python experiments/benchmark_engine.py`.
- **Result**: equity curve ~50–127M bars/s (flat across 10k→10M); strategy
  vectorized path O(n) vs per-bar O(n²); full tick run 1d/1yr ≈ 3.1s, 1h/6mo ≈
  2.3s.
- **Fix**: exposed and fixed a real bug — intraday `resample_ticks` split the
  18:30 trading-day boundary bin across two day-files, producing duplicate
  timestamps (and a shape mismatch in `build_equity_curve`); `load_bars` now
  merges duplicate boundary bars (open=first, close=last, high=max, low=min,
  volume=sum). Regression test added.

---

## 4. Workstream B — Multi-session orchestrator

> **Goal**: turn "hypothesis + atomic plan" into "hypothesis + atomic plan +
> a per-task prompt document", and drive the loop as *one fresh session per
> task* with file-backed context instead of one accumulating session.

### Task B.1 — Spec digest (decouple downstream from the raw paper) ✅ DONE
- **Action**: `run_planner_step` writes, alongside `plan.json`, a compact
  `spec_digest.md` (200–400 words: hypothesis intent, data/asset/time
  constraints, non-negotiable invariants). Planner still *reads* the full
  hypothesis once; worker/reviewer reference only the digest.
- **Why**: removes the #1 loop-cost multiplier — the full paper is no longer
  re-embedded into every step prompt.
- **Acceptance**: `candidate.json` (full) and `spec_digest.md` (compact) are
  both persisted; digest is self-contained (a fresh session reading only the
  digest + plan can proceed).
- **Result**: `src/loop/spec_digest.py` (`build_spec_digest`/`write_spec_digest`),
  deterministic from `ApprovedCandidate` + `TaskPlan`; written by the driver on
  fresh/replan/resume so a crash never leaves it missing.

### Task B.2 — Per-task prompt document generator ✅ DONE
- **Action**: New module `src/loop/prompt_doc.py` that, from a `TaskPlan` +
  hypothesis + spec digest + frozen engine contract, writes
  `candidates/{id}/prompts.md`:
  - a **COMMON CONTEXT** block (schema, locked decisions, project rules,
    anti-hallucination rules, reproducibility block, frozen protocol signature);
  - one **self-contained task prompt** per atomic task (`task_id`, description,
    acceptance, `target_files`).
- **Why**: this is the artifact that makes the reference's "one task per fresh
  session" work — each task is independently runnable with zero accumulated
  context.
- **Acceptance**: `prompts.md` is generated for a candidate; every task block
  is self-contained.
- **Result**: `prompt_doc.py` is now the single source of truth for *every* role's
  prompt (planner/worker/reviewer/statistician/auditor/committee), so prompt
  construction is centralised rather than spread across step modules.

### Task B.3 — Fresh-session-per-task driver (replace single-session threading) ✅ DONE
- **Action**: `src/loop/driver.py` stops threading one `session_id`. Each step
  invokes a **fresh session** with `prompts.md`'s COMMON CONTEXT + the specific
  task prompt + the current `train.py` snapshot (as a context file), and the
  required role. State (`current_task_index`, retries, replans) still persists
  via `state.json` exactly as today, so crash-resume is unchanged.
- **Why**: kills monotonic context growth (the session no longer accumulates);
  each call's context is bounded by the task + digest + file snapshot.
- **Acceptance**: `test_loop_branching.py`, `test_state_resume.py` pass with the
  new invocation path (mocked bridge asserts fresh-session arg lists, not a
  reused `session_id`).
- **Result**: planner/worker/reviewer invoke `opencode` with `session_id=None`;
  the planner's captured session id is provenance-only. `session_id` stays in the
  frozen `CandidateState` for backward compatibility but is never reused.

### Task B.4 — Role separation + staged agentic pipeline ✅ DONE
- **Action**: Add roles to `.opencode/agent/` to complete the committee:
  - **statistician** (read-only, reads raw result tables, questions methodology,
    never edits code);
  - **auditor** (read-only, bias/leakage/overfitting/assumption checks only);
  - **judge/committee** (aggregates the four audit axes into a PASS/VETO — the
    hard-veto on leakage, composite gate on the rest — per the closed architecture).
  Wire the pipeline as a **fully agentic stage sequence**: planner→plan,
  worker→build, reviewer→verify code, engine→execute, statistician→question,
  auditor→bias/leak/overfit, committee→PASS/VETO. No human stage.
- **Why**: this is the four-auditor committee + judge gate the architecture
  already names — but *agentic*, not human. isolation of roles keeps the review
  log honest (reviewer/auditor cannot edit; only worker writes).
- **Acceptance**: role files exist with `edit: deny` where read-only; the driver
  can invoke them in the staged order; the final PASS/VETO is produced by the
  committee agent with no human input.
- **Result**: three new agents (`statistician.md`, `auditor.md`, `judge.md`, all
  `edit: deny`) + step modules (`statistician_step.py`, `auditor_step.py`,
  `committee_step.py`) + additive schemas (`StatisticianNote`, `AuditorReport`,
  `CommitteeVerdict`). Wired into `run_full`: engine → statistician → auditor →
  committee → package. Committee `VETO` maps to `final_status="FAILED"` (reason
  `committee_veto: …`); `PASS` maps to `PASSED_TO_COMMITTEE` (frozen package
  schema unchanged).

### Task B.5 — Decision-status lifecycle + reproducibility + self-critique ✅ DONE
- **Action**: Adopt durable-record conventions:
  - decision statuses `Draft → Provisional → Locked → Deprecated` in the state/
    report trail (set by agents, `Locked`/`Deprecated` transitions logged);
  - a reproducibility metadata block (seed, git hash, versions, dataset hash,
    config) in every candidate package;
  - a five-question final self-critique appended to each agent prompt.
- **Why**: durable, audit-ready records the committee need; no human gate
  implies the record trail *is* the audit trail.
- **Acceptance**: `CandidatePackage`/trial log carries reproducibility metadata;
  producer prompts contain the self-critique block.
- **Result**: `src/loop/reproducibility.py` captures the block and `run_full`
  attaches it to the trial log (`reproducibility` key; additive, no schema
  migration) + writes `reproducibility.json`. `CommitteeVerdict.decision_status`
  carries the lifecycle; `SELF_CRITIQUE` + anti-hallucination rules are baked
  into every prompt via `prompt_doc._common_context`.

### Task B.6 — Multi-session validation (real run) ✅ DONE (automated) / live-run pending
- **Action**: re-run a known candidate end-to-end under the new driver and
  confirm: identical `train.py` produced, bounded per-step context, faster
  wall-clock, and successful crash-resume.
- **Acceptance**: `final_status="PASSED_TO_COMMITTEE"`, context tokens per step
  flat (not growing) across tasks, total time materially lower than the
  single-session baseline on a large hypothesis.
- **Result**: the *mechanism* is fully validated by `tests/test_multisession.py`
  (fresh-session arg lists asserted, flat per-step context by construction);
  `tests/test_end_to_end_smoke.py::test_full_staged_pipeline_real_opencode` now
  exercises the full staged pipeline against real OpenCode (marked `slow`; run
  `pytest -m slow` to confirm wall-clock, which requires a configured model).

---

## 5. Dependency graph

```
WORKSTREAM A (engine):
  A.0 (timeframe declaration) ──► A.1 (on-demand short-window resample)
        │                              │
        │                              └──► A.2 (O(n) loop) ──► A.3 (vectorize curve)
        │                                       │
        └────────────────► A.5 (streaming accumulator + pushdown) ◄─┘
        A.4 (vectorized strategy fast-path; gated, depends on A.2/A.3)
        A.6 (benchmark; depends on A.0–A.5)

WORKSTREAM B (orchestrator):
  B.1 (spec digest) ──► B.2 (prompts.md generator) ──► B.3 (fresh-session driver)
                                                                   │
  B.4 (roles + staged agentic pipeline) ──────────────────────────────┤
  B.5 (lifecycle + reproducibility + self-critique) ───────────────┘
  B.6 (multi-session validation; depends on B.1–B.5)

Sequencing rationale: A and B are independent and can proceed in parallel.
A.0–A.3 are the "priority 1" core (timeframe was planner decides, resample +
O(n) + vectorized); A.4 is gated on a frozen-contract decision.
B.1–B.3 solve the loop slowdown; B.4/B.5 extend quality, B.6 proves it.
```

---

## 6. Frozen vs. changed (explicit)

| artifact | status |
|----------|--------|
| `Strategy` protocol (`generate_positions`) | **frozen** (backward-compatible baseline) |
| `ExperimentSpec` schema | **frozen** |
| trial-log schema | **frozen** (reproducibility metadata *added*, not migrated) |
| `ApprovedCandidate` / `CandidatePackage` / `ReviewVerdict` / `TaskPlan` / `CandidateState` | **frozen** |
| engine orchestration loop internals (`_run_window`, `build_equity_curve`) | **changed** in A.2/A.3 — behavior-preserving (identical results) |
| `data/loader` | **extended** (A.1/A.5) — additive, existing callers unchanged |
| loop driver session model | **changed** in B.3 — single-session → fresh-session; state/resume unchanged |
| strategy spec / plan (`data_frequency` field) | **new** in A.0 — planner-declared timeframe; additive, not a change to the `Strategy` method signature |
| `VectorizedStrategy` (A.4) | **shipped** — additive protocol; per-bar `Strategy` unchanged |

---

## 7. Acceptance summary

- Engine processes **1M bars in seconds**, scales ~linearly; the planner-declared
  timeframe drives **on-demand short-window tick resampling** (bounded memory),
  and the frozen per-bar `Strategy` path still works unchanged.
- Orchestrator produces **hypothesis + atomic plan + `prompts.md`** and runs
  **one task per fresh session** with bounded context; crash-resume and the
  honest review log are preserved; a large-paper run completes materially
  faster than the current single-session loop. The pipeline is **fully agentic**
  (no human stage).
