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


def _ok(**kw):
    """A payload valid on both axes, overridden per test. Axis 2 became mandatory
    on 2026-09-27, so every payload that is meant to SUCCEED has to carry it."""
    base = {"relevance": "low", "reason": "r", "gate_relevance": "low",
            "gate_reason": "g"}
    base.update(kw)
    return base


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
    ok = rp.rank_one(_Fake(_ok(relevance="high",
                               hypothesis_edit="split the rule on regime")),
                     PAPER, ROW, "ctx")
    assert ok["hypothesis_edit"] == "split the rule on regime"


def test_low_and_medium_need_no_edit():
    for lvl in ("low", "medium"):
        got = rp.rank_one(_Fake(_ok(relevance=lvl)), PAPER, ROW, "ctx")
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


# --------------------------------------------------------------------------- #
# axis 2 -- does the paper bear on how we JUDGE a result? (added 2026-09-27)
#
# Axis 1 scored the first 224 papers and rated NONE of them high. That was the
# scale working as written, not a bad corpus: its first criterion is "what would
# the strategy do differently on Tuesday morning?", so a systematic falsification
# study on five-minute bars of the same index we trade -- with its own acceptance
# criteria, t >= 2.0, >= 30 trades per fold, year stability -- scores low, because
# it offers no edge to act on. For a project whose deliverable is the loop and its
# gate, that paper is among the most useful in the corpus. Axis 1 cannot say so.
# --------------------------------------------------------------------------- #

def test_the_second_axis_is_mandatory():
    """A payload with no `gate_relevance` is refused rather than defaulted. A
    default would silently reproduce the single-axis result on every future run."""
    with pytest.raises(ValueError, match="gate_relevance"):
        rp.rank_one(_Fake({"relevance": "low", "reason": "r"}), PAPER, ROW, "ctx")
    for bad in ("very high", "", None, "HIGH?"):
        with pytest.raises(ValueError, match="gate_relevance"):
            rp.rank_one(_Fake({"relevance": "low", "reason": "r",
                               "gate_relevance": bad}), PAPER, ROW, "ctx")


def test_high_on_the_gate_axis_needs_a_named_protocol_change():
    """The same bar as axis 1, for the same reason. A paper that would leave the
    gate, the accounting and the falsified-families list exactly as they are is
    not high, however well argued."""
    with pytest.raises(ValueError, match="high gate_relevance with no protocol_edit"):
        rp.rank_one(_Fake(_ok(gate_relevance="high", protocol_edit=None)),
                    PAPER, ROW, "ctx")
    got = rp.rank_one(_Fake(_ok(gate_relevance="high", bears_on="gate",
                                protocol_edit="measure our floor against theirs")),
                      PAPER, ROW, "ctx")
    assert got["protocol_edit"] == "measure our floor against theirs"
    assert got["bears_on"] == "gate"


def test_the_axes_are_independent():
    """The whole point: a paper with nothing to trade can still change how we
    judge. `low` on axis 1 and `high` on axis 2 has to be expressible, and is the
    expected shape for a falsification study on our own instrument."""
    got = rp.rank_one(_Fake(_ok(relevance="low", acts_on=None,
                                hypothesis_edit=None,
                                gate_relevance="high",
                                bears_on="falsified_family",
                                protocol_edit="add OHLCV intraday momentum to the "
                                              "falsified-families list")),
                      PAPER, ROW, "ctx")
    assert got["relevance"] == "low" and got["gate_relevance"] == "high"
    assert got["acts_on"] is None and got["protocol_edit"]


def test_bears_on_is_a_closed_set_or_null():
    for bad in ("everything", "strategy", "Gate"):
        with pytest.raises(ValueError, match="bears_on"):
            rp.rank_one(_Fake(_ok(bears_on=bad)), PAPER, ROW, "ctx")
    assert rp.rank_one(_Fake(_ok(bears_on=None)), PAPER, ROW, "ctx")["bears_on"] is None
    for good in rp.BEARS_ON:
        assert rp.rank_one(_Fake(_ok(bears_on=good)),
                           PAPER, ROW, "ctx")["bears_on"] == good


def test_the_prompt_defines_both_axes_and_says_they_are_independent():
    # Whitespace-normalised: the prompt is hard-wrapped, so a phrase the test
    # cares about can be split across a line break.
    text = " ".join(rp.PROMPT.read_text().lower().split())
    assert "gate_relevance" in text and "relevance" in text
    assert "two independent axes" in text or "two separate judgements" in text
    assert "do not average them" in text
    # the concrete case that motivated the axis has to be in the prompt, so the
    # model is told what axis 1 is blind to rather than left to infer it
    assert "falsification" in text and "no edge" in text


def test_the_resemblance_warning_covers_both_axes():
    """Ranking by resemblance to what scored well is selection on spent data. The
    warning predates the second axis and has to bind it too."""
    text = " ".join(rp.PROMPT.read_text().lower().split())
    assert "resemblance" in text
    assert "applies to both axes without exception" in text


def test_the_rerank_flag_adds_the_axis_without_erasing_the_first_pass():
    src = (REPO_ROOT / "scripts" / "rank_papers.py").read_text()
    assert '"--rerank"' in src
    # picks papers missing the SECOND axis, not papers missing the first
    assert 'r.get("gate_relevance") is None' in src
    # and a moved axis-1 label is kept, because it says something about the scale
    assert 'row["relevance_first_pass"] = was' in src


# --------------------------------------------------------------------------- #
# the boundary between the retrieval side and validation (added 2026-09-29)
# --------------------------------------------------------------------------- #
def test_the_ranker_never_reads_a_papers_outcome():
    """**§4: "Campaign k+1 may depend on prior campaigns' train results — never
    on their validation results."**

    The feedback edge writes a record onto each paper's row when its campaign
    concludes. That record is train-side by construction, but the committee
    verdict inside it is validation-informed: the report stage hands the auditors
    a summary with the validation numbers in it. So the field list is not the
    protection. This is.

    A ranker that saw `outcome` would be ranking papers by how their campaigns
    ended, which is selection on the thing being tested, and retrieval costs no
    trial count only because it never consults our data.
    """
    sentinel = "ZQ-OUTCOME-SENTINEL-4417"
    row = {**ROW,
           "outcome": [{"campaign_id": sentinel, "gate": sentinel,
                        "committee": sentinel, "stopped_by": sentinel}],
           "campaign_ids": [sentinel]}
    fake = _Fake(_ok())
    rp.rank_one(fake, PAPER, row, "ctx")
    assert sentinel not in fake.seen, (
        "a paper's campaign outcome reached the ranking prompt")


def test_the_assembled_context_carries_no_validation_number():
    """The context block is built from committed artefacts. `closed_questions`
    reads each campaign's tier-2 summary, which is the L1 gate on TRAIN, and
    deliberately not its validation. Asserted against the real tree, where six
    campaigns have validation scores on disk."""
    ctx = rp.context_block()
    import json
    import pathlib
    for d in (REPO_ROOT / "runs" / "campaigns").glob("*/validation.json"):
        v = json.loads(d.read_text())
        primary = (v.get("validation") or {}).get("primary")
        if primary is None:
            continue
        for form in (f"{primary:.4f}", f"{primary:.3f}", f"{primary:.2f}"):
            assert form.lstrip("-") not in ctx, (
                f"{d.parent.name}'s validation score {form} reached the ranker")
    # and it does carry the train-side verdict, which §4 permits
    assert "rejected" in ctx and "closed" in ctx.lower()
