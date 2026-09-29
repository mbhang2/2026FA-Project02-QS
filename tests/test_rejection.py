"""Rejection: the duty to name what the corpus already ruled out.

Covers `src/core/loop/rejection.py`. Three properties, each asserted against a
real registry in a temp directory:

1. The duty is a function of the *corpus*, not of the proposal. With no
   rejections recorded, any proposal passes -- there is nothing to disclose --
   and the rejected set is fixed before a proposal is read, so a proposal cannot
   narrow its own obligation.
2. A proposal that names nothing while rejections exist is refused, and a
   proposal that names a trial which is not in the registry is refused. The
   second is the negative control: the pattern this module takes from
   `tests/rag_research/docs/strategy_memo_negative.md`, where a fabricated
   `prior_invalidation_rejected` is the trap.
3. An outcome verdict this module cannot read is an error, not consent. A new
   verdict that quietly counted as `promoted` would stop the duty from firing at
   all, which is the one failure mode a disclosure check cannot detect later.
"""

from __future__ import annotations

import pytest

from src.core.loop.rejection import (
    REJECTING,
    Rejection,
    RejectionUndisclosed,
    RejectionUngrounded,
    check,
    cited,
    duty,
)
from src.core.loop.registry import Registry, RegistryError

CITING = """# Hypothesis

Intraday momentum in the opening range reverts once the spread normalises.

prior_invalidation_rejected: 2601.10001
"""


def _reg(tmp_path) -> Registry:
    return Registry(root=tmp_path, year=2026)


def _rejected(reg: Registry, arxiv_id: str, reason: str = "no edge") -> None:
    reg.append(kind="outcome", arxiv_id=arxiv_id,
               verdict="rejected", reason=reason)


# --------------------------------------------------------------------------- #
# the duty comes from the corpus
# --------------------------------------------------------------------------- #

def test_an_empty_corpus_imposes_no_duty(tmp_path):
    reg = _reg(tmp_path)
    assert not duty(reg)
    assert check(CITING, duty(reg)) == ()
    assert duty(reg).prompt_block() == ""


def test_a_rejected_trial_becomes_a_duty_with_its_reason(tmp_path):
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001", "the spread proxy did not survive the walk")

    d = duty(reg)
    assert [r.key for r in d.rejected] == ["2601.10001"]
    assert d.rejected[0].reason == "the spread proxy did not survive the walk"


def test_a_promoted_trial_is_not_a_rejection(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="outcome", arxiv_id="2601.10001", verdict="promoted")
    assert duty(reg).rejected == (), "a promotion is consent, not a rejection"


def test_the_duty_is_fixed_before_the_proposal_is_read(tmp_path):
    """Two proposals, same corpus, same duty -- a proposal cannot narrow it."""
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001")

    first = duty(reg)
    second = duty(reg)
    assert [r.key for r in first.rejected] == [r.key for r in second.rejected]

    # A proposal citing a rejection it did not look at is still refused: the
    # check is membership in the corpus's rejected set, not self-report.
    with pytest.raises(RejectionUngrounded):
        check("prior_invalidation_rejected: 2601.20002", second)


def test_every_rejecting_verdict_is_read_and_the_rest_is_an_error(tmp_path):
    reg = _reg(tmp_path)
    for i, verdict in enumerate(REJECTING):
        reg.append(kind="outcome", arxiv_id=f"2601.1000{i}",
                   verdict=verdict, reason="r")
    assert len(duty(reg).rejected) == len(REJECTING)


def test_an_unreadable_verdict_fails_loudly_rather_than_counting_as_consent(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="outcome", arxiv_id="2601.10001", verdict="promoted")
    reg.append(kind="outcome", arxiv_id="2601.20002", verdict="half_rejected")

    with pytest.raises(RegistryError, match="half_rejected"):
        duty(reg)


def test_a_screen_verdict_is_not_an_outcome(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="screen", arxiv_id="2601.10001",
               feasibility="infeasible", feasibility_reason="no data")
    assert duty(reg).rejected == ()


# --------------------------------------------------------------------------- #
# the negative control: a citation that resolves to nothing
# --------------------------------------------------------------------------- #

def test_a_silent_proposal_is_refused_when_trials_were_rejected(tmp_path):
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001")

    with pytest.raises(RejectionUndisclosed, match="2601.10001"):
        check("# Hypothesis\n\nThe opening range carries a premium.\n", duty(reg))


def test_a_fabricated_trial_is_refused(tmp_path):
    """The memo's trap: a reference to a trial that never ran."""
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001")

    with pytest.raises(RejectionUngrounded, match="2601.99999"):
        check("prior_invalidation_rejected: 2601.99999", duty(reg))


def test_a_memo_style_non_registry_citation_is_refused_as_unreadable(tmp_path):
    """`kb2_trial_104_sma_spread` is the RAG memos' vocabulary, not a key."""
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001")

    with pytest.raises(RejectionUngrounded, match="kb2_trial_104_sma_spread"):
        check("prior_invalidation_rejected: kb2_trial_104_sma_spread", duty(reg))


def test_a_citation_is_accepted_when_it_resolves(tmp_path):
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001", "no edge")

    got = check(CITING, duty(reg))
    assert [r.key for r in got] == ["2601.10001"]
    assert got[0].reason == "no edge"


# --------------------------------------------------------------------------- #
# reading the citation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("prior_invalidation_rejected: 2601.10001", ("2601.10001",)),
    ("  - prior_invalidation_rejected: 2601.10001  ", ("2601.10001",)),
    ("PRIOR_INVALIDATION_REJECTED: 2601.10001", ("2601.10001",)),
    ('prior_invalidation_rejected: "2601.10001"', ("2601.10001",)),
    ("prior_invalidation_rejected: 2601.10001, 2601.20002",
     ("2601.10001", "2601.20002")),
    ("prior_invalidation_rejected:\n", ()),
    ("no field here", ()),
    ("", ()),
])
def test_cited_reads_the_field_in_its_documented_shapes(text, expected):
    assert cited(text) == expected


def test_cited_ignores_prose_that_merely_mentions_the_field():
    """A sentence about the field is not a citation of a trial."""
    text = "We record prior_invalidation_rejected in every memo we write."
    assert cited(text) == ()


# --------------------------------------------------------------------------- #
# the prompt block
# --------------------------------------------------------------------------- #

def test_the_prompt_block_states_the_market_question_not_the_gate(tmp_path):
    reg = _reg(tmp_path)
    _rejected(reg, "2601.10001", "the spread proxy did not survive")

    block = duty(reg).prompt_block()
    assert "2601.10001" in block
    assert "the spread proxy did not survive" in block
    for leak in ("significant", "p-value", "threshold", "holdout",
                 "reject the trial", "gate"):
        assert leak not in block.lower(), (
            f"the prompt block leaks gate vocabulary: {leak!r}")


def test_the_prompt_block_orders_by_key_so_a_rerun_is_byte_identical(tmp_path):
    reg = _reg(tmp_path)
    for k in ("2601.30003", "2601.10001", "2601.20002"):
        _rejected(reg, k)
    first = duty(reg).prompt_block()
    assert first.index("2601.10001") < first.index("2601.20002") < first.index(
        "2601.30003")
    assert first == duty(reg).prompt_block()
