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


# --------------------------------------------------------------------------- #
# unattended: a statement in, a committed question out (Amendment 15)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,ok", [
    ("NO_OBJECTION (mimo-v2.6-pro)", True),
    ("CANNOT_TELL (mimo-v2.6-pro)", False),
    ("OBJECT (mimo-v2.6-pro)", False),
    ("DID-NOT-RUN (ReadTimeout) -- NOT reviewed", False),
    ("NO_OBJECTION (m) -- unreadable verdict, NOT reviewed", False),
    ("OBJECT (m) -- objected 2x, cap reached, taken WITHOUT the seat clearing it", False),
    ("NOT-REACHED (the mechanical gates were never all clean)", False),
    ("", False),
])
def test_only_a_seat_that_read_it_and_did_not_object_clears_it(wh, status, ok):
    """With no human, the seat is the only reader; anything short of a reading
    with no objection is a refusal."""
    assert wh.seat_cleared(status) is ok


@pytest.fixture()
def unattended(wh, tmp_path, monkeypatch):
    """The writer with every outside call faked, rehearsing into tmp_path."""
    monkeypatch.setenv("PRACTICUM_RUNS_ROOT", str(tmp_path))
    monkeypatch.setattr(wh, "pick_papers", lambda q: [])
    monkeypatch.setattr(wh.dp, "campaign_in_flight", lambda: "")
    monkeypatch.setattr(wh.dp, "_review_seat", lambda md: ([], "NO_OBJECTION (m)"))

    def no_commit(*a, **k):
        raise AssertionError("a rehearsal must not commit")

    monkeypatch.setattr(wh, "commit_files", no_commit)

    def use(*replies):
        client = _Client(*replies)
        monkeypatch.setattr(wh.dp, "_client", lambda model: (client, "served-m"))
        return client
    return use


def test_a_statement_becomes_a_marked_question(wh, unattended, tmp_path):
    unattended(_reply(_md()))
    got = wh.write_unattended("  CVD divergence   reverses trends ", campaign="nq-x-01")
    assert got["path"] == tmp_path / "hypotheses" / "cvd-divergence-reversal.md"
    assert got["commit"] is None                        # a rehearsal
    o = got["origin"]
    assert o["machine_written"] and o["amendment"] == 15
    assert o["read_by_a_human"] is False
    assert o["statement"] == "CVD divergence reverses trends"
    side = json.loads((tmp_path / "hypotheses" /
                       "cvd-divergence-reversal.origin.json").read_text())
    assert side["machine_written"] == {"amendment": 15, "campaign": "nq-x-01",
                                       "read_by_a_human": False}


def test_a_seat_that_could_not_tell_stops_it_before_anything_is_written(
        wh, unattended, tmp_path, monkeypatch):
    unattended(*[_reply(_md())] * 4)
    monkeypatch.setattr(wh.dp, "_review_seat", lambda md: ([], "CANNOT_TELL (m)"))
    with pytest.raises(wh.Unattended, match="did not clear it"):
        wh.write_unattended("q", campaign="nq-x-01")
    assert not (tmp_path / "hypotheses").exists()


def test_nothing_is_written_while_a_campaign_runs(wh, unattended, monkeypatch):
    unattended(_reply(_md()))
    monkeypatch.setattr(wh.dp, "campaign_in_flight", lambda: "nq-s07-01")
    with pytest.raises(wh.Unattended, match="nq-s07-01 is running"):
        wh.write_unattended("q", campaign="nq-x-01")


def test_a_stated_market_path_is_sent_back_to_the_writer(wh, unattended):
    client = unattended(_reply(_md("NQ fell 4% in March after the FOMC.")),
                        _reply(_md()))
    wh.write_unattended("q", campaign="nq-x-01")
    assert "realised outcome" in client.asked[1]


def test_a_gate_failure_is_a_refusal_not_a_crash(wh, unattended):
    unattended(*[_reply(_md("The result must be statistically significant."))] * 10)
    with pytest.raises(wh.Unattended, match="did not pass its gates"):
        wh.write_unattended("q", campaign="nq-x-01")


def test_outside_a_rehearsal_it_commits_marked(wh, unattended, tmp_path, monkeypatch):
    monkeypatch.delenv("PRACTICUM_RUNS_ROOT")
    monkeypatch.setattr(wh, "HYPOTHESES", tmp_path / "config" / "hypotheses")
    monkeypatch.setattr(wh, "ROOT", tmp_path)
    seen = {}

    def fake_commit(files, message):
        seen["files"], seen["message"] = files, message
        return "abc123def456"

    monkeypatch.setattr(wh, "commit_files", fake_commit)
    unattended(_reply(_md()))
    got = wh.write_unattended("CVD divergence reverses trends", campaign="nq-x-01")
    assert got["commit"] == "abc123def456" == got["origin"]["commit"]
    assert [f.name for f in seen["files"]][0] == "cvd-divergence-reversal.md"
    m = seen["message"]
    assert "Machine-Written: nq-x-01" in m
    assert "Statement, verbatim: CVD divergence reverses trends" in m
    assert "may NOT support a claim" in m
    assert "Hypothesis-Review: NO_OBJECTION" in m


def test_a_commit_takes_only_its_own_files(wh, tmp_path, monkeypatch):
    """Whatever else the researcher has staged stays staged, and out of a
    pre-registration commit."""
    import subprocess

    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "seed").write_text("x")
    git("add", "seed")
    git("commit", "-q", "-m", "seed")
    (tmp_path / "theirs.txt").write_text("staged, not ours")
    git("add", "theirs.txt")
    ours = tmp_path / "q.md"
    ours.write_text("# q\n")
    monkeypatch.setattr(wh, "ROOT", tmp_path)
    sha = wh.commit_files([ours], "hypothesis: q\n\nMachine-Written: c\n")
    assert git("show", "--name-only", "--format=", sha).split() == ["q.md"]
    assert git("diff", "--cached", "--name-only").split() == ["theirs.txt"]


def test_a_researchers_own_draft_is_not_held_to_the_outcome_lint(wh):
    """Their words, and a human has read them; the lint is for what the
    writer produces."""
    md = _md("Written 2026-09-21. NQ fell 4% in March, which prompted this.")
    assert not [f for f in wh.mechanical_findings(md, md, draft=True)
                if f.startswith("realised outcome")]
    assert [f for f in wh.mechanical_findings(md, md)
            if f.startswith("realised outcome")]


def test_a_substituted_writer_is_a_refusal_not_a_traceback(wh, unattended, monkeypatch, tmp_path):
    """Found live on 2026-11-11: the writer's model was being answered by
    gemini, and the preflight's refusal escaped `register` as a stack trace."""
    from audit.committee.llm import ModelSubstituted

    def substituted(model):
        raise ModelSubstituted(f"asked {model!r}, served 'gemini-3.1-pro-preview'")

    monkeypatch.setattr(wh.dp, "_client", substituted)
    with pytest.raises(wh.Unattended, match="writer's model could not be used"):
        wh.write_unattended("q", campaign="nq-x-01")
    assert not (tmp_path / "hypotheses").exists()


def test_register_writes_the_question_only_after_everything_that_could_refuse():
    """A committed Machine-Written question with no campaign on it is the
    failure: every local refusal and the proposer's preflight come first."""
    src = (ROOT / "scripts" / "nq_campaign.py").read_text()
    body = src.split("def stage_register", 1)[1].split("\ndef ", 1)[0]
    write_at = body.index("write_unattended(")
    for before in ("already exists. A pre-registration",
                   "declares_fit(seed_text)", "_static_ok(",
                   "if min_exposed is not None:", ".preflight()"):
        assert body.index(before) < write_at, before
    assert body.index('agent_cfg["hypothesis_origin"] = hypothesis_origin') > write_at


def test_the_writer_and_reviewer_models_are_the_ones_asked_for(wh, unattended, monkeypatch):
    """`--writer-model` / `--reviewer-model`: each reaches its own call, and
    the record says which ran."""
    unattended(_reply(_md()))
    asked = {}
    real_client = wh.dp._client

    def client(model):
        asked["writer"] = model
        return real_client(model)

    def seat(md, model=None):
        asked["reviewer"] = model
        return [], f"NO_OBJECTION ({model})"

    monkeypatch.setattr(wh.dp, "_client", client)
    monkeypatch.setattr(wh.dp, "_review_seat", seat)
    got = wh.write_unattended("q", campaign="nq-x-01", model="writer-m",
                              reviewer_model="reviewer-m")
    assert asked == {"writer": "writer-m", "reviewer": "reviewer-m"}
    assert got["origin"]["writer_model"] == "writer-m"
    assert got["origin"]["reviewer_model"] == "reviewer-m"
    assert got["origin"]["review"] == "NO_OBJECTION (reviewer-m)"


def test_without_the_flags_the_defaults_run(wh, unattended, monkeypatch):
    unattended(_reply(_md()))
    seen = {}
    real_client = wh.dp._client
    monkeypatch.setattr(wh.dp, "_client",
                        lambda m: (seen.setdefault("writer", m), real_client(m))[1])
    monkeypatch.setattr(wh.dp, "_review_seat",
                        lambda md, **k: (seen.setdefault("seat_kw", k),
                                         ([], "NO_OBJECTION (d)"))[1])
    got = wh.write_unattended("q", campaign="nq-x-01")
    assert seen == {"writer": wh.MODEL, "seat_kw": {}}
    assert got["origin"]["reviewer_model"] == "default"


def test_register_passes_both_models_through():
    src = (ROOT / "scripts" / "nq_campaign.py").read_text()
    for flag in ('"--writer-model"', '"--reviewer-model"',
                 "model=writer_model", "reviewer_model=reviewer_model",
                 "writer_model=a.writer_model", "reviewer_model=a.reviewer_model"):
        assert flag in src, flag
    sess = (ROOT / "scripts" / "research_session.py").read_text()
    assert '("--writer-model", "writer_model")' in sess
    assert '("--reviewer-model", "reviewer_model")' in sess


def test_the_committee_model_is_frozen_at_registration_and_read_at_report():
    """Chosen after the trials ran, it would be a committee chosen after seeing
    what it will judge."""
    src = (ROOT / "scripts" / "nq_campaign.py").read_text()
    reg = src.split("def stage_register", 1)[1].split("\ndef ", 1)[0]
    assert 'agent_cfg["audit_model"] = audit_model' in reg
    rep = src.split("def stage_report", 1)[1].split("\ndef ", 1)[0] \
        if "def stage_report" in src else src
    assert '(record.agent_config or {}).get("audit_model")' in src
    assert '"--audit-model"' in src and "audit_model=a.audit_model" in src
    sess = (ROOT / "scripts" / "research_session.py").read_text()
    assert '("--audit-model", "audit_model")' in sess
