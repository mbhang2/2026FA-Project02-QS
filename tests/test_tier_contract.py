"""The tier-2 / tier-3 contract (Task 43). Runs in CI; the teammate suites do not.

Two artifacts and two properties. The artifacts are
`runs/campaigns/<id>/<arm>/tier2-summary.json` and `tier3-verdict.json`; the
properties are that **nothing imports across the boundary** and that **tier 3
cannot promote**.

The second is the one worth testing hard. A veto-only auditor is safe with a
completely uncharacterised error rate, because its mistakes cost power and can
never manufacture a discovery. That safety lasts exactly as long as approval
stays unrepresentable, and "unrepresentable" is a claim about a type, which is
the kind of claim a test can hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit import tier3  # noqa: E402

ARMS = ("history", "control")


def _summary(arm: str) -> dict:
    return json.loads((REPO_ROOT / "runs" / "campaigns" / "task36" / arm
                       / "tier2-summary.json").read_text())


# --------------------------------------------------------------------------- #
# tier 3 cannot promote
# --------------------------------------------------------------------------- #
def test_there_are_exactly_three_verdicts_and_none_is_an_approval():
    names = {v.name for v in tier3.Verdict}
    assert names == {"PASS", "FAIL", "CANNOT_TELL"}
    for forbidden in ("PROMOTE", "ACCEPT", "APPROVE", "ADVANCE", "KEEP"):
        assert forbidden not in names


def test_the_verdict_type_carries_nothing_that_could_be_thresholded():
    """No `score`, no `confidence`, no `weight`. A field that ranks a candidate
    is a field somebody will threshold, and a threshold on an auditor's output is
    an approval with extra steps."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(tier3.AuditVerdict)}
    assert fields == {"verdict", "reasons", "checks", "escalate_to_human"}
    for forbidden in ("score", "confidence", "weight", "rank", "promote",
                      "priority", "strength"):
        assert forbidden not in fields


def test_a_string_verdict_is_refused_rather_than_coerced():
    """Strings are how a fourth verdict gets in."""
    with pytest.raises(TypeError, match="must be a Verdict"):
        tier3.AuditVerdict("PROMOTE")            # type: ignore[arg-type]
    with pytest.raises(TypeError):
        tier3.AuditVerdict("PASS")               # type: ignore[arg-type]


def test_a_veto_states_its_reasons():
    with pytest.raises(ValueError, match="states its reasons"):
        tier3.AuditVerdict(tier3.Verdict.FAIL)
    ok = tier3.AuditVerdict(tier3.Verdict.FAIL, ("because",))
    assert ok.as_dict()["is_veto"] is True


def test_the_emitted_record_says_it_cannot_promote():
    d = tier3.AuditVerdict(tier3.Verdict.PASS).as_dict()
    assert d["can_promote"] is False
    assert d["is_veto"] is False
    assert "PASS means nothing was found to veto" in d["promotion_note"]


def test_cannot_tell_is_not_a_veto_and_escalates():
    """Task 35 found a committee design whose ranking mode forbade it; Task 34's
    most valuable output was declining to claim what the data could not
    support."""
    d = tier3.AuditVerdict(tier3.Verdict.CANNOT_TELL, ("unsure",),
                           escalate_to_human=True).as_dict()
    assert d["is_veto"] is False
    assert d["escalate_to_human"] is True


def test_the_verdict_is_frozen():
    v = tier3.AuditVerdict(tier3.Verdict.PASS)
    with pytest.raises(Exception):
        v.verdict = tier3.Verdict.FAIL          # type: ignore[misc]


# --------------------------------------------------------------------------- #
# tier 3 does not count trials
# --------------------------------------------------------------------------- #
def test_tier_three_reads_n_from_the_summary_and_counts_nothing():
    """Tier 2 owns the correction. The audit agent's other path computes
    `n_trials` as `len(glob("experiments/*.json"))` -- a number that shrinks when
    a file is deleted -- and that number must not appear on this path."""
    source = (REPO_ROOT / "audit" / "tier3.py").read_text()
    assert 'glob("*.json")' not in source
    assert "ledger" in tier3.N_TRIALS_PROVENANCE_TIER2.lower()
    for arm in ARMS:
        summary = _summary(arm)
        verdict = tier3.audit_campaign_summary(summary)
        assert summary["n"]["trials"] == 100
        assert verdict.as_dict()["n_trials_provenance"] == \
            tier3.N_TRIALS_PROVENANCE_TIER2


# --------------------------------------------------------------------------- #
# the artifact
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("arm", ARMS)
def test_the_summary_carries_what_tier_three_needs(arm):
    s = _summary(arm)
    assert s["schema"] == "tier2-campaign-summary/1"
    assert s["campaign"]["record_digest"] == s["campaign"]["record_digest_now"]
    assert s["campaign"]["record_tampered"] is False
    assert s["n"]["trials"] and s["n"]["unaccounted"] == 0
    assert s["gate"]["is_the_gate"] and s["gate"]["observed_best"] is not None
    assert s["best_candidate"]["source"], "leakage review needs the source"
    assert s["provenance"]["code_fingerprint_unique_across_trials"] is True
    assert len(s["ledger"]["rows"]) == 100


@pytest.mark.parametrize("arm", ARMS)
def test_the_summary_carries_no_holdout_by_any_path(arm):
    """Stated rather than merely absent. An auditor whose verdict could depend on
    data the gate has not conditioned on is spending a budget nobody
    pre-registered."""
    s = _summary(arm)
    assert s["holdout"]["seen"] is False
    assert s["campaign"]["split"] == "train"

    # Every mention of the word, enumerated. An `or True` here -- which this
    # test had on its first draft -- is a vacuous assertion in the one test that
    # exists to be non-vacuous.
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and "holdout" in node.lower():
            yield path

    mentions = set(walk(s))
    assert mentions <= {".holdout.note", ".gate.note",
                        ".campaign.agent_config.run_facts",
                        ".campaign.note", ".campaign.stopping_rule",
                        ".n.note", ".null_moments.note",
                        ".diagnostics_only.note", ".ledger.note",
                        ".best_candidate.note"}, sorted(mentions)
    for row in s["ledger"]["rows"]:
        assert "holdout" not in json.dumps(row).lower()


@pytest.mark.parametrize("arm", ARMS)
def test_the_summary_rebuilds_to_the_same_digest(arm):
    """It is derived, so it must be reproducible from what it was derived from.
    `--check` is the CI form of this."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import campaign_summary

    rebuilt = campaign_summary.build(
        REPO_ROOT / "runs" / "campaigns" / "task36" / arm)
    assert rebuilt["digest"] == _summary(arm)["digest"]


# --------------------------------------------------------------------------- #
# nothing imports across the boundary
# --------------------------------------------------------------------------- #
def test_tier_three_imports_nothing_from_the_harness():
    import ast

    harness = set()
    for node in ast.walk(ast.parse(
            (REPO_ROOT / "audit" / "tier3.py").read_text())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        harness.update(n for n in names if n == "src" or n.startswith("src."))
    assert not harness, f"tier 3 imports {sorted(harness)}"


def test_tier_two_imports_nothing_from_the_audit_agent():
    import ast

    audit = set()
    for node in ast.walk(ast.parse(
            (REPO_ROOT / "scripts" / "campaign_summary.py").read_text())):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        audit.update(n for n in names if n.split(".")[0] == "audit")
    assert not audit, f"tier 2 imports {sorted(audit)}"


# --------------------------------------------------------------------------- #
# the deflation cross-check, on the real campaign
# --------------------------------------------------------------------------- #
def test_the_two_deflation_routes_disagree_on_the_real_campaign():
    """**A finding, pinned.** The closed form assumes N draws from ONE null;
    `max_of_n` takes the maximum over N draws from N DIFFERENT nulls, because
    Task 32 made the null per-candidate. Where those nulls differ in location the
    pooled mean sits below the candidates that drive the maximum, and the closed
    form's bar comes out too LOW -- the opposite of its direction on homogeneous
    input, which Task 42 measured as +0.017 to +0.036 high.

    On the Task 36 history arm the gap is large enough to flip the reading: the
    closed-form bar sits below the observed best while the harness's sits above
    it. Neither number is the gate -- both are E[max], the DSR-style margin --
    but a deflation computed from one pooled null would have reported a
    discovery on a campaign with no signal by construction.
    """
    cross = tier3.deflation_cross_check(_summary("history"))
    assert cross["available"]
    assert cross["difference"] < -0.05, (
        "the closed form is no longer materially below the simulated maximum; "
        "the heterogeneous-null explanation needs re-checking"
    )
    assert cross["closed_form_bar_cleared"] is True
    assert cross["simulated_bar_cleared"] is False
    assert cross["agree_on_the_margin"] is False


@pytest.mark.parametrize("arm", ARMS)
def test_tier_three_escalates_rather_than_vetoing_on_the_real_campaigns(arm):
    """Both Task 36 arms come back CANNOT_TELL: the run was `best-effort`
    isolated on macOS, which cannot bind a memory ceiling, and the deflation
    routes disagree. Neither is a defect in the campaign, and neither is
    something an auditor should wave through."""
    verdict = tier3.audit_campaign_summary(_summary(arm))
    assert verdict.verdict is tier3.Verdict.CANNOT_TELL
    assert verdict.escalate_to_human is True
    assert any("isolation level" in r for r in verdict.reasons)


def test_a_tampered_record_is_a_veto_not_an_escalation():
    """The one thing that is a defect rather than an unknown."""
    s = _summary("history")
    s["campaign"]["record_tampered"] = True
    verdict = tier3.audit_campaign_summary(s)
    assert verdict.verdict is tier3.Verdict.FAIL
    assert any("hash" in r for r in verdict.reasons)


def test_unaccounted_ledger_rows_are_a_veto():
    """Task 36's `summarise` raises on these; if one ever reaches a summary, the
    denominator is short by exactly those rows."""
    s = _summary("history")
    s["n"]["unaccounted"] = 3
    verdict = tier3.audit_campaign_summary(s)
    assert verdict.verdict is tier3.Verdict.FAIL
    assert any("N is short" in r for r in verdict.reasons)
