"""Streamed proposer calls, cheap repairs, and a probe that runs the proposal."""

from __future__ import annotations

import json
import subprocess

import pytest
import requests

from src.core.loop import llm, probe

PROMPT = "## SYSTEM\nS\n## USER\nU"
FILE = "```python\nx = 1\n```"


class Stream:
    status_code = 200
    text = ""
    headers = {"content-type": "text/event-stream"}

    def __init__(self, model="m", text=FILE, finish="stop", error=None):
        chunks = [{"model": model, "choices": [{"delta": {"reasoning_content": "t"}}]}]
        chunks += [{"model": model, "choices": [{"delta": {"content": c}}]} for c in text]
        if error:
            chunks.append({"error": error})
        if finish:
            chunks.append({"model": model, "choices": [{"delta": {}, "finish_reason": finish}]})
            chunks.append({"model": model, "choices": [], "usage": {
                "prompt_tokens": 5, "completion_tokens": 9,
                "completion_tokens_details": {"reasoning_tokens": 2}}})
        self.lines = [b"data: " + json.dumps(c).encode() for c in chunks]
        if finish:
            self.lines.append(b"data: [DONE]")

    def iter_lines(self):
        yield from self.lines

    def close(self):
        pass


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "not-a-real-key")
    return llm.LLMAgent(model="m", base_url="http://example.invalid/v1",
                        repair_reasoning_effort="low")


def post_returning(monkeypatch, *replies):
    sent = []
    queue = list(replies)

    def post(url, json=None, headers=None, timeout=None, stream=False):
        sent.append({"body": json, "timeout": timeout, "stream": stream})
        return queue.pop(0)

    monkeypatch.setattr(requests, "post", post)
    return sent


def test_a_stream_is_assembled_with_its_usage(agent, monkeypatch):
    sent = post_returning(monkeypatch, Stream())
    assert agent._one_attempt(PROMPT, "propose") == "x = 1\n"
    call = agent._pending[-1]
    assert (call.prompt_tokens, call.completion_tokens, call.reasoning_tokens) == (5, 9, 2)
    assert sent[0]["stream"] is True and sent[0]["body"]["stream"] is True
    assert sent[0]["timeout"] == (llm.STREAM_CONNECT_TIMEOUT_S, llm.STREAM_IDLE_TIMEOUT_S)


@pytest.mark.parametrize("reply", [Stream(finish=None), Stream(error="boom")])
def test_a_broken_stream_is_a_transport_failure(agent, monkeypatch, reply):
    post_returning(monkeypatch, reply)
    with pytest.raises(llm.LLMTransportError):
        agent._one_attempt(PROMPT, "propose")
    assert agent._pending[-1].ok is False


def test_only_repairs_are_cheap(agent, monkeypatch):
    sent = post_returning(monkeypatch, Stream(), Stream())
    agent._one_attempt(PROMPT, "propose")
    agent._one_attempt(PROMPT, "probe_repair")
    assert "reasoning_effort" not in sent[0]["body"]
    assert llm.REPAIR_FOCUS not in sent[0]["body"]["messages"][1]["content"]
    assert sent[1]["body"]["reasoning_effort"] == "low"
    assert llm.REPAIR_FOCUS in sent[1]["body"]["messages"][1]["content"]


def test_a_campaign_without_the_setting_is_unchanged(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "not-a-real-key")
    a = llm.LLMAgent(model="m", base_url="http://example.invalid/v1")
    assert "repair_reasoning_effort" not in a.config()
    sent = post_returning(monkeypatch, Stream())
    a._one_attempt(PROMPT, "probe_repair")
    assert "reasoning_effort" not in sent[0]["body"]
    assert sent[0]["body"]["messages"][1]["content"] == "U"


def test_a_rerouted_effort_is_asked_again_without_it(agent, monkeypatch):
    sent = post_returning(monkeypatch, Stream(model="other"), Stream())
    assert agent._one_attempt(PROMPT, "repair") == "x = 1\n"
    assert "reasoning_effort" in sent[0]["body"] and "reasoning_effort" not in sent[1]["body"]


def test_a_second_substitution_still_stops_the_campaign(agent, monkeypatch):
    post_returning(monkeypatch, Stream(model="other"), Stream(model="other"))
    with pytest.raises(llm.ModelSubstituted):
        agent._one_attempt(PROMPT, "repair")


def _worker(tmp_path, name):
    path = tmp_path / name / "src" / "strategies" / "current.py"
    path.parent.mkdir(parents=True)
    (tmp_path / name / "run.py").write_text("")
    (tmp_path / name / "pyproject.toml").write_text("")
    path.write_text("baseline\n")
    return path


def test_the_probe_runs_in_the_workers_tree(tmp_path, monkeypatch):
    path = _worker(tmp_path, "w0")
    ledger = tmp_path / "probe-ledger.jsonl"
    seen = {}

    def run(argv, capture_output, text, cwd):
        seen["cwd"], seen["file"] = cwd, path.read_text()
        digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()[:16]
        with ledger.open("a") as f:
            f.write(json.dumps({"strategy_digest": "another-worker"}) + "\n")
            f.write(json.dumps({"strategy_digest": digest, "mine": True}) + "\n")
            f.write(json.dumps({"strategy_digest": "another-worker"}) + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    row = probe.run_on_profile("proposal\n", "agent", ledger=ledger, strategy_path=path)
    assert seen == {"cwd": str((tmp_path / "w0").resolve()), "file": "proposal\n"}
    assert row["mine"] is True
    assert path.read_text() == "baseline\n"
