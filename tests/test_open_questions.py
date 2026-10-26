"""Questions we asked and could not answer — the third kind of context.

The ranker judged papers against questions we had CLOSED and against lines each
hypothesis wrote under *what this is not asking for*. Task 71 §14 found the
second list is not open questions at all: it is permanent data constraints and
standing decisions, so a paper "speaking to" one is proposing what we declined.

That left the kind a paper can actually help with missing entirely. It exists in
the artefacts — four auditors read every campaign and their objections are very
often an open question wearing a complaint — and it reached nobody.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "open_questions.py").read_text()
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "open_questions", REPO_ROOT / "scripts" / "open_questions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# §4: the committee is validation-informed whenever validation ran first
# --------------------------------------------------------------------------- #
def test_a_campaign_with_any_validation_artefact_is_refused(tmp_path,
                                                            monkeypatch):
    """`record_outcome.py` says it plainly: the report stage hands the auditors a
    summary that embeds the validation numbers. So an objection from a campaign
    that had been validated may be an objection ABOUT a validation number, and
    feeding it to the ranker puts validation into hypothesis selection."""
    mod = _mod()
    monkeypatch.setattr(mod, "CAMPAIGNS", tmp_path)
    d = tmp_path / "c1"
    d.mkdir()
    (d / "committee-verdict.json").write_text(json.dumps({"auditors": []}))
    ok, why = mod.eligible("c1")
    assert ok is True and why == ""
    (d / "validation.json").write_text("{}")
    ok, why = mod.eligible("c1")
    assert ok is False
    assert "validation artefact present" in why


def test_the_real_contaminated_campaigns_are_actually_refused():
    """Four campaigns had validation on disk before their committee ran."""
    mod = _mod()
    for name in ("nq-ml-02", "nq-ml-03", "nq-orderflow-01", "nq-orderflow-02"):
        ok, why = mod.eligible(name)
        assert ok is False, name
        assert "validation" in why
    # and the rule is the same one the refiner applies, not a second rule
    assert "refine_hypothesis.py" in FLAT
    assert "stated once and used twice" in FLAT or "used twice" in FLAT


# --------------------------------------------------------------------------- #
# what counts as an open question
# --------------------------------------------------------------------------- #
def test_only_unresolved_seats_contribute():
    mod = _mod()
    assert set(mod.UNRESOLVED) == {"VETO", "CANNOT_TELL"}
    assert "A PASS has no complaint in it" in FLAT


def test_an_auditor_that_never_ran_is_not_an_open_question():
    """A failed seat is recorded as a VETO with its failure as the reason, which
    is the right refusal — no verdict is not no veto — and is not an objection.
    Five were in the first draft, four of them a 16,384-token budget spent
    entirely on reasoning."""
    mod = _mod()
    mod.collect()
    assert mod.FAILED_SEATS, "the real artefacts contain failed seats"
    for f in mod.FAILED_SEATS:
        assert any(m in f["why"] for m in mod.NOT_AN_OBJECTION)
    qs = mod.distinct(mod.collect())
    for q in qs:
        assert "the auditor did not run" not in q["objection"]
        assert "finish_reason=" not in q["objection"]
    assert "not about the market" in FLAT


def test_the_questions_span_campaigns_rather_than_coming_from_one():
    """Taking the first N took all twelve from `nq-mvp-01` — a champion that
    traded three times, whose objections are true complaints and not questions
    any paper answers."""
    mod = _mod()
    qs = mod.distinct(mod.collect())
    assert len(qs) >= 6
    campaigns = {q["campaign"] for q in qs}
    assert len(campaigns) >= 4, (
        f"the open questions come from only {campaigns}; the round-robin is "
        "what stops one campaign filling every slot")
    assert mod.MAX_PER_CAMPAIGN <= 3


def test_nothing_is_rewritten_by_a_model():
    """A model writing our open questions is a model inventing research
    directions. The objections are quoted."""
    assert "quoted verbatim" in FLAT
    assert "No model turns them into questions" in FLAT
    for banned in ("import openai", "client.ask", "LLMAgent", "requests.post"):
        assert banned not in SRC, banned


# --------------------------------------------------------------------------- #
# it reaches the ranker, and is labelled as the opposite of the exclusions
# --------------------------------------------------------------------------- #
def test_the_block_reaches_the_ranking_context():
    spec = importlib.util.spec_from_file_location(
        "rank_papers", REPO_ROOT / "scripts" / "rank_papers.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    blk = rp.context_block()
    assert "Asked and unresolved" in blk
    assert "a paper that speaks to one of these IS worth more" in blk
    # and the exclusion list is still there, still labelled as exclusions
    assert "was NOT asking" in blk
    assert "Read these as exclusions" in blk


def test_a_broken_committee_file_does_not_kill_a_ranking_run():
    """Context is never fatal. A ranking run must not die because one campaign
    has a malformed artefact."""
    src = (REPO_ROOT / "scripts" / "rank_papers.py").read_text()
    i = src.index("import open_questions")
    assert "except Exception:" in src[i:i + 400]
    assert "Never fatal" in " ".join(src.split())


# --------------------------------------------------------------------------- #
# the loop's own complaints become its search terms
# --------------------------------------------------------------------------- #
def test_retrieval_was_blind_to_the_loop_and_now_is_not():
    """**The gap this closed.** The whole 474-paper corpus came from five
    hand-typed queries in September. `rank_papers.py` has read these objections
    since 2026-10-06 -- so the ranker knew what we had failed on -- while
    `fetch_scholar.py` required `--term` from a human, so the corpus could only
    ever hold what somebody thought to search for before any of the complaints
    existed. Seven of the ten standing objections are about whether an effect
    persists across months, weekdays or sessions, and not one paper had ever
    been fetched because of that."""
    assert "def search_terms" in SRC
    scholar = (REPO_ROOT / "scripts" / "fetch_scholar.py").read_text()
    assert "--from-open-questions" in scholar
    assert "open_questions.search_terms()" in " ".join(scholar.split())


def test_no_model_turns_an_objection_into_a_search_direction():
    """This module's own rule is that objections are quoted verbatim because "a
    model writing our open questions is a model inventing research directions".
    A model generating the search terms would be the same act one step removed:
    it could send us looking in a literature nobody's data asked for. So the
    mapping is a committed table and the derivation is pure string matching."""
    mod = _mod()
    body = SRC.split("def search_terms", 1)[1].split("\ndef ", 1)[0]
    for forbidden in ("Client(", "chat/completions", "requests.", "ask(", "llm"):
        assert forbidden not in body, f"search_terms reaches for {forbidden}"
    assert isinstance(mod.TERM_VOCABULARY, tuple) and mod.TERM_VOCABULARY


def test_every_vocabulary_entry_is_earned_by_text_in_the_corpus():
    """A table of guesses about what junk might look like is how a filter starts
    matching things nobody has seen. Each entry has to be readable off an
    objection that actually exists."""
    mod = _mod()
    # **`collect()`, not `distinct(collect())`.** `distinct` is the DISPLAY
    # list: it drops near-duplicates, spreads round-robin across campaigns and
    # caps at MAX_QUESTIONS. So whether a given objection survives it depends
    # on how many campaigns exist, which has nothing to do with whether a
    # vocabulary entry is earned. Registering a seventh campaign evicted
    # nq-s03-03's "Friday and Tuesday carry nearly all the profit" from the top
    # twelve and failed this test, while the objection sat in the record
    # unchanged -- three raw objections still match that entry. The docstring
    # above says the entry has to be readable off an objection that ACTUALLY
    # EXISTS, and that is the raw corpus.
    objections = [(o.get("objection") or "").lower() for o in mod.collect()]
    assert objections, "no standing objections; this test proves nothing"
    unearned = []
    for concept, needles, term in mod.TERM_VOCABULARY:
        if not any(n in text for text in objections for n in needles):
            unearned.append(concept)
    assert not unearned, (
        f"these vocabulary entries match nothing in the corpus: {unearned}. "
        f"Either the objections changed or the entry was invented.")


def test_a_derived_term_carries_the_objections_it_came_from():
    """Provenance is the point. A paper fetched this way has to be traceable
    back to the complaint that went looking for it."""
    mod = _mod()
    derived = [t for t in mod.search_terms() if t.get("term")]
    assert derived
    for t in derived:
        assert t["from_objections"] >= 1
        assert t["campaigns"] and all(c for c in t["campaigns"])
        assert t["axes"] and all(a in ("robustness", "statistics", "economics",
                                       "leakage") for a in t["axes"])


def test_an_objection_the_vocabulary_cannot_read_is_counted_not_dropped():
    """A silently unread objection is a gap nobody discovers. It is reported as
    `unmatched` so the table's coverage is visible."""
    mod = _mod()
    fake = [{"campaign": "nq-x", "axis": "economics", "verdict": "VETO",
             "objection": "the commission assumption understates the cost of "
                          "crossing the spread at the open"}]
    out = mod.search_terms(fake)
    assert [t for t in out if t.get("unmatched")], (
        "an objection matching no vocabulary entry vanished instead of being "
        "reported")
    assert not [t for t in out if t.get("term")]


def test_the_two_term_sources_are_mutually_exclusive():
    """`--term` and `--from-open-questions` write the same registry field, and a
    row whose `query` cannot say which produced it is a row with no provenance."""
    scholar = " ".join((REPO_ROOT / "scripts" / "fetch_scholar.py").read_text().split())
    assert "--term and --from-open-questions are two different sources" in scholar
    assert "from_open_questions concepts=" in scholar, (
        "the derived query no longer records the concepts it searched for")
    assert "objection_campaigns=" in scholar, (
        "the derived query no longer records which campaigns complained")
