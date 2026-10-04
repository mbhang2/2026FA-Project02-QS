"""The four-auditor committee: isolated, veto-only, and outside the harness.

Everything here is structural and runs offline. The committee's *judgement*
cannot be unit-tested -- it is a language model -- so what is tested is the shape
that constrains it: what each auditor can see, what verdicts exist, and what the
aggregate can and cannot say.
"""

from __future__ import annotations

import ast
import json
import re
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
def test_no_slice_ever_carries_the_holdout(axis):
    """The holdout is not the committee's business, in any campaign, ever.

    Section 8: it is opened once, by a human, at the end of the project. A
    reviewer whose verdict could depend on it is a reviewer who has spent a budget
    nobody pre-registered, and unlike validation there is no correction that buys
    the look back.
    """
    s = _slice(axis, SUMMARY, SOURCE, HYPOTHESIS).lower()
    assert "holdout" not in s, f"{axis} slice mentions the holdout"


@pytest.mark.parametrize("axis", AXES)
def test_a_campaign_without_a_validation_run_says_so_rather_than_going_quiet(axis):
    """Absence is stated, not omitted.

    This test used to forbid the word "validation" outright, and section 5 of the
    pre-registration overtook it: rule (c), the train-to-validation change, is
    named there as **committee evidence**. So the invariant is no longer "they
    must not see it" -- it is "they must not be able to mistake not-looking for
    looking-and-passing", which is the failure that actually costs something.
    """
    s = _slice(axis, SUMMARY, SOURCE, HYPOTHESIS)
    assert "validation" not in SUMMARY, "fixture drifted: SUMMARY has a validation block"
    if "validation" in s.lower():
        assert "NOT RUN" in s, (
            f"{axis} slice mentions validation for a campaign that has none, "
            "without saying it was not run")


@pytest.mark.parametrize("axis", ("statistics", "economics", "robustness"))
def test_validation_reaches_the_axes_that_weigh_it_unjudged(axis):
    """Section 5 rules (a) and (c), as evidence rather than a verdict.

    (c) is passed through raw and explicitly not as a threshold: "within X%" needs
    an X, the project does not fit one, and a winner that does *better* out of
    sample has not failed. The slice must therefore carry the number and must not
    carry a pass/fail reading of it.
    """
    summary = dict(SUMMARY)
    summary["validation"] = {
        "train": {"primary": 0.42},
        "validation": {"primary": 0.05, "outcome": "completed",
                       "score": {"primary": 0.05}, "diagnostics": {}},
        "rule_a_sortino_positive": True,
        "rule_c_decay": -0.37,
    }
    s = _slice(axis, summary, SOURCE, HYPOTHESIS)
    assert "0.42" in s and "0.05" in s, f"{axis} slice lost the two scores"
    assert "-0.37" in s, f"{axis} slice lost rule (c)"
    assert "NOT a pass/fail threshold" in s, (
        f"{axis} slice presents rule (c) without saying it is not a threshold")


def test_leakage_never_sees_validation_even_when_it_ran():
    """Leakage reads the source and nothing else. Handing it an out-of-sample
    score invites it to reason from the number instead of from the code, which is
    the one thing only it is positioned to do."""
    summary = dict(SUMMARY)
    summary["validation"] = {"train": {"primary": 0.42},
                             "validation": {"primary": 0.05},
                             "rule_a_sortino_positive": True,
                             "rule_c_decay": -0.37}
    assert "0.05" not in _slice("leakage", summary, SOURCE, HYPOTHESIS)


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


# --------------------------------------------------------------------------- #
# the client survives a reasoning model
# --------------------------------------------------------------------------- #
def test_an_empty_reply_names_the_cutoff_not_a_json_error(monkeypatch):
    """nq-ml-02's committee: three seats returned '' because deepseek-v4-pro
    spent all 4,096 completion tokens on reasoning and finish_reason was
    'length'. The artefact said 'reply was not JSON', which is true and useless.
    Now it says what happened."""
    import audit.committee.llm as L
    monkeypatch.setenv("LITELLM_BASE_URL", "http://x"); monkeypatch.setenv("LITELLM_API_KEY", "k")

    class R:
        status_code = 200; text = ""
        def json(self):
            return {"model": "deepseek-v4-pro",
                    "choices": [{"finish_reason": "length",
                                 "message": {"content": "", "reasoning_content": "x" * 5000}}],
                    "usage": {"completion_tokens": 4096,
                              "completion_tokens_details": {"reasoning_tokens": 4096}}}
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    c = L.Client(model="deepseek-v4-pro")
    with pytest.raises(L.LLMUnavailable, match="finish_reason='length'.*reasoning_tokens=4096"):
        c.ask("s", "u")
    assert c.calls[-1]["finish_reason"] == "length"
    assert c.calls[-1]["reasoning_tokens"] == 4096


def test_the_budget_and_effort_defaults_fit_a_reasoning_model():
    import audit.committee.llm as L
    c = L.Client.__new__(L.Client)
    assert L.Client.__dataclass_fields__["max_tokens"].default >= 16384
    assert L.Client.__dataclass_fields__["reasoning_effort"].default == "low"


def test_reasoning_effort_is_dropped_once_on_a_400_that_names_it(monkeypatch):
    import audit.committee.llm as L
    monkeypatch.setenv("LITELLM_BASE_URL", "http://x"); monkeypatch.setenv("LITELLM_API_KEY", "k")
    seen = []
    class Bad:
        status_code = 400; text = "UnsupportedParamsError: reasoning_effort is not supported"
        def json(self): return {}
    class Good:
        status_code = 200; text = ""
        def json(self):
            return {"model": "claude-haiku-4-5",
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"verdict": "PASS"}'}}],
                    "usage": {"completion_tokens": 10}}
    import requests
    def post(url, json=None, **k):
        seen.append(dict(json)); return Bad() if "reasoning_effort" in json else Good()
    monkeypatch.setattr(requests, "post", post)
    out = L.Client(model="claude-haiku-4-5").ask("s", "u")
    assert out == {"verdict": "PASS"}
    assert len(seen) == 2 and "reasoning_effort" in seen[0] and "reasoning_effort" not in seen[1]


def test_the_preflight_budget_covers_a_reasoning_models_thinking():
    """**Fifth site of one defect, 2026-09-27.** At `max_tokens=16` this probe
    raised on every reasoning model: the budget is spent before a visible token
    is emitted and the gateway reports the empty output as a content-policy
    violation, which reads like a refusal rather than a truncation.

    The other four: this client at 4,096 (three of nq-ml-02's four seats came
    back empty), `LLMAgent.preflight` at 16 (no reasoning model registrable),
    the proposer at 16,384 (4 of nq-ml-05's first 10 trials blocked), and the
    exposure count that reached a dataclass but not its serialiser. Fixing one
    site is not fixing the defect.

    `screen_papers.py`, `rank_papers.py` and `review.py` all call this.
    """
    from audit.committee import llm as C

    assert C.PREFLIGHT_MAX_TOKENS >= 256, (
        f"the probe asks for {C.PREFLIGHT_MAX_TOKENS} tokens; a reasoning model "
        "spends more than that before its first visible token")
    src = (REPO_ROOT / "audit" / "committee" / "llm.py").read_text()
    assert '"max_tokens": 16,' not in src and "max_tokens=16)" not in src


def test_an_unsupported_parameter_is_retried_when_it_arrives_as_a_substitution():
    """**Measured 2026-09-28: a refusal dressed as a success.**

    An identical request to `claude-haiku-4-5` is answered by
    `claude-haiku-4-5@20251001` without `reasoning_effort` and by
    `gemini-3.1-pro-preview` with it. The gateway does not return 400 for a
    parameter the model cannot take; it falls through its wildcard route to a
    model that can and returns HTTP 200.

    So the existing 400-branch never fired, and all 22 ranking calls of a
    re-rank raised ModelSubstituted. The retry now also fires on the symptom
    that actually occurs -- still only for a parameter the model does not
    accept, never for a verdict it did not like, and a second substitution
    without the parameter is a real one that reaches the caller.
    """
    src = (REPO_ROOT / "audit" / "committee" / "llm.py").read_text()
    ask = src[src.index("def ask("):src.index("def preflight(")]
    assert 'elif resp.status_code == 200 and "reasoning_effort" in body:' in ask
    assert "not _same(self.model, served_probe)" in ask, (
        "the retry must fire only when the model was actually substituted, not "
        "on every 200 that carried the parameter")
    # the parameter is dropped exactly once; there is no retry loop
    assert ask.count('body.pop("reasoning_effort")') == 2
    assert "while" not in ask, "one retry, not a loop"


# --------------------------------------------------------------------------- #
# a campaign judged by three seats must not read like one judged by four
# --------------------------------------------------------------------------- #
def _seat(axis, verdict, reasons=()):
    from audit.committee.review import AuditorVerdict
    return AuditorVerdict(axis=axis, verdict=verdict, reasons=tuple(reasons))


def test_the_verdict_says_how_many_auditors_actually_ran():
    """A seat that failed is recorded as a VETO with its failure as the reason --
    the right refusal, since no verdict is not no veto -- and it was invisible in
    every summary. 8 of 56 seats across the project are in that state, so one
    campaign in seven was judged by three auditors and read like four."""
    from audit.committee.review import CommitteeVerdict
    cv = CommitteeVerdict(verdict="VETO", auditors=(
        _seat("leakage", "PASS"),
        _seat("statistics", "VETO", ["concentrated in two hours"]),
        _seat("economics", "VETO",
              ["the auditor did not run: empty reply: finish_reason='length'"]),
        _seat("robustness", "PASS"),
    ))
    d = cv.as_dict()
    assert d["seats_total"] == 4
    assert d["seats_ran"] == 3
    assert d["seats_failed"] == ["economics"]
    assert d["incomplete"] is True
    assert "judged by fewer seats" in d["incomplete_note"]


def test_a_complete_committee_says_so_quietly():
    from audit.committee.review import CommitteeVerdict
    cv = CommitteeVerdict(verdict="VETO", auditors=(
        _seat("leakage", "PASS"),
        _seat("statistics", "VETO", ["a real objection"]),
    ))
    d = cv.as_dict()
    assert d["seats_ran"] == 2 and d["seats_failed"] == []
    assert d["incomplete"] is False and d["incomplete_note"] == ""


def test_the_budget_clears_what_a_reasoning_model_thinks():
    """4096 -> 16384 was already this fix once, with measured evidence. It failed
    again: four seats returned finish_reason='length' with the whole 16,384
    spent on reasoning and nothing written."""
    from audit.committee.llm import Client
    import dataclasses
    default = {f.name: f.default for f in dataclasses.fields(Client)}
    assert default["max_tokens"] >= 65_536, (
        f"the committee budget is {default['max_tokens']}; 16,384 was consumed "
        "entirely by reasoning on four real seats")


def test_usage_is_recorded_so_the_next_number_can_be_measured():
    """The verdict kept no usage at all, so when 16,384 failed there was no
    distribution of what a SUCCESSFUL auditor consumes -- only the failures.
    65,536 is matched to the proposer rather than measured, and this is what
    stops the next person guessing too."""
    from audit.committee.review import CommitteeVerdict
    cv = CommitteeVerdict(verdict="PASS", auditors=(_seat("leakage", "PASS"),),
                          usage=({"completion_tokens": 7000,
                                  "reasoning_tokens": 6900},))
    d = cv.as_dict()
    assert d["usage"] == [{"completion_tokens": 7000, "reasoning_tokens": 6900}]
    src = (REPO_ROOT / "audit" / "committee" / "llm.py").read_text()
    # Comment markers survive a naive flatten, so strip them first rather than
    # matching around a line break.
    flat = " ".join(re.sub(r"^\s*#\s?", "", src, flags=re.M).split())
    assert "nothing recorded what a SUCCESSFUL auditor consumes" in flat
    assert "the verdict file kept no usage at all" in flat


def test_the_client_timeout_outlasts_the_slowest_allowed_model():
    """mimo-v2.6-pro: 738 s measured on a cache miss, 2026-10-03. At the old
    180 s default every cold call to the smartest model we are allowed timed
    out before it could answer -- twice in one end-to-end refinement. The
    reviewer's failure was the quiet one: it degrades to 'did not run', which
    is honest but silently costs the gate."""
    import dataclasses
    import audit.committee.llm as L
    f = {x.name: x for x in dataclasses.fields(L.Client)}
    assert f["timeout_s"].default >= 800.0, (
        "must leave headroom over mimo's measured 738 s")


# --------------------------------------------------------------------------- #
# independence (decided and fixed 2026-10-04)
#
# Until this date `nq_campaign.py` passed the proposer's own model into
# `review()`, so all 17 recorded verdicts had a model auditing its own work.
# docs/setup/MODEL-ACCESS.md called it a decision rather than a patch because
# fixing it changes what every past verdict means. Michael decided it.
# --------------------------------------------------------------------------- #
def test_the_audit_seats_have_their_own_model():
    import importlib
    # `audit.committee` re-exports `review` as a function, which shadows the
    # submodule of the same name -- import it explicitly.
    R = importlib.import_module("audit.committee.review")
    assert R.AUDIT_MODEL == "mimo-v2.6-pro"
    import inspect
    assert inspect.signature(R.review).parameters["model"].default == R.AUDIT_MODEL, (
        "the committee must pick its own model, not take the campaign's")


def test_the_campaign_no_longer_dictates_the_auditors_model():
    """The exact expression that was the defect must not come back."""
    src = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    i = src.index("cv = review(")
    call = src[i:i + 400]
    import re
    assert "proposer_model=" in call, "it must record what wrote the code"
    # `proposer_model=(record...` contains `model=(record...`, so match the
    # bare argument only.
    assert not re.search(r"(?<!proposer_)model=\(record\.agent_config", call), (
        "this is the independence defect -- the auditors ran on the proposer's "
        "model")


def test_independence_is_a_tri_state_so_unknown_cannot_read_as_a_pass():
    from audit.committee import AuditorVerdict
    from audit.committee.review import CommitteeVerdict
    a = (AuditorVerdict(axis="leakage", verdict="PASS", reasons=()),)

    def cv(model, proposer):
        return CommitteeVerdict(verdict="PASS", auditors=a, model=model,
                                proposer_model=proposer)
    assert cv("mimo-v2.6-pro", "deepseek-v4-pro").independent is True
    assert cv("mimo-v2.6-pro", "mimo-v2.6-pro").independent is False
    assert cv("mimo-v2.6-pro", "").independent is None, (
        "unknown must not collapse into True")
    assert cv("", "deepseek-v4-pro").independent is None


def test_the_artefact_says_whether_the_check_was_independent():
    """It went unnoticed for 17 campaigns because answering it meant reading the
    caller. The verdict file now answers it."""
    from audit.committee import AuditorVerdict
    from audit.committee.review import CommitteeVerdict
    a = (AuditorVerdict(axis="leakage", verdict="PASS", reasons=()),)
    bad = CommitteeVerdict(verdict="PASS", auditors=a, model="x",
                           proposer_model="x").as_dict()
    assert bad["independent"] is False
    assert "not an independent check" in bad["independence_note"]
    unknown = CommitteeVerdict(verdict="PASS", auditors=a, model="x").as_dict()
    assert unknown["independent"] is None
    assert "cannot be determined" in unknown["independence_note"]
    good = CommitteeVerdict(verdict="PASS", auditors=a, model="x",
                            proposer_model="y").as_dict()
    assert good["independent"] is True
    assert good["independence_note"] == ""


def test_the_past_verdicts_are_not_claimed_to_be_independent():
    """All 17 on disk predate the fix. The doc must say so rather than let a
    reader assume the committee was always a separate check."""
    doc = (REPO_ROOT / "docs" / "setup" / "MODEL-ACCESS.md").read_text()
    flat = " ".join(doc.split())
    assert "none of them was an independent check" in flat
    assert "nothing retroactively makes them one" in flat
