"""The step before the loop: papers in, a linted hypothesis out.

A campaign proposes strategies, but nothing decides *what to investigate*. A
human writing `config/hypotheses/*.md` from scratch works, and until Task 47 it
was the only path. This module is the missing step: it fetches papers from
arXiv, freezes the retrieved set as a committed file, and runs a model -- the
*research* agent -- over a research brief, the papers and the documentation to
draft the hypothesis the proposing agent will work under.

## Where this runs, and what that means for the retrieval

Everything here lives in the loop driver, which has network -- the same
ordering decision `llm.py` makes. Two consequences, and both are deliberate:

  * **The paper text reaches the research agent and only the research agent.**
    The proposing agent's prompt is assembled by `llm.LLMAgent` from the
    *hypothesis*; nothing in this module is on that path. That is load-bearing:
    a research paper is full of p-values, thresholds and out-of-sample talk,
    and none of that may flow into a prompt that is guaranteed to say nothing
    about the gate. The `Hypothesis` lint in `hypothesis.py` is the enforcement
    at the boundary: the research agent's *output* must be clean before it can
    be registered, whatever the papers said.

  * **The strategy process never touches this code.** A strategy cannot cite a
    paper or seed itself from the retrieval. It proposes against the API and
    the hypothesis, like every other campaign.

## Frozen before registration, hashed like everything else

The retrieved set is written to `config/retrievals/<name>.json` and committed;
its digest travels in the campaign record beside the hypothesis digest and the
prompt digest. The commit date of the retrieval file is therefore the
pre-registration date, exactly as for `config/calendars/` and
`config/hypotheses/`. Editing the retrieval mid-campaign changes the digest and
the record then fails its own check -- the same alarm an edited budget rings.

Frozen is the point of fetching at all: the campaign answers from one snapshot
of the literature, not from whatever the search would return on the day the
report is read. The arXiv API (`export.arxiv.org/api/query`) is Atom XML and
needs only `urllib` and `xml.etree`; ar5iv full text is HTML that gets stripped
to a bounded excerpt. `requests` is already a driver dependency.
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .grounding import Ungrounded, check_quote, check_retrieval, check_text
from .hypothesis import findings
from .llm import Call, LLMError, ModelSubstituted, template_digest
from .registry import key_of

from .._roots import repo_root

REPO_ROOT = repo_root()
RESEARCH_TEMPLATE_PATH = REPO_ROOT / "config" / "prompts" / "research-agent.md"
SCREEN_TEMPLATE_PATH = REPO_ROOT / "config" / "prompts" / "screen-agent.md"
DISTIL_TEMPLATE_PATH = REPO_ROOT / "config" / "prompts" / "distil-agent.md"
DATA_DOC_PATH = REPO_ROOT / "docs" / "agent" / "data.md"
RUN_YAML_PATH = REPO_ROOT / "config" / "run.yaml"
OPEN_QUESTIONS_PATH = REPO_ROOT / "config" / "research" / "open-questions.md"

ARXIV_API = "https://export.arxiv.org/api/query"
AR5IV_BASE = "https://ar5iv.labs.arxiv.org/html"
_ATOM = "http://www.w3.org/2005/Atom"
_OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
_ARXIV = "http://arxiv.org/schemas/atom"

DEFAULT_FULLTEXT_CHARS = 16_000
# The corpus pipeline's tier-2 fetch: full text for the handful of papers that
# survive the screen and the re-rank, at a bound that fits a whole method
# section rather than an abstract-sized excerpt.
DISTIL_FULLTEXT_CHARS = 80_000

# The distil budget, as applied by DistilAgent. Enforced on the character
# estimate (len/4), with words, on both sides -- a page, not a sentence and
# not a chapter.
DISTIL_MIN_TOKENS = 800
DISTIL_MAX_TOKENS = 1_500

# The feed the corpus walks newest-first. q-fin.TR/ST/CP are trading, statistics
# and computational finance; q-fin.PM (portfolio management) and the rest are
# deliberately outside the sweep.
FEED_QUERY = "cat:q-fin.TR OR cat:q-fin.ST OR cat:q-fin.CP"


class RetrievalError(RuntimeError):
    """The retrieval set is unusable, or a frozen copy was edited."""


class ResearchDraftRejected(RuntimeError):
    """The research agent's draft could not be made fit to register.

    Either the model call path failed, or the bounded repair loop ran out and
    the draft still tells the proposing agent something about the gate or a
    closed split. Never silently repaired: the draft is human-visible before it
    becomes a hypothesis.
    """


# --------------------------------------------------------------------------- #
# the paper, and the frozen set
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Paper:
    """One retrieved paper. Frozen: it is part of a pre-registration."""

    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    published: str            # iso date from the feed
    abstract: str
    categories: tuple[str, ...]
    fulltext: str = ""        # a bounded excerpt, if fetched
    fulltext_src: str = ""    # "ar5iv" | "" when only the abstract exists

    def as_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.authors,
            "published": self.published,
            "abstract": self.abstract,
            "categories": self.categories,
            "fulltext": self.fulltext,
            "fulltext_src": self.fulltext_src,
        }


@dataclass(frozen=True, slots=True)
class RetrievalSet:
    """The frozen answer to one brief: the query and what it returned.

    Digest is over the whole thing -- metadata and abstract and any full-text
    excerpt -- because the campaign answers from this snapshot, and the snapshot
    is the input a re-run would have to read to mean the same thing.
    """

    name: str
    query: str
    max_results: int
    papers: tuple[Paper, ...]
    retrieved_at: str

    def validate(self) -> None:
        problems: list[str] = []
        if not self.name.strip():
            problems.append("name is empty")
        if not self.query.strip():
            problems.append("query is empty")
        if not self.papers:
            problems.append("no papers retrieved; nothing was frozen")
        for i, p in enumerate(self.papers):
            if not p.arxiv_id.strip():
                problems.append(f"paper {i}: empty arxiv id")
            if not p.title.strip():
                problems.append(f"paper {i}: empty title")
            if not p.abstract.strip():
                problems.append(f"paper {i}: empty abstract")
        if problems:
            raise RetrievalError("retrieval set is unusable:\n  "
                                 + "\n  ".join(problems))

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "query": self.query,
            "max_results": self.max_results,
            "retrieved_at": self.retrieved_at,
            "papers": [p.as_dict() for p in self.papers],
        }

    def write(self, path: Path) -> str:
        self.validate()
        if path.exists():
            raise RetrievalError(
                f"{path} already exists. A retrieval is frozen once; to fetch "
                "fresh, pick a new name so the old set stays on record.")
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = self.digest()
        path.write_text(json.dumps(
            {"retrieval": self.to_dict(), "digest": digest},
            indent=2, sort_keys=True) + "\n")
        return digest

    @classmethod
    def load(cls, path: Path) -> tuple["RetrievalSet", str]:
        """Read, re-hash, reject a mismatch. Same contract as CampaignRecord."""
        body = json.loads(path.read_text())
        d = body["retrieval"]
        papers = tuple(Paper(
            arxiv_id=p["arxiv_id"], title=p["title"],
            authors=tuple(p["authors"]), published=p["published"],
            abstract=p["abstract"], categories=tuple(p["categories"]),
            fulltext=p.get("fulltext", ""), fulltext_src=p.get("fulltext_src", ""),
        ) for p in d["papers"])
        rs = cls(name=d["name"], query=d["query"], max_results=d["max_results"],
                 papers=papers, retrieved_at=d["retrieved_at"])
        rs.validate()
        stored = body["digest"]
        if rs.digest() != stored:
            raise RetrievalError(
                f"{path} does not hash to what it says: stored {stored[:12]}, "
                f"recomputed {rs.digest()[:12]}. A retrieved set edited after "
                "a campaign registered is a bibliography that moved.")
        return rs, stored


def retrieval_block(retrieval: RetrievalSet | None) -> str:
    """The prompt section. Never reaches the proposing agent -- only `draft`."""
    if retrieval is None:
        return "(no retrieval supplied)"
    out = [f"name:      {retrieval.name}",
           f"query:     {retrieval.query}",
           f"retrieved: {retrieval.retrieved_at}",
           f"max_results: {retrieval.max_results}",
           ""]
    for i, p in enumerate(retrieval.papers):
        head = f"[{i}] {p.title}  (arxiv:{p.arxiv_id}, {p.published})"
        foot = "    by " + ", ".join(p.authors[:6])
        if len(p.authors) > 6:
            foot += f" et al. ({len(p.authors)} authors)"
        out += [head, foot, f"    abstract: {p.abstract}"]
        if p.fulltext_src and p.fulltext:
            out += [f"    full text excerpt ({p.fulltext_src}):\n"
                    f"    {p.fulltext[:2_000].replace(chr(10), chr(10) + '    ')}"]
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTTP: the driver's network, injected for tests
# --------------------------------------------------------------------------- #

def _http_get(url: str, *, timeout_s: float, **kw):
    """Small seam. `requests` lives in the driver, and so does this module."""
    import requests

    return requests.get(url, timeout=timeout_s, **kw)


def arxiv_search(query: str, *, max_results: int = 10,
                 timeout_s: float = 60.0,
                 http_get: Callable | None = None) -> list[Paper]:
    """Search the arXiv API and return metadata for the top `max_results`.

    Deterministic given the query at a point in time, which is what lets the
    result be frozen. The feed is Atom XML; `http_get` is injected so tests can
    answer without touching the network.
    """
    if not query.strip():
        raise RetrievalError("arxiv_search needs a non-empty query")
    get = http_get or _http_get
    params = {"search_query": query, "start": "0",
              "max_results": str(max_results), "sortBy": "relevance",
              "sortOrder": "descending"}
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    try:
        resp = get(url, timeout_s=timeout_s)
    except Exception as exc:
        raise RetrievalError(f"arxiv API request failed: {type(exc).__name__}: "
                             f"{exc}") from None
    if resp.status_code != 200:
        raise RetrievalError(
            f"arxiv API answered HTTP {resp.status_code}: {resp.text[:200]}")
    return _parse_atom(resp.text)


def _parse_atom(body: str, *, allow_empty: bool = False) -> list[Paper]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RetrievalError(f"arxiv API returned unparseable XML: {exc}") from None
    out: list[Paper] = []
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        eid = entry.findtext(f"{{{_ATOM}}}id") or ""
        title = entry.findtext(f"{{{_ATOM}}}title") or ""
        summary = entry.findtext(f"{{{_ATOM}}}summary") or ""
        published = entry.findtext(f"{{{_ATOM}}}published") or ""
        authors = tuple(n.text or "" for n in
                        entry.findall(f"{{{_ATOM}}}author/{{{_ATOM}}}name"))
        categories = tuple(c.get("term") or "" for c in
                           entry.findall(f"{{{_ATOM}}}category"))
        arxiv_id = _arxiv_id(eid)
        out.append(Paper(arxiv_id=arxiv_id, title=_clean(title),
                         authors=authors, published=published,
                         abstract=_clean(summary), categories=categories))
    if not out and not allow_empty:
        raise RetrievalError("arxiv API returned no entries for the query")
    return out


def _arxiv_id(entry_id: str) -> str:
    for marker in ("arxiv.org/abs/", "arxiv.org/pdf/"):
        idx = entry_id.find(marker)
        if idx != -1:
            stripped = entry_id[idx + len(marker):].strip()
            if stripped:
                return stripped
    raise RetrievalError(f"cannot extract an arxiv id from {entry_id!r}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(html: str) -> str:
    """ar5iv full text as a rough plain-text excerpt. Bounded by the caller."""
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    html = re.sub(r"(?is)<svg\b.*?</svg>", " ", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(html)).strip()


def fetch_fulltext(paper: Paper, *, limit_chars: int = DEFAULT_FULLTEXT_CHARS,
                   timeout_s: float = 60.0,
                   http_get: Callable | None = None) -> Paper:
    """The plain-text opening of a paper from ar5iv, artificially bounded.

    On any failure the paper keeps its abstract and no excerpt -- a candidate
    whose paper page is unreachable is not a reason to stop the whole stage. The
    abstract is already in the set, so "no full text" still means the brief was
    answered from the paper's own words.
    """
    get = http_get or _http_get
    try:
        resp = get(f"{AR5IV_BASE}/{paper.arxiv_id.split('v')[0]}",
                   timeout_s=timeout_s)
        if resp.status_code != 200:
            return Paper(**paper.as_dict())
        text = _html_to_text(resp.text)
    except Exception:
        return Paper(**paper.as_dict())
    if not text:
        return Paper(**paper.as_dict())
    return Paper(arxiv_id=paper.arxiv_id, title=paper.title,
                 authors=paper.authors, published=paper.published,
                 abstract=paper.abstract, categories=paper.categories,
                 fulltext=text[:limit_chars], fulltext_src="ar5iv")


def retrieve(name: str, *, query: str, max_results: int = 10,
             fulltext_papers: int = 3,
             fulltext_chars: int = DEFAULT_FULLTEXT_CHARS,
             timeout_s: float = 60.0,
             http_get: Callable | None = None) -> RetrievalSet:
    """Search, fetch the top few papers' full text, and freeze everything."""
    papers = arxiv_search(query, max_results=max_results, http_get=http_get)
    for idx in range(min(fulltext_papers, len(papers))):
        papers[idx] = fetch_fulltext(papers[idx], limit_chars=fulltext_chars,
                                     timeout_s=timeout_s, http_get=http_get)
    rs = RetrievalSet(name=name, query=query, max_results=max_results,
                      papers=tuple(papers),
                      retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()))
    rs.validate()
    return rs


# --------------------------------------------------------------------------- #
# the corpus feed: walk the arXiv wash, no brief required
# --------------------------------------------------------------------------- #

_QUERY_STOP = frozenset("""
    a an and are as at be but by for from have if in into is it its not of on
    or such that the their then there these they this to was were will with
    whose about would should could been being between both each few had has he
    her him his how i if them then there these they those through under until
    upon we when where which while who whom why our your their its this that
    those these i we you he she it they me him her us them my mine hers ours
    yours theirs do does did doing done is are was were been being be have has
    had having will would shall should can could may might must am
    """.split())


def default_query(text: str) -> str:
    """Default arXiv query for a prompt: its most frequent content bigram.

    Measured live on the API while building v1: a bare multi-term `all:` query
    is OR (noise), a long quoted sentence-phrase is an exact match that never
    occurs (nothing), AND-ing several generic singletons pulls in papers whose
    words coincide but whose topics do not (physics), and appending a frequent
    filler like "open" halves the hits. The form that returned the most
    on-topic papers for this repo's own brief was the single most frequent
    two-word collocation -- "intraday momentum" -- so that is the default.
    Headings are excluded (a "# Title" repeats the body and would win the
    frequency vote).

    It is still a default, not a law: `--query` overrides it, and the pipeline
    prints the query so the human can see and veto it before anything is
    fetched.
    """
    import re
    from collections import Counter

    body = " ".join(line for line in text.splitlines()
                    if line.lstrip() and not line.lstrip().startswith("#"))
    flat = re.sub(r"[#*_`{}\[\]()|:\\/0-9]+", " ", body).lower()
    tokens = [w for w in re.findall(r"[a-z]{3,}", flat) if w not in _QUERY_STOP]
    counts = Counter(tokens)
    bigrams = Counter(f"{tokens[i]} {tokens[i + 1]}"
                      for i in range(len(tokens) - 1))
    if bigrams:
        best, best_n = None, -1
        for gram in bigrams:                 # first occurrence wins ties
            if bigrams[gram] > best_n:
                best, best_n = gram, bigrams[gram]
        return f'all:"{best}"'
    if counts:
        return f'all:"{counts.most_common(1)[0][0]}"'
    return 'all:"intraday"'


def fetch_feed(*, query: str = FEED_QUERY, per_page: int = 100,
               max_pages: int = 3, delay_s: float = 3.0,
               timeout_s: float = 60.0,
               http_get: Callable | None = None) -> list[Paper]:
    """Walk the q-fin feed newest-first, metadata only, and return the union.

    `retrieve` answers one brief and freezes one snapshot. `fetch_feed` is the
    corpus's un-briefed eye: it pages the feed by submission date (arXiv caps a
    page at 100 entries), sleeps `delay_s` between pages, and hands every paper
    to the registry. Deduplicated by base id -- the newest occurrence wins,
    which is exactly who should own the row.

    Metadata only, as the v2 plan says: no bulk PDF fetching, no full text at
    this stage. Full text is the *distil* tier, for the handful of survivors.
    """
    if not query.strip():
        raise RetrievalError("fetch_feed needs a non-empty query")
    if per_page < 1 or max_pages < 1:
        raise RetrievalError("per_page and max_pages must be >= 1")
    get = http_get or _http_get
    seen: set[str] = set()
    out: list[Paper] = []
    for page in range(max_pages):
        params = {"search_query": query, "start": str(page * per_page),
                  "max_results": str(per_page), "sortBy": "submittedDate",
                  "sortOrder": "descending"}
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        try:
            resp = get(url, timeout_s=timeout_s)
        except Exception as exc:
            raise RetrievalError(
                f"arxiv feed request failed on page {page}: "
                f"{type(exc).__name__}: {exc}") from None
        if resp.status_code != 200:
            raise RetrievalError(
                f"arxiv feed answered HTTP {resp.status_code} on page {page}: "
                f"{resp.text[:200]}")
        fresh = 0
        for paper in _parse_atom(resp.text, allow_empty=True):
            if key_of(paper.arxiv_id) in seen:
                continue
            seen.add(key_of(paper.arxiv_id))
            out.append(paper)
            fresh += 1
        if not fresh:
            return out
        if page < max_pages - 1:
            time.sleep(delay_s)
    return out


def paper_block(paper: "Paper | None") -> str:
    """The prompt section for one paper, abstract plus any full-text excerpt."""
    if paper is None:
        return "(no paper supplied)"
    head = f"[{paper.title}]  (arxiv:{paper.arxiv_id}, {paper.published})"
    foot = "    by " + ", ".join(paper.authors[:6])
    if len(paper.authors) > 6:
        foot += f" et al. ({len(paper.authors)} authors)"
    out = [head, foot, f"    abstract: {paper.abstract}"]
    if paper.fulltext_src and paper.fulltext:
        out += [f"    full text excerpt ({paper.fulltext_src}):\n"
                f"    {paper.fulltext.replace(chr(10), chr(10) + '    ')}"]
    return "\n".join(out)


@dataclass(frozen=True, slots=True)
class DataContract:
    """The facts a screen decides against, from the harness's own run.yaml.

    The screen is only as honest as the numbers it decides against, and those
    numbers live in `config/run.yaml`, not in a prompt or a docstring -- a
    copy here would be a copy waiting to drift. `block()` renders the facts
    with the sample-size trap spelled out, because that trap is the screen's
    whole reason for existing.
    """

    facts: dict

    def block(self) -> str:
        f = self.facts
        n = f["n_sessions"]
        lines = [
            f"Dataset: {f['dataset']}, {n} sessions on the pre-registered "
            f"{f['split']} split.",
            f"Fill model: {f['fill_model']} -- a fill requires a trade "
            "strictly through your limit, so strategies pay the spread on "
            "every entry and exit.",
        ]
        if f.get("commission_per_side") is not None:
            lines.append(
                f"Commission: {f['commission_per_side']} index points per "
                "contract per side. NQ is $20 a point, so a round trip costs "
                "about $4.00 a contract and turnover is not free.")
        if f.get("action_cap") is not None and f.get("history_window") is not None:
            lines.append(
                f"Action cap: {f['action_cap']} per session. "
                f"History window: {f['history_window']} bars.")
        lines.append(
            f"Sample size: {n} sessions is the observation count for any "
            "strategy that makes one decision per session. A rule that acts "
            f"inside a session draws more fills, but its independent decisions "
            f"number {n}; an effect that needs thousands of independent "
            "observations to separate from cost is not testable here.")
        return "\n".join(lines)


def data_contract(*, profile: str = "nq-train", n_sessions: int = 384,
                  run_yaml: Path | None = None) -> DataContract:
    """The screen's data contract, read from the same file the harness reads.

    `profile` is a `profiles:` key in `config/run.yaml`; real NQ data is the
    `nq-train` profile with 384 training sessions -- the train split's count,
    fixed as `N_SESSIONS` in `scripts/research_campaign.py`. A different
    dataset screens against its own size. The commission figure is per SIDE,
    which is how the engine charges (a round trip is two fills).
    """
    import yaml

    path = run_yaml or RUN_YAML_PATH
    try:
        raw = yaml.safe_load(path.read_text())
        profile_cfg = raw["profiles"][profile]
    except (KeyError, FileNotFoundError) as exc:
        raise RetrievalError(
            f"cannot load the data contract profile {profile!r} from {path}: "
            f"{exc}") from None
    facts = {
        "profile": profile,
        "dataset": profile_cfg.get("dataset"),
        "split": profile_cfg.get("split"),
        "fill_model": profile_cfg.get("fill_model", "unfavorable"),
        "commission_per_side": profile_cfg.get("commission_per_contract"),
        "action_cap": profile_cfg.get("action_cap", 64),
        "history_window": profile_cfg.get("history_window", 512),
        "n_sessions": n_sessions,
    }
    return DataContract(facts=facts)


# --------------------------------------------------------------------------- #
# the research agent
# --------------------------------------------------------------------------- #

def _same_model(requested: str, served: str) -> bool:
    a = requested.rsplit("/", 1)[-1].lower()
    b = served.rsplit("/", 1)[-1].lower()
    return a.startswith(b) or b.startswith(a)


def _endpoint_url() -> tuple[str, str]:
    base_url = os.environ.get("LITELLM_BASE_URL", "")
    if not base_url:
        raise LLMError("LITELLM_BASE_URL is not set; the driver has no endpoint")
    if not os.environ.get("LITELLM_API_KEY"):
        raise LLMError("LITELLM_API_KEY is not set")
    return base_url, os.environ["LITELLM_API_KEY"]


@dataclass
class ResearchAgent:
    """Reads the brief and the retrieval, writes the hypothesis draft.

    The output is checked against the *hypothesis* lint in a bounded
    repair loop, because once written to `config/hypotheses/` the draft IS the
    proposing agent's brief. Papers may freely use the gate's vocabulary; the
    hypothesis may not carry it into the search. That asymmetry is the whole
    reason the retrieval is not read by `llm.py`.
    """

    model: str
    temperature: float = 0.3
    max_tokens: int = 8_192
    max_repair_attempts: int = 2
    request_timeout_s: float = 240.0
    seed: int | None = None
    base_url: str = ""
    template_path: Path = RESEARCH_TEMPLATE_PATH
    brief: str = ""
    retrieval: RetrievalSet | None = None
    # Facts of the campaign this hypothesis will feed: dataset, split, sizes.
    # Never whether an edge exists, and never anything the correction computes.
    run_facts: str = ""
    data_doc: str = ""
    calls: list[Call] = field(default_factory=list, init=False, repr=False)
    _pending: list[Call] = field(default_factory=list, init=False, repr=False)
    _n: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url, _ = _endpoint_url()
        self._template = self.template_path.read_text()
        if not self.data_doc:
            self.data_doc = DATA_DOC_PATH.read_text()

    def config(self) -> dict:
        """What goes into the campaign record. No secrets, by field."""
        return {
            "implementation": "src.core.loop.research.ResearchAgent",
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "endpoint": self.base_url,
            "prompt_template": str(self.template_path.relative_to(REPO_ROOT)),
            "prompt_template_digest": template_digest(self.template_path),
            "documents_shown": sorted({str(DATA_DOC_PATH.relative_to(REPO_ROOT))}),
            "run_facts": self.run_facts,
            "brief_len": len(self.brief),
            "retrieval": (None if self.retrieval is None else {
                "name": self.retrieval.name,
                "query": self.retrieval.query,
                "max_results": self.retrieval.max_results,
                "n_papers": len(self.retrieval.papers),
                "digest": self.retrieval.digest(),
            }),
            # The discipline, stated rather than folded into a "shows the gate"
            # boolean that has no meaning for an agent that is handed papers.
            "shows_papers_that_may_mention_gate_vocabulary": True,
            "draft_linted_against_gate_vocabulary": True,
            "retries_allowed": False,
            "max_repair_attempts": self.max_repair_attempts,
            "request_timeout_s": self.request_timeout_s,
        }

    def drain_log(self) -> dict:
        """The last draft's calls and what they cost."""
        calls, self._pending = self._pending, []
        return {
            "calls": [c.as_dict() for c in calls],
            "n_calls": len(calls),
            "repair_attempts": sum(1 for c in calls if c.kind == "repair"),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "latency_s": round(sum(c.latency_s for c in calls), 2),
            "blocked": bool(calls) and not calls[-1].ok,
            "models_served": sorted({c.model_served for c in calls
                                     if c.model_served}),
        }

    def preflight(self) -> dict:
        """One cheap call, to find out what the endpoint is actually serving.

        Narrow variant of LLMAgent.preflight for the research model. Raises
        `ModelSubstituted` when the answer is not what will be written in the
        record.
        """
        import requests

        base_url, key = _endpoint_url()
        body = {"model": self.model, "max_tokens": 16, "temperature": 0,
                "messages": [{"role": "user", "content": "reply with: ok"}]}
        resp = requests.post(
            f"{base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.request_timeout_s)
        if resp.status_code != 200:
            raise LLMError(f"the endpoint will not serve {self.model!r}: HTTP "
                           f"{resp.status_code}. {resp.text[:200]}")
        served = str(resp.json().get("model") or "")
        if not served:
            raise ModelSubstituted(
                f"the endpoint answered for {self.model!r} without naming a "
                "model, so substitution cannot be ruled out.")
        if not _same_model(self.model, served):
            raise ModelSubstituted(
                f"asked for {self.model!r}, endpoint answered {served!r}")
        return {"requested": self.model, "served": served}

    def _prompt(self, *, repair: str) -> str:
        return self._template.format(
            brief=self.brief.strip() or "(no research brief supplied)",
            run=self.run_facts.strip() or "(no run facts supplied)",
            retrieval=retrieval_block(self.retrieval),
            data=self.data_doc,
            repair=repair,
        )

    def draft(self) -> str:
        """One draft call, then a bounded lint-repair loop. Returns text only.

        Raises `ResearchDraftRejected` rather than returning something sullied:
        a draft that still mentions the gate or a closed split after the repair
        budget is not a draft, it is a verdict on the papers that a human should
        read, so the exception names the findings.
        """
        self._n += 1
        deadline = (time.monotonic()
                    + self.request_timeout_s * (self.max_repair_attempts + 2))
        try:
            text = self._one(self._prompt(repair=""), "draft")
        except ModelSubstituted:
            raise                       # a harness failure; never repaired
        except LLMError as exc:
            raise ResearchDraftRejected(f"the draft call failed: {exc}") from None

        for attempt in range(self.max_repair_attempts + 1):
            found = findings(text)
            if found:
                if attempt == self.max_repair_attempts:
                    raise ResearchDraftRejected(
                        f"{self.max_repair_attempts} repair attempts exhausted; "
                        "the draft still tells the proposing agent about the gate "
                        "or a closed split:\n  " + "\n  ".join(found)
                        + "\n\nThe papers may say it; the hypothesis may not.")
                problems = list(found)
                grounding_only = False
            else:
                # Not gate vocabulary, so the draft is clean on that axis. It
                # still has to be about the papers we froze: a quotation the set
                # does not contain, or an id outside it, is a hypothesis about a
                # source nobody read. Same loop, same refusal, second question.
                # A missing set is a wiring bug and `check_retrieval` refuses it
                # rather than waving the draft through -- a draft checked against
                # nothing passes for any claim.
                try:
                    check_retrieval(text, self.retrieval, source="the draft")
                except Ungrounded as exc:
                    if attempt == self.max_repair_attempts:
                        raise ResearchDraftRejected(
                            f"{self.max_repair_attempts} repair attempts "
                            "exhausted; the draft is not about the frozen set:\n  "
                            f"{exc}\n\nA hypothesis may lean only on the papers "
                            "that were frozen under this name.")
                    problems = [str(exc)]
                    grounding_only = True
                else:
                    return text
            if time.monotonic() > deadline:
                raise ResearchDraftRejected(
                    "research wall clock exceeded while repairing:\n  "
                    + "\n  ".join(problems[:3]))
            if grounding_only:
                repair = (
                    "Your previous draft cannot be used. It attributes words or "
                    "a paper to the frozen retrieval set that the set does not "
                    "contain. Copy the papers' wording exactly where you quote "
                    "them, and cite only the ids in the set. You may draw your "
                    "own conclusions in your own words; what you may not do is "
                    "put quotation marks around a sentence no paper wrote.\n\n"
                    "Findings:\n\n" + "\n".join(f"  - {p}" for p in problems) +
                    "\n\nPrevious draft:\n\n" + text + "\n\n"
                    "Rewrite the whole hypothesis now:\n")
            else:
                repair = (
                    "Your previous draft cannot be used. It contains vocabulary "
                    "that must never reach the proposing agent: nothing about "
                    "thresholds, p-values, significance, error rates or "
                    "rejection, validation or holdout, out-of-sample behaviour, "
                    "corrections, or a number of trials. The papers you were "
                    "given may use all of it; your hypothesis must not carry it "
                    "into the search. Restate the finding about the MARKET, not "
                    "about how an answer would be judged.\n\n"
                    "Findings:\n\n" + "\n".join(f"  - {p}" for p in problems) +
                    "\n\nPrevious draft:\n\n" + text + "\n\n"
                    "Rewrite the whole hypothesis now:\n")
            try:
                text = self._one(self._prompt(repair=repair), "repair")
            except ModelSubstituted:
                raise
            except LLMError as exc:
                raise ResearchDraftRejected(f"a repair call failed: {exc}") from None
        raise ResearchDraftRejected("unreachable")   # pragma: no cover

    def _one(self, prompt: str, kind: str) -> str:
        """One call. One. A failure here aborts the draft, never retries."""
        import requests

        system, user = prompt.split("## USER", 1)
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system.split("## SYSTEM", 1)[-1].strip()},
                {"role": "user", "content": user.strip()},
            ],
        }
        if self.seed is not None:
            body["seed"] = self.seed
        base_url, key = _endpoint_url()

        t0 = time.monotonic()
        try:
            resp = requests.post(
                f"{base_url}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=self.request_timeout_s)
        except Exception as exc:
            self._pending.append(Call(kind, False, time.monotonic() - t0,
                                      error=f"{type(exc).__name__}: {exc}"[:300]))
            raise LLMError(f"{type(exc).__name__}: {exc}") from None
        latency = time.monotonic() - t0

        if resp.status_code != 200:
            self._pending.append(Call(kind, False, latency,
                                      error=f"HTTP {resp.status_code}"))
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
            usage = payload.get("usage") or {}
            text = payload["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            self._pending.append(Call(kind, False, latency,
                                      error=f"unparseable envelope: {exc}"[:300]))
            raise LLMError(f"unparseable response envelope: {exc}") from None

        served = str(payload.get("model") or "")
        if served and not _same_model(self.model, served):
            raise ModelSubstituted(
                f"asked the endpoint for {self.model!r} and it answered with "
                f"{served!r}. The record will say {self.model!r}, so continuing "
                "would write a record that is false.")
        call = Call(kind, True, latency,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    model_served=served)
        self._pending.append(call)
        text = text.strip()
        if not text:
            self._pending.append(Call(kind, False, latency,
                                      prompt_tokens=call.prompt_tokens,
                                      completion_tokens=call.completion_tokens,
                                      error="empty/blank reply",
                                      model_served=served))
            raise LLMError("empty reply: the research agent returned nothing")
        return text


# --------------------------------------------------------------------------- #
# the corpus agents: screen and distil
#
# The pattern is ResearchAgent's, deliberately: one call is one call, recorded,
# never retried, and a repair loop is bounded. The difference is that these
# agents' text is *parsed as data* on the way out -- a JSON verdict for the
# screen, a Hypothesis-shaped page for the distil -- so a parse failure or a
# lint hit is a model failure that the repair loop names, never something to
# shrug through.
# --------------------------------------------------------------------------- #

class ScreenRejected(RuntimeError):
    """A screen verdict could not be parsed out of the screen agent in budget."""


class DistillRejected(RuntimeError):
    """A distillation could not be made clean and on-budget."""


def _extract_json(text: str) -> dict | None:
    """The first balanced `{...}` object in `text`, or None.

    The screen agent is told to emit nothing but a JSON object. A parse failure
    here is a *model* failure the screen's bounded repair loop is there to fix.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _estimate_tokens(text: str) -> int:
    """Rough token count for the length budget: about four chars per token."""
    return max(1, len(text) // 4)


@dataclass(frozen=True, slots=True)
class ScreenVerdict:
    """One paper's screen: testable, and how. `adaptation` binds when it must."""

    feasibility: str            # direct | adaptable | infeasible
    reason: str
    adaptation: str = ""
    quote: str = ""

    @property
    def testable(self) -> bool:
        return self.feasibility in ("direct", "adaptable")

    def as_dict(self) -> dict:
        return {
            "feasibility": self.feasibility,
            "reason": self.reason,
            "adaptation": self.adaptation,
            "quote": self.quote,
        }


@dataclass
class _ChatAgent:
    """Shared plumbing for the screen and distil agents.

    The one-call one-record discipline is copied from `ResearchAgent`, because
    it is the strictest version of the discipline there is: a screen verdict is
    right or wrong and a repair loop is bounded either way.
    """

    model: str
    temperature: float = 0.2
    max_tokens: int = 4_096
    max_repair_attempts: int = 1
    request_timeout_s: float = 240.0
    seed: int | None = None
    base_url: str = ""
    template_path: Path = SCREEN_TEMPLATE_PATH
    calls: list[Call] = field(default_factory=list, init=False, repr=False)
    _pending: list[Call] = field(default_factory=list, init=False, repr=False)
    _n: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url, _ = _endpoint_url()
        self._template = self.template_path.read_text()

    def config(self) -> dict:
        """What goes into the registry event beside the verdict. No secrets."""
        return {
            "implementation": f"{type(self).__module__}.{type(self).__qualname__}",
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "endpoint": self.base_url,
            "prompt_template": str(self.template_path.relative_to(REPO_ROOT)),
            "prompt_template_digest": template_digest(self.template_path),
            "retries_allowed": False,
            "max_repair_attempts": self.max_repair_attempts,
            "request_timeout_s": self.request_timeout_s,
        }

    def drain_log(self) -> dict:
        calls, self._pending = self._pending, []
        return {
            "calls": [c.as_dict() for c in calls],
            "n_calls": len(calls),
            "repair_attempts": sum(1 for c in calls if c.kind == "repair"),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "latency_s": round(sum(c.latency_s for c in calls), 2),
            "blocked": bool(calls) and not calls[-1].ok,
            "models_served": sorted({c.model_served for c in calls
                                     if c.model_served}),
        }

    def preflight(self) -> dict:
        """One cheap call, to find out what the endpoint is actually serving."""
        import requests

        base_url, key = _endpoint_url()
        body = {"model": self.model, "max_tokens": 16, "temperature": 0,
                "messages": [{"role": "user", "content": "reply with: ok"}]}
        resp = requests.post(
            f"{base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.request_timeout_s)
        if resp.status_code != 200:
            raise LLMError(f"the endpoint will not serve {self.model!r}: HTTP "
                           f"{resp.status_code}. {resp.text[:200]}")
        served = str(resp.json().get("model") or "")
        if not served:
            raise ModelSubstituted(
                f"the endpoint answered for {self.model!r} without naming a "
                "model, so substitution cannot be ruled out.")
        if not _same_model(self.model, served):
            raise ModelSubstituted(
                f"asked for {self.model!r}, endpoint answered {served!r}")
        return {"requested": self.model, "served": served}

    def _one(self, prompt: str, kind: str) -> str:
        """One call. One. A failure here aborts the step, never retries."""
        import requests

        system, user = prompt.split("## USER", 1)
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system.split("## SYSTEM", 1)[-1].strip()},
                {"role": "user", "content": user.strip()},
            ],
        }
        if self.seed is not None:
            body["seed"] = self.seed
        base_url, key = _endpoint_url()

        t0 = time.monotonic()
        try:
            resp = requests.post(
                f"{base_url}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=self.request_timeout_s)
        except Exception as exc:
            self._pending.append(Call(kind, False, time.monotonic() - t0,
                                      error=f"{type(exc).__name__}: {exc}"[:300]))
            raise LLMError(f"{type(exc).__name__}: {exc}") from None
        latency = time.monotonic() - t0

        if resp.status_code != 200:
            self._pending.append(Call(kind, False, latency,
                                      error=f"HTTP {resp.status_code}"))
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
            usage = payload.get("usage") or {}
            text = payload["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            self._pending.append(Call(kind, False, latency,
                                      error=f"unparseable envelope: {exc}"[:300]))
            raise LLMError(f"unparseable response envelope: {exc}") from None

        served = str(payload.get("model") or "")
        if served and not _same_model(self.model, served):
            raise ModelSubstituted(
                f"asked the endpoint for {self.model!r} and it answered with "
                f"{served!r}. The record will say {self.model!r}, so continuing "
                "would write a record that is false.")
        call = Call(kind, True, latency,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    model_served=served)
        self._pending.append(call)
        text = text.strip()
        if not text:
            self._pending.append(Call(kind, False, latency,
                                      prompt_tokens=call.prompt_tokens,
                                      completion_tokens=call.completion_tokens,
                                      error="empty/blank reply",
                                      model_served=served))
            raise LLMError("empty reply: the agent returned nothing")
        return text


@dataclass
class ScreenAgent(_ChatAgent):
    """One paper under one contract: direct, adaptable, or infeasible.

    The output is a strict JSON verdict; a model that wraps it in prose gets a
    bounded repair call naming exactly what it must change, and a model that
    still cannot produce it raises `ScreenRejected` rather than a lower
    standard being silently accepted.
    """

    paper: Paper | None = None
    contract: str = ""

    def config(self) -> dict:
        cfg = super().config()
        cfg["paper"] = None if self.paper is None else self.paper.arxiv_id
        cfg["contract_len"] = len(self.contract)
        return cfg

    def _prompt(self, *, repair: str) -> str:
        paper = self.paper
        return self._template.format(
            title=paper.title if paper is not None else "(no paper)",
            abstract=paper.abstract if paper is not None else "(no paper)",
            contract=self.contract.strip() or "(no contract supplied)",
            repair=repair,
        )

    def screen(self) -> ScreenVerdict:
        """Verdict this paper, with a bounded JSON repair loop."""
        self._n += 1
        deadline = (time.monotonic()
                    + self.request_timeout_s * (self.max_repair_attempts + 2))
        try:
            text = self._one(self._prompt(repair=""), "draft")
        except ModelSubstituted:
            raise
        except LLMError as exc:
            raise ScreenRejected(f"the screen call failed: {exc}") from None

        for attempt in range(self.max_repair_attempts + 1):
            verdict, problems = self._parse(text)
            if not problems:
                return verdict       # type: ignore[return-value]
            if attempt == self.max_repair_attempts:
                raise ScreenRejected(
                    f"{self.max_repair_attempts} repair attempts exhausted; "
                    "the screen never produced a usable verdict:\n  "
                    + "\n  ".join(problems)
                    + "\n\nA verdict that cannot be parsed is not a verdict; "
                    "re-screen under this key is a decision by hand.")
            if time.monotonic() > deadline:
                raise ScreenRejected(
                    "screen wall clock exceeded while repairing:\n  "
                    + "\n  ".join(problems[:3]))
            repair = (
                "Your previous answer was not usable: it was not exactly one "
                "JSON object, or it did not validate. Reply with ONLY the JSON "
                "object, nothing before or after it:\n\n"
                '    {"feasibility": "direct|adaptable|infeasible", '
                '"reason": "...", "adaptation": "", "quote": "..."}\n\n'
                "Problems:\n\n" + "\n".join(f"  - {p}" for p in problems) +
                "\n\nPrevious answer:\n\n" + text + "\n\n"
                "Now reply with the single corrected JSON object:\n")
            try:
                text = self._one(self._prompt(repair=repair), "repair")
            except ModelSubstituted:
                raise
            except LLMError as exc:
                raise ScreenRejected(f"a repair call failed: {exc}") from None
        raise ScreenRejected("unreachable")   # pragma: no cover

    def _parse(self, text: str) -> tuple[ScreenVerdict | None, list[str]]:
        """The verdict out of the text, plus every reason it was refused."""
        obj = _extract_json(text)
        if obj is None:
            return None, ["no JSON object found; reply with one object only"]
        feasibility = str(obj.get("feasibility") or "")
        reason = str(obj.get("reason") or "").strip()
        adaptation = str(obj.get("adaptation") or "").strip()
        quote = str(obj.get("quote") or "").strip()
        problems: list[str] = []
        if feasibility not in ("direct", "adaptable", "infeasible"):
            problems.append(
                f"feasibility must be direct|adaptable|infeasible, got "
                f"{feasibility!r}")
        if not reason:
            problems.append("reason is empty")
        if feasibility == "adaptable" and not adaptation:
            problems.append("adaptable requires a non-empty adaptation")
        if self.paper is not None and not problems:
            # The prompt asks for "the paper's own sentence"; nothing compared it
            # to the paper. A screen keyed to a quotation the paper does not
            # contain is keyed to nothing. Enforced here rather than after the
            # loop so the repair call names it like any other unusable field.
            try:
                check_quote(quote, self.paper, source="the screen")
            except Ungrounded as exc:
                problems.append(str(exc))
        if problems:
            return None, problems
        return (ScreenVerdict(feasibility=feasibility, reason=reason,
                              adaptation=adaptation, quote=quote), [])


@dataclass
class DistilAgent(_ChatAgent):
    """One survivor paper to one page: linted and inside the token budget.

    The distil output is a `config/hypotheses/`-shaped page, so it pays the
    same debt as a research draft -- the gate-vocabulary lint -- with the
    corpus's own addition on top: the length budget is enforced on the same
    repair loop, because a one-page distillation is a specific genre, not a
    vibe.
    """

    model: str
    temperature: float = 0.3
    max_tokens: int = 8_192
    max_repair_attempts: int = 2
    request_timeout_s: float = 240.0
    seed: int | None = None
    base_url: str = ""
    template_path: Path = DISTIL_TEMPLATE_PATH
    brief: str = ""
    run_facts: str = ""
    data_doc: str = ""
    paper: Paper | None = None
    budget_floor: int = DISTIL_MIN_TOKENS
    budget_ceiling: int = DISTIL_MAX_TOKENS
    # KB-2 as this desk spells it: what the corpus already ruled out, rendered
    # for the prompt. A *rejection* is not a campaign summary and not a paper, so
    # it is not in the retriever and not in the frozen set; it arrives as prose
    # and the page is checked against it afterwards. See `rejection.py`.
    prior_rejections: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.data_doc:
            self.data_doc = DATA_DOC_PATH.read_text()

    def config(self) -> dict:
        cfg = super().config()
        cfg["documents_shown"] = sorted(
            {str(DATA_DOC_PATH.relative_to(REPO_ROOT))})
        cfg["run_facts"] = self.run_facts
        cfg["brief_len"] = len(self.brief)
        cfg["paper"] = None if self.paper is None else self.paper.arxiv_id
        cfg["paper_fulltext_src"] = (
            None if self.paper is None else (self.paper.fulltext_src or None))
        cfg["budget_floor_tokens"] = self.budget_floor
        cfg["budget_ceiling_tokens"] = self.budget_ceiling
        cfg["output_linted_against_gate_vocabulary"] = True
        cfg["output_length_enforced_tokens"] = True
        return cfg

    def _prompt(self, *, repair: str) -> str:
        return self._template.format(
            brief=self.brief.strip() or "(no research brief supplied)",
            run=self.run_facts.strip() or "(no run facts supplied)",
            paper=paper_block(self.paper),
            prior=self.prior_rejections.strip() or "(nothing ruled out yet)",
            data=self.data_doc,
            repair=repair,
        )

    def distil(self) -> str:
        """One page, lint-clean and inside the budget, or `DistillRejected`."""
        self._n += 1
        deadline = (time.monotonic()
                    + self.request_timeout_s * (self.max_repair_attempts + 2))
        try:
            text = self._one(self._prompt(repair=""), "draft")
        except ModelSubstituted:
            raise
        except LLMError as exc:
            raise DistillRejected(f"the distillation call failed: {exc}") from None

        for attempt in range(self.max_repair_attempts + 1):
            problems = self._problems(text)
            if not problems:
                return text
            if attempt == self.max_repair_attempts:
                raise DistillRejected(
                    f"{self.max_repair_attempts} repair attempts exhausted; "
                    "the distillation still cannot be used:\n  "
                    + "\n  ".join(problems)
                    + "\n\nA one-page distillation that fails the lint or the "
                    "budget is not a hypothesis; it is a verdict on the paper "
                    "that a human should read.")
            if time.monotonic() > deadline:
                raise DistillRejected(
                    "distillation wall clock exceeded while repairing:\n  "
                    + "\n  ".join(problems[:3]))
            repair = (
                "Your previous draft cannot be used. It contains vocabulary "
                "that must never reach the proposing agent: nothing about "
                "thresholds, p-values, significance, error rates or rejection, "
                "validation or holdout, out-of-sample behaviour, corrections, "
                "or a number of trials -- and it must sit inside the length "
                f"budget of {self.budget_floor}..{self.budget_ceiling} tokens "
                f"(roughly {self.budget_floor * 4}..{self.budget_ceiling * 4} "
                "characters). The paper you were given may use all of it; your "
                "hypothesis must not carry it into the search.\n\n"
                "Problems:\n\n" + "\n".join(f"  - {p}" for p in problems) +
                "\n\nPrevious draft:\n\n" + text + "\n\n"
                "Rewrite the whole hypothesis now:\n")
            try:
                text = self._one(self._prompt(repair=repair), "repair")
            except ModelSubstituted:
                raise
            except LLMError as exc:
                raise DistillRejected(f"a repair call failed: {exc}") from None
        raise DistillRejected("unreachable")   # pragma: no cover

    def _problems(self, text: str) -> list[str]:
        """Every reason this text cannot be the finished page."""
        problems = [f"gate vocabulary: {f}" for f in findings(text)]
        # The page is written from one paper, so the paper is the whole
        # groundable world: a quotation it does not contain, or an id outside
        # it, is the page inventing a source. Checked on the same repair loop as
        # the lint, so the model is told what to fix rather than the harness
        # quietly editing its answer -- `--autofix` is a second author.
        if self.paper is not None:
            try:
                check_text(text, [self.paper], source="the distilled page")
            except Ungrounded as exc:
                problems.append(f"not grounded in {self.paper.arxiv_id}: {exc}")
        est = _estimate_tokens(text)
        if est < self.budget_floor:
            problems.append(
                f"under the length budget: ~{est} tokens, floor is "
                f"{self.budget_floor} (the page is expected, not a sentence)")
        elif est > self.budget_ceiling:
            problems.append(
                f"over the length budget: ~{est} tokens, ceiling is "
                f"{self.budget_ceiling}")
        return problems


# --------------------------------------------------------------------------- #
# the re-rank: negative filter first, relevance second, both model-free
# --------------------------------------------------------------------------- #

# Falsified hypothesis families from the dataset doc, condensed to the
# plain-text signals a title/abstract scan can match. The scan is crude on
# purpose -- it demotes, never silently drops, and the reason carries the
# citation so a false demotion is visible to the next human.
DEMOTION_PATH = "docs/datasets/nq-es-qqq.md"
DEMOTION_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GEX node attraction",
     ("gamma exposure", "gex ", " gex", "dealer gamma", "node attraction",
      "zero-day", "0dte", "gamma imbalance")),
    ("sequence models at the current sample size",
     ("sequence model", "gru", "lstm", "recurrent", "rnn", "1d-cnn",
      "1d cnn", "convolutional", "deep learning", "transformer")),
    ("two-stage confirmation architectures",
     ("two-stage", "two stage", "second-stage", "second stage",
      "confidence gate", "gating architecture", "confirmation stage")),
)


@dataclass(frozen=True, slots=True)
class RerankVerdict:
    """The re-rank's answer for one screened paper.

    `demoted` papers are excluded from distillation with a reason that names
    the falsified family and cites it -- demotion is visible and citable, never
    silent. `relevance` is the open-questions token overlap, or None when no
    open questions are supplied.
    """

    demoted: bool
    demoted_reason: str = ""
    relevance: int | None = None

    def as_dict(self) -> dict:
        return {
            "demoted": self.demoted,
            "demoted_reason": self.demoted_reason,
            "relevance": self.relevance,
        }


def _tokens(text: str) -> set[str]:
    """Lowercased content words, for the relevance overlap."""
    return set(re.findall(r"[a-z]{2,}", text.lower()))


def open_questions(path: Path | None = None) -> str:
    """The human-maintained list of what the corpus should watch for.

    Returns "" when the file does not exist -- it is an optional positive
    input, and the re-rank's negative filter works without one. Never derived
    from outcomes; outcomes are recorded, not consulted.
    """
    p = path or OPEN_QUESTIONS_PATH
    if not p.is_file():
        return ""
    return p.read_text().strip()


def rerank(paper: Paper, *, open_questions_text: str = "",
           prompt_text: str = "") -> RerankVerdict:
    """Demote falsified families; score relevance against a human positive signal.

    Deterministic and model-free. The negative filter is a keyword scan of the
    title and abstract against families already falsified on this data
    (`docs/datasets/nq-es-qqq.md`); the positive signal is the prompt when one
    is given, otherwise the human-maintained open-questions list. Both ends are
    facts a human maintains -- never an outcome or a model opinion.
    """
    hay = f"{paper.title} {paper.abstract}".lower()
    reason = ""
    for family, keywords in DEMOTION_FAMILIES:
        if any(kw in hay for kw in keywords):
            reason = (f"matches falsified family {family!r} on this data "
                      f"(plain-text keyword scan): {DEMOTION_PATH}")
            break
    positive = prompt_text or open_questions_text
    relevance = None
    if positive.strip():
        relevance = len(_tokens(positive) & _tokens(hay))
    return RerankVerdict(demoted=bool(reason), demoted_reason=reason,
                         relevance=relevance)