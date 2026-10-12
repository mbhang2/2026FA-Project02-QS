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


def test_the_screen_fetches_the_body_of_anything_it_keeps():
    """**Stage 0b was a standalone command nobody triggered.**

    It was run once by hand over the survivors that existed that morning. The
    next screen produced five more, and nothing fetched their bodies -- silently,
    because every consumer falls back to the abstract rather than failing. One of
    the five was the only paper the new search rated `high`. It went unnoticed
    until someone asked why that paper had no body.

    The screen is the one place the pipeline decides a paper matters, so it is
    where the body gets fetched. Best-effort: a network failure must not discard
    a screen verdict that is already worth keeping.
    """
    src = (REPO_ROOT / "scripts" / "screen_papers.py").read_text()
    assert "_fetch_bodies_for_new_survivors" in src
    # fires for both feasible verdicts, and only for them
    assert 'v["feasibility"] in ("direct", "adaptable")' in src
    # after the registry is written, so a crash in the fetch cannot lose a verdict
    body_call = src.index("_fetch_bodies_for_new_survivors(\n")
    registry_write = src.index('REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True)')
    assert registry_write < body_call, "verdicts are saved before the network is touched"
    # and it skips what is already there
    assert "if not (ff.TEXT_DIR / f\"{i}.txt\").exists()" in src


# --------------------------------------------------------------------------- #
# provenance: not every indexed record is a research report
# --------------------------------------------------------------------------- #
def test_an_automated_task_post_is_not_a_research_report():
    """**The sharpest failure this corpus can produce.** Measured 2026-10-12:
    15 registry rows are another automated research system's task queue,
    deposited on Zenodo with real DOIs by "E8 Intelligence Research" and indexed
    by OpenAlex as scholarly works. Eight were rated `direct` and one was one of
    only two top-tier candidates in the whole corpus -- they rank well precisely
    because they are on this project's exact topic.

    Distilling one would register a hypothesis whose cited evidence is a YouTube
    video, carrying a real DOI that makes it look sourced, and `grounding.py`
    would pass it because the claim genuinely IS in the record.

    Verbatim from `openalex-W7171513103`.
    """
    abstract = ('IDEA: AUTO VIDEO SCOUT — evaluate this trading strategy video '
                'for the swarm: VIDEO: "I Backtested the ORB Breakout + Pullback '
                'Strategy (5 Years of Data)" by Trading Steady — '
                'https://www.youtube.com/watch?v=xxxxxxxxxxx')
    title = ("LAB #303 NEUTRAL: VIDEO SCOUT: I Backtested the ORB Breakout + "
             "Pullback Strategy — E8 Intelligence Research")
    assert sp.not_research(title, abstract)


def test_every_marker_is_justified_by_a_record_in_the_corpus():
    """Each pattern earns its place by catching something real, so none is a
    guess about what junk might look like."""
    cases = [
        ("LAB #891 PROMISING: TG AUTO: New edge proposed: opening_range", ""),
        ("New edge proposed: opening_range_direction_continuation", ""),
        ("LAB #2323 NEUTRAL: VIDEO SCOUT: Live Free NQ Trading", ""),
        ("", "IDEA: AUTO VIDEO SCOUT — evaluate this for the swarm"),
        ("", "see https://youtu.be/abcdefghijk for the strategy"),
    ]
    for title, abstract in cases:
        assert sp.not_research(title, abstract), (title, abstract)


def test_it_does_not_flag_the_real_notes_from_the_same_depositor():
    """**The precision half, and the reason this is a provenance test and not a
    venue ban.** The same Zenodo depositor also has genuine analytical write-ups
    -- several bearing directly on this project's own gate design -- and 16 of
    the 30 Zenodo rows are those. A filter that rejected the venue would throw
    them away with the posts."""
    keep = [
        "The Hindsight-Selection Trap: Why a Backtest Can Pass Every Standard",
        "A Naive t Value Inflates with the Number of Observations per Day",
        "Chart Patterns Appear Just as Often on a Synthetic Random Walk",
        "A Sweep over Thresholds Cannot Produce a Plateau in Principle",
        "Bars Cut by Tick Count Are a Different Clock from Bars Cut by the Clock",
        "Hedging Demand and Market Intraday Momentum",
        "Nasdaq-100 Index Futures: Intraday Momentum or Reversal?",
        "Trading the 30-min Opening Range Breakouts",
    ]
    for title in keep:
        assert sp.not_research(title, "") is None, title


def test_the_corpus_itself_separates_cleanly():
    """Run against the committed registry: the posts are flagged, the notes are
    not, and the flag never lands on a row that was already distilled."""
    import json
    reg = json.loads(sp.REGISTRY.read_text())["papers"]
    live = [r for r in reg.values() if not r.get("duplicate_of")]
    flagged = [r for r in live if r.get("not_research")]
    assert flagged, "the committed registry has no flags; the pass never ran"
    for r in flagged:
        assert not r.get("hypothesis_path"), (
            f"{r['id']} is flagged as not-research but a hypothesis was already "
            f"distilled from it; that needs a human, not a flag")
    zen = [r for r in live if (r.get("venue") or "").startswith("Zenodo")]
    assert len(zen) - len(flagged) >= 10, (
        "the filter is eating the genuine Zenodo notes as well as the posts")


def test_a_flagged_record_is_marked_and_never_deleted():
    """Same rule as duplicates: a row carries decisions -- its screen verdict,
    its rank -- and deleting it would erase the record that we considered it."""
    src = (REPO_ROOT / "scripts" / "screen_papers.py").read_text()
    body = src.split("def mark_not_research", 1)[1].split("\ndef ", 1)[0]
    assert "not_research" in body
    for forbidden in ("del reg[", ".pop(", "remove("):
        assert forbidden not in body, f"{forbidden} deletes a row"


def test_a_flagged_record_cannot_reach_the_model_or_the_distiller():
    """The filter is worthless if the selection paths ignore it -- and with the
    gateway budget exhausted, a record that cannot be screened must also not be
    queued for screening."""
    screen_src = (REPO_ROOT / "scripts" / "screen_papers.py").read_text()
    assert screen_src.count('not p.get("not_research")') >= 2, (
        "the unread and rescreen selections do not both exclude flagged rows")
    retrieve_src = (REPO_ROOT / "scripts" / "retrieve.py").read_text()
    assert 'not v.get("not_research")' in retrieve_src, (
        "worth_distilling() can still hand a task post to the distiller")
