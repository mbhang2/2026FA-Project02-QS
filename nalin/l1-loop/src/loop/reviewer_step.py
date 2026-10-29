"""Reviewer step (Task 4.3): review one completed task -> ``ReviewVerdict``."""

from __future__ import annotations

from src.loop import paths
from src.opencode_bridge.invoke import invoke_opencode
from src.opencode_bridge.parse import parse_or_retry
from src.schemas import ApprovedCandidate, ReviewVerdict, Task, TaskPlan


def build_reviewer_prompt(
    task: Task,
    worker_summary: str,
    candidate: ApprovedCandidate,
    plan: TaskPlan,
) -> str:
    return (
        f"Original hypothesis: {candidate.hypothesis}\n\n"
        f"Full plan:\n{plan.model_dump_json(indent=2)}\n\n"
        f"Task under review: {task.task_id}\n"
        f"Worker change summary: {worker_summary}\n\n"
        "Review this task for (1) acceptance, (2) lookahead, (3) hypothesis drift, "
        "and return a ReviewVerdict."
    )


def _retry_fn(prompt, session_id, cwd):
    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("reviewer", adjusted, session_id, cwd=cwd).stdout

    return retry


def run_reviewer_step(
    task: Task,
    worker_summary: str,
    candidate: ApprovedCandidate,
    plan: TaskPlan,
    session_id: str,
) -> ReviewVerdict:
    prompt = build_reviewer_prompt(task, worker_summary, candidate, plan)
    cwd = str(paths.candidate_dir(candidate.candidate_id))

    result = invoke_opencode("reviewer", prompt, session_id, cwd=cwd)
    verdict: ReviewVerdict = parse_or_retry(
        result.stdout,
        ReviewVerdict,
        _retry_fn(prompt, session_id, cwd),
    )

    log_path = paths.candidate_file(candidate.candidate_id, "review_log.jsonl")
    with open(log_path, "a") as f:
        f.write(verdict.model_dump_json() + "\n")
    return verdict
