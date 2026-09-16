"""Tests for structured output extraction, parse-and-retry, and the bridge (Tasks 2.1, 2.2, 3.1, 3.3)."""

from __future__ import annotations

import pytest

from nalin_l1.opencode_bridge.invoke import (
    OpencodeInvocationError,
    OpencodeResult,
    ResultExtractionError,
    build_opencode_args,
    extract_result_block,
    invoke_opencode,
)
from nalin_l1.opencode_bridge.parse import ParseRetryError, parse_or_retry
from nalin_l1.schemas import TaskPlan


CLEAN = "some preamble\n<<<RESULT>>>\n{\"candidate_id\": \"c1\", \"tasks\": []}\n<<<END>>>\ntrailing"
MISSING = "no result block here at all"
DUPLICATED = "<<<RESULT>>>one<<<END>>> <<<RESULT>>>two<<<END>>>"


def test_extract_clean():
    content = extract_result_block(CLEAN)
    assert content == '{"candidate_id": "c1", "tasks": []}'


def test_extract_missing_raises():
    with pytest.raises(ResultExtractionError):
        extract_result_block(MISSING)


def test_extract_duplicated_raises():
    with pytest.raises(ResultExtractionError):
        extract_result_block(DUPLICATED)


def _valid_taskplan_raw() -> str:
    return (
        "<<<RESULT>>>\n"
        '{"candidate_id": "c1", "tasks": ['
        '{"task_id": "t1", "description": "d", "target_files": ["train.py"], "acceptance": "a"}'
        "]}\n<<<END>>>"
    )


_MALFORMED = "<<<RESULT>>>\n{\"candidate_id\": \"c1\"}\n<<<END>>>"


def test_parse_or_retry_succeeds_on_retry():
    calls = []

    def retry_fn(exc):
        calls.append(exc)
        return _valid_taskplan_raw()

    plan = parse_or_retry(_MALFORMED, TaskPlan, retry_fn, max_retries=2)
    assert isinstance(plan, TaskPlan)
    assert len(calls) == 1


def test_parse_or_retry_raises_after_max_retries():
    def retry_fn(exc):
        return _MALFORMED

    with pytest.raises(ParseRetryError):
        parse_or_retry(_MALFORMED, TaskPlan, retry_fn, max_retries=2)


# --- Task 3.1: invocation wrapper ---


def test_build_opencode_args_with_all_options():
    args = build_opencode_args(
        "planner", "make a plan", "sess-123", ["a.md", "b.md"]
    )
    assert args == [
        "opencode",
        "run",
        "--auto",
        "--format",
        "json",
        "--agent",
        "planner",
        "--session",
        "sess-123",
        "make a plan",
        "-f",
        "a.md",
        "-f",
        "b.md",
    ]


def test_build_opencode_args_no_session_no_files():
    args = build_opencode_args("worker", "do it", None, [])
    assert args == [
        "opencode",
        "run",
        "--auto",
        "--format",
        "json",
        "--agent",
        "worker",
        "do it",
    ]


from pathlib import Path

def test_invoke_opencode_passes_expected_args(monkeypatch):
    import subprocess

    captured = {}

    def fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, "hello", "err")

    monkeypatch.setattr("nalin_l1.opencode_bridge.invoke.subprocess.run", fake_run)
    result = invoke_opencode(
        "planner", "p", session_id="s", context_files=["x.md"], cwd="/tmp"
    )
    assert Path(captured["cmd"][0]).stem == "opencode"
    assert captured["cmd"][1:] == [
        "run",
        "--auto",
        "--format",
        "json",
        "--agent",
        "planner",
        "--session",
        "s",
        "p",
        "-f",
        "x.md",
    ]
    assert captured["cwd"] == "/tmp"
    assert isinstance(result, OpencodeResult)
    assert result.exit_code == 0


def test_invoke_parses_session_id_and_text(monkeypatch):
    import subprocess

    events = (
        '{"type":"step_start","sessionID":"ses_abc123","part":{"type":"step-start"}}\n'
        '{"type":"text","sessionID":"ses_abc123","part":{"type":"text","text":"hello"}}\n'
        '{"type":"step_finish","sessionID":"ses_abc123","part":{"type":"step-finish"}}\n'
    )

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, events, "")

    monkeypatch.setattr("nalin_l1.opencode_bridge.invoke.subprocess.run", fake_run)
    result = invoke_opencode("planner", "p")
    assert result.session_id == "ses_abc123"
    assert result.stdout == "hello"


# --- Task 3.3: bridge error handling ---


def test_invoke_opencode_raises_on_nonzero_exit(monkeypatch):
    import subprocess

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr("nalin_l1.opencode_bridge.invoke.subprocess.run", fake_run)
    with pytest.raises(OpencodeInvocationError) as exc:
        invoke_opencode("planner", "p")
    assert "boom" in str(exc.value)
