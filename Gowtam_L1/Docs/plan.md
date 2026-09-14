# L1 Agentic Loop — Atomic Implementation Plan

> **Document Type**: Implementation Plan (Atomic)
> **Scope**: L1 proper — the planner → worker → reviewer cycle that takes an
> approved hypothesis and turns it into a finished, engine-evaluated
> candidate. This depends on the previously built execution engine
> (formerly and confusingly also called "L1" — from here on referred to as
> **the engine**, since it's a dependency L1 calls, not L1 itself).
> **Harness**: OpenCode. Agent roles are configured as OpenCode agent
> definitions with per-role models/tools/permissions; the loop itself is a
> Python driver that invokes `opencode run` non-interactively per step.
> **Verified before writing this plan**: OpenCode supports `opencode run`
> as a genuine headless, scriptable command (`opencode run --agent <name>
> "<prompt>"`), with `--session`/`-s` for context continuity across calls
> and `--file`/`-f` for attaching context files. Agent roles (planner,
> worker, reviewer, etc.) are commonly defined as separate config
> entries/files with distinct models and tool permissions per role.
> **Not verified — confirm before Phase 3**: the exact CLI flag surface
> differs across OpenCode forks/versions (community configs show slightly
> different conventions for agent-definition file locations and some flags).
> Task 3.1 is written against the flags found in current documentation, but
> should be smoke-tested against your team's actual installed version
> before anything depends on it.
> **Upstream contract this plan is built against**: `Docs/reports/L1_READY.md`
> from the engine build — specifically the frozen `Strategy` protocol,
> `ExperimentSpec` schema, and trial log schema. Nothing here may propose a
> change to any of those; if a task seems to need one, that's a signal the
> task is scoped wrong.
> **Downstream contract this plan produces**: `CandidatePackage` — the
> schema the (not-yet-built) debate gate and committee will consume. Defined
> in Phase 0 and frozen at the end of Phase 0, for the same reason the
> engine's contracts were frozen early.

---

## What L1 actually does, restated precisely

Input: an `ApprovedCandidate` — a hypothesis that has already cleared
whatever comes before it (currently nothing does this yet, so L1 is built
and tested against a hand-written stub of this object; see Task 0.2).

Output: a `CandidatePackage` — the finished strategy code, the plan that
produced it, the full review history, the engine's trial log, or — if the
cycle failed — an equally complete failure record. Nothing is discarded.

In between: Planner writes an atomic task list. Worker implements tasks one
at a time. Reviewer checks each completed task and can say *proceed*, *fix
this one task and retry*, or *the plan itself is wrong past this point,
replan*. Once every task passes review, the driver calls the engine
directly (a plain function call, not an OpenCode agent) and packages the
result.

---

## Anti-Wavering / Anchoring, adapted for a per-candidate loop

The earlier AGENTS.md/PROGRESS.md pattern was repo-wide, for a human-paced
multi-session build. This loop runs unattended, potentially many times, so
the anchoring mechanism has to be per-candidate and machine-readable, not
just human-readable:

- **`candidates/{id}/state.json`** — the single source of truth for where
  a given candidate's cycle currently stands (task index, retry counts,
  status). Every step reads it before acting and writes it after. This is
  what makes the loop resumable after a crash (Task 6.4) instead of
  restarting from scratch.
- **One OpenCode session per candidate**, not per step — so the worker and
  reviewer calls within a single candidate's build share context (what's
  already been read, what's already been tried) without the driver having
  to re-explain the whole plan on every invocation.

---

## Directory Structure

```
l1-agentic-loop/
├── opencode.json                       # project-level OpenCode config
├── .opencode/
│   └── agent/
│       ├── planner.md                  # role, model, tools, permissions
│       ├── worker.md
│       └── reviewer.md
│
├── engine/                             # the previously built execution engine (imported, not modified)
│
├── candidates/
│   └── {candidate_id}/
│       ├── candidate.json              # ApprovedCandidate (input)
│       ├── plan.json                   # Planner's structured task list
│       ├── state.json                  # resumable loop state
│       ├── review_log.jsonl            # one line per review verdict, append-only
│       ├── strategy/                   # the strategy module being built
│       ├── trial_log.json              # engine.run_experiment output
│       └── package.json                # CandidatePackage (output)
│
├── src/
│   ├── __init__.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── approved_candidate.py
│   │   ├── task_plan.py
│   │   ├── review_verdict.py
│   │   ├── candidate_state.py
│   │   └── candidate_package.py
│   ├── opencode_bridge/
│   │   ├── __init__.py
│   │   └── invoke.py                   # subprocess wrapper around `opencode run`
│   ├── loop/
│   │   ├── __init__.py
│   │   ├── planner_step.py
│   │   ├── worker_step.py
│   │   ├── reviewer_step.py
│   │   ├── driver.py                   # the main loop
│   │   └── state_store.py              # read/write state.json
│   └── packaging/
│       ├── __init__.py
│       └── package_candidate.py
│
├── tests/
│   ├── test_schemas.py
│   ├── test_opencode_bridge.py         # mocked subprocess calls
│   ├── test_loop_branching.py          # PROCEED / MINOR_FIX / REPLAN, mocked
│   ├── test_state_resume.py
│   └── test_end_to_end_smoke.py        # real opencode invocation — slow, manual
│
└── pyproject.toml
```

---

## Phase 0 — Foundation and Frozen Contracts

> **Goal**: Define every schema this loop passes between components before
> writing any orchestration logic. Every one of these gets frozen at the
> end of this phase.

### Task 0.1 — Project Scaffolding
- **Action**: Create the directory structure above. `pyproject.toml` with
  `pydantic` (for schema validation), `pytest`. Add the engine (from the
  prior build) as a local dependency/import path.
- **Input**: None
- **Output**: Empty tree + `pyproject.toml`
- **Acceptance**: `python -c "import src; import engine"` succeeds.

### Task 0.2 — ApprovedCandidate Schema (Input Contract, Stubbed)
- **Action**: In `src/schemas/approved_candidate.py`, define
  `ApprovedCandidate`: `candidate_id: str`, `hypothesis: str`,
  `source_paper_ref: str | None`, `asset_ids: list[str]`, `start: str`,
  `train_end: str`, `val_end: str`, `end: str`, `prior_context: list[str]`
  (placeholder for future KB-2 hits — empty list until that layer exists).
  Since nothing upstream produces this yet, also write
  `fixtures/sample_candidate.json` by hand — a single realistic example
  (e.g., "implement 20-day momentum on the given asset") used by every test
  in this plan.
- **Input**: None
- **Output**: `approved_candidate.py` + `fixtures/sample_candidate.json`
- **Acceptance**: The fixture parses into a valid `ApprovedCandidate`
  instance without errors.

### Task 0.3 — Task Plan Schema (Planner's Output Contract)
- **Action**: In `src/schemas/task_plan.py`, define `Task` (`task_id: str`,
  `description: str`, `target_files: list[str]`, `acceptance: str`) and
  `TaskPlan` (`candidate_id: str`, `tasks: list[Task]`). This is the
  machine-readable equivalent of the atomic-plan documents from the engine
  build — same spirit (small, unambiguous, one acceptance criterion per
  task), different medium (JSON the driver can loop over, not prose a human
  reads).
- **Input**: None
- **Output**: `task_plan.py`
- **Acceptance**: A hand-written example `TaskPlan` with 3 tasks validates;
  a malformed one (missing `acceptance` on one task) fails validation with
  a specific, task-identifying error.

### Task 0.4 — Review Verdict Schema (Reviewer's Output Contract)
- **Action**: In `src/schemas/review_verdict.py`, define `ReviewVerdict`:
  `task_id: str`, `verdict: Literal["PROCEED", "MINOR_FIX", "REPLAN"]`,
  `reasoning: str`, `fix_instructions: str | None` (required if
  `MINOR_FIX`), `replan_reasoning: str | None` (required if `REPLAN`).
- **Input**: None
- **Output**: `review_verdict.py`
- **Acceptance**: `MINOR_FIX` without `fix_instructions` fails validation;
  `PROCEED` with either optional field set still validates (fields are
  simply ignored, not an error).

### Task 0.5 — Candidate State Schema (Resumability Contract)
- **Action**: In `src/schemas/candidate_state.py`, define `CandidateState`:
  `candidate_id: str`, `status: Literal["PLANNING", "BUILDING",
  "REVIEWING", "REPLANNING", "ENGINE_RUN", "PACKAGED", "FAILED"]`,
  `current_task_index: int`, `retry_count: int`, `replan_count: int`,
  `session_id: str | None`, `failure_reason: str | None`.
- **Input**: None
- **Output**: `candidate_state.py`
- **Acceptance**: Round-trips through JSON serialization without loss.

### Task 0.6 — CandidatePackage Schema (Output Contract)
- **Action**: In `src/schemas/candidate_package.py`, define
  `CandidatePackage`: `candidate: ApprovedCandidate`, `plan: TaskPlan`,
  `review_log: list[ReviewVerdict]`, `trial_log: dict | None` (null on
  failure), `final_status: Literal["PASSED_TO_COMMITTEE", "FAILED"]`,
  `failure_reason: str | None`. This is what the future debate/committee
  stages will be built against — same reasoning as freezing the trial log
  schema during the engine build.
- **Input**: `ApprovedCandidate`, `TaskPlan`, `ReviewVerdict` from prior
  tasks
- **Output**: `candidate_package.py`
- **Acceptance**: A hand-constructed example with `final_status="FAILED"`
  and `trial_log=None` validates — the schema must not require a trial log
  to exist, since failed candidates never reach the engine.
- **Decision Gate**: `ApprovedCandidate` and `CandidatePackage` are now
  frozen. Nothing built after this point may change their shape without
  revisiting this entire plan.

---

## Phase 1 — OpenCode Agent Definitions

> **Goal**: Configure the three roles as OpenCode agents, with permissions
> that make the review process structurally honest — specifically, the
> reviewer must not be able to edit code itself.

### Task 1.1 — Project Config and Permission Design
- **Action**: Create `opencode.json` at the project root. Decide and
  document the permission profile per role before writing the agent files:
  **Planner** — read-only (reads `L1_READY.md`, `candidate.json`, repo
  state), no edit/write/bash. **Worker** — edit/write/bash allowed, scoped
  to `candidates/{id}/strategy/` only. **Reviewer** — read-only plus
  bash restricted to running tests/checks (e.g., `pytest`, a static
  leakage-pattern grep) — explicitly no edit or write. The reviewer-cannot-
  edit rule is deliberate: every fix must go back through the worker, which
  is what keeps `review_log.jsonl` an honest record of what actually
  changed and why, rather than the reviewer silently patching things itself.
- **Input**: None
- **Output**: `opencode.json` with the three permission profiles declared
- **Acceptance**: Config loads without error (`opencode agent` lists all
  three roles, per whatever the installed version's inspection command is —
  confirm the exact command against your version).

### Task 1.2 — Planner Agent Definition
- **Action**: `.opencode/agent/planner.md` — system prompt: "You are given
  an approved research candidate and the frozen engine contract
  (`L1_READY.md`). Produce an atomic task plan for implementing this
  candidate as a `Strategy`-conforming module. Each task must be small
  enough to implement without ambiguity. Your final response must contain
  exactly one JSON block matching the `TaskPlan` schema, wrapped in
  `<<<RESULT>>> ... <<<END>>>`, and nothing else outside that block should
  be treated as the answer." Attach `L1_READY.md` and the schema definition
  as context files.
- **Input**: `L1_READY.md` (from engine build), `task_plan.py`'s schema
- **Output**: `planner.md`
- **Acceptance**: A manual test invocation (via TUI or a single `opencode
  run` call) against the Task 0.2 fixture produces a response containing a
  well-formed `<<<RESULT>>>` block.

### Task 1.3 — Worker Agent Definition
- **Action**: `.opencode/agent/worker.md` — system prompt: "You are given
  exactly one task from an approved plan. Implement only that task, within
  `candidates/{id}/strategy/`. Do not implement other tasks from the plan,
  even if they seem related. When done, report a short structured
  completion summary (files changed, one-line description) wrapped in
  `<<<RESULT>>> ... <<<END>>>`."
- **Input**: None (task content is passed per-invocation, not baked into
  the agent file)
- **Output**: `worker.md`
- **Acceptance**: Manual test: given a single trivial task ("create an
  empty `strategy.py` file with a docstring"), produces exactly that file
  and nothing else.

### Task 1.4 — Reviewer Agent Definition
- **Action**: `.opencode/agent/reviewer.md` — system prompt: "You are given
  a task, the worker's change, the original hypothesis, and the full plan.
  Before producing a verdict, explicitly check: (1) does the change satisfy
  the task's stated acceptance criterion, (2) does it introduce any
  lookahead — specifically, does it read `price_history` beyond the current
  bar or use any future-dated file — (3) does the implementation still
  plausibly serve the stated hypothesis, or has it drifted. Your final
  response must contain exactly one JSON block matching the
  `ReviewVerdict` schema, wrapped in `<<<RESULT>>> ... <<<END>>>`."
- **Input**: `review_verdict.py`'s schema
- **Output**: `reviewer.md`
- **Acceptance**: Manual test against a deliberately broken worker output
  (e.g., a strategy file that indexes `price_history[-1]` in a way that
  could reach a future row) produces a verdict flagging the leakage
  concern, not a silent `PROCEED`.

---

## Phase 2 — Structured Output Handling

> **Goal**: Make agent responses reliably machine-parseable, since
> `opencode run`'s raw output is a conversational transcript, not clean
> JSON.

### Task 2.1 — Delimited Result Extraction
- **Action**: In `src/opencode_bridge/invoke.py` (started here, finished in
  Phase 3), implement `extract_result_block(raw_output: str) -> str` —
  finds the last `<<<RESULT>>> ... <<<END>>>` span in the transcript and
  returns its contents. Raises a named error if zero or more than one such
  block is found.
- **Input**: None
- **Output**: `extract_result_block` function + unit test with fixture
  transcripts (clean, missing block, duplicated block)
- **Acceptance**: All three fixture cases behave as specified.

### Task 2.2 — Schema-Validated Parse-and-Retry Wrapper
- **Action**: In `src/loop/planner_step.py` and `reviewer_step.py`
  (interface only — full implementation in Phase 4), define a shared
  helper `parse_or_retry(raw_output, schema, retry_fn, max_retries=2)` —
  attempts `schema.parse_raw(extract_result_block(raw_output))`; on
  failure, calls `retry_fn` with the specific validation error appended to
  the original prompt, up to `max_retries` times, before raising a hard
  failure.
- **Input**: `extract_result_block` from Task 2.1, schemas from Phase 0
- **Output**: Shared parsing helper + tests using mocked `retry_fn`
- **Acceptance**: A malformed-then-valid two-call sequence succeeds on
  retry; an always-malformed sequence raises after `max_retries` exactly.

---

## Phase 3 — OpenCode Bridge

> **Goal**: One well-tested function that turns "invoke this role with this
> prompt" into a captured, parseable result — the only place in the whole
> system that shells out to OpenCode.

### Task 3.1 — Low-Level Invocation Wrapper
- **Action**: In `src/opencode_bridge/invoke.py`, implement
  `invoke_opencode(agent: str, prompt: str, session_id: str | None, context_files: list[str], cwd: str) -> OpencodeResult`
  wrapping a subprocess call shaped like:
  `opencode run --agent {agent} [--session {session_id}] [-f {file}]... "{prompt}"`
  run with `cwd` set to the candidate's directory, capturing stdout,
  stderr, and exit code into `OpencodeResult`. **Flag explicitly in the
  docstring**: exact flag names/behavior must be confirmed against the
  team's installed OpenCode version before this is trusted — treat this
  implementation as a draft to be smoke-tested against Task 1.2–1.4's
  manual invocations first.
- **Input**: None
- **Output**: `invoke_opencode` + `tests/test_opencode_bridge.py` (subprocess
  call mocked — this test validates argument construction, not that
  OpenCode itself behaves correctly)
- **Acceptance**: Given a set of inputs, the mocked subprocess call
  receives exactly the expected argument list.

### Task 3.2 — Session Continuity Manager
- **Action**: In `src/loop/driver.py` (started here), implement
  `create_candidate_session() -> str` (invoked once per candidate at loop
  start) and ensure every subsequent `invoke_opencode` call for that
  candidate passes the same `session_id`.
- **Input**: `invoke_opencode` from Task 3.1
- **Output**: Session creation logic + a test verifying the same
  `session_id` is threaded through a mocked planner→worker→reviewer
  sequence
- **Acceptance**: Test passes.

### Task 3.3 — Bridge Error Handling
- **Action**: Extend `invoke_opencode` to raise a named
  `OpencodeInvocationError` on non-zero exit code, capturing stderr in the
  exception — distinct from a schema-parse failure (Task 2.2), since a
  process crash and a malformed-but-present response need different
  handling upstream.
- **Input**: `invoke_opencode`
- **Output**: Updated `invoke.py` + test with a mocked non-zero exit
- **Acceptance**: Non-zero exit raises `OpencodeInvocationError`
  specifically, not a generic exception.

---

## Phase 4 — The Loop

> **Goal**: Wire Phases 0–3 into the actual per-candidate cycle.

### Task 4.1 — Planner Step
- **Action**: In `src/loop/planner_step.py`, implement
  `run_planner_step(candidate: ApprovedCandidate, session_id: str, prior_reasoning: str | None = None) -> TaskPlan` —
  builds the prompt (candidate details + `L1_READY.md` path as a context
  file + `prior_reasoning` appended if this is a replan, not a fresh plan),
  calls `invoke_opencode("planner", ...)`, parses via `parse_or_retry`
  against `TaskPlan`, writes `plan.json`.
- **Input**: Phases 0–3
- **Output**: `planner_step.py` + test with mocked bridge returning a
  canned valid `<<<RESULT>>>` block
- **Acceptance**: Returns a valid `TaskPlan`; `plan.json` is written.

### Task 4.2 — Worker Step
- **Action**: In `src/loop/worker_step.py`, implement
  `run_worker_step(task: Task, candidate_id: str, session_id: str, fix_instructions: str | None = None) -> str` —
  builds the prompt from the task (plus `fix_instructions` if this is a
  retry after `MINOR_FIX`), calls `invoke_opencode("worker", ...)`, returns
  the raw completion summary text (not schema-validated — the worker's
  output is code changes on disk, not a structured object; the summary is
  informational for the reviewer's prompt).
- **Input**: Phase 3
- **Output**: `worker_step.py` + mocked test
- **Acceptance**: Function returns the extracted summary text; a
  `fix_instructions` argument is confirmed present in the constructed
  prompt when provided.

### Task 4.3 — Reviewer Step
- **Action**: In `src/loop/reviewer_step.py`, implement
  `run_reviewer_step(task: Task, worker_summary: str, candidate: ApprovedCandidate, plan: TaskPlan, session_id: str) -> ReviewVerdict` —
  builds the prompt, calls `invoke_opencode("reviewer", ...)`, parses via
  `parse_or_retry` against `ReviewVerdict`, appends the verdict to
  `review_log.jsonl`.
- **Input**: Phases 0–3
- **Output**: `reviewer_step.py` + mocked test
- **Acceptance**: Returns a valid `ReviewVerdict`; `review_log.jsonl` gains
  exactly one new line per call.

### Task 4.4 — State Store
- **Action**: In `src/loop/state_store.py`, implement `load_state(candidate_id) -> CandidateState`
  and `save_state(state: CandidateState)` — reads/writes `state.json`
  atomically (write to a temp file, then rename, so a crash mid-write never
  leaves a corrupt state file).
- **Input**: `CandidateState` schema from Task 0.5
- **Output**: `state_store.py` + test including a simulated interrupted
  write (verify the old state.json is intact if the new write is
  interrupted)
- **Acceptance**: Atomic write test passes.

### Task 4.5 — Main Driver Loop
- **Action**: In `src/loop/driver.py`, implement
  `run_candidate(candidate: ApprovedCandidate) -> CandidateState`:
  1. If `state.json` exists for this candidate, resume from it (load
     existing `session_id`, `current_task_index`, etc.); otherwise
     initialize fresh state and call `run_planner_step`.
  2. Loop: `task = plan.tasks[state.current_task_index]`
  3. `run_worker_step(task, ..., fix_instructions=<from last MINOR_FIX verdict if any>)`
  4. `verdict = run_reviewer_step(task, ...)`
  5. Branch on `verdict.verdict`:
     - `PROCEED` → `current_task_index += 1`, `retry_count = 0`; if no
       tasks remain, status → `ENGINE_RUN` (Phase 5) and exit loop
     - `MINOR_FIX` → `retry_count += 1`; if `retry_count > MAX_RETRIES`,
       status → `FAILED`, `failure_reason` set, exit loop; else loop back
       to step 3 on the same task
     - `REPLAN` → `replan_count += 1`; if `replan_count > MAX_REPLANS`,
       status → `FAILED`, exit loop; else call `run_planner_step` again
       with `prior_reasoning=verdict.replan_reasoning`, splice the new
       plan's tasks in starting at the current index, reset `retry_count`
  6. `save_state(state)` after every single step, unconditionally
- **Input**: Tasks 4.1–4.4
- **Output**: `driver.py`
- **Acceptance**: A fully mocked run (canned `PROCEED` responses for a
  3-task plan) completes and returns `status="ENGINE_RUN"`.

### Task 4.6 — Hard Safety Limits
- **Action**: Add `MAX_STEPS` (total worker+reviewer invocations across the
  whole candidate, not just per-task), `MAX_RETRIES` (per task),
  `MAX_REPLANS` (per candidate), and a wall-clock timeout to `driver.py`'s
  loop, all as named constants at the top of the module, not buried in
  logic. If `MAX_STEPS` is hit before the loop reaches `ENGINE_RUN` or
  `FAILED` through normal branching, force `status="FAILED"`,
  `failure_reason="max_steps_exceeded"`.
- **Input**: `driver.py` from Task 4.5
- **Output**: Updated `driver.py` + test that an infinite-`MINOR_FIX`-loop
  fixture terminates at `MAX_RETRIES`, not indefinitely
- **Acceptance**: Test passes — this is the single most important test in
  this phase, since an unattended loop that can hang is worse than one that
  fails fast and loudly.

---

## Phase 5 — Engine Handoff and Packaging

> **Goal**: Once the loop produces a finished module, verify it
> independently, run it through the engine, and package the result.

### Task 5.1 — Independent Protocol Conformance Check
- **Action**: In `src/packaging/package_candidate.py`, before touching the
  engine, implement `verify_strategy_conformance(candidate_id: str) -> bool` —
  imports the finished module from `candidates/{id}/strategy/` and checks it
  against the engine's `Strategy` protocol directly (structural
  `isinstance`/protocol check), independent of whatever the Reviewer agent
  already claimed. This exists because the Reviewer's judgment is a
  language-model opinion, not a guarantee — the engine should never be
  invoked against something that doesn't actually satisfy its contract,
  regardless of what the review log says.
- **Input**: `Strategy` protocol from the engine build
- **Output**: `verify_strategy_conformance` + test with both a conforming
  and a non-conforming fixture module
- **Acceptance**: Both cases return the correct boolean; the non-conforming
  case does not raise an unhandled exception.

### Task 5.2 — Engine Invocation
- **Action**: In `package_candidate.py`, implement
  `run_engine_for_candidate(candidate: ApprovedCandidate) -> dict` —
  builds an `ExperimentSpec` from the candidate's `asset_ids`/date fields
  plus fixed default cost assumptions, calls the engine's
  `run_experiment(spec)` directly as a plain function call (no OpenCode
  involved here at all), writes `trial_log.json`. Any exception from the
  engine is caught and recorded as a distinct failure mode
  (`failure_reason="engine_error: ..."`) rather than conflated with a
  build-cycle failure.
- **Input**: Engine's `ExperimentSpec` and `run_experiment`
- **Output**: `run_engine_for_candidate` + test with a mocked engine call
- **Acceptance**: Successful and exception-raising engine calls both
  produce the correctly distinguished outcome.

### Task 5.3 — Candidate Package Assembly
- **Action**: Implement `assemble_package(candidate_id: str) -> CandidatePackage` —
  reads `candidate.json`, `plan.json`, `review_log.jsonl`, `trial_log.json`
  (if present) and `state.json`'s final status, constructs a
  `CandidatePackage`, writes `package.json`.
- **Input**: Phase 0 schemas, all per-candidate files
- **Output**: `assemble_package` + test
- **Acceptance**: A candidate that completed successfully produces a
  package with `final_status="PASSED_TO_COMMITTEE"` and a populated
  `trial_log`.

### Task 5.4 — Failure Path Packaging
- **Action**: Confirm `assemble_package` also handles `state.status ==
  "FAILED"` correctly — `trial_log=None`, `final_status="FAILED"`,
  `failure_reason` carried through from state. No special-casing should be
  needed if Phase 0's schema was designed correctly (Task 0.6's acceptance
  criterion already required this) — this task is the integration proof
  that it actually works end to end, not just in the schema-level unit
  test.
- **Input**: A fixture candidate with `state.status="FAILED"`
- **Output**: Test in `test_schemas.py` or a new integration test
- **Acceptance**: A failed candidate produces a complete, valid package —
  nothing about the failure is lost or requires special handling
  downstream.

---

## Phase 6 — Validation

### Task 6.1 — End-to-End Smoke Test (Real OpenCode, Trivial Candidate)
- **Action**: Using the Task 0.2 fixture (or an even simpler one — "reimplement
  buy-and-hold as a new module"), run `run_candidate` for real (not mocked)
  against actual OpenCode. This is the first point in this entire plan
  where the real CLI flags from Task 3.1 get proven, not assumed.
- **Input**: Full Phase 0–5 pipeline, a running OpenCode installation
- **Output**: A real `package.json` on disk
- **Acceptance**: Completes with `final_status="PASSED_TO_COMMITTEE"` and a
  trial log containing sane, non-NaN metrics.

### Task 6.2 — Induced MINOR_FIX Test
- **Action**: Construct a task whose obvious first implementation attempt
  has a small, specific flaw (e.g., an off-by-one in a lookback window).
  Run it for real and confirm the Reviewer catches it, issues `MINOR_FIX`,
  and the retried worker step actually fixes it.
- **Input**: Phase 6.1's working pipeline
- **Output**: A documented trace of one real `MINOR_FIX` cycle
- **Acceptance**: Final `review_log.jsonl` shows one `MINOR_FIX` followed by
  a `PROCEED` on the same task.

### Task 6.3 — Induced REPLAN Test
- **Action**: Construct a scenario where an early task's result reveals the
  plan's remaining steps no longer make sense (e.g., a data availability
  issue the planner didn't anticipate). Confirm the Reviewer triggers
  `REPLAN` and the Planner produces a sensibly revised remaining task list,
  not a full restart from task 1.
- **Input**: Phase 6.1's working pipeline
- **Output**: A documented trace of one real `REPLAN` cycle
- **Acceptance**: `plan.json`'s task list after the replan differs only in
  the tasks from the replan point onward — earlier completed tasks are
  untouched.

### Task 6.4 — Crash-Resume Test
- **Action**: Kill the driver process partway through a real candidate run
  (after at least one `PROCEED`), then re-invoke `run_candidate` for the
  same `candidate_id` and confirm it resumes from `state.json` rather than
  restarting task 1 or creating a new OpenCode session.
- **Input**: Phase 4.4's state store, Phase 6.1's working pipeline
- **Output**: A documented before/after state comparison
- **Acceptance**: Resumed run's `session_id` matches the original; task
  index does not reset.

### Task 6.5 — L1 Driver Synthesis Report
- **Action**: Write `Docs/reports/L1_DRIVER_READY.md`:
  - The frozen `ApprovedCandidate` and `CandidatePackage` schemas
  - The three OpenCode agent definitions and their permission profiles
  - Confirmed, version-pinned CLI invocation details (updating Task 3.1's
    draft with whatever was actually verified in Task 6.1)
  - `MAX_STEPS`/`MAX_RETRIES`/`MAX_REPLANS` defaults and how to override
    them
  - Explicit confirmation that Tasks 6.1–6.4 passed
  - What is still not built: the two knowledge bases, the debate/judge
    gate, L1.5/L2 search control, and the four-auditor committee — named
    explicitly, same as the engine's own synthesis report did
- **Input**: All of Phase 6
- **Output**: `Docs/reports/L1_DRIVER_READY.md`
- **Acceptance**: Every claim traces to a passing test or a real, observed
  Phase 6 run.
- **Decision Gate**: This is what the debate gate and committee get built
  against next. No changes to `ApprovedCandidate`, `CandidatePackage`, or
  the agent permission model without revisiting this plan.

---

## Dependency Graph

```
Phase 0 (Contracts)
  0.1 → 0.2 → 0.3 → 0.4 → 0.5 → 0.6
                                  │
Phase 1 (OpenCode Agents)         │
  1.1 → 1.2, 1.3, 1.4 (parallel, all need 0.1) ─────────────┐
                                                              │
Phase 2 (Structured Output)                                  │
  2.1 → 2.2 ◄──────────────────────────────────────────────┤
                                                              │
Phase 3 (Bridge)                                             │
  3.1 → 3.2 → 3.3 ◄────────────────────────────────────────┘
                    │
Phase 4 (The Loop)  │
  4.1, 4.2, 4.3 (parallel, each needs Phase 2+3) → 4.4 → 4.5 → 4.6
                                                              │
Phase 5 (Engine Handoff)                                     │
  5.1 → 5.2 → 5.3 → 5.4 ◄─────────────────────────────────────┘
                     │
Phase 6 (Validation)
  6.1 → 6.2 → 6.3 → 6.4 → 6.5
```

**Critical path**: 0.1 → 0.2 → ... → 0.6 → 1.1 → 1.2 → 2.1 → 2.2 → 3.1 →
3.2 → 3.3 → 4.1 → 4.4 → 4.5 → 4.6 → 5.1 → 5.2 → 5.3 → 6.1 → 6.2 → 6.3 →
6.4 → 6.5

---

## Total Task Count

| Phase | Tasks | Description |
|-------|-------|-------------|
| Phase 0 | 6 | Frozen contracts (input, output, plan, verdict, state schemas) |
| Phase 1 | 4 | OpenCode agent definitions and permissions |
| Phase 2 | 2 | Structured output extraction and retry handling |
| Phase 3 | 3 | OpenCode subprocess bridge |
| Phase 4 | 6 | The planner/worker/reviewer loop itself |
| Phase 5 | 4 | Engine handoff and packaging |
| Phase 6 | 5 | Validation, including real (non-mocked) runs |
| **Total** | **30** | |

---

## What deliberately isn't here

This plan takes one approved hypothesis and turns it into one fully
evaluated, fully documented candidate — pass or fail. It does not decide
which hypothesis to approve (that's the debate/judge gate), does not
remember past attempts across candidates (that's the two knowledge bases),
does not decide how much budget to spend on which idea family (that's
L1.5/L2), and does not independently audit the result beyond the
engine's own decay check and the reviewer's task-level checks (that's the
four-auditor committee). Every one of those depends on `CandidatePackage`
existing in its frozen shape — which is exactly why Phase 0 comes before
anything with an OpenCode agent in it.