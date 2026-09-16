"""Tests for state persistence, atomicity, session continuity and resume."""

from __future__ import annotations

import json

import pytest

from nalin_l1.loop import paths
from nalin_l1.loop.state_store import load_state, save_state
from nalin_l1.schemas import CandidateState


@pytest.fixture
def redirect_paths(tmp_path, monkeypatch):
    def _dir(candidate_id):
        d = tmp_path / candidate_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(paths, "candidate_dir", _dir)
    return tmp_path


def test_state_roundtrip(redirect_paths):
    cid = "resume_state"
    state = CandidateState(candidate_id=cid, status="BUILDING", current_task_index=3)
    save_state(state)
    loaded = load_state(cid)
    assert loaded == state
    assert loaded.current_task_index == 3


def test_load_state_missing_returns_none(redirect_paths):
    assert load_state("does_not_exist") is None


def test_save_state_is_atomic_no_leftover_tmp(redirect_paths):
    cid = "resume_atomic"
    state = CandidateState(candidate_id=cid, status="PACKAGED")
    save_state(state)
    assert (redirect_paths / cid / "state.json").exists()
    assert not (redirect_paths / cid / "state.json.tmp").exists()


def test_resume_preserves_session_and_skips_replan(redirect_paths, monkeypatch):
    from nalin_l1.loop import driver
    from nalin_l1.loop import planner_step, reviewer_step, worker_step
    from nalin_l1.opencode_bridge.invoke import OpencodeResult
    from nalin_l1.schemas import ApprovedCandidate

    cid = "resume_session"

    planner_calls = {"n": 0}

    def planner_fake(agent, prompt, session_id=None, context_files=None, cwd=None):
        planner_calls["n"] += 1
        tasks = [
            {"task_id": "t0", "description": "d", "target_files": ["train.py"], "acceptance": "a"},
            {"task_id": "t1", "description": "d", "target_files": ["train.py"], "acceptance": "a"},
        ]
        return OpencodeResult(
            f"<<<RESULT>>>\n{json.dumps({'candidate_id': cid, 'tasks': tasks})}\n<<<END>>>",
            "", 0, agent, session_id="fake-sess-123",
        )

    monkeypatch.setattr(planner_step, "invoke_opencode", planner_fake)
    monkeypatch.setattr(
        worker_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, fix_instructions=None, cwd=None:
            OpencodeResult("<<<RESULT>>>done<<<END>>>", "", 0, agent),
    )
    monkeypatch.setattr(
        reviewer_step, "invoke_opencode",
        lambda agent, prompt, session_id=None, cwd=None:
            OpencodeResult(
                f"<<<RESULT>>>\n{json.dumps({'task_id':'t','verdict':'PROCEED','reasoning':'r'})}\n<<<END>>>",
                "", 0, agent,
            ),
    )

    cand = ApprovedCandidate(
        candidate_id=cid, hypothesis="h", asset_ids=["asset_a"],
        start="2024-01-01", train_end="2024-02-01", val_end="2024-03-01", end="2024-04-01",
    )

    state1 = driver.run_candidate(cand)
    assert state1.status == "ENGINE_RUN"
    assert state1.session_id == "fake-sess-123"
    assert planner_calls["n"] == 1

    state2 = driver.run_candidate(cand)
    assert state2.session_id == state1.session_id
    assert planner_calls["n"] == 1  # resumed, did not re-plan
