"""Grounding: the draft must be about the papers we froze.

Covers `src/core/loop/grounding.py`. The property is one sentence -- a
quotation that is not in the frozen text, and an id that is not in the frozen
set, are refused -- and it is asserted from both sides:

1. **Pass-through.** A quotation the paper really contains survives, including
   across the differences that are not content: line wraps, curly quotes, case.
   A real draft must not be refused for typography.
2. **The negative control.** A fabricated quote fails. This is the fixture
   borrowed from the retrieved branch's Pass 4C, whose negative memo carries a
   confident Sharpe claim that no source contains: a validator that has only
   ever been shown true input has not been tested at all.
3. **Refusals are specific.** Each names the offending text and what the frozen
   set does hold, because the repair loop's next instruction is derived from
   this message.

Two things this deliberately does not do, asserted so they cannot be added by
accident: it never judges whether the remaining text is *supported* (entailment
is a second gate, and a gate next to the gate), and it never widens to accept a
near-miss, because an abstract re-typed from memory is a fabrication for the
purposes of a frozen pre-registration.
"""

from __future__ import annotations

import pytest

from src.core.loop.grounding import (
    MIN_QUOTE_CHARS,
    Ungrounded,
    check_quote,
    check_retrieval,
    check_text,
    cited_ids,
    contains,
    normalize,
    quoted_spans,
)
from src.core.loop.research import Paper, RetrievalSet

PAPER = Paper(
    arxiv_id="2601.10001v1",
    title="Option-Delta Order Flow",
    authors=("A. Writer",),
    published="2026-01-05",
    abstract=(
        "We study order flow in the options market. Delta-hedged demand "
        "predicts the next ten returns beyond the bid-ask spread, and the "
        "effect survives a placebo on randomised arrival times."),
    categories=("q-fin.TR",),
)

OTHER = Paper(
    arxiv_id="2601.20002",
    title="Clustering of Trades",
    authors=("B. Writer",),
    published="2026-01-06",
    abstract="Trades cluster in time. The clustering is strongest at the open.",
    categories=("q-fin.ST",),
)

# The fixture from the retrieved branch's negative memo: a confident, specific,
# entirely ungrounded number of exactly the kind a strategy memo would carry.
FABRICATED = (
    "We find a Sharpe ratio of 2.7 with an annualised return of 34% that holds "
    "out of sample across every regime since 2010."
)
REAL = (
    "Delta-hedged demand predicts the next ten returns beyond the bid-ask "
    "spread"
)


def _set(*papers: Paper) -> RetrievalSet:
    return RetrievalSet(
        name="t", query="q", max_results=10, papers=tuple(papers),
        retrieved_at="2026-01-07T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# normalization: pass-through
# --------------------------------------------------------------------------- #

def test_a_real_quotation_survives_case_wraps_and_curly_quotes():
    text = 'As the paper says, "Delta-hedged demand predicts the next ten returns".'
    assert check_text(text, [PAPER]) == ()


def test_a_quotation_across_a_line_wrap_matches_the_unwrapped_source():
    assert contains(PAPER.abstract, "the effect survives a placebo on randomised")
    assert contains("the effect survives a placebo\n    on randomised arrival",
                    "the effect survives a placebo on randomised arrival")


def test_normalization_folds_nfkc_and_whitespace_but_not_punctuation():
    assert normalize("  a— b \n c ") == "a— b c"
    assert normalize("ﬁve") == "five"


def test_curly_and_straight_double_quotes_are_both_read():
    assert quoted_spans(f'say "{REAL}"') == (REAL,)
    assert quoted_spans(f"say “{REAL}”") == (REAL,)


# --------------------------------------------------------------------------- #
# the negative control
# --------------------------------------------------------------------------- #

def test_a_fabricated_quotation_is_refused():
    with pytest.raises(Ungrounded, match="not in the frozen set"):
        check_text(f'"{FABRICATED}"', [PAPER])


def test_the_fabricated_claim_is_refused_without_quotes_too_when_citing_a_fake_id():
    """Not just quote-shaped: an id outside the set fails on its own."""
    text = "Following 2609.55555, the effect is present in every regime."
    with pytest.raises(Ungrounded, match="2609.55555"):
        check_text(text, [PAPER])


def test_the_refusal_names_the_offending_text_and_the_frozen_set():
    with pytest.raises(Ungrounded) as exc:
        check_text(f'"{FABRICATED}"', [PAPER, OTHER])
    msg = str(exc.value)
    assert "Sharpe ratio of 2.7" in msg, "the refusal must quote what failed"
    assert "2601.10001" in msg and "2601.20002" in msg, (
        "the refusal must say what the set does hold, or the repair has nothing "
        "to work from")


def test_a_near_miss_is_refused_rather_than_widened():
    """An abstract re-typed from memory is a fabrication here, by design."""
    almost = "Delta hedged demand predicts the next ten returns beyond the spread"
    assert almost not in normalize(PAPER.abstract).casefold()
    with pytest.raises(Ungrounded):
        check_text(f'"{almost}"', [PAPER])


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("see 2601.10001", ("2601.10001",)),
    ("see 2601.10001v2", ("2601.10001",)),
    ("2601.10001 and 2601.20002", ("2601.10001", "2601.20002")),
    ("2601.10001 twice: 2601.10001", ("2601.10001",)),
    ("no ids here", ()),
    ("", ()),
])
def test_cited_ids_reads_arxiv_ids_and_strips_versions(text, expected):
    assert cited_ids(text) == expected


def test_a_versioned_id_in_the_set_matches_an_unversioned_citation():
    """The set holds `2601.10001v1`; citing `2601.10001` must resolve."""
    assert check_text("Following 2601.10001, demand predicts returns.",
                      [PAPER]) == ("2601.10001",)


# --------------------------------------------------------------------------- #
# the screen's quote field
# --------------------------------------------------------------------------- #

def test_a_screen_quote_that_is_the_papers_own_sentence_passes():
    got = check_quote(REAL, PAPER)
    assert got == REAL


def test_a_screen_quote_the_paper_does_not_contain_is_refused():
    with pytest.raises(Ungrounded, match="does not appear in 2601.10001"):
        check_quote("the effect is strongest at the close of the session",
                    PAPER)


def test_an_empty_screen_quote_is_allowed_because_no_claim_was_made():
    assert check_quote("", PAPER) == ""
    assert check_quote("   ", PAPER) == ""


def test_a_too_short_quote_is_refused_because_it_cites_nothing():
    short = "the effect survives"
    assert len(normalize(short)) < MIN_QUOTE_CHARS
    with pytest.raises(Ungrounded, match="under the"):
        check_quote(short, PAPER)


def test_a_quote_too_short_to_check_is_not_silently_accepted_by_check_text():
    """`quoted_spans` drops short spans, so check_text must not pass them on."""
    assert quoted_spans('"yes"') == ()
    assert check_text('The paper says "yes".', [PAPER]) == ()


# --------------------------------------------------------------------------- #
# no set, no pass
# --------------------------------------------------------------------------- #

def test_checking_against_no_papers_is_an_error_rather_than_a_pass():
    with pytest.raises(Ungrounded, match="no papers"):
        check_text(f'"{FABRICATED}"', [])


def test_a_retrieval_less_draft_is_refused_rather_than_unchecked():
    with pytest.raises(Ungrounded, match="no retrieval set"):
        check_retrieval("Any claim at all.", None)


def test_check_retrieval_reads_the_frozen_set():
    assert check_retrieval(f'"{REAL}"', _set(PAPER)) == ()
    with pytest.raises(Ungrounded):
        check_retrieval(f'"{FABRICATED}"', _set(PAPER))


# --------------------------------------------------------------------------- #
# scope: what this check must never become
# --------------------------------------------------------------------------- #

def test_it_checks_existence_not_entailment():
    """A supported-sounding but unquoted inference is NOT this module's business.

    The paper says nothing about the open; a page that draws the inference in
    its own words is making a claim about the market, which is what a hypothesis
    is for. This module exists to catch attributed words that are not there, and
    asserting this keeps it from quietly growing into a second gate.
    """
    inference = (
        "The paper's mechanism implies that the effect should be strongest "
        "in the first thirty minutes, which is the question this hypothesis "
        "asks about the data."
    )
    assert check_text(inference, [PAPER]) == ()


def test_it_does_not_judge_quality_or_length():
    thin = "Traders should watch delta."
    assert check_text(thin, [PAPER]) == ()


def test_the_frozen_excerpt_counts_as_the_paper():
    """A quote read from the excerpt the set already froze is grounded."""
    with_fulltext = Paper(**{**PAPER.as_dict(),
                             "fulltext": "Opening inventory is the residual "
                                         "risk carried into the session.",
                             "fulltext_src": "ar5iv"})
    assert check_text(
        '"Opening inventory is the residual risk carried into the session."',
        [with_fulltext]) == ()
