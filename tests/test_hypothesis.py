"""The campaign's question: linted, hashed, and inert when absent.

Task 46. Three properties are worth more than the rest and each has a test whose
failure means something specific:

1. **A hypothesis cannot tell the agent about the gate.** The lint is the only
   thing standing between a human's free text and a prompt that `loop.md`
   guarantees says nothing about thresholds, p-values, N, validation or the
   holdout.
2. **An absent hypothesis changes nothing.** The `{hypothesis}` slot was added
   to a template whose digest is asserted against Task 36's in
   `scripts/positive_control.py`. The slot is additive and renders empty, so the
   two messages the model receives are byte-identical to Task 36's -- which is
   what lets a hypothesis-less campaign run today still be compared with
   Task 36's. `tests/fixtures/strategy-agent-task45.md` is the template as it
   stood immediately before the slot was added, kept so the claim is checked
   against the real thing rather than against a reconstruction.
3. **The question is pre-registered.** Its digest is in the campaign record, and
   editing the file after registration makes the record fail its own check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.loop import hypothesis as H
from src.core.loop import llm
from src.core.loop.campaign import CampaignRecord, CampaignTampered
from src.core.scoring.integrity import artifact_digests

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK45_TEMPLATE = Path(__file__).parent / "fixtures" / "strategy-agent-task45.md"
COMMITTED = REPO_ROOT / "config" / "hypotheses" / "intraday-continuation.md"


def _agent(**kw):
    import os

    # **Assigned, not `setdefault`.** `setdefault` defers to the shell, which
    # is the opposite of what the docstring above promises: with a live
    # LITELLM_BASE_URL exported, these agents pointed at the REAL gateway.
    # Measured 2026-10-20 once the gateway came back -- the suite went from
    # 136 s to >10 min at 7% CPU, sleeping through TRANSPORT_BACKOFF_S
    # (30, 120, 300, 600, 600) on retries against a live endpoint. A test that
    # behaves differently depending on who is running it is not a test, and
    # one that can spend real tokens is worse. example.invalid cannot resolve,
    # so leaking these values to later tests fails closed.
    os.environ["LITELLM_BASE_URL"] = "https://example.invalid/v1"
    os.environ["LITELLM_API_KEY"] = "not-a-key-and-never-sent"
    return llm.LLMAgent(model="test-model", **kw)


def _messages(rendered: str) -> tuple[str, str]:
    """Exactly the split `LLMAgent._call` performs, and nothing else.

    Duplicated rather than imported because the property under test is about
    what the model receives; a helper shared with the code under test would move
    with it and the test would keep passing while the claim stopped being true.
    """
    system, user = rendered.split("## USER", 1)
    return system.split("## SYSTEM", 1)[-1].strip(), user.strip()


# --------------------------------------------------------------------------- #
# 1. the lint
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,term", [
    ("Check whether it holds out of sample.", "out of sample"),
    ("It should survive on the holdout.", "holdout"),
    ("Confirm on the validation window.", "validation"),
    ("Fit in-sample first.", "in-sample"),
    ("Report a p-value for each.", "p-value"),
    ("Only keep it if the result is significant.", "significant"),
    ("Is the edge statistically real?", "statistically"),
    ("Watch for false discovery.", "false discovery"),
    ("Beware multiple testing here.", "multiple testing"),
    ("Apply a Benjamini-Hochberg step.", "Benjamini"),
    ("Use the deflated Sharpe ratio.", "deflat"),
    ("Keep FWER under control.", "FWER"),
    ("Run with q = 0.05.", "q ="),
    ("Use a 5% alpha level.", "alpha level"),
    ("Aim above the significance-threshold.", "significance-threshold"),
    ("We accept anything above zero.", "accept"),
    ("Reject the weak ones.", "Reject"),
    ("Budget 400 trials.", "400 trials"),
    ("Apply the correction afterwards.", "correction"),
])
def test_the_lint_rejects_gate_vocabulary_and_names_the_term(text, term):
    with pytest.raises(H.HypothesisRejected) as exc:
        H.lint(text)
    assert term.lower() in str(exc.value).lower(), (
        f"the lint rejected {text!r} without naming {term!r}; a linter that "
        "does not say which word it objected to makes a human guess")


@pytest.mark.parametrize("text", [
    "Does an outsized move inside a session continue, or give it back?",
    "Do overnight gaps fill during the first hour of the following session?",
    "Is realised volatility in the opening range informative about the close?",
    "The paper's signal rule uses a shifted zero threshold.",
])
def test_the_lint_passes_ordinary_research_prose(text):
    """Including a bare 'threshold', which is signal vocabulary, not the gate's.

    That case is not hypothetical: it was the only false positive the lint
    produced over the hypothesis prose already in this repo, and narrowing the
    pattern to `significance|acceptance|rejection|decision`-threshold is why it
    now passes.
    """
    H.lint(text)


def test_the_lint_reports_every_term_not_only_the_first():
    found = H.findings("Reject anything that is not significant out of sample.")
    assert len(found) >= 3, found


def test_an_empty_file_is_not_the_same_as_no_hypothesis(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n\n")
    with pytest.raises(H.HypothesisRejected, match="empty"):
        H.load(p)


def test_a_missing_file_says_so_rather_than_defaulting(tmp_path):
    with pytest.raises(FileNotFoundError, match="no hypothesis at"):
        H.load(tmp_path / "absent.md")


def test_the_committed_hypothesis_passes_its_own_lint():
    assert H.findings(COMMITTED.read_text()) == []


def test_every_byte_of_the_file_is_linted():
    """There is no comment syntax that escapes the lint, because there is none
    that escapes the prompt: `block()` pastes the file's text verbatim."""
    assert H.EXEMPT_PREFIXES == ()
    with pytest.raises(H.HypothesisRejected):
        H.lint("<!-- check it out of sample -->")


# --------------------------------------------------------------------------- #
# 2. absent means unchanged -- the property the positive control rests on
# --------------------------------------------------------------------------- #

SLOTS = dict(api="A", context_api="B", data="C", run="D", current="E",
             history="F", repair="G")


def test_no_hypothesis_reproduces_the_task45_messages_byte_for_byte():
    before = _messages(TASK45_TEMPLATE.read_text().format(**SLOTS))
    after = _messages(
        (REPO_ROOT / "config" / "prompts" / "strategy-agent.md")
        .read_text().format(**SLOTS, hypothesis=H.block(None)))
    assert before == after, (
        "adding the {hypothesis} slot moved what the model receives. "
        "scripts/positive_control.py accepts the new template digest ONLY "
        "because this is true; if it stops being true, Task 36's campaigns are "
        "no longer comparable and that constant must not be kept.")


def test_the_template_file_digest_did_change_and_that_is_expected():
    """The file moved even though the messages did not. Asserted so the two
    facts are not confused: the artifact fingerprint is over files."""
    import hashlib

    now = hashlib.sha256(
        (REPO_ROOT / "config" / "prompts" / "strategy-agent.md").read_bytes()
    ).hexdigest()
    task45 = hashlib.sha256(TASK45_TEMPLATE.read_bytes()).hexdigest()
    assert now != task45


def test_a_hypothesis_reaches_the_prompt_verbatim():
    h = H.load(COMMITTED)
    agent = _agent(hypothesis=h)
    prompt = agent._prompt((), repair="")
    _, user = _messages(prompt)
    assert h.text.strip() in user
    assert "The question this campaign is investigating" in user


def test_the_prompt_still_says_nothing_about_the_gate_with_a_hypothesis_in_it():
    from tests.test_llm_agent import GATE_WORDS  # the same list, not a copy

    agent = _agent(hypothesis=H.load(COMMITTED))
    # On the assembled messages, not the rendered file. The template's header
    # comment names every gate word precisely in order to forbid them, and it is
    # never sent -- checking the raw render would fail on the prohibition
    # itself.
    system, user = _messages(agent._prompt((), repair=""))
    sent = (system + "\n" + user).lower()
    assert [w for w in GATE_WORDS if w in sent] == []


# --------------------------------------------------------------------------- #
# 3. pre-registration
# --------------------------------------------------------------------------- #

def test_the_hypothesis_is_in_the_agents_pre_registration():
    cfg = _agent(hypothesis=H.load(COMMITTED)).config()
    assert cfg["hypothesis"]["path"] == "config/hypotheses/intraday-continuation.md"
    assert cfg["hypothesis"]["digest"] == H.load(COMMITTED).digest
    assert "text" not in cfg["hypothesis"], (
        "the record stores the question by reference, not by value; two copies "
        "of a pre-registration is how they come to disagree")


def test_no_hypothesis_records_none_rather_than_omitting_the_field():
    assert _agent().config()["hypothesis"] is None


def test_editing_the_hypothesis_after_registration_fails_the_record(tmp_path):
    src = tmp_path / "h.md"
    src.write_text("Does an outsized move inside a session continue?\n")
    h = H.load(src)

    path = tmp_path / "campaign.json"
    record = CampaignRecord(campaign_id="t", profile="p", budget=5, q=0.05,
                            hypothesis_path=str(src), hypothesis_digest=h.digest)
    record.write(path)
    CampaignRecord.read(path)                      # clean

    src.write_text("Do overnight gaps fill in the first hour?\n")
    reread, _ = CampaignRecord.read(path)          # the record itself is intact
    assert reread.hypothesis_digest != H.load(src).digest, (
        "the record must still name the digest the file had at registration; "
        "that mismatch is the whole alarm")

    body = json.loads(path.read_text())
    body["record"]["hypothesis_digest"] = H.load(src).digest
    path.write_text(json.dumps(body))
    with pytest.raises(CampaignTampered):
        CampaignRecord.read(path)


def test_adding_the_fields_did_not_move_any_committed_records_digest():
    """`digest()` drops None keys, so a record written before Task 46 still
    hashes to what it says. If this fails, every committed campaign reads as
    tampered and the cause is this task, not a tamper."""
    found = sorted(Path(REPO_ROOT / "runs" / "campaigns").rglob("campaign.json"))
    assert found, "no committed campaign records to check"
    for path in found:
        CampaignRecord.read(path)


# --------------------------------------------------------------------------- #
# 4. the artifact set
# --------------------------------------------------------------------------- #

def test_the_hypothesis_is_a_fingerprinted_artifact():
    digests = artifact_digests()
    key = "config/hypotheses/intraday-continuation.md"
    assert key in digests, (
        "a hypothesis that is not in the artifact set is a question that can be "
        "changed between two campaigns without either record noticing")


def test_a_changed_hypothesis_changes_the_artifact_fingerprint(tmp_path):
    root = tmp_path
    (root / "config" / "hypotheses").mkdir(parents=True)
    p = root / "config" / "hypotheses" / "q.md"
    p.write_text("Does an outsized move continue?\n")
    first = artifact_digests(root)
    p.write_text("Do overnight gaps fill?\n")
    assert artifact_digests(root) != first
