"""Worker step (Task 4.2): execute exactly one task into the single strategy file."""

from __future__ import annotations

from src.loop import paths
from src.opencode_bridge.invoke import extract_result_block, invoke_opencode
from src.schemas import Task


def build_worker_prompt(task: Task, candidate_id: str, fix_instructions: str | None) -> str:
    body = (
        f"Candidate id: {candidate_id}\n"
        f"Build into the single file candidates/{candidate_id}/{paths.STRATEGY_FILE}.\n\n"
        f"Task id: {task.task_id}\n"
        f"Description: {task.description}\n"
        f"Acceptance: {task.acceptance}\n"
    )
    if fix_instructions:
        body += f"\nReviewer requested fixes (apply exactly):\n{fix_instructions}"
    return body


def run_worker_step(
    task: Task,
    candidate_id: str,
    session_id: str,
    fix_instructions: str | None = None,
) -> str:
    """Invoke the worker for one task; return its raw completion summary text.

    The worker's output is code changes on disk, not a structured object, so the
    summary is not schema-validated — it is informational for the reviewer.
    """
    prompt = build_worker_prompt(task, candidate_id, fix_instructions)
    result = invoke_opencode(
        "worker", prompt, session_id, cwd=str(paths.candidate_dir(candidate_id))
    )
    # Prefer the structured summary block; fall back to raw stdout.
    try:
        return extract_result_block(result.stdout)
    except Exception:
        return result.stdout.strip()
