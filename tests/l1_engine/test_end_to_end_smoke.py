"""Real end-to-end smoke test (Task 6.1) — invokes actual OpenCode + the engine.

Slow and network/cost-bound, so it is marked ``slow`` and skipped by default.
Run with ``pytest -m slow``.
"""

from __future__ import annotations


import pytest

# Skipped, not removed and not relaxed: this module imports the pydantic
# schemas at import time, so the skip has to happen before that import.
pytest.importorskip(
    "pydantic",
    reason=(
        "pydantic is not in requirements.lock, and installing it would move `environment_fingerprint` and `lockfile_digest` -- both recorded on every committed ledger row, where `ledger_check` currently reports environment-drifted: 0. Parked since Task 40; a human decides whether these tests running is worth re-dating the provenance of every row."
    ),
)

import pytest

from alternatives.l1_engine.loop.driver import run_candidate
from alternatives.l1_engine.packaging.package_candidate import (
    assemble_package,
    run_engine_for_candidate,
    verify_strategy_conformance,
)
from alternatives.l1_engine.schemas import ApprovedCandidate

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
