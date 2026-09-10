"""Worker step (Task 4.2 / PLAN_NEXT B.3): execute exactly one task into the file.

As of Workstream B the worker runs in a *fresh* session per task, fed the
COMMON CONTEXT + the self-contained task prompt (plus the build snapshot and
digest as context files).
"""

from __future__ import annotations

from src.loop import paths
from src.loop.prompt_doc import _common_context, build_worker_prompt
from src.opencode_bridge.invoke import extract_result_block, invoke_opencode
from src.schemas import Task


def run_worker_step(
    task: Task,
    candidate_id: str,
    fix_instructions: str | None = None,
) -> str:
    """Invoke the worker for one task in a fresh session; return its summary text.

    The worker's output is code changes on disk, not a structured object, so the
    summary is not schema-validated — it is informational for the reviewer.
    """
    task_prompt = build_worker_prompt(task, candidate_id, fix_instructions)
    cwd = str(paths.candidate_dir(candidate_id))

    # Context files: the digest (self-contained summary) and the current snapshot
    # of the single build file (if present) so the worker sees prior work without
    # any accumulated session state.
    context_files = [
        str(paths.candidate_file(candidate_id, "spec_digest.md")),
    ]
    train_path = paths.candidate_file(candidate_id, paths.STRATEGY_FILE)
    if train_path.exists():
        context_files.append(str(train_path))

    result = invoke_opencode("worker", task_prompt, None, context_files, cwd=cwd)
    try:
        return extract_result_block(result.stdout)
    except Exception:
        return result.stdout.strip()
