"""Main per-candidate driver loop (Tasks 4.5, 4.6 / PLAN_NEXT B.3).

As of Workstream B the loop runs **one fresh OpenCode session per task**: the
planner, worker, and reviewer each start from the COMMON CONTEXT + a
self-contained task prompt (see ``src/loop/prompt_doc.py``), so context never
accumulates across the candidate. The planner session id is captured for
provenance only and is never reused. Candidate state (current task, retries,
replans) still persists atomically in ``state.json``, so crash-resume is
unchanged.
"""

from __future__ import annotations

import time

from src.loop import paths
from src.loop.planner_step import run_planner_step
from src.loop.prompt_doc import write_prompts_doc
from src.loop.reviewer_step import run_reviewer_step
from src.loop.spec_digest import write_spec_digest
from src.loop.state_store import load_state, save_state
from src.loop.worker_step import run_worker_step
from src.schemas import ApprovedCandidate, CandidateState, TaskPlan

# --- Hard safety limits (Task 4.6) ---
MAX_STEPS = 50                 # total worker+reviewer invocations across the candidate
MAX_RETRIES = 3                # consecutive MINOR_FIX retries per task
MAX_REPLANS = 3                # REPLAN replans per candidate
WALL_CLOCK_TIMEOUT_SECONDS = 7200


def run_candidate(candidate: ApprovedCandidate) -> CandidateState:
    candidate_id = candidate.candidate_id

    # Persist the input contract so later steps/packaging can reference it.
    paths.candidate_file(candidate_id, "candidate.json").write_text(
        candidate.model_dump_json(indent=2)
    )

    state = load_state(candidate_id)
    if state is None:
        state = CandidateState(candidate_id=candidate_id, status="PLANNING")
        plan, session_id = run_planner_step(candidate)
        state.session_id = session_id  # provenance only; not reused
        _write_plan(plan)
        _write_docs(candidate, plan)
        state.status = "BUILDING"
        save_state(state)
    else:
        plan = _load_plan(candidate_id)
        _write_docs(candidate, plan)  # regenerate docs if a prior crash lost them

    fix_instructions: str | None = None
    start_time = time.monotonic()
    steps = 0

    while True:
        if steps > MAX_STEPS:
            state.status = "FAILED"
            state.failure_reason = "max_steps_exceeded"
            save_state(state)
            return state
        if time.monotonic() - start_time > WALL_CLOCK_TIMEOUT_SECONDS:
            state.status = "FAILED"
            state.failure_reason = "wall_clock_timeout"
            save_state(state)
            return state

        if state.current_task_index >= len(plan.tasks):
            state.status = "ENGINE_RUN"
            save_state(state)
            return state

        task = plan.tasks[state.current_task_index]

        state.status = "BUILDING"
        save_state(state)
        worker_summary = run_worker_step(
            task, candidate_id, fix_instructions=fix_instructions
        )
        steps += 1

        state.status = "REVIEWING"
        save_state(state)
        verdict = run_reviewer_step(task, worker_summary, candidate, plan)
        steps += 1

        if verdict.verdict == "PROCEED":
            state.current_task_index += 1
            state.retry_count = 0
            fix_instructions = None
            save_state(state)
        elif verdict.verdict == "MINOR_FIX":
            state.retry_count += 1
            if state.retry_count > MAX_RETRIES:
                state.status = "FAILED"
                state.failure_reason = "max_retries_exceeded"
                save_state(state)
                return state
            fix_instructions = verdict.fix_instructions
            save_state(state)
        elif verdict.verdict == "REPLAN":
            state.replan_count += 1
            if state.replan_count > MAX_REPLANS:
                state.status = "FAILED"
                state.failure_reason = "max_replans_exceeded"
                save_state(state)
                return state
            state.status = "REPLANNING"
            save_state(state)
            new_plan, _ = run_planner_step(
                candidate, prior_reasoning=verdict.replan_reasoning
            )
            plan = _splice_plan(plan, new_plan, state.current_task_index)
            _write_plan(plan)
            _write_docs(candidate, plan)
            state.retry_count = 0
            fix_instructions = None
            save_state(state)


def _splice_plan(old: TaskPlan, new: TaskPlan, index: int) -> TaskPlan:
    """Splice the replanned tasks in at ``index``, preserving completed tasks."""
    return TaskPlan(candidate_id=old.candidate_id, tasks=old.tasks[:index] + new.tasks)


def _write_plan(plan: TaskPlan) -> None:
    paths.candidate_file(plan.candidate_id, "plan.json").write_text(
        plan.model_dump_json(indent=2)
    )


def _write_docs(candidate: ApprovedCandidate, plan: TaskPlan) -> None:
    """Write the two self-contained session anchors: digest + ``prompts.md``."""
    write_spec_digest(candidate, plan)
    write_prompts_doc(candidate, plan)


def _load_plan(candidate_id: str) -> TaskPlan:
    return TaskPlan.model_validate_json(
        paths.candidate_file(candidate_id, "plan.json").read_text()
    )
