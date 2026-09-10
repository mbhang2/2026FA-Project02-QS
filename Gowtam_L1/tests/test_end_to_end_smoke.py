"""Real end-to-end smoke test (Task 6.1) — invokes actual OpenCode + the engine.

Slow and network/cost-bound, so it is marked ``slow`` and skipped by default.
Run with ``pytest -m slow``.
"""

from __future__ import annotations

import pytest

from src.loop.driver import run_candidate
from src.loop.run import run_full
from src.packaging.package_candidate import (
    assemble_package,
    run_engine_for_candidate,
    verify_strategy_conformance,
)
from src.schemas import ApprovedCandidate

pytestmark = pytest.mark.slow


def _buy_and_hold_candidate() -> ApprovedCandidate:
    return ApprovedCandidate(
        candidate_id="smoke_buy_and_hold",
        hypothesis=(
            "Reimplement a simple buy-and-hold strategy: hold a constant full "
            "position (1.0) from the first bar onward."
        ),
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
    )


def test_full_loop_real_opencode():
    candidate = _buy_and_hold_candidate()
    state = run_candidate(candidate)

    assert state.status == "ENGINE_RUN", f"unexpected status {state.status}: {state.failure_reason}"

    assert verify_strategy_conformance(candidate.candidate_id) is True

    result = run_engine_for_candidate(candidate)
    assert result["composite_score"] == result["composite_score"]  # no NaN

    package = assemble_package(candidate.candidate_id)
    assert package.final_status == "PASSED_TO_COMMITTEE"
    assert package.trial_log is not None


def test_full_staged_pipeline_real_opencode():
    """Workstream B: full staged pipeline ends in a committee PASS/VETO."""
    from src.loop import paths

    candidate = _buy_and_hold_candidate()
    package = run_full(candidate)

    assert package.final_status in ("PASSED_TO_COMMITTEE", "FAILED")
    # Every staged artifact is persisted, regardless of the committee verdict.
    for name in ("spec_digest.md", "prompts.md", "auditor_report.json",
                 "statistician_note.json", "committee_verdict.json", "reproducibility.json"):
        assert paths.candidate_file(candidate.candidate_id, name).exists(), name
