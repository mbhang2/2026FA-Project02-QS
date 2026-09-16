"""Reviewer step (Task 4.3 / PLAN_NEXT B.3): review one task -> ``ReviewVerdict``.

As of Workstream B the reviewer runs in a *fresh* session per task, fed the
COMMON CONTEXT + the self-contained task prompt (no re-embedded full paper).
"""

from __future__ import annotations

from loop import paths
from loop.prompt_doc import build_reviewer_prompt
from opencode_bridge.invoke import invoke_opencode
from opencode_bridge.parse import parse_or_retry
from schemas import ApprovedCandidate, ReviewVerdict, Task, TaskPlan


def _retry_fn(prompt, cwd):
    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("reviewer", adjusted, None, cwd=cwd).stdout

    return retry


def run_reviewer_step(
    task: Task,
    worker_summary: str,
    candidate: ApprovedCandidate,
    plan: TaskPlan,
) -> ReviewVerdict:
    prompt = build_reviewer_prompt(task, worker_summary, candidate, plan)
    cwd = str(paths.candidate_dir(candidate.candidate_id))

    result = invoke_opencode("reviewer", prompt, None, cwd=cwd)
    verdict: ReviewVerdict = parse_or_retry(
        result.stdout,
        ReviewVerdict,
        _retry_fn(prompt, cwd),
    )

    log_path = paths.candidate_file(candidate.candidate_id, "review_log.jsonl")
    with open(log_path, "a") as f:
        f.write(verdict.model_dump_json() + "\n")
    return verdict
