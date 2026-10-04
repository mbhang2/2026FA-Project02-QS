"""The reviewer for the question itself — Amendment 11's sixth gate.

The other five are mechanical: slug, heading, forbidden vocabulary, grounding,
family-unchanged, length. `nq-s04-03` passed every one of them with a question
whose champion made four trades and scored `-inf`, so `n_nulls` was 0, not one
candidate reached the correction, and twenty trials bought nothing.

Measured 2026-10-03 against four deliberately bad questions and one real one:
5 of 5 correct. It is not a rubber stamp. It did NOT catch `nq-s04-03`'s
revision, which on reading is defensible — the question does not mandate rarity,
the proposer collapsed on its own — and that limit is recorded here rather than
claimed away.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "review_hypothesis.py").read_text()
FLAT = " ".join(SRC.split())
PROMPT = (REPO_ROOT / "config" / "prompts" / "hypothesis-review.md").read_text()
PFLAT = " ".join(PROMPT.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "review_hypothesis", REPO_ROOT / "scripts" / "review_hypothesis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# it cannot approve
# --------------------------------------------------------------------------- #
def test_approval_is_not_expressible():
    """Same rule as `audit/committee`: a reviewer that can approve becomes the
    thing that decides, and the decision belongs to a human and to the gate."""
    mod = _mod()
    assert set(mod.VERDICTS) == {"OBJECT", "CANNOT_TELL", "NO_OBJECTION"}
    assert not any("APPROV" in v.upper() or "PASS" in v.upper()
                   for v in mod.VERDICTS)
    assert "It cannot approve" in FLAT
    assert "NO_OBJECTION is not approval" in PFLAT or \
challenge_text(PROMPT), "the prompt must say NO_OBJECTION is not approval"


def challenge_text(t):
    return "This does not mean the question is good" in " ".join(t.split())


def test_no_objection_carries_its_own_disclaimer(monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod, "review", lambda *a, **k: {
        "verdict": "NO_OBJECTION", "concerns": [], "note": "",
        "model": "m", "model_served": "m",
        "approval_note": "NO_OBJECTION is not approval."})
    assert mod.findings("x", "y") == []


# --------------------------------------------------------------------------- #
# an unreadable or empty objection is not a pass
# --------------------------------------------------------------------------- #
def test_an_unreadable_verdict_becomes_cannot_tell_not_a_pass(monkeypatch):
    """No verdict is not no veto — the committee's rule, applied here."""
    mod = _mod()

    class _C:
        def __init__(self, **kw): pass
        def ask(self, system, user): return {"verdict": "looks fine to me"}

    import audit.committee.llm as L
    monkeypatch.setattr(L, "Client", _C)
    monkeypatch.setattr(L, "preflight", lambda m: m)
    r = mod.review("# q", None)
    assert r["verdict"] == "CANNOT_TELL"
    assert r["unreadable"] is True
    assert mod.findings("# q", None) == [], "CANNOT_TELL is not an objection"


def test_objecting_without_naming_a_concern_is_not_an_objection(monkeypatch):
    mod = _mod()

    class _C:
        def __init__(self, **kw): pass
        def ask(self, system, user): return {"verdict": "OBJECT", "concerns": []}

    import audit.committee.llm as L
    monkeypatch.setattr(L, "Client", _C)
    monkeypatch.setattr(L, "preflight", lambda m: m)
    r = mod.review("# q", None)
    assert r["verdict"] == "CANNOT_TELL"
    assert "without naming a concern" in r["concerns"][0]


# --------------------------------------------------------------------------- #
# what it may and may not see
# --------------------------------------------------------------------------- #
def test_no_score_or_verdict_reaches_the_reviewer():
    mod = _mod()
    ctx = mod.context("# the question", "# its parent")
    assert "Deliberately withheld" in ctx
    assert "every score, p-value and gate verdict" in ctx
    assert "validation" in ctx
    # and it never opens a campaign directory
    for banned in ("runs/campaigns", "tier2-summary", "ledger.jsonl",
                   "validation.json", "observed_best"):
        assert banned not in SRC, banned


def test_the_prompt_names_what_is_not_an_objection():
    """A reviewer that objects to style or to an expected negative answer would
    block the questions this project most wants to ask."""
    for phrase in ("expect the answer to be no", "question is simple",
                   "phrased it differently", "does not say how to implement"):
        assert phrase in PFLAT, phrase


def test_the_five_concerns_are_named_and_numbered():
    for n, word in ((1, "Not answerable with this data"),
                    (2, "Not answerable by any strategy that can trade"),
                    (3, "Not falsifiable"),
                    (4, "More than one question"),
                    (5, "not the question it claims to be")):
        assert word in PROMPT, (n, word)


# --------------------------------------------------------------------------- #
# the gate, in the refiner
# --------------------------------------------------------------------------- #
def test_the_reviewer_runs_only_on_a_mechanically_clean_draft():
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    flat = " ".join(r.split())
    # the mechanical gates are the loop's exit condition; the reviewer runs
    # inside it. Anchored on the exit rather than its exact operand list, which
    # grew a `shape` term when the slug became repairable.
    i = r.index("if not (leaks or loose or fam or length")
    assert "review_findings(md, parent)" in r[i:i + 1200]
    assert "only on a mechanically clean file" in flat


def test_a_reviewer_objection_produces_a_revision_not_a_dead_session():
    """Michael's complaint: waking up to a session that ran a third of its plan.
    An objection joins the repair loop like any other finding."""
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    assert "MAX_REVIEW_REPAIRS" in r
    assert "A reviewer read your revision and" in r
    flat = " ".join(r.split())
    assert "two exchanges is a revision, more is an argument" in flat


def test_a_reviewer_that_cannot_run_is_not_an_objection():
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    flat = " ".join(r.split())
    assert "treating that as no objection, which it is not" in flat


def test_the_two_kinds_of_silence_are_distinguished():
    """An empty finding list means either 'read it, said nothing' or 'never
    ran'. Collapsing them lets a dead gate look like a passed one -- which is
    exactly what the first end-to-end run did when mimo timed out."""
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    assert "def review_findings(md: str, parent: str) -> tuple[list[str], str]:" in r
    assert 'return review_hypothesis.findings(md, parent), "ran"' in r
    assert 'return [], f"did not run ({type(exc).__name__})"' in r


def test_the_written_file_reports_what_the_reviewer_did():
    """Six gates, not five -- and the sixth is reported separately because it
    is a judgement, not a measurement."""
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    flat = " ".join(r.split())
    assert "mechanical gates passed: shape, lint, grounding, family," in flat
    assert 'print(f"  reviewer: {reviewer}"' in r
    assert "which is not approval" in flat


def test_hitting_the_review_cap_is_not_reported_as_a_pass():
    """Two objections then a write is a question no reviewer cleared. The log
    has to say so, or the cap quietly becomes an approval."""
    r = (REPO_ROOT / "scripts" / "refine_hypothesis.py").read_text()
    flat = " ".join(r.split())
    assert "cap reached" in flat
    assert "WITHOUT the reviewer clearing it" in flat
