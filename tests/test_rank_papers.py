"""Stage 2 of paper retrieval: the ranker's contract, and where its context comes from.

No network. The valuable assertions are that `high` cannot be returned without a
concrete change to the next hypothesis, and that the "what we know" block is built
from committed artifacts rather than a hand-maintained list -- the latter being the
gap the design originally had.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "rank_papers", REPO_ROOT / "scripts" / "rank_papers.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

PAPER = {"title": "Order flow regimes", "abstract": "We detect regimes.", "categories": []}
ROW = {"feasibility": "adaptable", "adaptation": "use NQ", "claim": "regimes exist"}


class _Fake:
    def __init__(self, payload): self.payload = payload
    def ask(self, system, user): self.seen = user; return self.payload


def test_a_level_outside_the_three_is_refused():
    for bad in ("very high", "", None, "HIGH?"):
        with pytest.raises(ValueError):
            rp.rank_one(_Fake({"relevance": bad, "reason": "r"}), PAPER, ROW, "ctx")


def test_high_without_a_hypothesis_edit_is_refused():
    """A campaign is fifty looks at the data. A paper that would leave the next
    hypothesis identical has not earned them, so `high` requires naming the edit."""
    with pytest.raises(ValueError, match="high relevance with no hypothesis_edit"):
        rp.rank_one(_Fake({"relevance": "high", "reason": "r",
                           "hypothesis_edit": None}), PAPER, ROW, "ctx")
    ok = rp.rank_one(_Fake({"relevance": "high", "reason": "r",
                            "hypothesis_edit": "split the rule on regime"}), PAPER, ROW, "ctx")
    assert ok["hypothesis_edit"] == "split the rule on regime"


def test_low_and_medium_need_no_edit():
    for lvl in ("low", "medium"):
        got = rp.rank_one(_Fake({"relevance": lvl, "reason": "r"}), PAPER, ROW, "ctx")
        assert got["relevance"] == lvl and got["hypothesis_edit"] is None


# --------------------------------------------------------------------------- #
# the context block -- assembled, not maintained
# --------------------------------------------------------------------------- #

def test_the_open_questions_come_from_the_hypothesis_files():
    """The design originally said relevance needed a list of open questions that
    did not exist. It does exist: every hypothesis names what it set aside, before
    its campaign ran. All four of ours defer regime models."""
    got = rp.deferrals()
    assert got, "no deferrals parsed; the `What this is not asking for` heading moved"
    assert len(got) >= 4
    assert any("regime" in d.lower() for d in got), (
        "no hypothesis defers a regime model any more -- if that is deliberate the "
        "ranker's strongest positive signal has gone and this test should change "
        "with it")


def test_closed_questions_carry_the_verdict_but_not_the_score():
    """Outcomes are supplied so a closed question is not re-asked. Supplying the
    scores would invite ranking by resemblance to what scored well, which is
    selection on data already spent -- and retrieval costs no N precisely because
    it does not look at the market."""
    got = rp.closed_questions()
    assert got
    joined = " ".join(got)
    assert "rejected" in joined or "ACCEPTED" in joined
    for leak in ("sortino", "p=", "p =", "-0.", "+0."):
        assert leak not in joined.lower(), f"a score leaked into the ranker's context: {leak!r}"


def test_the_falsified_families_reach_the_ranker():
    got = rp.falsified()
    assert len(got) >= 3
    assert any("sequence model" in f.lower() for f in got)
    assert all(f in rp.context_block() for f in got)


def test_the_prompt_forbids_ranking_by_resemblance_to_past_scores():
    p = rp.PROMPT.read_text()
    assert "Do not rank a paper higher because it resembles something that scored well" in p
    for lvl in rp.LEVELS:
        assert f'"{lvl}"' in p, f"the prompt never offers {lvl!r}"
