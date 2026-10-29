"""Tests for engine handoff and packaging (Tasks 5.1–5.4)."""

from __future__ import annotations

import json

import pytest

from src.loop import paths
from src.packaging.package_candidate import (
    EngineRunError,
    assemble_package,
    run_engine_for_candidate,
    verify_strategy_conformance,
)
from src.schemas import ApprovedCandidate, CandidateState, Task, TaskPlan

pytestmark = pytest.mark.usefixtures("redirect_candidate_paths")

CONFORMING = '''
from src.strategy.protocol import Strategy
import pandas as pd

class MyStrategy(Strategy):
    def generate_positions(self, price_history):
        return pd.Series(1.0, index=price_history.index)
'''

NONCONFORMING = '''
class NotAStrategy:
    def wrong_method(self, x):
        return 1
'''


def _candidate(cid="pack_test"):
    return ApprovedCandidate(
        candidate_id=cid,
        hypothesis="h",
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
    )


def _write_strategy(cid, content):
    paths.candidate_file(cid, "train.py").write_text(content)


def test_conformance_true_for_conforming():
    cid = "pack_conf_ok"
    _write_strategy(cid, CONFORMING)
    assert verify_strategy_conformance(cid) is True


def test_conformance_false_for_nonconforming():
    cid = "pack_conf_bad"
    _write_strategy(cid, NONCONFORMING)
    assert verify_strategy_conformance(cid) is False


def test_conformance_false_when_missing():
    assert verify_strategy_conformance("pack_conf_missing") is False


def test_run_engine_success_writes_trial_log(monkeypatch):
    from src.packaging import package_candidate

    cid = "pack_engine_ok"
    _write_strategy(cid, CONFORMING)

    def fake_run_experiment(spec):
        return {"validation_metrics": {}, "holdout_metrics": {}, "decay": {}, "composite_score": 0.0}

    monkeypatch.setattr(package_candidate, "run_experiment", fake_run_experiment)
    result = run_engine_for_candidate(_candidate(cid))
    assert result["composite_score"] == 0.0
    assert paths.candidate_file(cid, "trial_log.json").exists()


def test_run_engine_error_recorded_distinctly(monkeypatch):
    from src.packaging import package_candidate

    cid = "pack_engine_err"
    _write_strategy(cid, CONFORMING)

    def fake_run_experiment(spec):
        raise RuntimeError("diverging metrics")

    monkeypatch.setattr(package_candidate, "run_experiment", fake_run_experiment)
    with pytest.raises(EngineRunError) as exc:
        run_engine_for_candidate(_candidate(cid))
    assert str(exc.value).startswith("engine_error:")


def test_assemble_success_package(monkeypatch):
    from src.packaging import package_candidate

    cid = "pack_assemble_ok"
    _write_strategy(cid, CONFORMING)
    cand = _candidate(cid)
    paths.candidate_file(cid, "candidate.json").write_text(cand.model_dump_json(indent=2))
    plan = TaskPlan(candidate_id=cid, tasks=[Task(task_id="t0", description="d", target_files=["train.py"], acceptance="a")])
    paths.candidate_file(cid, "plan.json").write_text(plan.model_dump_json(indent=2))
    paths.candidate_file(cid, "trial_log.json").write_text(json.dumps({"composite_score": 1.0}))

    from src.loop import state_store as ss
    monkeypatch.setattr(package_candidate, "load_state", lambda cid: CandidateState(candidate_id=cid, status="ENGINE_RUN"))

    pkg = assemble_package(cid)
    assert pkg.final_status == "PASSED_TO_COMMITTEE"
    assert pkg.trial_log == {"composite_score": 1.0}
    assert paths.candidate_file(cid, "package.json").exists()


def test_assemble_failed_package_no_trial_log(monkeypatch):
    from src.packaging import package_candidate

    cid = "pack_assemble_fail"
    cand = _candidate(cid)
    paths.candidate_file(cid, "candidate.json").write_text(cand.model_dump_json(indent=2))
    plan = TaskPlan(candidate_id=cid, tasks=[])
    paths.candidate_file(cid, "plan.json").write_text(plan.model_dump_json(indent=2))

    monkeypatch.setattr(
        package_candidate, "load_state",
        lambda cid: CandidateState(candidate_id=cid, status="FAILED", failure_reason="max_steps_exceeded"),
    )

    pkg = assemble_package(cid)
    assert pkg.final_status == "FAILED"
    assert pkg.trial_log is None
    assert pkg.failure_reason == "max_steps_exceeded"
