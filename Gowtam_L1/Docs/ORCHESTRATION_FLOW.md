# L1 Orchestrator — End-to-End Flow

> **Purpose**: trace one hypothesis from the CLI trigger all the way to a
> packaged, committee-adjudicated candidate, covering every module in the
> orchestration path and what each one does.
>
> **Reading order for the pipeline**: `run.py` (entry) → `driver.py` (build
> loop) → step modules → OpenCode bridge → packaging/engine → committee roles.

---

## 1. The pipeline at a glance

```
CLI command
   │  python -m src.loop.run --hypothesis-file hypotheses/Test1.md
   ▼
src/loop/run.py            parse args → build ApprovedCandidate → run_full()
   │
   ├─ 1. driver.run_candidate()        BUILD LOOP  (planner → worker ⇄ reviewer)
   │      ├─ planner_step.run_planner_step()     → plan.json (+ spec_digest.md, prompts.md)
   │      └─ per task: worker_step.run_worker_step()  → train.py
   │                   reviewer_step.run_reviewer_step() → review_log.jsonl  (PROCEED/MINOR_FIX/REPLAN)
   │
   ├─ 2. verify_strategy_conformance()  structural Strategy check
   ├─ 3. run_engine_for_candidate()     ENGINE  → trial_log.json
   ├─ 4. run_statistician_step()        read raw results, question methodology → statistician_note.json
   ├─ 5. run_auditor_step()             four-axis audit → auditor_report.json
   ├─ 6. run_committee_step()           PASS/VETO → committee_verdict.json
   ├─ 7. _write_reproducibility()       reproducibility.json (+ inject into trial_log)
   └─ 8. assemble_package()             package.json  (frozen CandidatePackage)
```

Every agent-role invocation is a **fresh** `opencode run` call — no session
continuity — fed a self-contained prompt + file snapshot (see §4).

---

## 2. Entry point — `src/loop/run.py`

The CLI is `python -m src.loop.run`. `main()` builds an `argparse` parser
(`build_parser()`) and dispatches:

| flag | meaning |
|------|---------|
| `--hypothesis` / `--hypothesis-file` / `--stdin` | the hypothesis text (mutually exclusive) |
| `--name` | explicit candidate id; **reuse to resume** a crashed/paused run |
| `--assets --start --train-end --val-end --end` | data window (defaults point at the placeholder `asset_a` set) |
| `--paper-ref` | optional source-paper reference |
| `--skip-engine` | run the build loop only, stop before engine/committee |
| `--list` | summarize every past candidate |

Key functions:

- `_slug()` / `auto_candidate_id()` — a timestamped id
  (`{slug}_{YYYYmmdd_HHMMSS_fffff}`), so re-runs never clash.
- `_load_hypothesis()` — normalizes the three input sources into one string.
- `cmd_run()` — builds an `ApprovedCandidate` and calls `run_full()` (or
  `run_candidate()` only, under `--skip-engine`).
- `run_full()` — the whole staged pipeline (§3).
- `summarize()` / `cmd_list()` — reads `state.json` + `package.json` back for
  display.

### The input contract — `ApprovedCandidate`

```python
class ApprovedCandidate(BaseModel):
    candidate_id: str
    hypothesis: str
    source_paper_ref: Optional[str]
    asset_ids: list[str]
    start, train_end, val_end, end: str
    prior_context: list[str]
```

This is **frozen** (`src/schemas/approved_candidate.py`). It is the shape the
(not-yet-built) upstream debate/judge gate is expected to emit; today it is
built by hand in `cmd_run()`.

---

## 3. `run_full()` — the staged pipeline (`src/loop/run.py`)

`run_full(candidate) -> CandidatePackage` is the orchestrator's top-level
sequence. It is deliberately the *only* place the stage order is expressed.

```
run_full(candidate):
    state = run_candidate(candidate)                 # ① build loop (§4)

    if state.status == "FAILED": return assemble_package()   # build failed, no engine

    if not verify_strategy_conformance(cid):          # ② structural gate
        state = FAILED("strategy_non_conforming"); return assemble_package()

    run_engine_for_candidate(candidate)               # ③ engine (§6) — may raise EngineRunError
    run_statistician_step(candidate)                  # ④ read-only methodology review
    run_auditor_step(candidate)                       # ⑤ four-axis audit
    verdict = run_committee_step(candidate)           # ⑥ binding PASS/VETO

    _write_reproducibility(candidate)                 # ⑦ reproducibility block

    if verdict.verdict == "VETO":                     # ⑧ map verdict onto frozen final_status
        state = FAILED(f"committee_veto: {verdict.reasoning}")
    else:
        state = PACKAGED
    return assemble_package()                          # ⑨ write package.json
```

Notes:

- The **frozen** `CandidateState.status` / `CandidatePackage.final_status`
  literals are unchanged by Workstream B. The committee's `PASS` maps to
  `final_status="PASSED_TO_COMMITTEE"`; `VETO` maps to `final_status="FAILED"`
  (`reason = "committee_veto: …"`). The richer verdict lives in its own
  `committee_verdict.json`.
- **There is no human stage.** Every decision is made by an agent role; the
  committee's verdict is final.

---

## 4. Stage ① — the build loop (`src/loop/driver.py`)

`run_candidate(candidate) -> CandidateState` drives planner → (worker ⇄
reviewer), task by task.

### 4.1 State — `src/loop/state_store.py` + `src/schemas/candidate_state.py`

- `load_state(cid)` / `save_state(state)` read/write `candidates/{id}/state.json`
  **atomically** (write temp then `os.replace`), so a mid-write crash never
  corrupts it.
- `CandidateState` holds `status`, `current_task_index`, `retry_count`,
  `replan_count`, `session_id`, `failure_reason`. This is the crash-resume
  anchor: on re-entry, `run_candidate` reloads it and skips already-finished
  tasks.

### 4.2 Layout — `src/loop/paths.py`

Central filesystem helpers:

- `candidate_dir(cid)` → `candidates/{cid}/`
- `candidate_file(cid, name)` → `candidates/{cid}/{name}`
- `L1_READY_PATH` → `Docs/reports/L1_READY.md` (the frozen engine contract)
- `STRATEGY_FILE = "train.py"` — the **single-file ground rule** (the worker
  writes everything into this one file).

### 4.3 Driver loop

`run_candidate`:

1. Persists `candidate.json`.
2. `load_state(cid)` — if present, **resume** (reload plan, regenerate the
   digest/prompts docs); otherwise start fresh.
3. Fresh branch: `planner_step.run_planner_step(candidate)` returns
   `(plan, session_id)`. `session_id` is stored in state for **provenance only**
   — it is never reused. The plan is written to `plan.json`, and the two
   self-contained session anchors are written (§4.4).
4. Loop over `plan.tasks[state.current_task_index]`, alternating worker then
   reviewer, branching on the reviewer's verdict:
   - **PROCEED** → advance `current_task_index`.
   - **MINOR_FIX** → increment `retry_count`, re-run worker with
     `fix_instructions`.
   - **REPLAN** → increment `replan_count`, call planner again with the
     reviewer's reasoning, splice the new tasks in after the completed ones.
5. When `current_task_index >= len(tasks)` → `status = "ENGINE_RUN"` and return.

**Safety limits** (module constants, intentionally at the top):

| constant | value | effect |
|----------|-------|--------|
| `MAX_STEPS` | 50 | total worker+reviewer invocations |
| `MAX_RETRIES` | 3 | consecutive MINOR_FIX per task |
| `MAX_REPLANS` | 3 | replans per candidate |
| `WALL_CLOCK_TIMEOUT_SECONDS` | 7200 | hard ceiling → `wall_clock_timeout` |

Each breach ends the candidate with a **named** `failure_reason` — it never hangs.

### 4.4 Self-contained session anchors (`spec_digest.py`, `prompt_doc.py`)

The core Workstream-B idea: *nothing in a later session depends on what an
earlier session remembered.* Two durable files replace accumulated conversation:

- **`spec_digest.md`** (`spec_digest.py`) — a compact 200–400 word digest:
  hypothesis intent, asset/time constraints, non-negotiable invariants, and a
  one-line-per-task index. Derived deterministically from `ApprovedCandidate` +
  `TaskPlan`. The full paper is read **once** (by the planner); everyone else
  sees only this.
- **`prompts.md`** (`prompt_doc.py`) — the **COMMON CONTEXT** block (frozen
  contract ref, single-file rule, mid-price convention, anti-hallucination
  rules, reproducibility requirement, decision-status lifecycle, five-question
  **self-critique**) plus one self-contained task prompt per task.

`prompt_doc.py` is the *single source of truth for every role's prompt*:
`build_planner_prompt`, `build_worker_prompt`, `build_reviewer_prompt`,
`build_statistician_prompt`, `build_auditor_prompt`, `build_committee_prompt`.

The planner prompt also carries **granularity rules** (Workstream-B follow-up):
3–6 tasks, never a standalone boilerplate task, group related rules so each
task is reviewable in one pass.

---

## 5. The OpenCode bridge — `src/opencode_bridge/`

The bridge is the **only** place that shells out to OpenCode.

### 5.1 `invoke.py`

- `build_opencode_args(agent, prompt, session_id, context_files)` constructs:

  ```
  opencode run --auto --format json --agent <role> [--session <id>] <prompt> -f <file>...
  ```

  - `--auto` — never block on an interactive prompt.
  - `--format json` — NDJSON events, from which we capture the real `sessionID`
    and reassemble assistant text.
  - `--agent <role>` — selects the agent definition in `.opencode/agent/`.
  - **Prompt must precede `-f`** (array-typed flags greedily consume positionals).
  - Workstream B: `session_id` is always `None`, so every call is a **fresh**
    session.

- `invoke_opencode(...)` runs it via `subprocess.run`, then `_parse_events`
  reassembles the text and captures `sessionID`. Returns an `OpencodeResult`
  (`stdout`, `stderr`, `exit_code`, `agent`, `session_id`). Raises
  `OpencodeInvocationError` on a non-zero exit.
- `extract_result_block(raw)` pulls the single `<<<RESULT>>> … <<<END>>>` span
  that every role is prompted to emit; raises if zero or >1 present.

### 5.2 `parse.py` — schema-validated parse-and-retry

`parse_or_retry(raw, Schema, retry_fn, max_retries=2)`:

1. `extract_result_block` → `Schema.model_validate_json`.
2. On failure, calls `retry_fn(exc)` (re-invokes the agent with the parse error
   appended) up to `max_retries`, then raises `ParseRetryError`.

This is what makes agent output a *typed contract*, not free text.

### 5.3 Agent definitions — `.opencode/agent/*.md`

| role | file | permissions | purpose |
|------|------|-------------|---------|
| planner | `planner.md` | `edit: deny`, `bash: deny` | hypothesis → `TaskPlan` |
| worker | `worker.md` | `edit: allow`, `bash: allow` | the **only** role that writes code |
| reviewer | `reviewer.md` | `edit: deny`, `bash: allow` | acceptance + lookahead + drift → `ReviewVerdict` |
| statistician | `statistician.md` | `edit: deny`, `bash: allow` | methodology notes on raw results |
| auditor | `auditor.md` | `edit: deny`, `bash: allow` | four-axis bias/leak/overfit audit |
| judge | `judge.md` | `edit: deny`, `bash: allow` | aggregates → `PASS`/`VETO` |

The model is **not pinned** in any of these files — all roles resolve to the
same default model through the LiteLLM provider configured in
`~/.config/opencode/opencode.json` (see §9).

---

## 6. The step modules (`src/loop/*_step.py`)

Each step module follows the same pattern: build a prompt → `invoke_opencode`
(with role-specific context files) → `parse_or_retry` into a schema → write an
artifact.

### 6.1 `planner_step.py`

`run_planner_step(candidate, prior_reasoning=None) -> (TaskPlan, session_id)`:

- Prompt = `build_planner_prompt` (COMMON CONTEXT + full candidate + granularity
  rules; `prior_reasoning` appended on REPLAN).
- Context files: `L1_READY.md`, `candidate.json`.
- Parses into `TaskPlan` and **also writes `plan.json`** (so a crash between
  planning and the driver's persist step still leaves a valid plan).

### 6.2 `worker_step.py`

`run_worker_step(task, candidate_id, fix_instructions=None) -> str`:

- Prompt = `build_worker_prompt` (task id/description/acceptance/target_files +
  optional `fix_instructions`).
- Context files: `spec_digest.md` + current `train.py` (if present) — so the
  worker sees prior work with **no accumulated session context**.
- Worker's output is code on disk (not a schema). The returned summary is
  informational; `extract_result_block`, falling back to raw stdout.

### 6.3 `reviewer_step.py`

`run_reviewer_step(task, worker_summary, candidate, plan) -> ReviewVerdict`:

- Prompt = `build_reviewer_prompt` (COMMON CONTEXT + **compact** task + task
  index — **no** full paper).
- Parses into `ReviewVerdict`, appends it to `review_log.jsonl` (the honest,
  append-only audit trail).

### 6.4 `statistician_step.py`

`run_statistician_step(candidate) -> StatisticianNote`: read-only review of
`trial_log.json`; writes `statistician_note.json`.

### 6.5 `auditor_step.py`

`run_auditor_step(candidate) -> AuditorReport`: four-axis
(`lookahead`/`bias`/`overfitting`/`assumptions`) findings, each
`pass`/`flag`/`veto`; writes `auditor_report.json`. `leak_veto` is a computed
property (any `lookahead: veto`).

### 6.6 `committee_step.py`

`run_committee_step(candidate) -> CommitteeVerdict`: aggregates
`auditor_report.json` + `statistician_note.json` + `trial_log.json` into
`PASS`/`VETO`; writes `committee_verdict.json`.

---

## 7. Conformance + engine (`src/packaging/package_candidate.py`)

### 7.1 `verify_strategy_conformance(cid)`

Imports `train.py` via `load_strategy` and checks `isinstance(strategy, Strategy)`.
Independent of what the reviewer *claimed* — the engine never runs against a
non-conforming module.

`src/strategy/loader.py::load_strategy(path)` imports the module and scans for
exactly one class structurally conforming to `Strategy` (raises
`NoStrategyFoundError` otherwise).

`src/strategy/protocol.py` defines:

- `Strategy` (frozen): `generate_positions(price_history) -> pd.Series`.
- `VectorizedStrategy` (additive fast-path):
  `generate_positions_vectorized(prices) -> pd.Series`, with the contract that
  `positions[t]` depends only on bars `< t` (implemented via
  `signal.shift(1).fillna(0.0)`).

### 7.2 `run_engine_for_candidate(candidate)`

Builds an `ExperimentSpec` from the candidate (asset ids + date windows +
the `train.py` module path), calls `run_experiment`, and writes
`trial_log.json` via `write_trial_log`. Engine exceptions are wrapped in
`EngineRunError("engine_error: …")` so an engine failure is recorded distinctly
from a build failure.

---

## 8. The engine — `src/orchestrator/run_experiment.py`

`run_experiment(spec) -> dict`:

1. `spec.validate()` — `ExperimentSpec` (`src/config/experiment_spec.py`) checks
   non-empty fields and `start < train_end < val_end < end`.
2. `load_strategy(spec.strategy_module_path)`.
3. `_load_prices(spec)` —
   - `data_source == "ticks"` → `load_bars()` (`src/data/ticks.py`), which
     streams daily parquet ticks → resamples to `data_frequency` (causal,
     `closed="right"`), merging boundary-split bins.
   - else → `load_prices_pit()` (`src/data/loader.py`) for the placeholder CSV,
     enforcing the point-in-time (`as_of`) no-lookahead check.
4. If the strategy is a `VectorizedStrategy`, run
   `assert_causal_vectorized` (`src/execution/leakage_guard.py`) once — it
   perturbs a suffix and asserts earlier positions are unchanged (the *active*
   no-lookahead guarantee, replacing the structural one the per-bar path has).
5. `split_periods(spec)` (`src/splitting/windows.py`) → `train`, `validation`,
   `holdout` ranges (holdout always exists, by construction).
6. `_run_window(strategy, prices, range, spec)` for validation and holdout:
   - **Vectorized** strategies: one causal whole-series pass.
   - **Per-bar** strategy: a monotonic-cursor loop (O(n) orchestration) feeding
     `generate_positions(history)` under `assert_no_lookahead`.
   - `build_equity_curve(position_series, prices, fee_bps, slippage_bps)`
     (`src/execution/portfolio.py`) — vectorized numpy equity accumulation.
7. `_compute_metrics(curve, positions)` assembles the metric bundle:
   `cumulative_return`, `sharpe_ratio`, `annualized_volatility`, `max_drawdown`,
   `cvar`, `stability_score`, `per_window_sharpe`, `turnover`,
   `pnl_concentration` (all in `src/metrics/`).
8. `decay_check(val_metrics, hold_metrics)` (`src/metrics/decay.py`) →
   `sharpe_decay_ratio`, `cr_decay_ratio`, `decay_flag`.
9. `composite_score({**val_metrics, **decay})` (`src/metrics/composite.py`) →
   `sharpe − max_drawdown + stability − decay`.

The result dict:
```
{ validation_metrics, holdout_metrics, decay, composite_score }
```

---

## 9. Reproducibility + packaging

### 9.1 `src/loop/reproducibility.py` + `_write_reproducibility`

`capture_reproducibility(candidate)` computes at run-time: `random_seed`,
`git_commit` (`git rev-parse HEAD`), `execution_time`, `python_version`,
`key_dependencies` (numpy/pandas versions), `dataset_hash` (MD5 of the first
tick parquet), and `config` (the candidate window). `_write_reproducibility`
writes `reproducibility.json` and **attaches it** to `trial_log.json` (additive
`reproducibility` key — no schema migration).

### 9.2 `assemble_package(cid) -> CandidatePackage`

Reads `candidate.json` + `plan.json` + `review_log.jsonl` + `trial_log.json`,
and derives `final_status` from `state.status` (failed → `FAILED` with the
named reason; otherwise `PASSED_TO_COMMITTEE`). Writes `package.json`. This is
the frozen output contract the KB-2 / debate gate consume.

---

## 10. Per-candidate artifact map

| file | written by | purpose |
|------|-----------|---------|
| `candidate.json` | `driver` (persist) | frozen input contract |
| `plan.json` | `planner_step` | atomic `TaskPlan` |
| `spec_digest.md` | `spec_digest` (via driver) | compact self-contained summary |
| `prompts.md` | `prompt_doc` (via driver) | COMMON CONTEXT + per-task prompts |
| `state.json` | `state_store` | atomic resumable loop state |
| `train.py` | worker (via OpenCode) | the single built strategy file |
| `review_log.jsonl` | `reviewer_step` | append-only `ReviewVerdict` trail |
| `trial_log.json` | `package_candidate` | engine result (serializable) |
| `statistician_note.json` | `statistician_step` | methodology notes |
| `auditor_report.json` | `auditor_step` | four-axis findings |
| `committee_verdict.json` | `committee_step` | binding PASS/VETO |
| `reproducibility.json` | `run._write_reproducibility` | seed/git/versions/hash |
| `package.json` | `package_candidate` | frozen `CandidatePackage` |

---

## 11. End-to-end trace (numbered)

1. `python -m src.loop.run --hypothesis-file hypotheses/Test1.md`
2. `run.py::main` → `build_parser` → `cmd_run`; hypothesis loaded from file.
3. `auto_candidate_id()` (or `--name`) mints the id; `ApprovedCandidate` built.
4. `run_full(candidate)`.
5. `driver.run_candidate`: writes `candidate.json`, `load_state` → none → fresh.
6. `planner_step.run_planner_step`: `openocade run … --agent planner` (fresh)
   with `candidate.json` + `L1_READY.md` → `TaskPlan` → `plan.json`.
7. `driver` writes `spec_digest.md` + `prompts.md`.
8. For task t0: `worker_step` → `openocade run … --agent worker` (fresh) →
   edits `train.py`.
9. `reviewer_step` → `openocade run … --agent reviewer` (fresh) → `ReviewVerdict`
   → `review_log.jsonl`. PROCEED → advance; else MINOR_FIX/REPLAN.
10. Repeat 8–9 for every task (respecting safety limits) → `status=ENGINE_RUN`.
11. `verify_strategy_conformance` imports `train.py`, confirms `Strategy`.
12. `run_engine_for_candidate` → `run_experiment`: load bars (ticks or CSV),
    causality check, split, simulate (vectorized or O(n) per-bar), compute
    metrics + decay + composite → `trial_log.json`.
13. `statistician_step` → `statistician_note.json`.
14. `auditor_step` → `auditor_report.json`.
15. `committee_step` → `committee_verdict.json` (PASS/VETO).
16. `_write_reproducibility` → `reproducibility.json` + borrowed into trial log.
17. VETO → `state=FAILED`; PASS → `state=PACKAGED`.
18. `assemble_package` → `package.json`.
19. `summarize()` prints: status / final / composite / val_sharpe / holdout / decay.

---

## 12. Failure modes & recovery

| failure | raised by | recorded as | recovery |
|---------|-----------|-------------|----------|
| non-zero `opencode` exit | `invoke_opencode` | `OpencodeInvocationError` | crash; re-run with `--name` resumes |
| >1 / zero result blocks | `extract_result_block` | `ResultExtractionError` → retry | parse retry |
| unparseable JSON | `parse_or_retry` | `ParseRetryError` after `max_retries` | hard failure |
| missing/dup Strategy | `strategy.load_strategy` | `NoStrategyFoundError` | conformance gate → `FAILED` |
| engine exception | `run_experiment` | `EngineRunError("engine_error: …")` | `FAILED` |
| lookahead | `leakage_guard` | `LookaheadViolation` | engine raises; `FAILED` |
| step/retry/replan/time | `driver` limits | named `failure_reason` | resume via `--name` |
| committee VETO | `committee_step` | `FAILED (committee_veto: …)` | recorded, not resumable (final) |

---

## 13. Where a model can be assigned

No `.opencode/agent/*.md` pins a `model:`. The routing is via the LiteLLM
provider in `~/.config/opencode/opencode.json` (`baseURL`, `apiKey` from env),
with the default models set through the `ANTHROPIC_DEFAULT_*_MODEL`
environment variables (e.g. `ANTHROPIC_DEFAULT_OPUS_MODEL`). To differentiate
(e.g. a fast model on planner/worker, a strong verifier on reviewer/auditor/
judge), add a per-role `model:` line to the relevant agent frontmatter — no
code change required.

.venv/bin/python -m src.loop.run --hypothesis-file hypotheses/Test1.md