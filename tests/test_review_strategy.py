"""The code reviewer — Brian's point 2, third place: "code review between trials".

The per-trial gate that exists is `compile()`, an AST pass and mypy against the
Strategy protocol: syntax and types. **Nothing had ever read 19 of a campaign's
20 candidates** for whether the code is a fair attempt at the question; the
four-seat committee reads one champion, after the fact.

Measured 2026-10-04 on `nq-s04-03` — the campaign whose champion made four
trades and scored `-inf`, so `n_nulls` was 0 and twenty trials bought nothing:

    OBJECT 4, NO_OBJECTION 8, CANNOT_TELL 2, seat failed 1   (of 15)

Concern 2, "it can barely act", fired on the candidates that could not act, with
mechanical diagnoses — "the opening range is calculated incorrectly using only
the first bar of the session", "entry orders are almost always rejected by the
trading framework". **Score-blind**, so those are statements about the code and
not about an outcome it was shown. The 2 CANNOT_TELLs are the `agent_blocked`
placeholders, which contain no strategy at all.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "review_strategy.py").read_text()
FLAT = " ".join(SRC.split())
PROMPT = (REPO_ROOT / "config" / "prompts" / "strategy-review.md").read_text()
PFLAT = " ".join(PROMPT.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "review_strategy", REPO_ROOT / "scripts" / "review_strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# it is not a gate, and that is the protocol-relevant part
# --------------------------------------------------------------------------- #
def test_it_records_and_does_not_block():
    """Blocking a candidate before it is scored changes which candidates the
    search sees. That is a change to a registered procedure, not a bug fix, and
    it needs its own pre-registration -- the same reason `max_of_n` counts every
    candidate rather than the ones we liked."""
    assert "RECORDED, not gated" in SRC
    assert '"is_a_gate": False' in SRC
    assert "needs its own" in FLAT and "pre-registration" in FLAT
    # nothing here may touch the campaign machinery
    assert "nq_campaign" not in SRC
    assert "current.py" not in SRC


def test_it_cannot_approve():
    m = _mod()
    assert m.VERDICTS == ("OBJECT", "CANNOT_TELL", "NO_OBJECTION")
    assert "NO_OBJECTION is not approval" in FLAT
    assert "does not mean the strategy" in PFLAT


# --------------------------------------------------------------------------- #
# score-blind
# --------------------------------------------------------------------------- #
def test_the_seat_is_never_shown_a_score():
    """The scores sit on disk beside the sources. A reviewer told "this one won"
    finds reasons it is sound, which would make the objections worthless as
    evidence."""
    m = _mod()
    ctx = m.context("def f(): pass", "# the question\n")
    assert "Deliberately withheld" in ctx
    assert "this candidate's score" in ctx
    assert "whether this candidate was the one the campaign kept" in ctx
    assert '"score_blind": True' in SRC


def test_the_candidate_list_carries_no_score():
    """`candidates()` reads the ledger, which has `score` on every row. Asserted
    on what it returns rather than on its source, because the word "score"
    appears in its own docstring saying it withholds one."""
    m = _mod()
    rows = m.candidates("nq-s04-03")
    assert rows, "no candidates found to check"
    for r in rows:
        assert "score" not in r, r
        assert set(r) <= {"source_name", "path", "digest", "outcome"}, r
    # and the context builder is given only the source and the question
    import inspect
    sig = inspect.signature(m.context)
    assert list(sig.parameters) == ["source", "hypothesis"]


# --------------------------------------------------------------------------- #
# it reviews the code that actually ran
# --------------------------------------------------------------------------- #
def test_the_hypothesis_digest_is_verified():
    """A hypothesis is a pre-registration. Reviewing candidates against a file
    that changed since they ran would judge them on a question they were never
    asked."""
    assert "hypothesis_digest" in SRC
    assert "never asked" in FLAT
    assert "hashlib" in SRC


def test_the_hypothesis_path_is_read_from_the_record():
    """It lives under `record`, not at the top level of campaign.json."""
    assert 'rec = c.get("record") or {}' in SRC


def test_a_real_campaign_resolves_and_the_digest_matches():
    m = _mod()
    text, path = m.hypothesis_for("nq-s04-03")
    assert path.endswith(".md") and len(text) > 500
    cands = m.candidates("nq-s04-03")
    assert len(cands) >= 10, "every scored candidate's source should be found"
    for c in cands:
        assert c["path"].is_file()


# --------------------------------------------------------------------------- #
# the seat's own failures are not verdicts
# --------------------------------------------------------------------------- #
def test_a_failed_call_is_reported_as_a_coverage_hole():
    """First run over nq-s04-03 lost 2 of 15 replies to `Unterminated string` --
    a newline inside a JSON string, because this seat quotes source lines. 13%
    silent loss of coverage filed as "cannot tell" would read as a judgement."""
    assert "MAX_FORMAT_RETRIES" in SRC
    assert '"failed": True' in SRC
    assert "SEAT_FAILED" in SRC
    assert "coverage hole, not a verdict" in FLAT
    assert "seats_failed" in SRC


def test_the_model_is_independent_of_every_proposer():
    """Same reasoning the committee was fixed for on 2026-10-04: the model that
    audits must not be the model that wrote."""
    m = _mod()
    assert m.MODEL == "gemini-2.5-pro"
    assert "claude-haiku-4-5" in FLAT and "deepseek-v4-pro" in FLAT


def test_the_prompt_does_not_ask_for_improvements():
    """A reviewer that suggests features is a second proposer. The question is
    whether the code is a fair attempt, not whether it could be better."""
    assert "Suggesting improvements is not reviewing" in PFLAT
    assert "Style, naming, comments" in PFLAT
    assert "will probably lose money" in PFLAT


def test_the_recorded_result_exists_and_says_what_it_is():
    """The run on nq-s04-03 is committed as evidence."""
    f = REPO_ROOT / "runs" / "campaigns" / "nq-s04-03" / "strategy-reviews.json"
    if not f.exists():
        return
    d = json.loads(f.read_text())
    assert d["is_a_gate"] is False
    assert d["score_blind"] is True
    assert d["tally"].get("OBJECT", 0) >= 1, "it objected to something"
    assert "not a pass rate" in d["approval_note"]
