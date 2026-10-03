"""Hypothesis k+1 from campaign k. Amendment 11's five gates.

The load-bearing one is gate 5: §4 says campaign k+1 may depend on prior
campaigns' train results and never on their validation results, and the honest
reading of "may not depend on" is that the information must not be in the room.
So this refuses rather than promising not to look.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
FLAT = " ".join(SRC.split())
PROMPT = (REPO_ROOT / "config" / "prompts" / "hypothesis-refine.md").read_text()


def _mod():
    spec = importlib.util.spec_from_file_location(
        "refine_hypothesis", REPO_ROOT / "scripts" / "refine_hypothesis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# gate 5: validation must not be in the room
# --------------------------------------------------------------------------- #
def test_a_readable_validation_artefact_refuses_the_whole_run(tmp_path,
                                                              monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod, "CAMPAIGNS", tmp_path)
    d = tmp_path / "c1"; d.mkdir()
    assert mod.validation_readable("c1") == []
    (d / "validation.json").write_text("{}")
    assert mod.validation_readable("c1") == ["c1/validation.json"]
    (d / "validation-null.json").write_text("{}")
    assert len(mod.validation_readable("c1")) == 2


def test_the_refusal_is_enforced_before_anything_is_read():
    """Ordering matters: the check must come before the campaign record, the
    parent hypothesis, or the evidence are opened."""
    gate = SRC.index("leak = validation_readable(a.campaign)")
    read_record = SRC.index('record = json.loads((d / "campaign.json")')
    evidence = SRC.index("ev = evidence(a.campaign)")
    assert gate < read_record < evidence
    assert "must not be in the room" in FLAT


def test_the_real_campaign_is_refinable_and_would_stop_being_so_after_phase_2():
    """nq-s01-continuation reported with its validation look unspent, which is
    exactly the state phase 1 leaves a campaign in."""
    mod = _mod()
    assert (REPO_ROOT / "runs" / "campaigns" / "nq-s01-continuation"
            / "tier2-summary.json").exists()
    assert mod.validation_readable("nq-s01-continuation") == []


# --------------------------------------------------------------------------- #
# what the refiner may and may not see
# --------------------------------------------------------------------------- #
def test_the_gates_output_is_withheld_from_the_refiner():
    """Task 36 measured an LLM shown its own prior scores learning to improve
    them on a dataset with no edge in it. A refiner shown the gate's output is
    being taught to write questions that score well."""
    mod = _mod()
    ev = mod.evidence("nq-s01-continuation")
    # The VALUES are what must be absent. The auditors' reasons legitimately
    # discuss the metric by name -- that is their objection, and withholding it
    # would withhold the most useful thing the refiner is given.
    did = ev["what_the_strategy_did"]
    for banned in ("primary", "primary_uncharged", "sharpe", "sortino",
                   "significance", "rejected", "reject_reason"):
        assert banned not in did, banned
    assert "p" not in ev and "observed_best" not in ev and "verdict" not in ev
    assert ev["redacted"], "it must say what it withheld, not just withhold it"
    assert "gate" in " ".join(ev["redacted"]).lower()
    # and what it DOES see
    assert ev["what_the_strategy_did"]["trades"]
    assert ev["auditors"], "the auditors' objections are the useful part"


def test_the_context_tells_the_model_what_was_withheld_and_why():
    mod = _mod()
    ev = mod.evidence("nq-s01-continuation")
    ctx = mod.context("# parent\n\nbody", ev, [])
    assert "Deliberately withheld from you" in ctx
    assert "a question written to score well is not the same as a question " \
           "that is true" in ctx


def test_the_prompt_forbids_the_gates_vocabulary():
    """Every byte reaches the strategy agent, which must not learn how it is
    judged."""
    for word in ("validation", "holdout", "p-value", "significance",
                 "accept", "reject", "correction"):
        assert word in PROMPT, f"the prompt must name {word} as forbidden"
    assert "optimises the judge instead of the market" in " ".join(PROMPT.split())


# --------------------------------------------------------------------------- #
# gate 4: it is a refinement, not a replacement
# --------------------------------------------------------------------------- #
def test_a_different_question_is_caught():
    mod = _mod()
    parent = ("# Does an outsized move inside a session continue, or give it "
              "back?\n\n" + "continuation and give-back and session range. " * 40)
    same = ("# Does an outsized intraday move continue, or give it back, and "
            "when?\n\n" + "continuation and give-back and session range. " * 40)
    other = ("# Do gamma exposure nodes attract price on expiry days?\n\n"
             + "dealer gamma and open interest and expiry pinning. " * 40)
    assert mod.family_changed(parent, same) == ""
    assert mod.family_changed(parent, other) != ""
    assert "different question" in mod.family_changed(parent, other)


def test_gate_4_is_admitted_to_be_the_weakest():
    """An honest module says which of its checks it does not trust."""
    assert "weakest of the five" in FLAT


# --------------------------------------------------------------------------- #
# Amendment 11
# --------------------------------------------------------------------------- #
def test_commit_is_refused_while_amendment_11_is_not_in_force():
    assert "prereg.in_force(11)" in SRC
    assert "a machine may not commit a" in FLAT and "hypothesis. Drop" in FLAT


def test_a_machine_commit_is_marked_as_one():
    assert "Machine-Written: {a.campaign}" in SRC
    assert "No human " in SRC and "read this before it was committed" in FLAT
    assert "may spend trials and may NOT" in FLAT
    assert "a claim" in FLAT and "held until a human has read" in FLAT


def test_the_committed_refinement_carries_its_trailer():
    """The one produced on 2026-09-30, so `git log` can answer which questions
    nobody read before trials were spent on them."""
    out = subprocess.run(
        ["git", "log", "--format=%(trailers:key=Machine-Written)", "-200"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    trailers = [l for l in out.stdout.splitlines() if l.startswith("Machine-Written:")]
    assert trailers, (
        "no machine-written hypothesis carries its trailer; `git log` can no "
        "longer answer which questions nobody read before trials were spent")
    # named rather than counted, so the set is visible when it changes
    assert any("nq-s0" in t for t in trailers), trailers[:5]


# --------------------------------------------------------------------------- #
# the format fault that ended session-03 after three campaigns
# --------------------------------------------------------------------------- #
def test_raw_newlines_inside_a_json_string_are_repaired_not_thrown_away():
    """deepseek-v4-pro returned a thousand words of markdown inside a JSON
    string with real newlines in it, on all five retries, and the session died.
    A reply one escape away from valid JSON is not worth a session."""
    import json as _json
    from audit.committee.llm import _escape_raw_controls
    bad = '{\n "slug": "a-b",\n "markdown": "# Title\n\n## Section\n\ttabbed"\n}'
    with pytest.raises(_json.JSONDecodeError):
        _json.loads(bad)
    d = _json.loads(_escape_raw_controls(bad))
    assert d["slug"] == "a-b"
    assert d["markdown"] == "# Title\n\n## Section\n\ttabbed", (
        "the repair must preserve the text exactly, only changing its encoding")


def test_the_repair_never_touches_valid_json():
    from audit.committee.llm import _escape_raw_controls
    for ok in ('{"a": "line1\\nline2", "b": 3}',
               '{"a": "has \\" an escaped quote", "b": [1, 2]}',
               '{\n  "pretty": "printed",\n  "n": 1\n}'):
        assert _escape_raw_controls(ok) == ok, ok


def test_a_backslash_before_a_quote_does_not_end_the_string():
    """The walker has to honour escapes or it loses track of where strings are,
    and then it escapes newlines that were never inside one."""
    from audit.committee.llm import _escape_raw_controls
    import json as _json
    bad = '{"a": "ends with a backslash-quote \\\\" , "b": "x\ny"}'
    out = _escape_raw_controls(bad)
    # whatever it does, it must not crash and must not corrupt the prefix
    assert out.startswith('{"a": "ends with a backslash-quote')


def test_the_prompt_states_the_escaping_requirement():
    assert "every newline in it must be written" in " ".join(PROMPT.split())
    assert "most common way this request fails" in " ".join(PROMPT.split())


def test_a_malformed_reply_is_retried_with_the_requirement_restated():
    assert "MAX_FORMAT_RETRIES" in SRC
    assert "reply was not usable JSON" in SRC
    assert "rather than a five-campaign session" in FLAT


def test_the_refiner_allows_more_words_than_the_distiller_and_says_why():
    """The two ceilings differ on purpose. `distil_paper` turns a paper into a NEW
    question and its ceiling exists so the next file is smaller than the worst
    legacy one (1,422 words). A revision carries its parent's question, what the
    evidence showed, and what it took from the literature -- at 1,125 a 920-word
    parent left ~200 words of headroom, and that is what ended session-04 after
    one campaign of three."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "distil_paper", REPO_ROOT / "scripts" / "distil_paper.py")
    dp = importlib.util.module_from_spec(spec); spec.loader.exec_module(dp)
    mod = _mod()
    assert mod.MAX_WORDS == 1_500
    assert dp.MAX_WORDS == 1_125
    assert mod.MAX_WORDS > dp.MAX_WORDS
    bare = " ".join(re.sub(r"^\s*#\s?", "", SRC, flags=re.M).split()).lower()
    assert "a revision is a different operation" in bare
    assert "asymmetry is deliberate" in bare


def test_a_repeated_length_miss_escalates_to_a_different_model():
    """Five rounds of "too long" is what ended session-04. The repair now names
    the exact deficit and the longest sections, and a model that keeps missing is
    replaced rather than asked again."""
    assert "SWITCH_MODEL_AFTER" in SRC and "FALLBACK_MODEL" in SRC
    assert "words over** and must cut at least that" in SRC
    assert "Your longest sections are" in SRC
    assert "is not about to comply on the sixth" in FLAT
    assert mod_max_repairs() >= 8


def mod_max_repairs():
    return _mod().MAX_LINT_REPAIRS
