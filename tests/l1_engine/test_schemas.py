"""Tests for the frozen loop schemas (Phase 0: Tasks 0.2–0.6)."""

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

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from alternatives.l1_engine.schemas import (
    ApprovedCandidate,
    CandidatePackage,
    CandidateState,
    ReviewVerdict,
    Task,
    TaskPlan,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_candidate.json"


# --- Task 0.2: ApprovedCandidate ---


def test_sample_candidate_fixture_parses():
    data = json.loads(FIXTURE.read_text())
    candidate = ApprovedCandidate.model_validate(data)
    assert candidate.candidate_id == "cand_momentum_20d"
    assert candidate.asset_ids == ["asset_a"]


# --- Task 0.3: TaskPlan ---


def test_task_plan_validates():
    plan = TaskPlan(
        candidate_id="c1",
        tasks=[
            Task(task_id="t1", description="d", target_files=["a.py"], acceptance="a"),
            Task(task_id="t2", description="d", target_files=["b.py"], acceptance="b"),
            Task(task_id="t3", description="d", target_files=["c.py"], acceptance="c"),
        ],
    )
    assert len(plan.tasks) == 3


def test_task_plan_missing_acceptance_fails_with_task_id():
    with pytest.raises(ValidationError) as exc:
        TaskPlan(
            candidate_id="c1",
            tasks=[
                Task(task_id="t1", description="d", target_files=["a.py"], acceptance="a"),
                Task(task_id="t2", description="d", target_files=["b.py"]),
            ],
        )
    assert "acceptance" in str(exc.value)


# --- Task 0.4: ReviewVerdict ---


def test_minor_fix_requires_fix_instructions():
    with pytest.raises(ValidationError):
        ReviewVerdict(task_id="t1", verdict="MINOR_FIX", reasoning="r")


def test_proceed_ignores_optional_fields():
    v = ReviewVerdict(
        task_id="t1", verdict="PROCEED", reasoning="r", fix_instructions="unused"
    )
    assert v.verdict == "PROCEED"


def test_replan_requires_replan_reasoning():
    with pytest.raises(ValidationError):
        ReviewVerdict(task_id="t1", verdict="REPLAN", reasoning="r")


# --- Task 0.5: CandidateState ---


def test_candidate_state_json_roundtrip():
    state = CandidateState(candidate_id="c1", status="BUILDING", current_task_index=2)
    assert CandidateState.model_validate_json(state.model_dump_json()) == state


# --- Task 0.6: CandidatePackage ---


def test_failed_package_validates_without_trial_log():
    candidate = ApprovedCandidate.model_validate_json(FIXTURE.read_text())
    plan = TaskPlan(candidate_id=candidate.candidate_id, tasks=[])
    pkg = CandidatePackage(
        candidate=candidate,
        plan=plan,
        review_log=[],
        trial_log=None,
        final_status="FAILED",
        failure_reason="max_steps_exceeded",
    )
    assert pkg.final_status == "FAILED"
    assert pkg.trial_log is None
