"""Cross-campaign constraints: store, compiler, frozen snapshot, prompt slot.

The proposer may see prior *constraints* (closed families, harness rules).
It must not see prior *results* (scores, parameter pairs, date windows,
near-misses). Empty `block(None)` keeps the `{knowledge}` slot additive so a
knowledge-less campaign still matches Task 45's messages.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.core.loop import knowledge as K
from src.core.loop.campaign import CampaignRecord, CampaignTampered
from src.core.loop.hypothesis import block as hypothesis_block
from src.core.scoring.integrity import ARTIFACT_GLOBS, artifact_digests

REPO_ROOT = Path(__file__).resolve().parents[1]
LLM_SRC = REPO_ROOT / "src" / "core" / "loop" / "llm.py"


def _agent(**kw):
    import os
    from src.core.loop import llm

    os.environ.setdefault("LITELLM_BASE_URL", "https://example.invalid/v1")
    os.environ.setdefault("LITELLM_API_KEY", "not-a-key-and-never-sent")
    return llm.LLMAgent(model="test-model", **kw)


def _messages(rendered: str) -> tuple[str, str]:
    system, user = rendered.split("## USER", 1)
    return system.split("## SYSTEM", 1)[-1].strip(), user.strip()


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #

def test_the_committed_store_passes_its_own_check():
    assert K.check_store() == []
    assert K.check_campaign_index() == []


def test_visible_to_agent_true_is_refused():
    row = {
        "id": "kf-x", "kind": "closed_family", "status": "closed",
        "claim": "A mechanism that is already closed.",
        "dataset": "nq-trades", "split": "train",
        "visible_to_agent": True,
    }
    errors = K.check_row(row, ids={"kf-x"})
    assert any("visible_to_agent" in e for e in errors), errors


def test_near_miss_requires_how_it_died():
    row = {
        "id": "kf-x", "kind": "near_miss", "status": "unconfirmed",
        "claim": "Looked good on train.",
        "dataset": "nq-trades", "split": "train", "campaign_id": "nq-mvp-01",
        "visible_to_agent": False,
    }
    errors = K.check_row(row, ids={"kf-x"})
    assert any("how_it_died" in e for e in errors), errors


# --------------------------------------------------------------------------- #
# compiler
# --------------------------------------------------------------------------- #

def test_compile_emits_closed_families_and_harness_rules_for_nq():
    text = K.compile_view(dataset="nq-trades")
    assert "### Constraints carried from prior research" in text
    assert "GEX node attraction" in text
    assert "Sequence models" in text
    assert "Two-stage confirmation" in text
    assert "second confirming step" in text
    assert "Do not raise the objective by trading less" in text
    assert "calendar dates" in text
    assert "ctx.history()" in text
    assert "fills are identical" in text


def test_compile_drops_near_misses_and_indexes():
    text = K.compile_view(dataset="nq-trades")
    assert "converges on almost no trading" not in text
    assert "nq-mvp-01" not in text
    assert "config/hypotheses/" not in text
    assert "Turnover collapsed" not in text


def test_compile_refuses_a_parameter_pair_in_a_closed_claim():
    rows = [{
        "id": "kf-leak", "kind": "closed_family", "status": "closed",
        "claim": "Momentum 20/50 looked good on this dataset.",
        "dataset": "nq-trades", "split": "train",
        "visible_to_agent": False,
    }]
    with pytest.raises(K.KnowledgeRejected, match="parameter pair"):
        K.compile_view(rows, dataset="nq-trades")


def test_compile_refuses_a_calendar_window_in_a_rule():
    rows = [{
        "id": "kf-leak", "kind": "harness_lesson", "status": "harness",
        "claim": "An indicator was concentrated.",
        "rule_for_agent": "Prefer the rule that worked from March to June 2025.",
        "visible_to_agent": False,
    }]
    with pytest.raises(K.KnowledgeRejected, match="calendar window"):
        K.compile_view(rows, dataset="nq-trades")


def test_compile_is_byte_stable():
    assert K.compile_view(dataset="nq-trades") == K.compile_view(dataset="nq-trades")


def test_compile_for_an_unknown_dataset_still_emits_harness_rules():
    text = K.compile_view(dataset="no-such-dataset")
    assert "GEX node attraction" not in text
    assert "Do not raise the objective by trading less" in text


# --------------------------------------------------------------------------- #
# prompt slot
# --------------------------------------------------------------------------- #

def test_block_none_is_empty():
    assert K.block(None) == ""
    assert K.block(K.KnowledgeView(path=None, text="", digest=K.digest_text(""))) == ""


def test_empty_knowledge_keeps_the_task45_messages():
    """Same claim as test_hypothesis.test_no_hypothesis_reproduces_...,
    with the knowledge slot filled by an empty view."""
    slots = dict(api="A", context_api="B", data="C", run="D", current="E",
                 history="F", repair="G")
    task45 = REPO_ROOT / "tests" / "fixtures" / "strategy-agent-task45.md"
    before = _messages(task45.read_text().format(**slots))
    after = _messages(
        (REPO_ROOT / "config" / "prompts" / "strategy-agent.md")
        .read_text().format(
            **slots, hypothesis=hypothesis_block(None),
            knowledge=K.block(None)))
    assert before == after


def test_a_compiled_view_reaches_the_prompt():
    view = K.KnowledgeView(
        path=None, text=K.compile_view(dataset="nq-trades"),
        digest=K.digest_text(K.compile_view(dataset="nq-trades")))
    agent = _agent(knowledge=view)
    _, user = _messages(agent._prompt((), repair=""))
    assert "Constraints carried from prior research" in user
    assert "Do not re-explore" in user


def test_the_prompt_with_knowledge_still_says_nothing_about_the_gate():
    # Same list as tests/test_llm_agent.py GATE_WORDS, inlined so this module
    # does not depend on pytest's package layout.
    gate_words = (
        "p-value", "p value", "false-acceptance", "false acceptance",
        "benjamini", "hochberg", "holm", "westfall", "fwer", "fdr",
        "multiple testing", "multiple-testing", "multiple-comparison",
        "accept/reject", "rejected by the gate", "deflated sharpe",
        "null distribution", "bonferroni", "family-wise", "familywise",
    )

    view = K.KnowledgeView(
        path=None, text=K.compile_view(dataset="nq-trades"),
        digest=K.digest_text(K.compile_view(dataset="nq-trades")))
    agent = _agent(knowledge=view)
    system, user = _messages(agent._prompt((), repair=""))
    sent = (system + "\n" + user).lower()
    assert [w for w in gate_words if w in sent] == []


def test_knowledge_is_in_the_agents_pre_registration():
    text = K.compile_view(dataset="nq-trades")
    view = K.KnowledgeView(path=None, text=text, digest=K.digest_text(text))
    cfg = _agent(knowledge=view).config()
    assert cfg["knowledge"]["digest"] == view.digest
    assert cfg["shows_the_gate"] is False


def test_no_knowledge_records_none():
    assert _agent().config()["knowledge"] is None


# --------------------------------------------------------------------------- #
# freeze / artifacts
# --------------------------------------------------------------------------- #

def test_editing_the_snapshot_after_registration_fails_the_record(tmp_path):
    path = tmp_path / "campaign.json"
    snap = tmp_path / "knowledge.md"
    view = K.write_view(snap, dataset="nq-trades")
    record = CampaignRecord(
        campaign_id="t", profile="p", budget=5, q=0.05,
        knowledge_path=str(snap), knowledge_digest=view.digest)
    record.write(path)
    CampaignRecord.read(path)

    snap.write_text(snap.read_text() + "\n- extra\n")
    reread, _ = CampaignRecord.read(path)
    assert reread.knowledge_digest != K.digest_text(snap.read_text())

    body = json.loads(path.read_text())
    body["record"]["knowledge_digest"] = K.digest_text(snap.read_text())
    path.write_text(json.dumps(body))
    with pytest.raises(CampaignTampered):
        CampaignRecord.read(path)


def test_the_store_is_not_an_artifact():
    """Editing docs/knowledge during a run must not invalidate a trial.
    The freeze is the campaign-local copy + campaign-record digest."""
    assert not any("knowledge" in g for g in ARTIFACT_GLOBS)
    assert not any(k.startswith("docs/knowledge/") for k in artifact_digests())


def test_llm_does_not_name_the_store():
    src = LLM_SRC.read_text()
    assert "log.jsonl" not in src
    assert "docs/knowledge" not in src


def test_knowledge_check_script_exits_zero():
    r = subprocess.run(
        [sys.executable, "scripts/knowledge_check.py"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
