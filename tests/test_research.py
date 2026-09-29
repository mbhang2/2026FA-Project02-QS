"""The research stage, offline. No network is touched by anything in here.

Two boundary claims carry most of the weight, and both mirror `test_llm_agent.py`:

  * **The retrieval never reaches the proposing agent.** The mechanism is
    structural -- `llm.py` assembles the proposing prompt from the hypothesis,
    never from this module -- and the checkable edge is the lint: a draft that
    carries the papers' own p-value / out-of-sample vocabulary is refused by
    `draft`'s bounded repair loop, and the same lint is what `register` runs on
    the committed file. Papers may say it; the hypothesis may not.

  * **The retrieved set is frozen and hashed.** `write` once, `load` refuses a
    copy that no longer hashes to what it says, and the digest is stable across
    a write/read round trip -- the same contract `CampaignRecord` has.

HTTP is injected (`http_get`), so the arXiv API and ar5iv are exercised against
canned bytes rather than the network. The research model is exercised by
replacing `_one` with a fake, so no endpoint is called.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import hypothesis as H  # noqa: E402
from src.core.loop import research  # noqa: E402
from src.core.loop.research import (  # noqa: E402
    Paper, ResearchAgent, ResearchDraftRejected, RetrievalError, RetrievalSet,
    arxiv_search, fetch_fulltext, retrieve,
)

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
 <opensearch:totalResults>2</opensearch:totalResults>
 <entry>
  <id>http://arxiv.org/abs/1903.02228v3</id>
  <title> Learning the dynamics of technical trading strategies </title>
  <summary>We study how technical trading rules behave on noisy prices, and
  the search reports p-values for a population of rules.</summary>
  <published>2019-03-06T18:17:52Z</published>
  <author><name>Jim Murphy</name></author>
  <author><name>Diego Gebbie</name></author>
  <category term="q-fin.ST" scheme="http://arxiv.org/schemas/atom"/>
 </entry>
 <entry>
  <id>http://arxiv.org/abs/2201.00001</id>
  <title>Another paper</title>
  <summary>A second abstract, out of sample quiet.</summary>
  <published>2022-01-01T00:00:00Z</published>
  <author><name>Ann Author</name></author>
  <category term="q-fin.TR" scheme="http://arxiv.org/schemas/atom"/>
 </entry>
</feed>"""

HTML_PAGE = """<!doctype html><html><head><style>p {color:red}</style>
<script>alert(1)</script></head><body>
<h1>Learning the dynamics of technical trading strategies</h1>
<p>We study the dynamics with &amp;<b>italic</b> markup and
<span>whitespace   from <i>tags</i></span>.</p>
<p>Second paragraph that continues the plain text beyond the limit.</p>
</body></html>"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def atom_http(monkeypatch):
    def fake(url: str, timeout_s: float, **kw):
        return _FakeResponse(200, ATOM)

    monkeypatch.setattr(research, "_http_get", fake)
    return fake


# One frozen paper, for the draft tests. `draft` now checks the draft against
# the frozen set, so a draft agent without one is the wiring bug `check_retrieval`
# refuses -- the default here is a real set, and a test that means to assert the
# refusal passes `retrieval=None` explicitly.
_DEFAULT_RETRIEVAL = RetrievalSet(
    name="default", query='all:"technical trading"', max_results=1,
    papers=(Paper(arxiv_id="1903.02228v3", title="Learning the dynamics",
                  authors=("A. Writer",), published="2019-03-06T18:17:52Z",
                  abstract="We study whether a large move continues or gives "
                           "it back, on intraday data.",
                  categories=("q-fin.ST",), fulltext=""),
            ),
    retrieved_at="2026-01-01T00:00:00Z")


def _agent(**kw) -> ResearchAgent:
    import os

    os.environ.setdefault("LITELLM_BASE_URL", "https://example.invalid/v1")
    os.environ.setdefault("LITELLM_API_KEY", "not-a-key-and-never-sent")
    return ResearchAgent(model="test-model",
                         brief=kw.pop("brief", "Does a large move continue, or give it back?"),
                         retrieval=kw.pop("retrieval", _DEFAULT_RETRIEVAL), **kw)


# --------------------------------------------------------------------------- #
# arxiv search and parsing
# --------------------------------------------------------------------------- #

def test_arxiv_search_parses_atom(atom_http):
    papers = arxiv_search('all:"technical trading"')
    assert len(papers) == 2
    p = papers[0]
    assert p.arxiv_id == "1903.02228v3"
    assert p.title == "Learning the dynamics of technical trading strategies"
    assert p.authors == ("Jim Murphy", "Diego Gebbie")
    assert p.published == "2019-03-06T18:17:52Z"
    assert "noisy prices" in p.abstract
    assert p.categories == ("q-fin.ST",)


def test_arxiv_search_nonzero_status_is_an_error(monkeypatch):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(500, "boom")

    monkeypatch.setattr(research, "_http_get", fake)
    with pytest.raises(RetrievalError):
        arxiv_search('all:"x"')


def test_arxiv_search_empty_result_is_an_error(monkeypatch):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(200, '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>')

    monkeypatch.setattr(research, "_http_get", fake)
    with pytest.raises(RetrievalError):
        arxiv_search('all:"x"')


def test_arxiv_search_empty_query_is_refused(atom_http):
    with pytest.raises(RetrievalError):
        arxiv_search("   ")


# --------------------------------------------------------------------------- #
# full text, and the bounded excerpt
# --------------------------------------------------------------------------- #

def test_fetch_fulltext_strips_html_and_bounds_excerpt(monkeypatch):
    def fake(url, timeout_s, **kw):
        assert url.startswith("https://ar5iv.labs.arxiv.org/html/1903.02228")
        return _FakeResponse(200, HTML_PAGE)

    monkeypatch.setattr(research, "_http_get", fake)
    paper = Paper(arxiv_id="1903.02228v3", title="t", authors=(),
                  published="", abstract="a", categories=())
    got = fetch_fulltext(paper, limit_chars=60)
    assert got.fulltext_src == "ar5iv"
    assert "<b>" not in got.fulltext
    assert "&amp;" not in got.fulltext
    assert len(got.fulltext) <= 60
    # markdown loss must not touch the id
    assert got.arxiv_id == "1903.02228v3"
    assert got.abstract == "a"


def test_fetch_fulltext_404_keeps_abstract_only(monkeypatch):
    def fake(url, timeout_s, **kw):
        return _FakeResponse(404, "missing")

    monkeypatch.setattr(research, "_http_get", fake)
    paper = Paper(arxiv_id="9999.00001", title="t", authors=(),
                  published="", abstract="kept", categories=())
    got = fetch_fulltext(paper)
    assert got.fulltext == ""
    assert got.fulltext_src == ""
    assert got.abstract == "kept"


def test_retrieve_fetches_full_text_for_the_top_few_only(atom_http, monkeypatch):
    seen = []
    real = research._http_get

    def fake(url, timeout_s, **kw):
        seen.append(url)
        if url.startswith(research.AR5IV_BASE):
            return _FakeResponse(200, "<html><body>full text body</body></html>")
        return real(url, timeout_s, **kw)

    monkeypatch.setattr(research, "_http_get", fake)
    rs = retrieve("n", query='all:"technical trading"', max_results=2,
                  fulltext_papers=1)
    assert len(rs.papers) == 2
    assert rs.papers[0].fulltext_src == "ar5iv"
    assert rs.papers[0].fulltext == "full text body"
    assert rs.papers[1].fulltext_src == ""
    assert sum(1 for u in seen if u.startswith(research.AR5IV_BASE)) == 1


# --------------------------------------------------------------------------- #
# the retrieved set is frozen
# --------------------------------------------------------------------------- #

def test_retrieval_set_round_trips_with_stable_digest(tmp_path, atom_http):
    rs = retrieve("n", query='all:"technical trading"', max_results=2,
                  fulltext_papers=0)
    path = tmp_path / "config" / "retrievals" / "n.json"
    digest = rs.write(path)
    loaded, ld = RetrievalSet.load(path)
    assert ld == digest
    assert loaded == rs
    assert loaded.to_dict() == rs.to_dict()


def test_retrieval_write_refuses_overwrite(tmp_path, atom_http):
    rs = retrieve("n", query='all:"technical trading"', max_results=1,
                  fulltext_papers=0)
    path = tmp_path / "n.json"
    rs.write(path)
    with pytest.raises(RetrievalError):
        rs.write(path)


def test_retrieval_load_rejects_tampering(tmp_path, atom_http):
    rs = retrieve("n", query='all:"technical trading"', max_results=1,
                  fulltext_papers=0)
    path = tmp_path / "n.json"
    rs.write(path)
    blob = json.loads(path.read_text())
    blob["retrieval"]["papers"][0]["abstract"] = "rewritten after the fact"
    path.write_text(json.dumps(blob))
    with pytest.raises(RetrievalError):
        RetrievalSet.load(path)


def test_retrieval_set_requires_papers(tmp_path):
    rs = RetrievalSet(name="n", query="all:x", max_results=0, papers=(),
                      retrieved_at="2026-01-01T00:00:00Z")
    with pytest.raises(RetrievalError):
        rs.validate()
    with pytest.raises(RetrievalError):
        rs.write(tmp_path / "x.json")


# --------------------------------------------------------------------------- #
# the research agent drafts a hypothesis, and the lint is the boundary
# --------------------------------------------------------------------------- #

def _dirty(attempt: int):
    if attempt == 0:
        return ("# Does a large move continue?\n\nThe papers report p-values "
                "and some find significance out of sample.\n")
    return "# Does a large move continue, or give it back?\n"
    "\nBoth accounts are widely claimed. Searching this instrument at this "
    "time scale tells us which one.\n"


def test_draft_returns_clean_text_when_clean():
    agent = _agent()
    agent._one = lambda prompt, kind: _dirty(999)   # clean answer
    text = agent.draft()
    assert H.findings(text) == []
    H.lint(text)                                     # the registered-file test


def test_draft_without_a_retrieval_set_is_refused():
    """No frozen set is a wiring bug, not a licence to draft. A draft checked
    against nothing passes for any claim, so `check_retrieval` refuses it."""
    agent = _agent(retrieval=None)
    agent._one = lambda prompt, kind: _dirty(999)   # clean, but ungroundable
    with pytest.raises(ResearchDraftRejected) as ei:
        agent.draft()
    assert "no retrieval set" in str(ei.value)


def test_draft_repairs_a_quote_not_in_the_frozen_set():
    """A quotation the frozen papers do not contain drives the same bounded
    repair loop as the lint, and a grounded rewrite is accepted."""
    agent = _agent()
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        agent._pending.append(research.Call(kind, True, 0.1, prompt_tokens=100,
                                            completion_tokens=5,
                                            model_served="test-model"))
        if len(calls) == 1:
            return ('# Does a large move continue?\n\nAs 1234.56789 put it, '
                    '"markets are efficient at all horizons and never trend".\n')
        return "# Does a large move continue, or give it back?\n"

    agent._one = fake
    text = agent.draft()
    assert calls == ["draft", "repair"]
    assert "frozen set" not in text
    log = agent.drain_log()
    assert log["repair_attempts"] == 1


def test_draft_repairs_banned_vocabulary():
    agent = _agent()
    calls = []

    def fake(prompt, kind):
        calls.append(kind)
        agent._pending.append(research.Call(kind, True, 0.1, prompt_tokens=100,
                                            completion_tokens=5,
                                            model_served="test-model"))
        return _dirty(len(calls) - 1)

    agent._one = fake
    text = agent.draft()
    assert calls == ["draft", "repair"]
    assert "p-value" not in text and "significance" not in text
    log = agent.drain_log()
    assert log["n_calls"] == 2
    assert log["repair_attempts"] == 1
    assert log["models_served"] == ["test-model"]


def test_draft_exhausted_repairs_raise_naming_findings():
    agent = _agent()
    agent._one = lambda prompt, kind: _dirty(0)      # always dirty
    with pytest.raises(ResearchDraftRejected) as ei:
        agent.draft()
    assert "p-values" in str(ei.value)
    assert "repair attempts" in str(ei.value)


def test_draft_repair_prompt_directs_to_market_language():
    """The repair instruction phrases the rule for the OUTPUT (linted), not for
    the papers. The prompt itself may name the concepts -- only the draft is
    linted -- but it sends the model back to behaviour, not to any bar."""
    agent = _agent()
    seen = {}

    def fake(prompt, kind):
        seen[kind] = prompt
        return _dirty(len(seen) - 1)

    agent._one = fake
    agent.draft()
    assert "about the MARKET" in seen["repair"]
    assert "not about how an answer would be judged" in seen["repair"]


def test_prompt_assembles_every_slot(atom_http):
    rs = retrieve("n", query='all:"technical trading"', max_results=1,
                  fulltext_papers=0)
    agent = _agent(brief="Does a large move continue, or give it back?",
                   retrieval=rs, run_facts="Dataset: real NQ futures trades.")
    prompt = agent._prompt(repair="")
    assert "give it back" in prompt
    assert "1903.02228v3" in prompt
    assert "Learning the dynamics" in prompt
    assert "real NQ futures trades" in prompt
    assert "technical trading" in prompt


def test_research_prompt_template_is_not_the_proposing_prompt():
    """The research template must not grow a slot the proposing template uses;
    they are different artefacts by design."""
    research_tpl = (REPO_ROOT / "config" / "prompts"
                    / "research-agent.md").read_text()
    strategy_text = (REPO_ROOT / "config" / "prompts"
                     / "strategy-agent.md").read_text()
    assert "{hypothesis}" not in research_tpl
    assert "{history}" not in research_tpl
    assert "## USER" in research_tpl and "## SYSTEM" in research_tpl
    assert "{hypothesis}" in strategy_text   # the proposing template keeps its slots


# --------------------------------------------------------------------------- #
# the campaign record carries the research stage
# --------------------------------------------------------------------------- #

def test_campaign_record_research_fields_move_the_digest(tmp_path):
    from src.core.loop.campaign import CampaignRecord

    base = dict(campaign_id="x", profile="nq-train", budget=10, q=0.05)
    plain = CampaignRecord(**base)
    d0 = plain.digest()

    with_research = CampaignRecord(
        **base,
        hypothesis_path="config/hypotheses/a.md", hypothesis_digest="h1",
        research_prompt_path="config/research/a.md",
        research_prompt_digest="r1",
        retrieval_path="config/retrievals/a.json", retrieval_digest="v1",
        research_config={"implementation": "src.core.loop.research.ResearchAgent"},
    )
    d1 = with_research.digest()
    assert d1 != d0

    path = tmp_path / "campaign.json"
    dw = with_research.write(path)
    rec, dr = CampaignRecord.read(path)
    assert dr == dw == d1
    assert rec.retrieval_digest == "v1"

    # Explicit None stays None: digest hash drops None keys, so a record that
    # never had these fields is untouched by their existence.
    none_research = CampaignRecord(**base, research_prompt_path=None,
                                   research_prompt_digest=None,
                                   retrieval_path=None, retrieval_digest=None,
                                   research_config=None)
    assert none_research.digest() == d0