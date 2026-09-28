"""Stage 3 of paper retrieval: the one stage that produces a registerable file.

No network. The assertions that carry weight are the three refusals, because this
is the first place in the project where **a machine writes prose that goes verbatim
into the strategy agent's prompt**:

1. A distillation mentioning the gate is refused, not warned about. It is linted
   with the same function `nq_campaign.py` runs at registration, before anything is
   written.
2. It cannot write while a campaign runs, because `config/hypotheses/*.md` is
   fingerprinted and a new file there invalidates the trial that straddles it.
3. It never overwrites an existing hypothesis, whose commit date is its
   pre-registration date.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "distil_paper", REPO_ROOT / "scripts" / "distil_paper.py")
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

PAPER = {"title": "Structural limits of intraday momentum in MNQ futures",
         "abstract": "We falsify a family of OHLCV momentum signals."}
ROW = {"id": "2605.04004v3", "feasibility": "direct",
       "claim": "intraday momentum does not survive costs",
       "adaptation": "test on NQ five-minute bars"}

BODY = """# Intraday momentum in this instrument does not survive its own costs

From paper 2605.04004v3, a falsification study of OHLCV momentum signals. This
campaign asks whether the same families fail here once every trade is charged.

## The claim
A rule built from bar opens, highs, lows, closes and volume, entered on a
continuation signal and exited on a bracket, does not earn more than it pays in
commission over a few hundred sessions of this instrument.

## Why it might be true, and why it might not
Costs are paid per trade and continuation signals fire often, so turnover is the
enemy. Against it: the paper used five-minute bars and one bracket shape, and a
slower rule on a wider bracket is a different animal.

## What is yours to choose
The bar interval, the lookback, how the signal is scaled by recent range, and the
stop and target.

## What is NOT yours to choose
That the signal comes from bars alone. A rule reaching for the aggressor split is
answering a different question.

## What would make the answer interesting
A negative answer tells us the family is closed here and the next campaign should
look at flow instead of price. A positive one tells us the paper's rules were the
binding constraint rather than the family.

## What this is not asking for
Regime conditioning. Overnight positions. Anything using the volume profile.

## Why this one
It is the only paper in the corpus that tests this family on this index and states
the rules it used well enough to follow. Earlier campaigns here asked about flow
and about auction features; none asked whether the simplest price-only family is
already closed. Answering that narrows what the next campaign should reach for,
and it is cheap to answer because the rules are simple enough to state in a
paragraph and simple enough to implement without a fitted model.

## What the signal is, in words
A continuation reading compares the most recent completed bar against a lookback
window of the same instrument, scaled so that a quiet morning and a fast
afternoon produce comparable numbers. When the reading is large and positive the
rule joins the move; when it is large and negative it joins the other way; in
between it does nothing. Scaling by the window's own range rather than by a fixed
number of points is what makes the reading mean the same thing in both regimes,
and choosing that scaling is part of what is yours to choose.

## Position and exit
One position at a time, opened with a protective stop and a target placed at the
moment of entry rather than on a later callback, so the position is never
unprotected. Holding across the session close is outside this question. How wide
the stop and the target are, and whether they are symmetric, is yours.

## Turnover is part of the claim
The paper's own finding is that this family fails on cost rather than on
direction, so a rule that trades constantly and a rule that trades rarely are
testing different things. Both are inside the question; which one is better is
part of what the campaign answers, and reporting how often the rule traded is as
much a part of the answer as how it did.
"""


def _ok(**kw):
    base = {"slug": "intraday-momentum-costs",
            "title": "Intraday momentum does not survive its own costs",
            "markdown": BODY, "adapted_claim": "momentum net of costs on NQ bars",
            "cannot_test": None, "sets_aside": ["regime conditioning"]}
    base.update(kw)
    return base


class _Fake:
    def __init__(self, payload): self.payload = payload
    def ask(self, system, user): self.seen = user; return self.payload


# --------------------------------------------------------------------------- #
# refusal 1: the lint, before anything is written
# --------------------------------------------------------------------------- #
def test_a_distillation_that_mentions_the_gate_is_refused():
    """The sharpest version of the leak the lint exists for. A human writes
    "check it holds out of sample" once by accident; a model that learns the
    phrasing writes it every time."""
    for leak in ("We check it holds out of sample.",
                 "The effect should be statistically significant.",
                 "Report the p-value for each rule.",
                 "Hold out the last year and validate there."):
        bad = BODY.replace("## The claim", f"## The claim\n\n{leak}\n")
        with pytest.raises(ValueError, match="tells the strategy agent about the gate"):
            dp.distil_one(_Fake(_ok(markdown=bad)), PAPER, ROW)


def test_the_lint_is_the_same_one_the_campaign_runs_at_registration():
    """A file that passes here must not fail there. Same function, not a copy."""
    import inspect
    src = inspect.getsource(dp.distil_one)
    assert "from src.core.loop.hypothesis import findings" in src

    from src.core.loop import hypothesis as H
    got = dp.distil_one(_Fake(_ok()), PAPER, ROW)
    H.lint(got["markdown"], source="distilled")          # must not raise


def test_a_clean_distillation_passes_and_keeps_the_body():
    got = dp.distil_one(_Fake(_ok()), PAPER, ROW)
    assert got["slug"] == "intraday-momentum-costs"
    assert got["markdown"].startswith("# ")
    assert got["markdown"].endswith("\n")
    assert got["sets_aside"] == ["regime conditioning"]


# --------------------------------------------------------------------------- #
# the shape checks
# --------------------------------------------------------------------------- #
def test_the_file_must_cite_the_paper_id():
    """Without it a result cannot be traced to the paper that proposed it, and
    the corpus would have to be frozen instead of referenced."""
    stripped = BODY.replace("2605.04004v3", "the paper")
    with pytest.raises(ValueError, match="does not cite the paper id"):
        dp.distil_one(_Fake(_ok(markdown=stripped)), PAPER, ROW)


def test_the_word_budget_is_enforced_at_both_ends():
    with pytest.raises(ValueError, match="outside"):
        dp.distil_one(_Fake(_ok(markdown="# Too short\n\n2605.04004v3\n")),
                      PAPER, ROW)
    padded = BODY + ("\nfiller " * dp.MAX_WORDS)
    with pytest.raises(ValueError, match="outside"):
        dp.distil_one(_Fake(_ok(markdown=padded)), PAPER, ROW)


def test_the_budget_is_tighter_than_the_largest_hypothesis_already_committed():
    """`auction-market-features.md` is 1,422 words and predates the budget. The
    point of a ceiling is that the next one is smaller, not that the biggest
    existing file defines it."""
    biggest = max(len(p.read_text().split())
                  for p in (REPO_ROOT / "config" / "hypotheses").glob("*.md"))
    assert dp.MAX_WORDS < biggest


def test_a_bad_slug_is_refused():
    for bad in ("Intraday_Momentum", "one", "a b c", "", "has spaces",
                "too-many-words-in-this-slug-by-far-indeed", "trailing-"):
        with pytest.raises(ValueError, match="slug"):
            dp.distil_one(_Fake(_ok(slug=bad)), PAPER, ROW)


def test_a_slug_is_normalised_rather_than_refused_for_case_alone():
    """Case is a formatting accident, not a bad name. Lowercasing it is kinder
    than refusing a whole distillation over a capital letter."""
    got = dp.distil_one(_Fake(_ok(slug="Intraday-Momentum-Costs")), PAPER, ROW)
    assert got["slug"] == "intraday-momentum-costs"


def test_markdown_must_be_a_file_not_a_fragment():
    with pytest.raises(ValueError, match="'#' heading"):
        dp.distil_one(_Fake(_ok(markdown="The claim is that...\n2605.04004v3")),
                      PAPER, ROW)


# --------------------------------------------------------------------------- #
# refusal 2: not while a campaign runs
# --------------------------------------------------------------------------- #
def test_it_refuses_to_write_while_a_campaign_is_in_flight():
    """`config/hypotheses/*.md` is in ARTIFACT_GLOBS, so a new file there changes
    artifact_fingerprint for every run started afterwards and invalidates one
    mid-flight. A stray file in that directory did this to nq-orderflow-02."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert "def campaign_in_flight" in src
    assert "ARTIFACT_GLOBS" in src
    assert "nq-orderflow-02" in src
    # the guard is consulted before writing, and --dry-run is the way past it
    assert "if running and not a.dry_run:" in src
    assert callable(dp.campaign_in_flight)


def test_the_hypotheses_directory_really_is_fingerprinted():
    """The guard above is only worth having if this is true. Asserted rather than
    trusted, because the whole reason for the guard is this glob."""
    from src.core.scoring import integrity
    assert any("config/hypotheses" in g for g in integrity.ARTIFACT_GLOBS)


# --------------------------------------------------------------------------- #
# refusal 3: never overwrite a pre-registration
# --------------------------------------------------------------------------- #
def test_it_refuses_a_paper_that_already_has_a_hypothesis():
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert 'if row.get("hypothesis_path"):' in src
    assert "commit date is its pre-registration date" in src
    assert "if dest.exists():" in src


def test_it_refuses_a_paper_stage_1_called_infeasible():
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert 'row.get("feasibility") not in ("direct", "adaptable")' in src


# --------------------------------------------------------------------------- #
# what it is NOT allowed to do
# --------------------------------------------------------------------------- #
def test_it_does_not_score_rank_or_run_anything():
    """Section 4 of the design: the moment a candidate hypothesis is backtested to
    decide whether to keep it, it is a look and it counts. A pipeline that made ten
    and registered the most promising would have taken ten campaigns' worth of
    looks and reported one."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    for forbidden in ("run.py", "--stage run", "score(", "Engine", "primary",
                      "sortino"):
        assert forbidden not in src.split('"""')[2], forbidden  # skip the docstring
    assert "NOT registered" in src


def test_the_distiller_is_not_told_how_relevant_the_paper_was():
    """Stage 2's relevance is a judgement about spending a campaign. A distiller
    told "this was rated highly" writes a more confident question than the
    evidence supports."""
    fake = _Fake(_ok())
    dp.distil_one(fake, PAPER, {**ROW, "relevance": "high",
                                "gate_relevance": "high",
                                "relevance_reason": "excellent",
                                "protocol_edit": "change the floor"})
    for leaked in ("high", "excellent", "change the floor", "relevance"):
        assert leaked not in fake.seen, leaked


def test_the_prompt_tells_the_model_what_it_may_not_write():
    """The lint refuses; the prompt is what stops the refusal being the normal
    case. Both, because a prompt alone is advice."""
    text = " ".join(dp.PROMPT.read_text().lower().split())
    assert "every byte of your output reaches the strategy agent" in text
    assert "refuses the file" in text
    for idea in ("withheld", "budget of attempts", "error rates"):
        assert idea in text, idea


def test_the_prompt_is_a_fingerprinted_artifact():
    """It shaped the question, which shaped every score in the campaign."""
    from src.core.scoring import integrity
    rel = str(dp.PROMPT.relative_to(REPO_ROOT))
    assert rel in integrity.artifact_digests()


def test_the_registry_row_records_the_model_that_wrote_the_question():
    """A machine-written pre-registration has to say which machine. Amendment 8."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert '"model": MODEL, "model_served": served' in src
    assert '"hypothesis_path"' in src


def test_every_committed_hypothesis_still_passes_the_lint():
    """Not about stage 3, but this is where a regression would show: the lint is
    now load-bearing for machine-written files and must stay true of the
    hand-written ones."""
    from src.core.loop import hypothesis as H
    for p in (REPO_ROOT / "config" / "hypotheses").glob("*.md"):
        assert H.findings(p.read_text()) == [], p.name
