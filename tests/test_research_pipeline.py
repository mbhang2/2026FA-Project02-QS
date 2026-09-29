"""The corpus pipeline stages, offline. No network, no endpoint.

Each new stage is exercised the way `test_research.py` exercises `draft`:
HTTP through an injected `http_get`, the model through a replaced `_one`.
The three boundary claims carried:

  * **The feed is a dedup by base id.** A paper re-encountered on a later
    page (a new version submitted after the old) is the corpus's paper, not a
    second row, and `register` makes the row write-once anyway.
  * **The screen's output is strict data.** A verdict that does not parse as
    exactly the four-field JSON (and an `adaptable` verdict with no
    adaptation bound) is refused and repaired, not shrugged through.
  * **The distil's output pays the lint and the budget.** The page must be
    clean of gate vocabulary (the `findings` lint) and inside 800-1,500 tokens
    by the character estimate; both are enforced on the bounded repair loop.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import research  # noqa: E402
from src.core.loop.research import (  # noqa: E402
    DataContract,
    DistilAgent,
    DistillRejected,
    Paper,
    RerankVerdict,
    RetrievalError,
    ScreenAgent,
    ScreenRejected,
    ScreenVerdict,
    data_contract,
    default_query,
    fetch_feed,
    open_questions,
    rerank,
)

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
 <opensearch:totalResults>2</opensearch:totalResults>
 <entry>
  <id>http://arxiv.org/abs/1903.02228v3</id>
  <title> Learning the dynamics of technical trading strategies </title>
  <summary>We study how technical trading rules behave on noisy prices.</summary>
  <published>2026-02-01T18:17:52Z</published>
  <author><name>Jim Murphy</name></author>
  <category term="q-fin.ST" scheme="http://arxiv.org/schemas/atom"/>
 </entry>
 <entry>
  <id>http://arxiv.org/abs/2201.00001v2</id>
  <title>Another feed paper</title>
  <summary>A second abstract.</summary>
  <published>2026-02-01T19:00:00Z</published>
  <author><name>Ann Author</name></author>
  <category term="q-fin.TR" scheme="http://arxiv.org/schemas/atom"/>
 </entry>
</feed>"""

ATOM_OLD_VERSION = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
 <entry>
  <id>http://arxiv.org/abs/1903.02228v1</id>
  <title> Learning the dynamics of technical trading strategies </title>
  <summary>An older version of the same paper.</summary>
  <published>2026-01-01T18:17:52Z</published>
  <author><name>Jim Murphy</name></author>
  <category term="q-fin.ST" scheme="http://arxiv.org/schemas/atom"/>
 </entry>
</feed>"""

EMPTY_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
</feed>"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _env(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "not-a-key-and-never-sent")


def _paper(arxiv_id="2602.55555", title="Volume Imbalance and the Close",
           abstract="We study how volume imbalance at the close predicts the "
                    "next open, on NQ futures intraday data.",
           categories=("q-fin.TR",)) -> Paper:
    return Paper(arxiv_id=arxiv_id, title=title,
                 authors=("A. Writer",), published="2026-02-20",
                 abstract=abstract, categories=categories)


# --------------------------------------------------------------------------- #
# the feed
# --------------------------------------------------------------------------- #

def test_fetch_feed_walks_pages_and_dedups_by_base_id(monkeypatch):
    seen = []

    def fake(url, timeout_s, **kw):
        seen.append(url)
        if "start=0" in url:
            return _FakeResponse(200, ATOM)
        return _FakeResponse(200, ATOM_OLD_VERSION)

    monkeypatch.setattr(research, "_http_get", fake)
    papers = fetch_feed(max_pages=2, delay_s=0.0)
    ids = [p.arxiv_id for p in papers]
    assert ids == ["1903.02228v3", "2201.00001v2"], (
        "the newest version wins and the old version of the same base id is "
        "not a second paper")
    assert any("start=1" in u for u in seen)


def test_fetch_feed_zero_fresh_papers_stops_early(monkeypatch):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(200, ATOM)

    monkeypatch.setattr(research, "_http_get", fake)
    papers = fetch_feed(max_pages=5, delay_s=0.0)
    assert len(papers) == 2


def test_fetch_feed_empty_tail_page_is_end_of_feed_not_an_error(monkeypatch):
    """A query with fewer hits than a page must walk the pages it has and stop.

    arXiv answers a page offset past the end of a result set with a 200 feed
    containing zero entries. That is the feed being exhausted, not a broken
    query, so `fetch_feed` returns what it found instead of raising.
    """

    def fake(url, timeout_s, **kw):
        if "start=0" in url:
            return _FakeResponse(200, ATOM)
        return _FakeResponse(200, EMPTY_FEED)

    monkeypatch.setattr(research, "_http_get", fake)
    papers = fetch_feed(max_pages=3, delay_s=0.0)
    assert [p.arxiv_id for p in papers] == ["1903.02228v3", "2201.00001v2"]


def test_fetch_feed_http_failure_is_an_error(monkeypatch):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(500, "boom")

    monkeypatch.setattr(research, "_http_get", fake)
    with pytest.raises(RetrievalError):
        fetch_feed()


def test_fetch_feed_refuses_empty_query(monkeypatch, tmp_path):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(200, ATOM)

    monkeypatch.setattr(research, "_http_get", fake)
    with pytest.raises(RetrievalError):
        fetch_feed(query="   ")


# --------------------------------------------------------------------------- #
# the data contract
# --------------------------------------------------------------------------- #

def test_data_contract_reads_the_harnesss_own_file(tmp_path):
    run_yaml = tmp_path / "run.yaml"
    run_yaml.write_text(
        "profiles:\n"
        "  nq-train:\n"
        "    dataset: nq\n    split: train\n    fill_model: unfavorable\n"
        "    commission_per_contract: 0.10\n"
        "campaign:\n  n_trials: 50\n")
    c = data_contract(n_sessions=384, run_yaml=run_yaml)
    block = c.block()
    assert "384 sessions" in block
    assert "commission" in block.lower()
    assert "0.1" in block            # 0.10 per side renders as the float it is
    assert "one decision per session" in block
    assert c.facts["dataset"] == "nq"


def test_data_contract_is_a_frozen_facts_bag(tmp_path):
    run_yaml = tmp_path / "run.yaml"
    run_yaml.write_text("profiles:\n  nq-train:\n    dataset: nq\n")
    c = data_contract(n_sessions=12, run_yaml=run_yaml)
    assert c.facts["n_sessions"] == 12
    assert c.facts["fill_model"] == "unfavorable"


# --------------------------------------------------------------------------- #
# the screen
# --------------------------------------------------------------------------- #

# The quote is a verbatim span of `_paper`'s abstract. It was "we test at the
# daily frequency" -- a plausible sentence the paper does not contain, which the
# grounding check now refuses, as it should. These tests are about repair, so the
# fixture quotes for real.
GOOD_VERDICT = json.dumps({
    "feasibility": "adaptable",
    "reason": "the paper needs one-bar decisions; close bars exist",
    "adaptation": "use per-bar decisions on the existing session data",
    "quote": "volume imbalance at the close predicts the next open",
})


def _screen_agent(**kw) -> ScreenAgent:
    _env(kw.pop("monkeypatch", None))
    return ScreenAgent(model="test-model",
                       paper=kw.pop("paper", _paper()),
                       contract=kw.pop("contract", data_contract().block()),
                       **kw)


def test_extract_json_skips_prose_and_fences():
    assert research._extract_json(GOOD_VERDICT)["feasibility"] == "adaptable"
    fenced = "```json\n" + GOOD_VERDICT + "\n```"
    assert research._extract_json(fenced)["feasibility"] == "adaptable"
    assert research._extract_json("no json here") is None


def test_screen_returns_the_verdict_when_clean(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: GOOD_VERDICT
    verdict = agent.screen()
    assert isinstance(verdict, ScreenVerdict)
    assert verdict.feasibility == "adaptable"
    assert verdict.reason
    assert verdict.adaptation
    assert verdict.testable


def test_screen_repairs_a_broken_object_and_recovers(monkeypatch):
    """The draft is broken JSON; the repair (fenced, in prose) is accepted."""
    agent = _screen_agent(monkeypatch=monkeypatch)
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        agent._pending.append(research.Call(
            kind, True, 0.1, prompt_tokens=100, completion_tokens=5,
            model_served="test-model"))
        if kind == "draft":
            return '{"feasibility": "adaptable", "reason": "truncated'
        return "```json\n" + GOOD_VERDICT + "\n```"

    agent._one = fake
    verdict = agent.screen()
    assert calls == ["draft", "repair"]
    assert verdict.feasibility == "adaptable"
    log = agent.drain_log()
    assert log["n_calls"] == 2
    assert log["repair_attempts"] == 1


def test_screen_rejects_an_adaptable_verdict_without_adaptation(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: json.dumps({
        "feasibility": "adaptable", "reason": "x", "adaptation": "",
        "quote": ""})
    with pytest.raises(ScreenRejected) as ei:
        agent.screen()
    assert "adaptable requires a non-empty adaptation" in str(ei.value)


def test_screen_rejects_feasibility_getting_a_new_word(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: json.dumps({
        "feasibility": "maybe", "reason": "x", "adaptation": "",
        "quote": ""})
    with pytest.raises(ScreenRejected):
        agent.screen()


def test_screen_exhausted_nonsense_raises(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: "I think this paper is interesting."
    with pytest.raises(ScreenRejected) as ei:
        agent.screen()
    assert "repair attempts exhausted" in str(ei.value)


def test_the_screen_prompt_carries_paper_and_contract(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch,
                          contract="the desk runs 384 sessions")
    prompt = agent._prompt(repair="")
    assert _paper().title in prompt
    assert "the desk runs 384 sessions" in prompt
    assert "## USER" in prompt and "## SYSTEM" in prompt


def test_infeasible_is_not_a_parser_error(monkeypatch):
    agent = _screen_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: json.dumps({
        "feasibility": "infeasible",
        "reason": "the paper needs tick-level data the desk does not have",
        "adaptation": "", "quote": ""})
    verdict = agent.screen()
    assert not verdict.testable
    assert "tick-level" in verdict.reason


# --------------------------------------------------------------------------- #
# the re-rank
# --------------------------------------------------------------------------- #

def test_rerank_demotes_a_falsified_family_with_a_citation():
    paper = _paper(title="GRU sequence models for NQ closing auctions",
                   abstract="a recurrent neural architecture")
    v = rerank(paper)
    assert v.demoted is True
    assert "sequence models at the current sample size" in v.demoted_reason
    assert "docs/datasets/nq-es-qqq.md" in v.demoted_reason


def test_rerank_lets_a_clean_paper_through():
    v = rerank(_paper())
    assert v.demoted is False
    assert v.demoted_reason == ""
    assert v.relevance is None     # no open questions supplied


def test_rerank_relevance_is_the_token_overlap():
    v = rerank(_paper(title="Volume Imbalance",
                      abstract="imbalance"),
               open_questions_text="is volume imbalance at the open a "
                                   "directional signal?")
    assert v.relevance >= 1
    assert v.demoted is False


def test_rerank_is_an_explicit_dataclass():
    v = RerankVerdict(demoted=True, demoted_reason="family: F",
                      relevance=0)
    assert v.as_dict()["demoted_reason"] == "family: F"


def test_open_questions_is_optional(tmp_path):
    assert open_questions(tmp_path / "missing.md") == ""
    p = tmp_path / "open.md"
    p.write_text("is volume imbalance directional?")
    assert "volume imbalance" in open_questions(p)


def test_rerank_prefers_the_prompt_to_open_questions():
    v = rerank(_paper(title="Watchmaker", abstract="closing pressure"),
               open_questions_text="falsified filler",
               prompt_text="does closing pressure predict the open?")
    assert v.relevance >= 1
    assert v.demoted is False


def test_default_query_picks_the_most_frequent_content_bigram_from_a_prompt():
    q = default_query("Does closing pressure predict the next open, and how "
                      "strong is that signal? We study closing pressure.")
    assert q == 'all:"closing pressure"'
    assert default_query("# a title alone is excluded and leaves no query") \
        == 'all:"intraday"'


def test_default_query_falls_back_to_filled_phrase():
    assert default_query("the a an and") == 'all:"intraday"'


# --------------------------------------------------------------------------- #
# the distil
# --------------------------------------------------------------------------- #

def _distil_agent(**kw) -> DistilAgent:
    _env(kw.pop("monkeypatch", None))
    return DistilAgent(
        model="test-model",
        paper=kw.pop("paper", _paper()),
        brief=kw.pop("brief", "What direction does the close give the open?"),
        run_facts=kw.pop("run_facts", "Dataset: real NQ futures trades."),
        data_doc=kw.pop("data_doc", "bars, volume, close."),
        **kw)


def _page() -> str:
    """A lint-clean page of ~4,200 characters (~1,050 tokens): inside budget."""
    paras = [
        "One account says the imbalance at the close is absorbed overnight, so "
        "the next open is a fair continuation of the close. The competing "
        "account says the absorption is only partial, and the part of the book "
        "that did not finish before the close leaks into the next session's "
        "opening bars, so the larger the closing imbalance the more of the move "
        "the open still has ahead of it.",
        "An observation that discriminates them is a session whose closing "
        "imbalance is large relative to that session's own ordinary range: "
        "continuation says the open tracks the close; the partial-absorption "
        "account says the direction gives some of it back, in the first few "
        "bars after the open, and that the give-back grows with the size of "
        "the undisabsorbed book. Entry and exit are part of the claim, and "
        "both pay the spread and the per-side commission at the cost floor.",
        "What is deliberately NOT being asked is whether the imbalance can be "
        "forecast, or why it arose. The move is already known when the claim "
        "starts; only what a realised closing imbalance means for the next "
        "open is in question. There is no side information beyond the bar "
        "series, the volume and the close, no regime conditioning is assumed, "
        "and the claim does not depend on a particular time of day.",
    ]
    body = "\n\n".join(paras)
    s = body
    while len(s) < 4000:
        s += "\n\n" + body
    return "# Does closing pressure predict the next open?\n\n" + s[:4200]


PAGE = _page()


def _long_page() -> str:
    OUT = "A filler sentence that repeats to reach a length well past the budget. "
    return "# X\n\n" + (OUT * 90)


def _short_page() -> str:
    return "# X\n\nToo brief.\n"


def test_distil_returns_a_clean_page(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: PAGE
    text = agent.distil()
    assert research.findings(text) == []
    est = research._estimate_tokens(text)
    assert research.DISTIL_MIN_TOKENS <= est <= research.DISTIL_MAX_TOKENS


def test_distil_repairs_a_leak(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        agent._pending.append(research.Call(
            kind, True, 0.1, prompt_tokens=100, completion_tokens=5,
            model_served="test-model"))
        dirty = PAGE.replace("closing pressure", "closing pressure's p-value "
                             "and its significance out of sample")
        return PAGE if kind == "repair" else dirty

    agent._one = fake
    text = agent.distil()
    assert calls == ["draft", "repair"]
    assert "p-value" not in text
    log = agent.drain_log()
    assert log["repair_attempts"] == 1


def test_distil_repairs_over_the_budget(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        return PAGE if kind == "repair" else _long_page()

    agent._one = fake
    text = agent.distil()
    assert calls == ["draft", "repair"]
    assert research._estimate_tokens(text) <= research.DISTIL_MAX_TOKENS


def test_distil_repairs_under_the_budget(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        return PAGE if kind == "repair" else _short_page()

    agent._one = fake
    text = agent.distil()
    assert calls == ["draft", "repair"]
    assert research._estimate_tokens(text) >= research.DISTIL_MIN_TOKENS


def test_distil_exhaustion_raises_naming_problems(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    agent._one = lambda prompt, kind: _short_page()
    with pytest.raises(DistillRejected) as ei:
        agent.distil()
    assert "under the length budget" in str(ei.value)


def test_distil_config_records_the_budget(monkeypatch):
    agent = _distil_agent(monkeypatch=monkeypatch)
    cfg = agent.config()
    assert cfg["budget_floor_tokens"] == research.DISTIL_MIN_TOKENS
    assert cfg["budget_ceiling_tokens"] == research.DISTIL_MAX_TOKENS
    assert cfg["output_linted_against_gate_vocabulary"] is True
    assert cfg["paper"] == "2602.55555"


def test_distil_prompt_carries_paper_abstract_and_full_text(monkeypatch):
    full = _paper()
    full = Paper(arxiv_id=full.arxiv_id, title=full.title,
                 authors=full.authors, published=full.published,
                 abstract=full.abstract, categories=full.categories,
                 fulltext="a method section of the survivor paper",
                 fulltext_src="ar5iv")
    agent = _distil_agent(monkeypatch=monkeypatch, paper=full)
    prompt = agent._prompt(repair="")
    assert "a method section of the survivor paper" in prompt
    assert "Volume Imbalance" in prompt