"""Planner step (Task 4.1): hypothesis -> atomic ``TaskPlan``."""

from __future__ import annotations

from nalin_l1.loop import paths
from nalin_l1.opencode_bridge.invoke import invoke_opencode
from nalin_l1.opencode_bridge.parse import parse_or_retry
from nalin_l1.schemas import ApprovedCandidate, TaskPlan


def build_planner_prompt(
    candidate: ApprovedCandidate, prior_reasoning: str | None = None
) -> str:
    body = (
        f"Approved candidate:\n{candidate.model_dump_json(indent=2)}\n\n"
        "Produce an atomic task plan for implementing this candidate as a "
        "Strategy-conforming module in the single file "
        f"candidates/{candidate.candidate_id}/{paths.STRATEGY_FILE}."
    )
    if prior_reasoning:
        body += (
            "\n\nThis is a replan. Prior plan is invalid past this point for the "
            f"following reason:\n{prior_reasoning}"
        )
    return body


def run_planner_step(
    candidate: ApprovedCandidate,
    session_id: str | None = None,
    prior_reasoning: str | None = None,
) -> tuple[TaskPlan, str | None]:
    """Produce a ``TaskPlan`` and return it along with the (resolved) session id.

    On a fresh candidate ``session_id`` is ``None``; the first invoke creates a
    real OpenCode session whose id is captured and returned so the driver can
    persist it for the whole candidate.
    """
    prompt = build_planner_prompt(candidate, prior_reasoning)
    context_files = paths.context_files(
        paths.L1_READY_PATH,
        paths.candidate_file(candidate.candidate_id, "candidate.json"),
    )
    cwd = str(paths.candidate_dir(candidate.candidate_id))

    result = invoke_opencode("planner", prompt, session_id, context_files, cwd=cwd)
    current_session = result.session_id or session_id

    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode(
            "planner", adjusted, current_session, context_files, cwd=cwd
        ).stdout

    plan: TaskPlan = parse_or_retry(result.stdout, TaskPlan, retry)

    plan_path = paths.candidate_file(candidate.candidate_id, "plan.json")
    plan_path.write_text(plan.model_dump_json(indent=2))
    return plan, current_session
