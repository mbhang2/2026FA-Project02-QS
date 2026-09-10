"""Workstream B tests: multi-session orchestrator (PLAN_NEXT B.1–B.5)."""

from __future__ import annotations

import json

import pytest

from src.loop import (
    auditor_step,
    committee_step,
    driver,
    paths,
    planner_step,
    prompt_doc,
    reviewer_step,
    statistician_step,
    worker_step,
)
from src.loop.reproducibility import capture_reproducibility
from src.loop.spec_digest import build_spec_digest
from src.opencode_bridge.invoke import OpencodeResult
from src.schemas import (
    ApprovedCandidate,
    AuditorReport,
    CommitteeVerdict,
    StatisticianNote,
    Task,
    TaskPlan,
)

pytestmark = pytest.mark.usefixtures("redirect_candidate_paths")


def _candidate(cid="ms_1") -> ApprovedCandidate:
    return ApprovedCandidate(
        candidate_id=cid,
        hypothesis="20-day momentum: long when close > 20-day SMA.",
        asset_ids=["xauusd"],
        start="2024-01-01",
        train_end="2024-03-01",
        val_end="2024-06-01",
        end="2024-08-01",
    )


def _plan(cid="ms_1") -> TaskPlan:
    return TaskPlan(
        candidate_id=cid,
        tasks=[
            Task(task_id="t0", description="compute signal", target_files=["train.py"], acceptance="a"),
            Task(task_id="t1", description="wire protocol", target_files=["train.py"], acceptance="b"),
        ],
    )


# --- B.1: spec digest ---


def test_spec_digest_is_compact_and_self_contained():
    cand = _candidate()
    digest = build_spec_digest(cand, _plan())
    for needle in ("Spec Digest", cand.candidate_id, "xauusd", "t0", "t1", "train.py", "No lookahead"):
        assert needle in digest
    assert len(digest.split()) < 500


# --- B.2 + B.5: prompts.md ---


def test_prompts_doc_has_common_context_and_one_block_per_task():
    cand = _candidate()
    doc = prompt_doc.build_prompts_doc(cand, _plan())
    assert "COMMON CONTEXT" in doc
    assert "TASK t0" in doc
    assert "TASK t1" in doc
    # self-contained checks
    assert "FINAL SELF-CRITIQUE" in doc
    assert "ANTI-HALLUCINATION" in doc


def test_common_context_references_frozen_contract_and_single_file():
    cand = _candidate()
    ctx = prompt_doc._common_context(cand)
    assert "L1_READY.md" in ctx
    assert "train.py" in ctx
    assert "mid = (bid + ask) / 2" in ctx


def test_driver_writes_digest_and_prompts(monkeypatch):
    cid = "ms_writes"
    cand = _candidate(cid)
    plan_block = f"<<<RESULT>>>\n{json.dumps({'candidate_id': cid, 'tasks': [_plan(cid).tasks[0].model_dump()]})}\n<<<END>>>"

    def fake(agent, prompt, session_id=None, context_files=None, cwd=None):
        return OpencodeResult(stdout=plan_block, stderr="", exit_code=0, agent=agent)

    monkeypatch.setattr(planner_step, "invoke_opencode", fake)
    monkeypatch.setattr(
        worker_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, context_files=None, cwd=None:
            OpencodeResult("<<<RESULT>>>done<<<END>>>", "", 0, agent),
    )
    monkeypatch.setattr(
        reviewer_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, cwd=None:
            OpencodeResult("<<<RESULT>>>\n" + json.dumps({"task_id": "t0", "verdict": "PROCEED", "reasoning": "r"}) + "\n<<<END>>>", "", 0, agent),
    )

    state = driver.run_candidate(cand)
    assert state.status == "ENGINE_RUN"
    assert paths.candidate_file(cid, "spec_digest.md").exists()
    assert paths.candidate_file(cid, "prompts.md").exists()
    assert paths.candidate_file(cid, "plan.json").exists()


# --- B.3: fresh session ---


def test_worker_invoked_in_fresh_session(monkeypatch):
    cid = "ms_fresh"
    captured = {}

    def fake(agent, prompt, session_id=None, context_files=None, cwd=None):
        captured["session_id"] = session_id
        return OpencodeResult("<<<RESULT>>>done<<<END>>>", "", 0, agent)

    monkeypatch.setattr(worker_step, "invoke_opencode", fake)
    paths.candidate_file(cid, "spec_digest.md").write_text("digest")
    worker_step.run_worker_step(_plan(cid).tasks[0], cid)
    assert captured["session_id"] is None


def test_reviewer_invoked_in_fresh_session(monkeypatch):
    cid = "ms_fresh_rev"
    captured = {}

    def fake(agent, prompt, session_id=None, cwd=None):
        captured["session_id"] = session_id
        return OpencodeResult(
            "<<<RESULT>>>\n" + json.dumps({"task_id": "t0", "verdict": "PROCEED", "reasoning": "r"}) + "\n<<<END>>>",
            "", 0, agent,
        )

    monkeypatch.setattr(reviewer_step, "invoke_opencode", fake)
    reviewer_step.run_reviewer_step(_plan(cid).tasks[0], "summary", _candidate(cid), _plan(cid))
    assert captured["session_id"] is None


# --- B.4: staged post-engine roles ---


def test_statistician_step_parses_and_writes(monkeypatch):
    cid = "ms_stat"
    monkeypatch.setattr(
        statistician_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, context_files=None, cwd=None:
            OpencodeResult("<<<RESULT>>>\n" + json.dumps({"candidate_id": cid, "summary": "s", "concerns": ["c1"]}) + "\n<<<END>>>", "", 0, agent),
    )
    paths.candidate_file(cid, "trial_log.json").write_text("{}")
    note = statistician_step.run_statistician_step(_candidate(cid))
    assert isinstance(note, StatisticianNote)
    assert note.concerns == ["c1"]
    assert paths.candidate_file(cid, "statistician_note.json").exists()


def test_auditor_step_parses_and_writes(monkeypatch):
    cid = "ms_audit"
    payload = {
        "candidate_id": cid,
        "findings": [
            {"axis": "lookahead", "verdict": "pass", "detail": "clean"},
            {"axis": "overfitting", "verdict": "flag", "detail": "short window"},
        ],
    }
    monkeypatch.setattr(
        auditor_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, context_files=None, cwd=None:
            OpencodeResult("<<<RESULT>>>\n" + json.dumps(payload) + "\n<<<END>>>", "", 0, agent),
    )
    report = auditor_step.run_auditor_step(_candidate(cid))
    assert isinstance(report, AuditorReport)
    assert report.leak_veto is False
    assert paths.candidate_file(cid, "auditor_report.json").exists()


def test_committee_step_parses_and_writes(monkeypatch):
    cid = "ms_cmte"
    monkeypatch.setattr(
        committee_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, context_files=None, cwd=None:
            OpencodeResult("<<<RESULT>>>\n" + json.dumps({"candidate_id": cid, "verdict": "PASS", "reasoning": "ok"}) + "\n<<<END>>>", "", 0, agent),
    )
    verdict = committee_step.run_committee_step(_candidate(cid))
    assert isinstance(verdict, CommitteeVerdict)
    assert verdict.verdict == "PASS"
    assert paths.candidate_file(cid, "committee_verdict.json").exists()


# --- B.5: reproducibility metadata ---


def test_reproducibility_capture_has_required_keys():
    repro = capture_reproducibility(_candidate("ms_repro"))
    for key in ("random_seed", "git_commit", "execution_time", "python_version",
                "key_dependencies", "dataset_hash", "config"):
        assert key in repro
    assert repro["config"]["asset_ids"] == ["xauusd"]
