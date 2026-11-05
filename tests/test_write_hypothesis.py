"""A question in, a hypothesis out -- or a draft judged refined enough, or refined.

No network: the model is a fake that replays replies, and the review seat is
stubbed. These pin the assessment, the gates the writer reuses, and that the
constraints it emits are the ones `fidelity.load` accepts.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def wh(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "write_hypothesis", ROOT / "scripts" / "write_hypothesis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The review seat is a model call; these tests are about everything else.
    monkeypatch.setattr(mod.dp, "_review_seat", lambda md: ([], "stubbed"))
    return mod


def _md(extra_not_yours: str = "Positions are opened and closed within one session.",
        words: int = 420) -> str:
    filler = " ".join(["the market"] * (words // 2))
    return f"""# Cumulative volume delta that fails to confirm a new high marks a reversal

A question about cumulative volume delta on NQ.

## The claim

When price makes a new intraday extreme and cumulative volume delta does not, the
move tends to reverse over the rest of the session. {filler}

## Why it might be true, and why it might not

Exhaustion of aggressive buyers, or noise.

## What is yours to choose

The bar interval and the lookback.

## What is NOT yours to choose

{extra_not_yours}
The strategy trades against the move.

## What would make the answer interesting

Either answer tells us whether delta leads price here.

## What this is not asking for

Order book depth.
"""


def test_a_complete_draft_is_refined_enough(wh):
    v = wh.assess(_md())
    assert v["refined_enough"], v
    assert v["findings"] == [] and v["advice"] == []


def test_a_one_line_idea_is_not(wh):
    v = wh.assess("I want a trend reversal strategy that looks into CVD")
    assert not v["refined_enough"]
    text = " ".join(v["findings"])
    assert "heading" in text and "what is NOT yours to choose" in text
    assert "length" in text


def test_gate_vocabulary_makes_a_draft_not_refined(wh):
    v = wh.assess(_md("The effect must be statistically significant."))
    assert not v["refined_enough"]
    assert any("gate vocabulary" in f for f in v["findings"])


def test_a_citation_we_do_not_hold_is_found(wh):
    v = wh.assess(_md("It follows arXiv:2699.99999, which we do not hold."))
    assert any("ungrounded" in f for f in v["findings"])


def test_the_seat_only_runs_on_a_mechanically_clean_draft(wh, monkeypatch):
    calls = []
    monkeypatch.setattr(wh.dp, "_review_seat",
                        lambda md: (calls.append(md), (["too vague"], "OBJECT"))[1])
    assert not wh.assess("one line")["refined_enough"] and calls == []
    v = wh.assess(_md())
    assert calls and not v["refined_enough"] and v["concerns"] == ["too vague"]


def test_emitted_constraints_are_checked_against_the_markdown(wh):
    md = _md()
    good = {"constraints": [{"id": "one-session", "says":
                             "Positions are opened and closed within one session.",
                             "check": "no_overnight", "from": "question"}],
            "judged": [{"id": "fades", "says": "The strategy trades against the move.",
                        "from": "question"}]}
    y = wh.constraints_yaml("cvd-divergence", good)
    assert wh.constraint_findings(y, md) == []
    bad = {"constraints": [{"id": "x", "says": "Never hold overnight.",
                            "check": "no_overnight", "from": "question"}]}
    assert wh.constraint_findings(wh.constraints_yaml("s", bad), md)
    assert wh.constraint_findings(wh.constraints_yaml("s", {}), md) == []


class _Client:
    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def ask(self, system, user):
        self.asked.append(user)
        return self.replies.pop(0)


def _reply(md, **kw):
    return {"slug": "cvd-divergence-reversal", "markdown": md,
            "question_as_asked": "q", "adapted_claim": "c", "cannot_test": None,
            "papers_used": [], **kw}


def test_the_writer_repairs_then_returns_a_clean_file(wh):
    leaky = _md("The result must be statistically significant.")
    client = _Client(_reply(leaky), _reply(_md(), constraints=[
        {"id": "one-session", "says": "Positions are opened and closed within one session.",
         "check": "no_overnight", "from": "question"}]))
    got = wh.write(client, "I want a trend reversal strategy that looks into CVD", "", [])
    assert got["slug"] == "cvd-divergence-reversal"
    assert "no_overnight" in got["constraints_yaml"]
    assert "Your previous answer was refused" in client.asked[1]
    assert "gate vocabulary" in client.asked[1]
    # the question reaches the writer, which is the point
    assert "looks into CVD" in client.asked[0]


def test_the_writer_refuses_a_file_that_never_passes(wh):
    leaky = _md("The result must be statistically significant.")
    client = _Client(*[_reply(leaky)] * 10)
    with pytest.raises(ValueError, match="did not pass its gates"):
        wh.write(client, "q", "", [])


def test_bad_constraints_are_dropped_not_the_question(wh):
    bad = [{"id": "x", "says": "Not in the file.", "check": "no_overnight",
            "from": "question"}]
    client = _Client(*[_reply(_md(), constraints=bad)] * 10)
    got = wh.write(client, "q", "", [])
    assert got["markdown"].startswith("# ") and got["constraints_yaml"] == ""


def test_a_draft_is_never_overwritten(wh, tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "HYPOTHESES", tmp_path)
    monkeypatch.setattr(wh, "ROOT", tmp_path)
    (tmp_path / "cvd-divergence-reversal.md").write_text("mine")
    got = {"slug": "cvd-divergence-reversal", "markdown": "# x\n",
           "constraints_yaml": "", "papers_used": [], "question_as_asked": "",
           "adapted_claim": "", "cannot_test": None, "review": "", "words": 1}
    with pytest.raises(FileExistsError):
        wh.write_files(got, question="q", draft_path=None, papers=[], model="m",
                       served="m")
    assert (tmp_path / "cvd-divergence-reversal.md").read_text() == "mine"


def test_provenance_sits_beside_the_file_not_in_it(wh, tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "HYPOTHESES", tmp_path)
    monkeypatch.setattr(wh, "ROOT", tmp_path)
    got = {"slug": "cvd-x", "markdown": "# x\n", "constraints_yaml": "schema: x\njudged: []\n",
           "papers_used": [], "question_as_asked": "q", "adapted_claim": "c",
           "cannot_test": None, "review": "NO_OBJECTION", "words": 1}
    files = wh.write_files(got, question="the question", draft_path=None,
                           papers=[], model="m", served="m")
    assert [f.name for f in files] == ["cvd-x.md", "cvd-x.constraints.yaml",
                                       "cvd-x.origin.json"]
    assert json.loads((tmp_path / "cvd-x.origin.json").read_text())["question"] == "the question"
    assert (tmp_path / "cvd-x.md").read_text() == "# x\n"


def test_the_prompt_makes_the_question_the_subject():
    p = (ROOT / "config" / "prompts" / "question-distil.md").read_text()
    assert "The researcher's question is the subject" in p
    assert "Never replace the researcher's question with a paper's" in p


@pytest.mark.parametrize("heading", ["## The question", "## The claim"])
def test_sections_are_matched_by_meaning_not_spelling(wh, heading):
    md = _md().replace("## The claim", heading).replace(
        "## What is yours to choose", "## What is yours to decide").replace(
        "## What is NOT yours to choose", "## What is NOT yours")
    assert wh.assess(md)["findings"] == []


# --------------------------------------------------------------------------- #
# the writer fixes only what the researcher fixed
# --------------------------------------------------------------------------- #
def test_a_limit_the_researcher_did_not_ask_for_is_dropped(wh):
    client = _Client(_reply(_md(), constraints=[
        {"id": "one-session", "says": "Positions are opened and closed within one session.",
         "check": "no_overnight", "from": "inferred"}],
        judged=[{"id": "fades", "says": "The strategy trades against the move.",
                 "from": "question"}]))
    got = wh.write(client, "q", "", [])
    assert got["dropped"] == ["one-session"]
    assert "no_overnight" not in got["constraints_yaml"]
    assert "fades" in got["constraints_yaml"]


def test_a_constraint_with_no_origin_is_not_the_researchers(wh):
    kept, dropped = wh.split_by_origin([{"id": "a", "from": "draft"}, {"id": "b"},
                                        {"id": "c", "from": "house"}])
    assert [k["id"] for k in kept] == ["a"] and "from" not in kept[0]
    assert [d["id"] for d in dropped] == ["b", "c"], "house comes from the file, never the model"


def test_the_house_file_is_valid_and_its_sentences_are_inserted_verbatim(wh):
    house = wh.load_house()
    md = wh.add_house(_md(), house)
    for h in house:
        assert h["says"] in md
    # inside the NOT-yours section, before the next heading
    section = md.split("## What is NOT yours to choose", 1)[1].split("\n## ", 1)[0]
    assert all(h["says"] in section for h in house)
    assert wh.add_house(md, house) == md, "idempotent"
    y = wh.constraints_yaml("cvd", {}, house)
    assert wh.constraint_findings(y, md) == []


def test_house_constraints_reach_the_written_file(wh):
    client = _Client(_reply(_md()))
    got = wh.write(client, "q", "", [], house=wh.load_house())
    assert got["house"] and "house-one-contract" in got["constraints_yaml"]
    assert "At most 1 contract is held at a time." in got["markdown"]


def test_the_writer_is_told_not_to_set_limits():
    p = (ROOT / "config" / "prompts" / "question-distil.md").read_text()
    assert "Fix only what the researcher fixed" in p
    assert '"from"' in p


def test_one_bad_constraint_drops_itself_not_the_others_or_the_review(wh, monkeypatch):
    seen = []
    monkeypatch.setattr(wh.dp, "_review_seat", lambda md: (seen.append(md), ([], "NO_OBJECTION"))[1])
    reply = _reply(_md(), constraints=[
        {"id": "instrument-is-nq", "says": "The strategy trades against the move.",
         "check": "code_uses", "any_of": ["NQ index"], "from": "question"},
        {"id": "one-session", "says": "Positions are opened and closed within one session.",
         "check": "no_overnight", "from": "question"}])
    client = _Client(reply)
    got = wh.write(client, "q", "", [])
    assert len(client.asked) == 1, "a bad constraint is not a reason to rewrite"
    assert seen, "the review seat still reads the question"
    assert "one-session" in got["constraints_yaml"]
    assert "instrument-is-nq" not in got["constraints_yaml"]


def test_a_judged_requirement_filed_as_a_constraint_is_moved(wh):
    out = {"constraints": [{"id": "div", "says": "The strategy trades against the move.",
                            "check": "judged", "from": "question"}]}
    fixed, why = wh.valid_items(out, _md())
    assert why == [] and fixed["constraints"] == []
    assert [j["id"] for j in fixed["judged"]] == ["div"]
