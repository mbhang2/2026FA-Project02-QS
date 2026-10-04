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
    """A file that passes here must not fail there. Same function, not a copy —
    and the repair loop and the final check go through one helper so they cannot
    drift apart."""
    import inspect
    assert ("from src.core.loop.hypothesis import findings"
            in inspect.getsource(dp._lint_findings))
    for fn in (dp.distil_one,):
        assert "_lint_findings(" in inspect.getsource(fn)

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
    sentinel = "ZQ-RANKED-SENTINEL-7741"
    dp.distil_one(fake, PAPER, {**ROW,
                                "relevance": sentinel,
                                "gate_relevance": sentinel,
                                "relevance_reason": sentinel,
                                "gate_reason": sentinel,
                                "protocol_edit": sentinel,
                                "hypothesis_edit": sentinel,
                                "bears_on": sentinel})
    # A sentinel rather than English words: the context now carries the whole
    # paper, in which "high" and "relevance" legitimately occur.
    assert sentinel not in fake.seen


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
    assert '"model": a.model, "model_served": served' in src
    assert '"--model"' in src, "which model wrote the question is a choice, recorded"
    assert '"hypothesis_path"' in src


def test_every_committed_hypothesis_still_passes_the_lint():
    """Not about stage 3, but this is where a regression would show: the lint is
    now load-bearing for machine-written files and must stay true of the
    hand-written ones."""
    from src.core.loop import hypothesis as H
    for p in (REPO_ROOT / "config" / "hypotheses").glob("*.md"):
        assert H.findings(p.read_text()) == [], p.name


def test_the_lint_repair_is_bounded_and_carries_no_market_data():
    """A distillation that leaks gets told which words it used and one more try.

    Safe for the same reason the agent's static repair is: the findings are
    properties of the text the model just wrote — which banned word on which
    line — and say nothing about our data. Bounded anyway, because a model that
    cannot state the claim without the vocabulary in three attempts is telling
    us something worth hearing rather than working around.
    """
    import inspect
    assert dp.MAX_LINT_REPAIRS >= 3, (
        'handing the model the whole paper makes the leak MORE likely: a '
        "falsification study's body is saturated with the banned words")
    src = inspect.getsource(dp.distil_one)
    # Two budgets since 2026-10-04 -- mechanical repairs and the agentic seat's
    # rounds -- so the loop bound is their sum and each has its own counter.
    # The mechanical bound is still MAX_LINT_REPAIRS, which the call-count test
    # below is the real guard on.
    assert "for _ in range(MAX_LINT_REPAIRS + MAX_REVIEW_REPAIRS):" in src
    assert "if mech >= MAX_LINT_REPAIRS:" in src
    assert "while" not in src, "bounded, not a loop until it passes"


def test_a_repair_that_still_leaks_is_refused_rather_than_written():
    """Three strikes and the file does not exist. The lint is the arbiter, not
    the number of attempts."""
    class _Stubborn:
        def __init__(self): self.calls = 0
        def ask(self, system, user):
            self.calls += 1
            return _ok(markdown=BODY.replace(
                "## The claim", "## The claim\n\nWe check it out of sample.\n"))
    c = _Stubborn()
    with pytest.raises(ValueError, match="tells the strategy agent about the gate"):
        dp.distil_one(c, PAPER, ROW)
    assert c.calls == dp.MAX_LINT_REPAIRS + 1, "one call, then two repairs, then stop"


def test_a_repair_that_succeeds_is_accepted():
    class _FixesItself:
        def __init__(self): self.calls = 0
        def ask(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return _ok(markdown=BODY.replace(
                    "## The claim", "## The claim\n\nWe check it out of sample.\n"))
            return _ok()
    c = _FixesItself()
    got = dp.distil_one(c, PAPER, ROW)
    assert c.calls == 2 and got["slug"] == "intraday-momentum-costs"


def test_the_prompt_names_the_forbidden_words_literally():
    """Describing the concepts was not enough: the first real distillation of a
    paper whose contribution IS an evaluation protocol leaked 'out-of-sample'
    and 'validation' on its first attempt."""
    text = " ".join(dp.PROMPT.read_text().lower().split())
    for word in ("out-of-sample", "validation", "significance", "p-value"):
        assert word in text, word
    assert "evaluation protocol" in text


def test_the_word_budget_is_repairable_not_only_fatal():
    """A file 29 words over is a file the model can shorten; refusing it outright
    throws away the whole distillation. Full text made this the binding check --
    the model writes more when it has more to write about."""
    assert dp._too_long("word " * (dp.MAX_WORDS + 10)).endswith(
        f"over the ceiling of {dp.MAX_WORDS}")
    assert dp._too_long("word " * 10).endswith(f"under the floor of {dp.MIN_WORDS}")
    assert dp._too_long("word " * 500) == ""
    note = dp._repair_note(0, [], [], "929 words, 29 over the ceiling of 900")
    assert "29 over" in note and "450 to 700" in note
    assert "Cut the paper's framing, not the claim" in note


def test_the_repair_note_reports_every_finding_not_just_the_worst():
    """**The first version reported one category at a time and never converged.**
    On 2605.04004v3 it went leak, then length, then missing citation over three
    attempts, fixing one constraint and breaking another. Four checks reported
    one at a time cannot be satisfied in three tries; four reported together can
    be satisfied in one."""
    n = dp._repair_note(0, ["line 3: 'validation' -- names it"],
                        ["'0.5' appears in no source"],
                        "929 words, 29 over", "2605.04004v3")
    for expected in ("Forbidden terms", "'validation'",
                     "Quantities your sources do not contain", "0.5",
                     "Length", "929 words", "paper id", "2605.04004v3"):
        assert expected in n, expected
    assert "Fix **all** of the following" in n
    # and only the categories that fired
    only_length = dp._repair_note(0, [], [], "929 words, 29 over", "")
    assert "Length" in only_length
    assert "Forbidden terms" not in only_length and "paper id" not in only_length


def test_the_missing_citation_is_repairable_too():
    """Every check that is a property of the text is repairable. The citation is
    the one stage 3 insists on hardest -- it is what makes a result traceable to
    its source -- so refusing outright over a missing id wastes a distillation
    the model could fix in one line."""
    note = dp._repair_note(0, [], [], "", "2605.04004v3")
    assert "The paper id `2605.04004v3` does not appear" in note

    class _ForgetsThenRemembers:
        def __init__(self): self.calls = 0
        def ask(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return _ok(markdown=BODY.replace("2605.04004v3", "the source paper"))
            return _ok()
    c = _ForgetsThenRemembers()
    got = dp.distil_one(c, PAPER, ROW)
    assert c.calls == 2 and ROW["id"] in got["markdown"]


# --------------------------------------------------------------------------- #
# the agentic seat on the FIRST question (Brian point 2a, 2026-10-04)
#
# Verified live on 2605.17724v1: the seat objected with concern 4 (more than one
# question), the model repaired it, and the second draft cleared. The mechanical
# gates had passed the bundled version -- lint, grounding, length and the
# citation are all string checks and none can see two questions in one file.
# --------------------------------------------------------------------------- #
def test_the_reviewer_runs_on_a_distilled_question():
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert "def _review_seat(md: str) -> tuple[list[str], str]:" in src
    assert "MAX_REVIEW_REPAIRS = 2" in src
    assert "review_hypothesis.py" in src


def test_there_is_no_family_seat_here_and_the_reason_is_stated():
    """A first question has no parent, so there is nothing to drift from.
    Running the family seat here would be asking a model to compare a file with
    nothing."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    import re
    flat = " ".join(re.sub(r"^\s*#\s?", "", src, flags=re.M).split())
    assert "family_review" not in src
    assert "no parent, so there is nothing to drift from" in flat


def test_the_seat_runs_only_on_a_mechanically_clean_draft():
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    loop = src[src.index("for _ in range(MAX_LINT_REPAIRS"):
               src.index('    slug = str(out.get("slug")')]
    assert loop.index("if not (leaks or loose or length or uncited):") < \
           loop.index("_review_seat(md_so_far)")


def test_the_review_rounds_do_not_eat_the_mechanical_ones():
    """At MAX_LINT_REPAIRS = 3 a single objection would have left one repair for
    four checks that have to be satisfied together -- which is the exact failure
    `_repair_note`'s docstring says took three rounds to converge."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert "range(MAX_LINT_REPAIRS + MAX_REVIEW_REPAIRS)" in src


def test_the_seat_is_never_fatal_and_says_when_it_did_not_run():
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    assert 'DID-NOT-RUN ({type(exc).__name__}) -- NOT reviewed' in src
    assert "NOT-REACHED" in src
    flat = " ".join(src.split())
    assert "the reviewer never read this question" in flat


def test_what_the_seat_did_reaches_the_registry():
    """The registry row is this path's durable record, the way the commit
    trailer is the refine path's. The seat fails open, so without this a
    question nobody read is indistinguishable later from one that passed."""
    src = (REPO_ROOT / "scripts" / "distil_paper.py").read_text()
    i = src.index('"model": a.model, "model_served": served,')
    assert '"review": got["review"]' in src[i:i + 500]
