"""Committee stage (PLAN_NEXT B.4/B.5): aggregate audit axes into PASS/VETO."""

from __future__ import annotations

from src.loop import paths
from src.loop.prompt_doc import build_committee_prompt
from src.opencode_bridge.invoke import invoke_opencode
from src.opencode_bridge.parse import parse_or_retry
from src.schemas import ApprovedCandidate, CommitteeVerdict


def _retry_fn(prompt, cwd):
    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("judge", adjusted, None, cwd=cwd).stdout

    return retry


def run_committee_step(candidate: ApprovedCandidate) -> CommitteeVerdict:
    """Read-only aggregation of statistician + auditor into a binding PASS/VETO.

    Leakage is a hard VETO; the committee reads the auditor's four-axis report
    plus the statistician's notes and the raw trial metrics.
    """
    cid = candidate.candidate_id
    prompt = build_committee_prompt(candidate)
    context_files = [
        str(paths.candidate_file(cid, "auditor_report.json")),
        str(paths.candidate_file(cid, "statistician_note.json")),
        str(paths.candidate_file(cid, "trial_log.json")),
    ]
    cwd = str(paths.candidate_dir(cid))

    result = invoke_opencode("judge", prompt, None, context_files, cwd=cwd)
    verdict: CommitteeVerdict = parse_or_retry(
        result.stdout, CommitteeVerdict, _retry_fn(prompt, cwd)
    )

    paths.candidate_file(cid, "committee_verdict.json").write_text(
        verdict.model_dump_json(indent=2)
    )
    return verdict
