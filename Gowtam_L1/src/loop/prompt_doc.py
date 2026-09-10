"""Per-task prompt document (PLAN_NEXT B.2 / B.5).

Adopts the reference project's *multi-session mechanism*: a re-pasted COMMON
CONTEXT block plus one self-contained prompt per atomic task, so every task runs
in a fresh session with zero accumulated context. Also bakes in the
anti-hallucination rules and the five-question final self-critique (B.5).

This module is the single source of truth for prompt construction — every agent
role's prompt is built here, whether emitted into ``prompts.md`` or passed
in-line to an invocation.
"""

from __future__ import annotations

from pathlib import Path

from src.loop import paths
from src.schemas import ApprovedCandidate, Task, TaskPlan

SELF_CRITIQUE = (
    "FINAL SELF-CRITIQUE (answer in writing before stopping):\n"
    "1. List three possible bugs in the code or analysis you produced.\n"
    "2. List three hidden assumptions that, if violated, would invalidate the result.\n"
    "3. Describe one alternative implementation that would test the same thing differently.\n"
    "4. Cite the specific constraint (frozen contract / task acceptance) that justifies this.\n"
    "5. State what you are least confident about in this output."
)

ANTI_HALLUCINATION = (
    "ANTI-HALLUCINATION RULES:\n"
    "1. Do not write conclusions before seeing actual output numbers.\n"
    "2. Do not hardcode expected results.\n"
    "3. Do not add features, functions, or files not specified in the task.\n"
    "4. Do not modify any file outside the ones listed in the task.\n"
    "5. If behavior depends on data, compute and check — do not assume."
)


def _common_context(candidate: ApprovedCandidate) -> str:
    return (
        "COMMON CONTEXT — L1 agentic research loop (read this first).\n\n"
        "You are an agent role in a fully-agentic research pipeline (no human in the loop).\n"
        f"Candidate id: {candidate.candidate_id}\n"
        f"Assets: {', '.join(candidate.asset_ids)} | window {candidate.start}..{candidate.end}\n"
        f"Single strategy file: candidates/{candidate.candidate_id}/{paths.STRATEGY_FILE}\n\n"
        "PROJECT RULES (non-negotiable):\n"
        "- Single-file ground rule: ALL strategy code lives in train.py. No submodules.\n"
        "- No lookahead: positions at bar t depend only on bars strictly before t.\n"
        "- Frozen engine contract: Docs/reports/L1_READY.md (Strategy protocol).\n"
        "- Canonical price is mid = (bid + ask) / 2 (project-wide convention).\n"
        "- Roles never mix; read-only roles must not edit or write files.\n\n"
        "REPRODUCIBILITY METADATA (required): every produced report/artifact must\n"
        "record random_seed, git_commit, execution_time, python_version,\n"
        "key_dependencies, dataset_hash, and config (see src/loop/reproducibility.py).\n\n"
        "DECISION STATUS LIFECYCLE: Draft -> Provisional -> Locked -> Deprecated.\n"
        "Agents set Provisional; only the committee sets Locked, and only a later bug\n"
        "fix sets Deprecated.\n\n"
        f"{ANTI_HALLUCINATION}\n\n"
        f"{SELF_CRITIQUE}"
    )


def build_planner_prompt(
    candidate: ApprovedCandidate, prior_reasoning: str | None = None
) -> str:
    body = (
        f"{_common_context(candidate)}\n\n"
        "ROLE: planner (read-only).\n"
        f"Approved candidate:\n{candidate.model_dump_json(indent=2)}\n\n"
        "Produce an atomic task plan for implementing this candidate as a "
        "Strategy-conforming module in the single file "
        f"candidates/{candidate.candidate_id}/{paths.STRATEGY_FILE}.\n\n"
        "GRANULARITY RULES (speed vs correctness):\n"
        "- Emit between 3 and 6 tasks total — no more.\n"
        "- NEVER emit a standalone task for boilerplate (module docstring, "
        "imports, CONFIG dict, RANDOM_SEED, or a stub/skeleton class). Fold all "
        "of that into the first substantive task.\n"
        "- Group related rules into one task each (e.g. one task for ALL "
        "momentum-family signals, one for ALL oscillators, one for ALL "
        "band/stateful rules), so each task is reviewable in a single pass yet "
        "large enough to be worth a full build+review round-trip.\n"
        "- Keep exactly one aggregate acceptance criterion per task, and make "
        "sure every task's target_files points only at the single file."
    )
    if prior_reasoning:
        body += (
            "\n\nThis is a replan. Prior plan is invalid past this point for the "
            f"following reason:\n{prior_reasoning}"
        )
    return body


def build_worker_prompt(
    task: Task, candidate_id: str, fix_instructions: str | None = None
) -> str:
    body = (
        f"ROLE: worker (the only role that writes code).\n"
        f"Candidate id: {candidate_id}\n"
        f"Build into the single file candidates/{candidate_id}/{paths.STRATEGY_FILE}.\n\n"
        f"TASK\n"
        f"task_id: {task.task_id}\n"
        f"description: {task.description}\n"
        f"acceptance: {task.acceptance}\n"
        f"target_files: {', '.join(task.target_files)}\n"
    )
    if fix_instructions:
        body += f"\nReviewer requested fixes (apply exactly):\n{fix_instructions}"
    return body


def build_reviewer_prompt(
    task: Task,
    worker_summary: str,
    candidate: ApprovedCandidate,
    plan: TaskPlan,
) -> str:
    # Reviewer references only the compact task + digest, never the full paper.
    task_index = "\n".join(
        f"- `{t.task_id}`: {t.description}" for t in plan.tasks
    )
    return (
        f"{_common_context(candidate)}\n\n"
        f"ROLE: reviewer (read-only; you never edit files).\n"
        f"Task under review: {task.task_id} — {task.description}\n"
        f"Acceptance: {task.acceptance}\n"
        f"Worker change summary: {worker_summary}\n\n"
        f"Task index (context only):\n{task_index}\n\n"
        "Review this task for (1) acceptance, (2) lookahead, (3) hypothesis drift, "
        "and return a ReviewVerdict."
    )


def build_statistician_prompt(candidate: ApprovedCandidate) -> str:
    return (
        f"{_common_context(candidate)}\n\n"
        "ROLE: statistician (read-only; you never edit code).\n"
        "Read the raw trial results in candidates/"
        f"{candidate.candidate_id}/trial_log.json. Question the methodology only:\n"
        "- Are the reported metrics (sharpe, drawdown, CVaR, stability, turnover,\n"
        "  concentration, decay) internally consistent with each other?\n"
        "- Is the sample size / window length adequate for the claim?\n"
        "- What statistical threats (multiple testing, non-stationarity, tail risk)\n"
        "  apply? Do NOT declare the candidate good or bad."
    )


def build_auditor_prompt(candidate: ApprovedCandidate) -> str:
    return (
        f"{_common_context(candidate)}\n\n"
        "ROLE: auditor (read-only; you never edit code).\n"
        "Audit candidates/"
        f"{candidate.candidate_id}/train.py against exactly four axes:\n"
        "1. lookahead / data leakage,\n"
        "2. selection / look-back bias,\n"
        "3. overfitting,\n"
        "4. assumption violations.\n"
        "For each axis emit a finding; leak is a hard veto. Do not edit anything."
    )


def build_committee_prompt(candidate: ApprovedCandidate) -> str:
    return (
        f"{_common_context(candidate)}\n\n"
        "ROLE: committee (read-only; you never edit code).\n"
        "Aggregate the four audit axes + the statistician's notes into a single\n"
        "verdict: PASS or VETO. Leakage is a hard VETO; otherwise weigh bias,\n"
        "overfitting, and assumption violations against the reported metrics.\n"
        "This is the final, binding decision — there is no human gate."
    )


def _task_block(candidate: ApprovedCandidate, task: Task) -> str:
    return (
        f"### TASK {task.task_id}\n"
        f"- description: {task.description}\n"
        f"- acceptance: {task.acceptance}\n"
        f"- target_files: {', '.join(task.target_files)}\n"
        f"- output: edit candidates/{candidate.candidate_id}/{paths.STRATEGY_FILE}"
    )


def build_prompts_doc(candidate: ApprovedCandidate, plan: TaskPlan) -> str:
    """Render ``prompts.md``: COMMON CONTEXT + one self-contained task prompt each."""
    sections = [_common_context(candidate), ""]
    for task in plan.tasks:
        sections.append(_task_block(candidate, task))
    return "\n\n".join(sections)


def write_prompts_doc(candidate: ApprovedCandidate, plan: TaskPlan) -> Path:
    """Persist ``prompts.md`` to the candidate directory and return its path."""
    path = paths.candidate_file(candidate.candidate_id, "prompts.md")
    path.write_text(build_prompts_doc(candidate, plan))
    return path
