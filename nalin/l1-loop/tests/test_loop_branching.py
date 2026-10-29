"""Tests for the driver loop branching: PROCEED / MINOR_FIX / REPLAN (Tasks 4.5, 4.6)."""

from __future__ import annotations

import json

import pytest

from src.loop import driver, planner_step, reviewer_step, worker_step
from src.opencode_bridge.invoke import OpencodeResult
from src.schemas import ApprovedCandidate, ReviewVerdict

pytestmark = pytest.mark.usefixtures("redirect_candidate_paths")


def _candidate(candidate_id: str) -> ApprovedCandidate:
    return ApprovedCandidate(
        candidate_id=candidate_id,
        hypothesis="20-day momentum",
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
    )


def _plan_block(tasks: list[dict]) -> str:
    payload = {"candidate_id": "x", "tasks": tasks}
    return f"<<<RESULT>>>\n{json.dumps(payload)}\n<<<END>>>"


def _verdict_block(verdict: str, task_id: str, *, extra: dict | None = None) -> str:
    data = {"task_id": task_id, "verdict": verdict, "reasoning": "r"}
    if verdict == "MINOR_FIX":
        data["fix_instructions"] = "fix it"
    if verdict == "REPLAN":
        data["replan_reasoning"] = "plan is wrong"
    if extra:
        data.update(extra)
    return f"<<<RESULT>>>\n{json.dumps(data)}\n<<<END>>>"


def _three_tasks():
    return [
        {"task_id": "t0", "description": "d", "target_files": ["train.py"], "acceptance": "a"},
        {"task_id": "t1", "description": "d", "target_files": ["train.py"], "acceptance": "a"},
        {"task_id": "t2", "description": "d", "target_files": ["train.py"], "acceptance": "a"},
    ]


def _mock_agent(monkeypatch, agent_module, stdout: str):
    def fake(agent, prompt, session_id=None, context_files=None, cwd=None):
        return OpencodeResult(stdout=stdout, stderr="", exit_code=0, agent=agent)

    monkeypatch.setattr(agent_module, "invoke_opencode", fake)


def test_full_proceed_plan_reaches_engine_run(monkeypatch):
    cid = "branch_proceed"
    _mock_agent(monkeypatch, planner_step, _plan_block(_three_tasks()))
    _mock_agent(monkeypatch, worker_step, "<<<RESULT>>>done<<<END>>>")
    _mock_agent(monkeypatch, reviewer_step, _verdict_block("PROCEED", "t0"))

    state = driver.run_candidate(_candidate(cid))
    assert state.status == "ENGINE_RUN"
    assert state.current_task_index == 3


def test_minor_fix_retries_then_proceeds(monkeypatch):
    cid = "branch_minorfix"
    _mock_agent(monkeypatch, planner_step, _plan_block(_three_tasks()[:1]))

    worker_prompts = []

    def worker_fake(agent, prompt, session_id=None, cwd=None):
        worker_prompts.append(prompt)
        return OpencodeResult("", "", 0, agent)

    monkeypatch.setattr(worker_step, "invoke_opencode", worker_fake)

    reviewer_calls = {"n": 0}

    def reviewer_fake(agent, prompt, session_id=None, cwd=None):
        reviewer_calls["n"] += 1
        if reviewer_calls["n"] == 1:
            return OpencodeResult(_verdict_block("MINOR_FIX", "t0"), "", 0, agent)
        return OpencodeResult(_verdict_block("PROCEED", "t0"), "", 0, agent)

    monkeypatch.setattr(reviewer_step, "invoke_opencode", reviewer_fake)

    state = driver.run_candidate(_candidate(cid))
    assert state.status == "ENGINE_RUN"
    assert len(worker_prompts) == 2
    assert "fix it" in worker_prompts[1]


def test_replan_splices_new_tasks(monkeypatch):
    cid = "branch_replan"

    planner_calls = {"n": 0}

    def planner_fake(agent, prompt, session_id=None, context_files=None, cwd=None):
        planner_calls["n"] += 1
        if planner_calls["n"] == 1:
            return OpencodeResult(_plan_block(_three_tasks()[:1]), "", 0, agent)
        return OpencodeResult(_plan_block(_three_tasks()[1:2]), "", 0, agent)

    monkeypatch.setattr(planner_step, "invoke_opencode", planner_fake)
    _mock_agent(monkeypatch, worker_step, "<<<RESULT>>>done<<<END>>>")

    reviewer_calls = {"n": 0}

    def reviewer_fake(agent, prompt, session_id=None, cwd=None):
        reviewer_calls["n"] += 1
        if reviewer_calls["n"] == 1:
            return OpencodeResult(_verdict_block("REPLAN", "t0"), "", 0, agent)
        return OpencodeResult(_verdict_block("PROCEED", "t1"), "", 0, agent)

    monkeypatch.setattr(reviewer_step, "invoke_opencode", reviewer_fake)

    state = driver.run_candidate(_candidate(cid))
    assert state.replan_count == 1
    assert state.status == "ENGINE_RUN"


def test_infinite_minor_fix_hits_max_retries(monkeypatch):
    cid = "branch_maxretry"
    _mock_agent(monkeypatch, planner_step, _plan_block(_three_tasks()[:1]))
    _mock_agent(monkeypatch, worker_step, "<<<RESULT>>>done<<<END>>>")
    _mock_agent(monkeypatch, reviewer_step, _verdict_block("MINOR_FIX", "t0"))

    state = driver.run_candidate(_candidate(cid))
    assert state.status == "FAILED"
    assert state.failure_reason == "max_retries_exceeded"
