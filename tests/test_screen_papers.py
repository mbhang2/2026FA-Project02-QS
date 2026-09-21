"""Stage 1 of paper retrieval: the screen's contract with the registry.

No network. What is worth pinning is that a verdict outside the three is refused,
that `adaptable` cannot pass without saying what was adapted, and that the prompt
still states the constraints the screen exists to apply.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "screen_papers", REPO_ROOT / "scripts" / "screen_papers.py")
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

PAPER = {"title": "Order flow imbalance", "abstract": "We study signed volume.",
         "categories": ["q-fin.TR"]}


class _Fake:
    def __init__(self, payload): self.payload = payload
    def ask(self, system, user): return self.payload


def test_a_verdict_outside_the_three_is_refused():
    """A model that answers 'maybe' or 'likely feasible' must not be written to
    the registry as though it were a decision."""
    for bad in ("maybe", "feasible", "", None, "DIRECT?"):
        with pytest.raises(ValueError):
            sp.screen_one(_Fake({"feasibility": bad, "reason": "r"}), PAPER)


def test_adaptable_without_an_adaptation_is_refused():
    """An adaptation changes what is being tested. Accepting `adaptable` with no
    statement of the change is how a downstream reader comes to believe the
    paper's own result was confirmed."""
    with pytest.raises(ValueError, match="adaptable without a stated adaptation"):
        sp.screen_one(_Fake({"feasibility": "adaptable", "reason": "r",
                             "adaptation": None}), PAPER)


def test_the_three_verdicts_round_trip():
    got = sp.screen_one(_Fake({
        "feasibility": "  Adaptable  ", "reason": "equities, not futures",
        "adaptation": "substitute NQ", "claim": "signed flow predicts returns"}), PAPER)
    assert got["feasibility"] == "adaptable", "case and whitespace should normalise"
    assert got["adaptation"] == "substitute NQ"
    assert got["claim"] == "signed flow predicts returns"

    for v in ("direct", "infeasible"):
        out = sp.screen_one(_Fake({"feasibility": v, "reason": "r"}), PAPER)
        assert out["feasibility"] == v
        assert out["adaptation"] is None


def test_the_prompt_states_the_constraints_the_screen_exists_to_apply():
    """The screen is only as good as what the prompt tells it we have. Each of
    these has a specific failure attached to dropping it."""
    p = sp.PROMPT.read_text()
    assert sp.PROMPT.exists()
    for token in ("384", "aggressor", "cross-section", "order book",
                  "0.10 index points", "$20 per index point"):
        assert token in p, f"the prompt no longer states {token!r}"
    # The sample-size trap: we can build daily bars and still not have the
    # observations a daily claim needs.
    assert "384 observations" in p or "384** observations" in p or "384 train sessions" in p
    assert '"feasibility"' in p and '"adaptation"' in p, "the JSON contract moved"


def test_every_verdict_word_in_the_prompt_is_one_the_code_accepts():
    """The prompt and the validator must name the same three, or a well-formed
    answer gets rejected as malformed."""
    p = sp.PROMPT.read_text()
    for v in sp.VERDICTS:
        assert f"`{v}`" in p, f"the prompt never offers {v!r}"
