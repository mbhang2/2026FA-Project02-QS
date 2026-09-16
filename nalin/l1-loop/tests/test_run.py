"""Tests for the lightweight runner (src/loop/run.py)."""

from __future__ import annotations

import pytest

from src.loop import run
from src.schemas import ApprovedCandidate


def _candidate(cid="run_test"):
    return ApprovedCandidate(
        candidate_id=cid,
        hypothesis="h",
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
    )


def test_slug_sanitizes():
    assert run._slug("20-Day Momentum Strategy!") == "20_day_momentum_strategy"


def test_auto_candidate_id_is_unique():
    a = run.auto_candidate_id("momentum")
    b = run.auto_candidate_id("momentum")
    assert a != b
    assert a.startswith("momentum_")


def test_run_full_success_path(monkeypatch):
    from src.schemas import CandidateState, CandidatePackage, TaskPlan

    cid = "run_success"

    monkeypatch.setattr(
        run, "run_candidate", lambda c: CandidateState(candidate_id=cid, status="ENGINE_RUN")
    )
    monkeypatch.setattr(run, "verify_strategy_conformance", lambda c: True)
    monkeypatch.setattr(run, "run_engine_for_candidate", lambda c: {"composite_score": 1.0})

    captured = {}

    def fake_assemble(c):
        captured["called"] = True
        return CandidatePackage(
            candidate=_candidate(cid),
            plan=TaskPlan(candidate_id=cid, tasks=[]),
            review_log=[],
            trial_log={"result": {"composite_score": 1.0}},
            final_status="PASSED_TO_COMMITTEE",
        )

    monkeypatch.setattr(run, "assemble_package", fake_assemble)
    saved = {}
    monkeypatch.setattr(run, "save_state", lambda s: saved.__setitem__("status", s.status))

    pkg = run.run_full(_candidate(cid))
    assert pkg.final_status == "PASSED_TO_COMMITTEE"
    assert saved["status"] == "PACKAGED"


def test_run_full_conformance_failure(monkeypatch):
    from src.schemas import CandidateState, CandidatePackage, TaskPlan

    cid = "run_conf_fail"
    monkeypatch.setattr(
        run, "run_candidate", lambda c: CandidateState(candidate_id=cid, status="ENGINE_RUN")
    )
    monkeypatch.setattr(run, "verify_strategy_conformance", lambda c: False)

    saved = {}
    monkeypatch.setattr(run, "save_state", lambda s: saved.__setitem__("reason", s.failure_reason))
    monkeypatch.setattr(
        run, "assemble_package",
        lambda c: CandidatePackage(
            candidate=_candidate(cid), plan=TaskPlan(candidate_id=cid, tasks=[]),
            review_log=[], final_status="FAILED", failure_reason="strategy_non_conforming",
        ),
    )

    pkg = run.run_full(_candidate(cid))
    assert pkg.final_status == "FAILED"
    assert saved["reason"] == "strategy_non_conforming"


def test_run_full_engine_error(monkeypatch):
    from src.schemas import CandidateState, CandidatePackage, TaskPlan

    cid = "run_engine_err"
    monkeypatch.setattr(
        run, "run_candidate", lambda c: CandidateState(candidate_id=cid, status="ENGINE_RUN")
    )
    monkeypatch.setattr(run, "verify_strategy_conformance", lambda c: True)

    def boom(c):
        raise run.EngineRunError("engine_error: nope")

    monkeypatch.setattr(run, "run_engine_for_candidate", boom)

    saved = {}
    monkeypatch.setattr(run, "save_state", lambda s: saved.__setitem__("reason", s.failure_reason))
    monkeypatch.setattr(
        run, "assemble_package",
        lambda c: CandidatePackage(
            candidate=_candidate(cid), plan=TaskPlan(candidate_id=cid, tasks=[]),
            review_log=[], final_status="FAILED", failure_reason="engine_error: nope",
        ),
    )

    pkg = run.run_full(_candidate(cid))
    assert saved["reason"] == "engine_error: nope"


def test_run_full_build_failure(monkeypatch):
    from src.schemas import CandidateState, CandidatePackage, TaskPlan

    cid = "run_build_fail"
    monkeypatch.setattr(
        run, "run_candidate",
        lambda c: CandidateState(candidate_id=cid, status="FAILED", failure_reason="max_steps_exceeded"),
    )
    monkeypatch.setattr(
        run, "assemble_package",
        lambda c: CandidatePackage(
            candidate=_candidate(cid), plan=TaskPlan(candidate_id=cid, tasks=[]),
            review_log=[], final_status="FAILED", failure_reason="max_steps_exceeded",
        ),
    )
    pkg = run.run_full(_candidate(cid))
    assert pkg.final_status == "FAILED"


def test_cli_help_and_input_plumbing(tmp_path, monkeypatch, capsys):
    # hypothesis-file
    hf = tmp_path / "h.md"
    hf.write_text("multi\nline\nhypothesis")
    ns = argparse_ns(hypothesis_file=str(hf), hypothesis=None, stdin=False)
    assert run._load_hypothesis(ns) == "multi\nline\nhypothesis"


def argparse_ns(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)
