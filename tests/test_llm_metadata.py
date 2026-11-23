"""LiteLLM metadata says where a call sits and carries nothing from its content."""

from __future__ import annotations

import importlib

import pytest

from audit.committee import llm as committee
from src.core.loop import llm as loop


@pytest.mark.parametrize("module", [loop, committee])
def test_metadata_has_the_call_context(module, monkeypatch):
    monkeypatch.setenv("PRACTICUM_LLM_CAMPAIGN", "btc-cvd-01")
    m = module.call_metadata("probe_repair", trial=7, attempt=2)
    assert (m["project"], m["campaign"], m["stage"], m["trial"], m["attempt"]) == (
        "practicum", "btc-cvd-01", "probe_repair", "7", "2")
    assert {"campaign:btc-cvd-01", "stage:probe_repair", "trial:7", "attempt:2"} <= set(m["tags"])


@pytest.mark.parametrize("module", [loop, committee])
def test_missing_context_is_left_out(module, monkeypatch):
    monkeypatch.delenv("PRACTICUM_LLM_CAMPAIGN", raising=False)
    m = module.call_metadata("propose")
    assert "campaign" not in m and "trial" not in m and "attempt" not in m


@pytest.mark.parametrize("module", [loop, committee])
def test_a_value_cannot_carry_text_or_grow(module, monkeypatch):
    monkeypatch.setenv("PRACTICUM_LLM_CAMPAIGN", "a b\nsecret=" + "x" * 200)
    value = module.call_metadata("stage")["campaign"]
    assert len(value) <= 80 and " " not in value and "\n" not in value and "=" not in value


class _Reply:
    status_code = 200
    text = ""

    def __init__(self, model):
        self.model = model
        self.headers = {}

    def json(self):
        return {"model": self.model, "usage": {},
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}


def test_the_committee_client_sends_its_stage(monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(json)
        return _Reply(json["model"])

    import requests
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "not-a-real-key")
    token = committee.CALL_STAGE.set("committee:leakage")
    try:
        committee.Client(model="m").ask("SYSTEM TEXT", "USER TEXT")
    finally:
        committee.CALL_STAGE.reset(token)
    assert seen["metadata"]["stage"] == "committee:leakage"
    assert "SYSTEM TEXT" not in str(seen["metadata"]) and "USER TEXT" not in str(seen["metadata"])
    assert [m["content"] for m in seen["messages"]] == ["SYSTEM TEXT", "USER TEXT"]


def test_the_agent_config_does_not_record_the_tags():
    fields = loop.LLMAgent.__dataclass_fields__
    assert fields["tag_trial"].init is False and fields["_tag_attempt"].init is False
