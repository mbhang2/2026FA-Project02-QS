"""Amendment 24: the LLM agent's kernel mode, its prompt and its interface document."""

from __future__ import annotations

import re

import pytest

from src.core.engine import kernel as K
from src.core.loop import llm


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:0")
    monkeypatch.setenv("LITELLM_API_KEY", "test")
    return llm.LLMAgent(model="m", kind="kernel", run_facts="facts")


def test_the_baseline_kernel_is_in_the_language():
    K.check(llm.KERNEL_BASELINE_PATH.read_text())


def test_the_interface_names_every_constant():
    text = llm.KERNEL_DOC_PATHS["api"].read_text()
    missing = [name for name in K.CONSTANTS if not re.search(rf"`{name}`|\b{name}\b", text)]
    assert not missing, missing


def test_the_prompt_never_mentions_the_gate():
    text = llm.KERNEL_TEMPLATE_PATH.read_text().split("## SYSTEM", 1)[1].lower()
    for word in ("p-value", "threshold", "accept", "reject", "bound", "correction", "null"):
        assert word not in text, word


def test_the_kernel_agent_reads_the_kernel_interface(agent):
    assert agent.template_path == llm.KERNEL_TEMPLATE_PATH
    config = agent.config()
    assert config["kind"] == "kernel"
    assert "docs/agent/kernel-api.md" in config["documents_shown"]
    prompt = agent._prompt((), repair="")
    assert "def kernel(event, book, state, orders):" in prompt and "The kernel interface" in prompt
    assert "facts" in prompt and "{" + "api}" not in prompt


def test_the_kernel_agent_checks_kernels_not_strategies(agent):
    assert agent._static_ok(llm.KERNEL_BASELINE_PATH.read_text()) == (True, "")
    ok, message = agent._static_ok("class Strategy:\n    pass\n")
    assert not ok and "kernel" in message


def test_a_strategy_agent_is_unchanged(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:0")
    monkeypatch.setenv("LITELLM_API_KEY", "test")
    plain = llm.LLMAgent(model="m")
    assert plain.template_path == llm.TEMPLATE_PATH and "kind" not in plain.config()


def test_a_blocked_kernel_proposal_is_refused_and_still_counts():
    blocked = llm.BLOCKED_TEMPLATE.format(reason="r", detail="d")
    with pytest.raises(K.KernelRejected):
        K.check(blocked)
    from src.core.loop.campaign import TRIAL_OUTCOMES

    assert "refused" in TRIAL_OUTCOMES
