"""Is every quantity in a distilled claim actually in its sources?

No network, no model. The check is deterministic by design, which is the whole
argument for it: `docs/design/architecture.md` says make the bug unrepresentable
rather than ask a model whether it happened, and asking a model whether another
model hallucinated needs a third check.

**The defect it was built from**, in the first real distillation of
`2605.04004v3` on 2026-09-28:

| | |
|---|---|
| the paper | "gross return before friction is 0.07-1.50" |
| the hypothesis | "gross returns in the 0.5-1.5 point range" |

A lower bound wrong by a factor of seven, in what would have become a
pre-registered question, and invisible to the hypothesis lint because nothing in
it is gate vocabulary. The idea is Bryan Rodriguez's `aider-validate`; this is
its deterministic core.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "grounding", REPO_ROOT / "scripts" / "grounding.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

PAPER = "2605.04004v3"

# Third-party full texts are local-only (.gitignore, docs/PUBLIC_RELEASE.md):
# a clone has none until `scripts/fetch_fulltext.py` re-fetches them.
needs_fulltext = pytest.mark.skipif(
    not (REPO_ROOT / "data" / "papers" / "text").is_dir(),
    reason="data/papers/text/ is local-only; run scripts/fetch_fulltext.py")


# --------------------------------------------------------------------------- #
# the defect, on the real paper
# --------------------------------------------------------------------------- #
@needs_fulltext
def test_the_fabricated_range_is_caught_against_the_whole_paper():
    """The exact sentence the model wrote, against the whole paper.

    **And the reason ranges are checked as pairs.** Presence-grounding weakens
    as the source grows: in 44,000 characters the bare number `0.5` appears
    somewhere, so it reads as grounded. The defect was a misquoted RANGE, and a
    range is a pair -- `0.07-1.50` is in the paper, `0.5-1.5` is not, wherever
    its halves occur.
    """
    src = g.sources_for(PAPER)
    assert any(len(s) > 40_000 for s in src), "the full text must be a source"

    bad = ("Most simple momentum rules generate gross returns in the 0.5-1.5 "
           "point range, which is insufficient.")
    assert g.ungrounded_ranges(bad, *src) == ["0.5-1.5"]
    assert g.findings(bad, *src), "the file must be refused"

    good = ("Eleven families fail because gross return before friction is "
            "0.07-1.50 points.")
    assert g.findings(good, *src) == []


def test_a_range_of_small_integers_is_prose_not_a_claim():
    assert g.ungrounded_ranges("hold for 3 to 5 bars", "") == []
    assert g.ungrounded_ranges("returns of 0.2 to 0.4 points", "") == ["0.2-0.4"]


def test_a_descending_pair_is_not_a_range():
    """`5-3` is a citation, a date or a subtraction."""
    assert g.ungrounded_ranges("see 5-3 and 2024-2021", "") == []


def test_the_papers_own_figures_are_all_grounded():
    """A faithful distillation must not trip the check. Every figure the
    abstract states has to read as grounded, or the check is unusable."""
    src = g.sources_for(PAPER)
    abstract = json.loads(
        (REPO_ROOT / "data" / "papers" / "raw" / f"{PAPER}.json").read_text()
    )["abstract"]
    assert g.findings(abstract, *src) == []


@needs_fulltext
def test_the_full_text_is_what_makes_a_body_figure_groundable():
    """Grounding against the abstract alone would flag every figure the paper
    states in its body -- a check that punishes the faithful distillation and
    passes the vague one. Stage 0b is what fixes that."""
    full = REPO_ROOT / "data" / "papers" / "text" / f"{PAPER}.txt"
    assert full.exists(), "run scripts/fetch_fulltext.py --survivors"
    body = full.read_text()
    assert len(body) > 20 * len(json.loads(
        (REPO_ROOT / "data" / "papers" / "raw" / f"{PAPER}.json").read_text()
    )["abstract"])
    assert body in g.sources_for(PAPER)


# --------------------------------------------------------------------------- #
# numeric, not textual
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("claim,source", [
    ("the floor is 2 points", "a 2.0-point friction floor"),
    ("the floor is 2.0 points", "a 2 point friction floor"),
    ("it returned 1.5 points", "it returned 1.50 points"),
    ("11.82 points", "+11.82 pts"),
    ("1,250 sessions", "1250 sessions"),
])
def test_formatting_never_causes_a_false_alarm(claim, source):
    assert g.ungrounded_numbers(claim, source) == [], (claim, source)


def test_a_different_value_is_not_grounded_however_close():
    assert g.ungrounded_numbers("the floor is 2.1 points", "a 2.0-point floor")
    assert g.ungrounded_numbers("it returned 11.83 points", "+11.82 pts")


# --------------------------------------------------------------------------- #
# what is deliberately not checked
# --------------------------------------------------------------------------- #
def test_an_arxiv_id_is_a_citation_not_a_quantity():
    """Stage 3 REQUIRES the file to cite the paper id. Reading `2605.04004v3` as
    the number 2605 would punish the one thing it insists on."""
    assert g.ungrounded_numbers("Adapted from 2605.04004v3.", "nothing here") == []
    assert g.ungrounded_numbers("See 2609.07989v1 and 2605.11423v3.", "") == []


def test_a_year_needs_no_source():
    """A hypothesis may date itself; the date is not the paper's to support."""
    assert g.ungrounded_numbers("Written 2026. Data from 2021 to 2025.", "") == []


def test_small_integers_are_structural_rather_than_claims():
    """Requiring a source for every `1` and `2` in English prose is noise."""
    assert g.ungrounded_numbers(
        "One position at a time, with 2 exits and 3 signals.", "") == []
    assert g.CLAIM_MIN == 10.0
    assert g.ungrounded_numbers("947 trading days", "") == ["947"]


def test_a_decimal_is_always_a_claim_however_small():
    assert g.ungrounded_numbers("a threshold of 0.07", "") == ["0.07"]


# --------------------------------------------------------------------------- #
# quoted spans
# --------------------------------------------------------------------------- #
def test_a_fabricated_quote_is_caught_and_a_real_one_is_not():
    src = "None of the fourteen families passed all five criteria."
    assert g.ungrounded_quotes('The paper says "all fourteen families passed".', src)
    assert g.ungrounded_quotes(
        'The paper says "None of the fourteen families passed".', src) == []


def test_quote_matching_ignores_whitespace_and_curly_quotes():
    src = "gross   return\nbefore friction is low"
    assert g.ungrounded_quotes('It notes “gross return before friction”.', src) == []


def test_a_short_span_is_not_treated_as_a_quote():
    """Twelve characters is the floor; below it, quoted words are emphasis."""
    assert g.ungrounded_quotes('the so-called "edge" here', "nothing") == []


# --------------------------------------------------------------------------- #
# the sources
# --------------------------------------------------------------------------- #
def test_the_prompt_is_a_source_because_it_supplied_our_own_numbers():
    """The prompt tells the model what our data is. A number it handed over is
    one the model was entitled to write down, and this rule replaces a
    hand-maintained allowlist of "our" constants, which would rot."""
    src = g.sources_for(PAPER)
    prompt = (REPO_ROOT / "config" / "prompts" / "paper-distil.md").read_text()
    assert any(prompt == s for s in src)
    assert "543" in prompt, "the prompt states our session count"
    assert g.ungrounded_numbers("We have 543 sessions.", *src) == []


def test_an_unknown_paper_raises_rather_than_checking_nothing():
    with pytest.raises(KeyError):
        g.sources_for("0000.00000v9")


def test_findings_reports_both_kinds():
    out = g.findings('It claims 0.5 and says "a fabricated sentence here".', "empty")
    assert any("0.5" in f for f in out)
    assert any("quoted" in f for f in out)
    assert g.findings("nothing numeric", "empty") == []


# --------------------------------------------------------------------------- #
# the integration
# --------------------------------------------------------------------------- #
def test_stage_3_refuses_a_file_whose_numbers_are_not_in_the_paper():
    import importlib.util as iu
    sp = iu.spec_from_file_location("dp", REPO_ROOT / "scripts" / "distil_paper.py")
    dp = iu.module_from_spec(sp); sp.loader.exec_module(dp)

    from tests.test_distil_paper import BODY, ROW, PAPER as PAPER_META, _ok

    class _Fabricates:
        def __init__(self): self.calls = 0
        def ask(self, system, user):
            self.calls += 1
            return _ok(markdown=BODY.replace(
                "## The claim",
                "## The claim\n\nGross returns sit in the 0.5-1.5 point range, "
                "and the floor is 7.77 points.\n"))

    c = _Fabricates()
    with pytest.raises(ValueError, match="quantities its sources do not"):
        dp.distil_one(c, PAPER_META, ROW)
    assert c.calls == dp.MAX_LINT_REPAIRS + 1, "it gets repairs, then it is refused"


def test_the_repair_note_tells_it_not_to_round():
    import importlib.util as iu
    sp = iu.spec_from_file_location("dp", REPO_ROOT / "scripts" / "distil_paper.py")
    dp = iu.module_from_spec(sp); sp.loader.exec_module(dp)
    note = dp._repair_note(0, [], ["'0.5' appears in no source"])
    assert "0.5" in note
    assert "Do not round" in note
    assert "0.07-1.50" in note, "the note shows the real example, not an abstraction"
    assert "a pair" in note, "a range is checked as a pair, so half-right is wrong"
    # **Both are reported together.** Prioritising one made the loop oscillate:
    # the model fixed the leak and broke the length, then fixed that and dropped
    # the citation, never converging in three attempts.
    both = dp._repair_note(0, ["line 3: 'validation' -- names the validation split"],
                           ["'0.5' appears in no source"])
    assert "Forbidden terms" in both and "0.5" in both


def test_sentence_punctuation_inside_the_closing_quote_is_not_a_fabrication():
    """**The first real distillation was refused for quoting its source
    correctly.** American convention puts the period inside the quote:
    `"... Falsification Study."` The paper's title has no trailing period, so the
    span did not match and the file was rejected. Punctuation is not a claim."""
    title = ("Structural Limits of OHLCV-Based Intraday Momentum Signals in MNQ "
             "Futures: A Systematic Falsification Study")
    assert g.ungrounded_quotes(f'The paper "{title}." tested fourteen families.',
                               title) == []
    assert g.ungrounded_quotes(f'It is called "{title}"; see it.', title) == []
    # the real title, against the real sources
    assert g.ungrounded_quotes(f'From "{title}."', *g.sources_for(PAPER)) == []
    # and a genuinely different span is still caught
    assert g.ungrounded_quotes('The paper "A Study Of Something Else Entirely".',
                               title)


def test_an_ungrounded_span_is_reported_in_full_not_truncated():
    """A report that hides the span cannot be acted on. The title failure above
    matched when tested by hand, and a 90-character cut hid the trailing period
    that differed -- the same diagnostics gap as the proposer's finish_reason."""
    long_span = "x" * 200
    out = g.ungrounded_quotes(f'It says "{long_span}".', "nothing")
    assert out and out[0] == long_span and "..." not in out[0]


# --------------------------------------------------------------------------- #
# a citation is a claim that a paper exists, and nothing checked it
# --------------------------------------------------------------------------- #
def test_a_pnl_figure_is_not_a_citation():
    """`net 1133.4125 index points` is in a real committed hypothesis. A naive
    id-matcher reads it as arXiv 1133.4125 and refuses the file."""
    known = {"2605.11423", "2609.13825"}
    assert g.citations("net 1133.4125 index points", known) == set()
    assert g.unverifiable_citations("the range was 2605.0 to 2610.5",
                                            known) == []


def test_a_marked_or_versioned_id_is_always_a_citation():
    known = {"2605.11423"}
    assert g.citations("see arXiv:2605.11423", known) == {"2605.11423"}
    assert g.citations("9999.12345v2 proves it", known) == {"9999.12345"}


def test_a_bare_id_counts_only_when_the_registry_knows_it():
    """Otherwise every four-digit decimal in a P&L table becomes a citation."""
    known = {"2605.11423"}
    assert g.citations("2605.11423 shows this", known) == {"2605.11423"}
    assert g.citations("1133.4125 index points", known) == set()


def test_an_invented_paper_is_refused():
    """The refiner is only ever SHOWN papers from our registry, so an id outside
    it was invented or lifted from some paper's bibliography -- neither has been
    fetched, screened or ranked here."""
    known = {"2605.11423"}
    assert g.unverifiable_citations("see arXiv:2501.99999", known) == \
        ["2501.99999"]
    assert g.unverifiable_citations("9999.12345v2 proves it", known) == \
        ["9999.12345"]


def test_the_citation_check_reaches_findings():
    out = g.findings("see arXiv:2501.99999 for the result", "a source")
    assert any("in no registry we hold" in f for f in out), out


def test_every_committed_hypothesis_verifies():
    """Six carry citations. They were checked by hand on 2026-10-03 and were all
    real, which was luck rather than design -- this is the design."""
    import pathlib as _p
    bad = {}
    for f in sorted((REPO_ROOT / "config" / "hypotheses").glob("*.md")):
        u = g.unverifiable_citations(f.read_text())
        if u:
            bad[f.name] = u
    assert not bad, f"hypotheses cite papers we do not hold: {bad}"


# --------------------------------------------------------------------------- #
# citations beyond arXiv (2026-10-04, with fetch_scholar.py)
# --------------------------------------------------------------------------- #
def test_an_invented_doi_is_refused():
    """The corpus stopped being arXiv-only, so the citation check had to stop
    being arXiv-only. A fabricated journal DOI is EASIER to invent convincingly
    than a fabricated arXiv id, because the shape is less familiar."""
    bad = "as 10.1016/j.jbankfin.2099.999999 shows, the effect persists"
    assert g.unverifiable_citations(bad) == ["10.1016/j.jbankfin.2099.999999"]


def test_a_doi_is_a_citation_on_sight():
    """Unlike a bare NNNN.NNNNN, a DOI's shape cannot be a quantity, so it does
    not need to be marked or already known to count as a claim about a paper."""
    assert g.citations("see 10.1002/fut.22375") == {"10.1002/fut.22375"}


def test_a_number_is_still_not_a_doi():
    """The regression this check exists to avoid: a plain measurement read as a
    citation would refuse every honest file."""
    assert g.citations("net 1133.4125 index points") == set()
    assert g.citations("a 10.5 point move") == set()
    assert g.unverifiable_citations("10.5 points, or 10.25 at worst") == []


def test_a_registered_doi_verifies():
    """Whatever `fetch_scholar.py` actually put in the registry must be citable,
    or the refiner cannot use the half of the corpus that is not arXiv."""
    import json
    reg = json.loads((REPO_ROOT / "data" / "papers" / "registry.json").read_text())
    dois = [v["doi"] for v in reg["papers"].values() if v.get("doi")]
    if not dois:
        return
    claim = f"following doi:{dois[0]}, we ask whether"
    assert g.unverifiable_citations(claim) == [], dois[0]


def test_an_openalex_id_counts_only_when_held():
    """`W` plus digits is too thin a shape to assume: a false positive here would
    refuse a clean file over a stray token."""
    import json
    reg = json.loads((REPO_ROOT / "data" / "papers" / "registry.json").read_text())
    held = [v["openalex_id"] for v in reg["papers"].values()
            if v.get("openalex_id")]
    assert g.citations("W9999999999 is not a thing we hold") == set()
    if held:
        assert g.citations(f"see {held[0]}") == {held[0]}


def test_registry_ids_includes_the_rows_own_identifiers():
    """A hypothesis should be able to cite the DOI, which is what anyone reading
    the paper would write, rather than our internal registry key."""
    ids = g.registry_ids()
    assert len(ids) > 284
    assert any("/" in i for i in ids), "no DOI reached the known set"


def test_a_doi_is_not_read_as_a_quantity():
    """**`PAPER_ID` has stripped arXiv ids since the first distillation**, so
    `2605.04004v3` is not reported as the ungrounded number 2605. When the
    corpus stopped being arXiv-only the stripping did not follow, and
    `doi:10.2139/ssrn.7124578` had `10.2139` pulled out of it and demanded of
    the sources. It refused the first non-arXiv distillation and would have
    refused every one of them."""
    src = "a short-side opening reversal over 194 trades, 59.79% win rate"
    assert g.ungrounded_numbers(
        "Following doi:10.2139/ssrn.7124578, we ask whether it reverses.",
        src) == []
    assert g.ungrounded_numbers("Following arXiv:2605.04004v3 we ask.", src) == []
    # and a real invented number is still caught
    assert g.ungrounded_numbers("We expect a 37.5 point move.", src) == ["37.5"]


def test_every_identifier_strip_site_uses_the_same_helper():
    """The first fix did not take because there were two stripping sites and
    only one was changed; the range check had two more. An identifier has to
    stop being a number in every place or in none."""
    import re
    src = (REPO_ROOT / "scripts" / "grounding.py").read_text()
    # The one legitimate use is inside the helper itself; every other site must
    # go through it. Located by function rather than by a literal, which went
    # stale the moment the helper grew a second line.
    body = src[src.index("def _strip_identifiers"):src.index("def numbers(")]
    outside = src.replace(body, "")
    assert "PAPER_ID.sub" not in outside, (
        "a bare PAPER_ID.sub site is one the DOI fix will not reach")
    assert outside.count("_strip_identifiers(") >= 4
