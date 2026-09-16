"""Spec digest (PLAN_NEXT B.1): a compact, self-contained summary of a candidate.

The planner *reads* the full hypothesis exactly once. Everything downstream —
worker, reviewer, statistician, auditor, committee — references only this
200–400 word digest (plus the atomic plan), not the raw paper, so a fresh
session never re-embeds a full hypothesis. It is derived deterministically from
``ApprovedCandidate`` + ``TaskPlan`` (no extra agent round-trip).

Content: hypothesis intent, asset/time constraints, non-negotiable invariants,
the declared target timeframe (default ``1d``), and a one-line-per-task index.
"""

from __future__ import annotations

from pathlib import Path

from loop import paths
from loop.schemas import ApprovedCandidate, TaskPlan

DEFAULT_TIMEFRAME = "1d"


def _hypothesis_intent(candidate: ApprovedCandidate) -> str:
    hypothesis = candidate.hypothesis.strip()
    if not hypothesis:
        return "(empty hypothesis)"
    # Cap to keep the digest compact; the full hypothesis remains in candidate.json.
    return hypothesis if len(hypothesis) <= 800 else hypothesis[:797] + "..."


def build_spec_digest(candidate: ApprovedCandidate, plan: TaskPlan) -> str:
    """Render the compact spec digest markdown for a candidate."""
    tasks = "\n".join(
        f"- `{t.task_id}`: {t.description}" for t in plan.tasks
    ) or "- (no tasks)"

    invariants = (
        "- Single-file ground rule: all strategy code lives in `train.py`.\n"
        "- No lookahead: positions at bar `t` depend only on bars `< t`.\n"
        "- Point-in-time data only; the engine's no-lookahead guard is authoritative.\n"
        "- One acceptance criterion per task."
    )

    return (
        f"# Spec Digest — {candidate.candidate_id}\n\n"
        f"## Hypothesis intent\n{_hypothesis_intent(candidate)}\n\n"
        f"## Constraints\n"
        f"- assets: {', '.join(candidate.asset_ids)}\n"
        f"- range: {candidate.start} .. {candidate.end}\n"
        f"- train_end: {candidate.train_end} | val_end: {candidate.val_end}\n"
        f"- target timeframe: {DEFAULT_TIMEFRAME}\n\n"
        f"## Non-negotiable invariants\n{invariants}\n\n"
        f"## Task index\n{tasks}\n"
    )


def write_spec_digest(candidate: ApprovedCandidate, plan: TaskPlan) -> Path:
    """Persist ``spec_digest.md`` to the candidate directory and return its path."""
    path = paths.candidate_file(candidate.candidate_id, "spec_digest.md")
    path.write_text(build_spec_digest(candidate, plan))
    return path
