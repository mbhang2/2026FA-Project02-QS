"""Between campaigns: diagnose from the redacted evidence, retrieve for it.

No network: the model is a fake, and the corpus is built here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import outcome_retrieval as orr  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DOCS = [
    {"id": "p-regime", "title": "Volatility regime filters for order flow signals",
     "claim": "Order flow imbalance predicts returns only in high volatility regimes.",
     "acts_on": "Trade the flow signal only when realised volatility is elevated."},
    {"id": "p-conc", "title": "Profit concentration and fragility of intraday strategies",
     "claim": "Returns concentrated in few sessions rarely persist.",
     "adaptation": "Require profit spread across sessions; cap per-session contribution."},
    {"id": "p-other", "title": "Lead-lag between index futures maturities",
     "claim": "The most liquid maturity leads."},
]

EVIDENCE = """# What a strategy written against it actually did
{"what_the_strategy_did": {"trades": 40}, "concentration": {"top5_share": 0.91}}

# What the auditors objected to

## robustness -- VETO
- profit is concentrated in five sessions, top5_share 0.91
- the order flow signal fires in calm sessions where it loses
"""


class _Client:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def ask(self, system, user):
        self.asked.append((system, user))
        return self.reply


def test_bm25_ranks_the_paper_about_the_problem_first():
    ranked = orr.bm25("volatility regime filter order flow", DOCS)
    assert ranked[0][1]["id"] == "p-regime"
    assert all(d["id"] != "p-other" for _, d in ranked[:1])


def test_a_problem_not_in_the_evidence_is_dropped():
    """A problem the evidence does not contain is the model's prior, and a
    search for it retrieves what it already believed."""
    client = _Client({"problems": [
        {"what": "Profit sits in a handful of sessions.",
         "evidence": "profit is concentrated in five sessions",
         "look_for": "x", "query": "profit concentration sessions"},
        {"what": "Should use machine learning.",
         "evidence": "deep networks outperform linear models on futures",
         "look_for": "x", "query": "deep learning futures"},
    ]})
    probs = orr.diagnose(client, "# q", EVIDENCE)
    assert [p["query"] for p in probs] == ["profit concentration sessions"]


def test_the_diagnosis_reads_only_what_it_is_given():
    client = _Client({"problems": []})
    orr.diagnose(client, "# the question", EVIDENCE)
    system, user = client.asked[0]
    assert "not told how the study scored it" in system
    assert user == "# The question\n\n# the question\n\n# The evidence\n\n" + EVIDENCE


def test_each_paper_knows_what_it_was_retrieved_for():
    probs = [{"what": "Profit sits in a handful of sessions.", "query": "profit concentration sessions"},
             {"what": "The signal fires in calm markets.", "query": "volatility regime order flow"}]
    got = orr.retrieve(probs, DOCS)
    by = {p["id"]: p for p in got}
    assert by["p-conc"]["retrieved_for"] == ["Profit sits in a handful of sessions."]
    assert by["p-regime"]["retrieved_for"] == ["The signal fires in calm markets."]
    assert "p-other" not in by or len(got) <= orr.MAX_PAPERS
    assert by["p-regime"]["claim"].startswith("Order flow imbalance")


def test_no_diagnosis_says_so_in_the_commit():
    assert "no problem diagnosed" in orr.commit_lines({"problems": [], "papers": []})
    assert orr.render({"problems": []}) == ""


def test_the_commit_records_problem_query_and_paper():
    found = {"problems": [{"what": "Profit sits in five sessions.", "query": "profit concentration",
                           "evidence": "e", "look_for": "l"}],
             "papers": [{"id": "p-conc", "retrieved_for": ["Profit sits in five sessions."]}],
             "fetched": None}
    text = orr.commit_lines(found)
    assert "Profit sits in five sessions.  [query: profit concentration]" in text
    assert "p-conc -- Profit sits in five sessions." in text


def test_fetching_new_papers_is_opt_in(monkeypatch):
    called = []
    monkeypatch.setattr(orr, "fetch_new", lambda probs, path, paper_model=None: called.append(probs) or 0)
    monkeypatch.setattr(orr, "corpus", lambda: DOCS)
    client = _Client({"problems": [{"what": "Profit sits in five sessions.",
                                    "evidence": "profit is concentrated in five sessions",
                                    "look_for": "x", "query": "profit concentration"}]})
    orr.plan(client, "# q", EVIDENCE, parent_path=ROOT / "x.md")
    assert called == []
    orr.plan(client, "# q", EVIDENCE, parent_path=ROOT / "x.md", fetch=True)
    assert len(called) == 1


def test_fetch_ranks_against_the_live_question(monkeypatch):
    seen = {}
    monkeypatch.setattr(orr.subprocess, "run",
                        lambda argv, cwd: seen.setdefault("argv", argv) and type("R", (), {"returncode": 0}))
    orr.fetch_new([{"query": "profit concentration"}], ROOT / "config" / "hypotheses" / "q.md")
    argv = seen["argv"]
    assert "scripts/retrieve.py" in argv
    assert argv[argv.index("--term") + 1] == "profit concentration"
    assert "--asking" in argv and "--distil" not in argv


# --------------------------------------------------------------------------- #
# the refiner, wired to it
# --------------------------------------------------------------------------- #
def _ev():
    return {"what_the_strategy_did": {"trades": 40}, "concentration": {"top5_share": 0.91},
            "candidates_tried": 20,
            "auditors": [{"axis": "robustness", "verdict": "VETO",
                          "reasons": ["profit is concentrated in five sessions"]}],
            "redacted": ["the gate's p-value", "the gate's verdict"]}


def test_the_diagnosis_and_the_refiner_read_the_same_evidence():
    rh = _load("refine_hypothesis")
    ev = _ev()
    assert rh.evidence_text(ev) in rh.context("# q", ev, [])


def test_retrieved_papers_reach_the_refiner_with_their_reason():
    rh = _load("refine_hypothesis")
    paper = {**orr.shape(DOCS[1]), "retrieved_for": ["Profit sits in five sessions."]}
    found = {"problems": [{"what": "Profit sits in five sessions.", "evidence": "e",
                           "look_for": "spread it", "query": "q"}], "papers": [paper]}
    text = rh.context("# q", _ev(), [paper], orr.render(found))
    assert "What the last campaign suggests needs work" in text
    assert "**Retrieved because:** Profit sits in five sessions." in text
    assert "**What it claims:** Returns concentrated in few sessions" in text
    assert "p-value" not in orr.render(found)


def test_the_refiner_retrieves_by_default_and_can_be_told_not_to():
    src = (ROOT / "scripts" / "refine_hypothesis.py").read_text()
    assert '"--no-retrieve"' in src and '"--fetch-new"' in src
    assert "if not a.no_retrieve:" in src
    assert "evidence_text(ev), parent_path=parent_path" in src
    sess = (ROOT / "scripts" / "research_session.py").read_text()
    assert '["--no-retrieve"]' in sess and '["--fetch-new"]' in sess


def test_the_real_registry_yields_papers_for_a_real_problem():
    docs = orr.corpus()
    if not docs:
        pytest.skip("no registry in this checkout")
    got = orr.retrieve([{"what": "w", "query": "order flow imbalance"}], docs)
    assert got and all(p["retrieved_for"] == ["w"] for p in got)


# --------------------------------------------------------------------------- #
# vetting: shared words are not an answer
# --------------------------------------------------------------------------- #
def _cands():
    probs = [{"what": "The signal fires in calm markets.", "look_for": "a regime filter",
              "query": "volatility regime order flow"}]
    return probs, orr.retrieve(probs, DOCS)


def test_only_papers_judged_to_answer_their_problem_are_kept():
    probs, cands = _cands()
    ids = [c["id"] for c in cands]
    client = _Client({"papers": [
        {"id": "p-regime", "addresses": True, "how": "Gate the flow signal on realised volatility."},
        *[{"id": i, "addresses": False, "how": "different subject"} for i in ids if i != "p-regime"]]})
    kept = orr.vet(client, probs, cands)
    assert [k["id"] for k in kept] == ["p-regime"]
    assert kept[0]["how"] == "Gate the flow signal on realised volatility."


def test_a_paper_the_reader_skipped_is_not_kept():
    probs, cands = _cands()
    assert orr.vet(_Client({"papers": []}), probs, cands) == []
    assert orr.vet(_Client({"papers": [{"id": "p-regime", "addresses": True, "how": ""}]}),
                   probs, cands) == []


def test_when_nothing_answers_the_record_says_the_refiner_used_the_diagnosis_alone():
    found = {"problems": [{"what": "w", "query": "q", "evidence": "e", "look_for": "l"}],
             "papers": [], "rejected": ["p-other"], "fetched": None}
    text = orr.commit_lines(found)
    assert "every change is the refiner's own judgement" in text
    assert "Left to the refiner's own judgement (no paper answered):\n  - w" in text
    assert "judged not to answer it: p-other" in text


def test_the_refiner_does_not_fall_back_to_overlap_after_a_diagnosis():
    """Falling back would hand it the loose matches vetting just rejected."""
    src = (ROOT / "scripts" / "refine_hypothesis.py").read_text()
    branch = src.split('elif found["problems"]:', 1)[1].split("else:", 1)[0]
    assert "papers = []" in branch



# --------------------------------------------------------------------------- #
# a thin library: discretion, per problem
# --------------------------------------------------------------------------- #
def _mixed():
    a = {"what": "Profit sits in five sessions.", "evidence": "e", "look_for": "l", "query": "q"}
    b = {"what": "Absorption is measured bar-wide.", "evidence": "e", "look_for": "l", "query": "q"}
    paper = {"id": "p-conc", "retrieved_for": [a["what"]], "how": "cap per-session share"}
    return {"problems": [a, b], "papers": [paper], "rejected": [], "fetched": None}


def test_each_problem_says_whether_a_paper_answers_it_or_it_is_the_refiners_call():
    text = orr.render(_mixed())
    first, second = text.split("2. ")
    assert "answered by: p-conc" in first
    assert "no paper answers this -- use your own judgement" in second
    assert "address the problem on your own judgement" in text
    assert "give no number you were not given" in text


def test_the_problems_left_to_judgement_are_exactly_the_unanswered_ones():
    assert orr.own_judgement(_mixed()) == ["Absorption is measured bar-wide."]
    text = orr.commit_lines(_mixed())
    assert "own judgement (no paper answered):\n  - Absorption is measured bar-wide." in text
    assert "p-conc -- cap per-session share" in text


def test_the_refiner_is_told_a_thin_library_is_no_reason_to_change_nothing():
    prompt = (ROOT / "config" / "prompts" / "hypothesis-refine.md").read_text()
    section = prompt.split("### When no paper fits, use your own judgement", 1)[1].split("###", 1)[0]
    assert "change the question on your" in section and "own judgement" in section
    assert "not a reason to leave a diagnosed problem alone" in section
    # discretion is not licence: the two rules it keeps
    assert "same question" in section and "every number must still come from" in section


class _Seq:
    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def ask(self, system, user):
        self.asked.append(user)
        return self.replies.pop(0)


_FROM_QUESTION = {"what": "w", "query": "q", "look_for": "l",
                  "evidence": "only a few hundred recent bars are pushed to the agent"}
_FROM_EVIDENCE = {"what": "Profit sits in five sessions.", "query": "profit concentration",
                  "look_for": "l", "evidence": "profit is concentrated in five sessions"}


def test_a_diagnosis_lost_to_quoting_the_question_is_asked_once_more():
    """Measured live: one run in three quoted the question as its evidence."""
    client = _Seq({"problems": [_FROM_QUESTION]}, {"problems": [_FROM_EVIDENCE]})
    probs = orr.diagnose(client, "# q", EVIDENCE)
    assert [p["query"] for p in probs] == ["profit concentration"]
    assert len(client.asked) == 2 and "was set aside" in client.asked[1]


def test_it_is_asked_at_most_twice():
    client = _Seq({"problems": [_FROM_QUESTION]}, {"problems": [_FROM_QUESTION]})
    assert orr.diagnose(client, "# q", EVIDENCE) == []
    assert len(client.asked) == 2


@pytest.mark.parametrize("first", [{"problems": []}, {"problems": [_FROM_EVIDENCE, _FROM_QUESTION]}])
def test_no_reask_when_the_model_named_nothing_or_something_stood(first):
    client = _Seq(first)
    orr.diagnose(client, "# q", EVIDENCE)
    assert len(client.asked) == 1


def test_the_instruction_says_the_question_is_not_evidence():
    assert "A line of the question itself is not" in orr.DIAGNOSE_SYSTEM
