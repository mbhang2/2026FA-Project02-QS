"""Auditor stage (PLAN_NEXT B.4): four-axis bias/leakage/overfit audit."""

from __future__ import annotations

from src.loop import paths
from src.loop.prompt_doc import build_auditor_prompt
from src.opencode_bridge.invoke import invoke_opencode
from src.opencode_bridge.parse import parse_or_retry
from src.schemas import ApprovedCandidate, AuditorReport


def _retry_fn(prompt, cwd):
    def retry(exc):
        adjusted = prompt + f"\n\n<parse-error>\n{exc}\n</parse-error>\nRetry, correcting the JSON."
        return invoke_opencode("auditor", adjusted, None, cwd=cwd).stdout

    return retry


def run_auditor_step(candidate: ApprovedCandidate) -> AuditorReport:
    """Read-only four-axis audit of the built strategy (fresh session)."""
    cid = candidate.candidate_id
    prompt = build_auditor_prompt(candidate)
    context_files = [
        str(paths.candidate_file(cid, paths.STRATEGY_FILE)),
        str(paths.candidate_file(cid, "trial_log.json")),
        str(paths.candidate_file(cid, "spec_digest.md")),
    ]
    cwd = str(paths.candidate_dir(cid))

    result = invoke_opencode("auditor", prompt, None, context_files, cwd=cwd)
    report: AuditorReport = parse_or_retry(
        result.stdout, AuditorReport, _retry_fn(prompt, cwd)
    )

    paths.candidate_file(cid, "auditor_report.json").write_text(
        report.model_dump_json(indent=2)
    )
    return report
