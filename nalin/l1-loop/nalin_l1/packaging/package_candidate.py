"""Engine handoff and candidate packaging (Tasks 5.1–5.4)."""

from __future__ import annotations

import json

from nalin_l1.config.experiment_spec import ExperimentSpec
from nalin_l1.loop import paths
from nalin_l1.loop.state_store import load_state
from nalin_l1.orchestrator.run_experiment import run_experiment
from nalin_l1.schemas import (
    ApprovedCandidate,
    CandidatePackage,
    ReviewVerdict,
    TaskPlan,
)
from nalin_l1.serialization.trial_log import write_trial_log
from nalin_l1.strategy.loader import load_strategy
from nalin_l1.strategy.protocol import Strategy

DEFAULT_FEE_BPS = 1.0
DEFAULT_SLIPPAGE_BPS = 1.0


class EngineRunError(Exception):
    """Raised when the engine raises while evaluating a candidate."""


def verify_strategy_conformance(candidate_id: str) -> bool:
    """Import the built module and check it satisfies the ``Strategy`` protocol.

    Independent of whatever the Reviewer agent claimed: the engine must never be
    invoked against something that does not actually satisfy its contract.
    """
    module_path = paths.candidate_file(candidate_id, paths.STRATEGY_FILE)
    if not module_path.exists():
        return False
    try:
        strategy = load_strategy(str(module_path))
        return isinstance(strategy, Strategy)
    except Exception:
        return False


def run_engine_for_candidate(candidate: ApprovedCandidate) -> dict:
    """Run the built strategy through the engine and write ``trial_log.json``.

    Returns the engine result dict on success. Raises :class:`EngineRunError`
    (message prefixed ``engine_error:``) to record an engine-level failure as
    distinct from a build-cycle failure.
    """
    spec = ExperimentSpec(
        asset_ids=candidate.asset_ids,
        start=candidate.start,
        train_end=candidate.train_end,
        val_end=candidate.val_end,
        end=candidate.end,
        strategy_module_path=str(
            paths.candidate_file(candidate.candidate_id, paths.STRATEGY_FILE)
        ),
        fee_bps=DEFAULT_FEE_BPS,
        slippage_bps=DEFAULT_SLIPPAGE_BPS,
        output_dir=str(paths.candidate_dir(candidate.candidate_id)),
    )
    try:
        result = run_experiment(spec)
    except Exception as exc:
        raise EngineRunError(f"engine_error: {exc}") from exc

    write_trial_log(
        result, spec, str(paths.candidate_file(candidate.candidate_id, "trial_log.json"))
    )
    return result


def assemble_package(candidate_id: str) -> CandidatePackage:
    """Assemble the frozen ``CandidatePackage`` from all per-candidate files."""
    candidate = ApprovedCandidate.model_validate_json(
        paths.candidate_file(candidate_id, "candidate.json").read_text()
    )
    plan = TaskPlan.model_validate_json(
        paths.candidate_file(candidate_id, "plan.json").read_text()
    )
    review_log = _read_review_log(candidate_id)
    state = load_state(candidate_id)

    trial_log_path = paths.candidate_file(candidate_id, "trial_log.json")
    if state is not None and state.status == "FAILED":
        final_status = "FAILED"
        failure_reason = state.failure_reason
        trial_log = None
    else:
        final_status = "PASSED_TO_COMMITTEE"
        failure_reason = None
        trial_log = (
            json.loads(trial_log_path.read_text()) if trial_log_path.exists() else None
        )

    package = CandidatePackage(
        candidate=candidate,
        plan=plan,
        review_log=review_log,
        trial_log=trial_log,
        final_status=final_status,
        failure_reason=failure_reason,
    )
    paths.candidate_file(candidate_id, "package.json").write_text(
        package.model_dump_json(indent=2)
    )
    return package


def _read_review_log(candidate_id: str) -> list[ReviewVerdict]:
    log_path = paths.candidate_file(candidate_id, "review_log.jsonl")
    if not log_path.exists():
        return []
    verdicts = []
    for line in log_path.read_text().splitlines():
        if line.strip():
            verdicts.append(ReviewVerdict.model_validate_json(line))
    return verdicts
