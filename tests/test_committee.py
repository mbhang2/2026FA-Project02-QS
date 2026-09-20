"""The four-auditor committee: isolated, veto-only, and outside the harness.

Everything here is structural and runs offline. The committee's *judgement*
cannot be unit-tested -- it is a language model -- so what is tested is the shape
that constrains it: what each auditor can see, what verdicts exist, and what the
aggregate can and cannot say.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from audit.committee import AXES, AuditorVerdict, CommitteeVerdict
from audit.committee.review import _slice, _prompt, prompt_digests

REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCE = '"""A candidate."""\n\nclass Strategy:\n    SECRET_MARKER = 1\n'
HYPOTHESIS = "# HYPOTHESIS_MARKER\n\nDoes an outsized move continue or give back?"
SUMMARY = {
    "n": {"trials": 50, "by_outcome": {"completed": 48}},
    "gate": {"p": 0.8236, "verdict": "REJECT", "observed_best": 0.0256,
             "GATE_MARKER": True},
    "deflation_view": {"margin": -0.1409},
    "diagnostics_only": {"correction": {"bh_discoveries": 0}},
    "campaign": {"profile": "nq-train", "dataset": "nq-trades", "split": "train",
                 "agent_config": {"commission_per_contract": 0.10}},
    "provenance": {"isolation": {"level": "best-effort"}},
    "best_candidate": {
        "source": SOURCE,
        "score": {"primary": 0.0256, "metric": "sharpe_per_session",
                  "net_pnl": 0.63, "trade_count": 3, "n_sessions": 384,
                  "downside_sessions": 1},
        "diagnostics": {"by_month": {"2024-08": -0.77}, "gross_pnl": 0.69,
                        "commission": 0.06, "best_session_share": 0.5},
    },
}


# --------------------------------------------------------------------------- #
# the vocabulary has no word for approval
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["APPROVE", "ACCEPT", "PROMOTE", "GOOD", "pass "])
def test_an_auditor_cannot_return_approval(bad):
    with pytest.raises(ValueError, match="not one of"):
        AuditorVerdict(axis="leakage", verdict=bad)


def test_a_veto_must_state_its_reasons():
    with pytest.raises(ValueError, match="states its reasons"):
        AuditorVerdict(axis="leakage", verdict="VETO")


def test_the_committee_verdict_says_it_cannot_approve():
    v = CommitteeVerdict(verdict="PASS", auditors=(
        AuditorVerdict(axis=a, verdict="PASS") for a in AXES))
    d = CommitteeVerdict(verdict="PASS", auditors=tuple(
        AuditorVerdict(axis=a, verdict="PASS") for a in AXES)).as_dict()
    assert d["can_approve"] is False
    assert "cannot approve" in d["approval_note"]
    del v


# --------------------------------------------------------------------------- #
# aggregation: any one can veto
# --------------------------------------------------------------------------- #

def _agg(verdicts):
    """The rule in `review`, applied to a hand-built set."""
    if any(v == "VETO" for v in verdicts):
        return "VETO"
    if any(v == "CANNOT_TELL" for v in verdicts):
        return "CANNOT_TELL"
    return "PASS"


@pytest.mark.parametrize("verdicts,expected", [
    (["PASS", "PASS", "PASS", "PASS"], "PASS"),
    (["PASS", "VETO", "PASS", "PASS"], "VETO"),
    (["VETO", "VETO", "VETO", "VETO"], "VETO"),
    (["PASS", "CANNOT_TELL", "PASS", "PASS"], "CANNOT_TELL"),
    (["VETO", "CANNOT_TELL", "PASS", "PASS"], "VETO"),
])
def test_any_single_auditor_can_veto(verdicts, expected):
    assert _agg(verdicts) == expected


def test_a_veto_outranks_a_cannot_tell():
    """A veto is a finding; a cannot-tell is an absence of one. An absence must
    never dilute a finding into an escalation."""
    assert _agg(["VETO", "CANNOT_TELL"]) == "VETO"


# --------------------------------------------------------------------------- #
# isolation: each auditor sees ONLY its own evidence
# --------------------------------------------------------------------------- #

def test_the_isolation_invariant_is_about_verdicts_not_about_the_source():
    """The first version of this test asserted only leakage may see the code,
    which was an over-tight reading of isolation and would have blocked the
    economics auditor from doing its main job.

    **The invariant is that no auditor sees another auditor's verdict or
    reasons.** That is what stops anchoring. Two auditors reading the same source
    for different questions -- leakage for lookahead, economics for whether the
    code implements the hypothesis -- is not anchoring; it is two reviews.
    """
    assert "SECRET_MARKER" in _slice("leakage", SUMMARY, SOURCE)
    assert "SECRET_MARKER" in _slice("economics", SUMMARY, SOURCE, HYPOTHESIS)
    for axis in ("statistics", "robustness"):
        assert "SECRET_MARKER" not in _slice(axis, SUMMARY, SOURCE), (
            f"{axis} has no question that needs the source")
    # and nothing carries a verdict from elsewhere
    for axis in AXES:
        text = _slice(axis, SUMMARY, SOURCE, HYPOTHESIS)
        assert "auditors" not in text and "vetoed_by" not in text


def test_only_economics_is_given_the_hypothesis():
    """It owns the question nobody else asks: does the code implement what the
    campaign was pre-registered to test? If it does not, the pre-registration is
    decorative whatever the numbers say."""
    assert "HYPOTHESIS_MARKER" in _slice("economics", SUMMARY, SOURCE, HYPOTHESIS)
    for axis in ("leakage", "statistics", "robustness"):
        assert "HYPOTHESIS_MARKER" not in _slice(axis, SUMMARY, SOURCE, HYPOTHESIS)


def test_economics_says_when_it_cannot_judge_the_match():
    s = _slice("economics", SUMMARY, SOURCE, None)
    assert "NOT SUPPLIED" in s and "cannot judge question 1" in s


def test_the_leakage_auditor_does_not_see_the_gate():
    """It judges code. Showing it the verdict invites it to agree with the gate
    instead of reading the file."""
    s = _slice("leakage", SUMMARY, SOURCE)
    assert "GATE_MARKER" not in s and "0.8236" not in s


def test_an_absent_source_is_said_rather_than_faked():
    s = _slice("leakage", SUMMARY, None)
    assert "NO SOURCE AVAILABLE" in s


def test_the_economics_auditor_gets_what_it_needs_to_price_the_costs():
    """Its first live run said 'commission per contract was not provided',
    because the first draft looked for it under `provenance`. It is in the
    campaign's agent_config and the paid total is in the diagnostics."""
    # The slice is a document now -- hypothesis, source, then the evidence in a
    # fenced block -- so the numbers are extracted rather than parsed whole.
    import re

    text = _slice("economics", SUMMARY, SOURCE, HYPOTHESIS)
    block = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert block, "the economic evidence should still be a json block"
    ev = json.loads(block.group(1))
    assert ev["commission_per_contract_per_side_points"] == 0.10
    assert ev["commission_paid_points"] == 0.06
    assert ev["gross_pnl_points"] == 0.69
    # Units are labelled because the first live run read a rate in index points
    # as dollars and produced confident, wrong arithmetic from it.
    assert "INDEX POINTS" in ev["_units"] and ev["usd_per_index_point"] == 20


@pytest.mark.parametrize("axis", AXES)
def test_no_slice_carries_validation_or_holdout(axis):
    """The campaign is scored on train only, and nothing about the other splits
    may reach a reviewer whose verdict could then depend on a budget nobody
    pre-registered."""
    s = _slice(axis, SUMMARY, SOURCE, HYPOTHESIS).lower()
    for word in ("holdout", "validation"):
        assert word not in s, f"{axis} slice mentions {word}"


# --------------------------------------------------------------------------- #
# the prompts, and the boundary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("axis", AXES)
def test_every_axis_has_a_prompt_and_it_forbids_approval(axis):
    p = _prompt(axis)
    assert "cannot approve" in p.lower()
    assert "VETO" in p and "CANNOT_TELL" in p
    assert f"Your axis: {axis.upper()}" in p


def test_the_prompt_digests_are_recorded():
    """The prompts shaped the verdicts, so their versions belong in the record
    for the same reason the proposer's template digest does."""
    d = prompt_digests()
    assert set(d) == {f"{a}.md" for a in AXES} | {"_shared.md"}
    assert all(len(v) == 16 for v in d.values())


def test_the_committee_imports_nothing_from_the_harness():
    """`audit/` is a separate tier and the contract is a file. The duplicated
    client in `audit/committee/llm.py` is the price of that, and this is the test
    that makes it a price rather than an oversight."""
    for path in sorted((REPO_ROOT / "audit" / "committee").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad = [n for n in names if n == "src" or n.startswith("src.")]
            assert not bad, f"{path.name} imports {bad} from the harness"
