"""Statistician stage (PLAN_NEXT B.4): reads raw results and questions methodology."""

from __future__ import annotations

from src.loop import paths
from src.loop.prompt_doc import build_statistician_prompt
from src.opencode_bridge.invoke import invoke_opencode
from src.opencode_bridge.parse import parse_or_retry
from src.schemas import ApprovedCandidate, StatisticianNote


def _retry_fn(prompt, cwd):
    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("statistician", adjusted, None, cwd=cwd).stdout

    return retry


def run_statistician_step(candidate: ApprovedCandidate) -> StatisticianNote:
    """Read-only statistical review of the raw trial results (fresh session)."""
    cid = candidate.candidate_id
    prompt = build_statistician_prompt(candidate)
    context_files = [
        str(paths.candidate_file(cid, "trial_log.json")),
        str(paths.candidate_file(cid, "spec_digest.md")),
    ]
    cwd = str(paths.candidate_dir(cid))

    result = invoke_opencode("statistician", prompt, None, context_files, cwd=cwd)
    note: StatisticianNote = parse_or_retry(
        result.stdout, StatisticianNote, _retry_fn(prompt, cwd)
    )

    paths.candidate_file(cid, "statistician_note.json").write_text(
        note.model_dump_json(indent=2)
    )
    return note
