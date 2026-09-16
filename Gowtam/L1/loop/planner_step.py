"""Planner step (Task 4.1 / PLAN_NEXT B.1–B.3): hypothesis -> atomic ``TaskPlan``.

As of Workstream B the planner runs in a *fresh* OpenCode session every time
(no session continuity across the candidate). The session id it captures is
strictly provenance; it is never reused for later roles.
"""

from __future__ import annotations

from loop import paths
from loop.prompt_doc import build_planner_prompt
from opencode_bridge.invoke import invoke_opencode
from opencode_bridge.parse import parse_or_retry
from schemas import ApprovedCandidate, TaskPlan


def run_planner_step(
    candidate: ApprovedCandidate,
    prior_reasoning: str | None = None,
) -> tuple[TaskPlan, str | None]:
    """Produce a ``TaskPlan`` in a fresh session; return it plus the session id.

    ``session_id`` (2nd element) is the fresh session that produced the plan; it
    is returned for provenance only and is not reused by later roles.
    """
    prompt = build_planner_prompt(candidate, prior_reasoning)
    context_files = paths.context_files(
        paths.CONTEXT_API_PATH,
        paths.candidate_file(candidate.candidate_id, "candidate.json"),
    )
    cwd = str(paths.candidate_dir(candidate.candidate_id))

    result = invoke_opencode("planner", prompt, None, context_files, cwd=cwd)

    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("planner", adjusted, None, context_files, cwd=cwd).stdout

    plan: TaskPlan = parse_or_retry(result.stdout, TaskPlan, retry)

    plan_path = paths.candidate_file(candidate.candidate_id, "plan.json")
    plan_path.write_text(plan.model_dump_json(indent=2))
    return plan, result.session_id
