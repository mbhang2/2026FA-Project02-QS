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

from .hypothesis import findings
from .llm import Call, LLMError, ModelSubstituted, template_digest

from .._roots import repo_root

REPO_ROOT = repo_root()
RESEARCH_TEMPLATE_PATH = REPO_ROOT / "config" / "prompts" / "research-agent.md"
DATA_DOC_PATH = REPO_ROOT / "docs" / "agent" / "data.md"

ARXIV_API = "https://export.arxiv.org/api/query"
AR5IV_BASE = "https://ar5iv.labs.arxiv.org/html"
_ATOM = "http://www.w3.org/2005/Atom"
_OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
_ARXIV = "http://arxiv.org/schemas/atom"

DEFAULT_FULLTEXT_CHARS = 16_000


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


def _parse_atom(body: str) -> list[Paper]:
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
    if not out:
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
            if not found:
                return text
            if attempt == self.max_repair_attempts:
                raise ResearchDraftRejected(
                    f"{self.max_repair_attempts} repair attempts exhausted; the "
                    "draft still tells the proposing agent about the gate or a "
                    "closed split:\n  " + "\n  ".join(found)
                    + "\n\nThe papers may say it; the hypothesis may not." )
            if time.monotonic() > deadline:
                raise ResearchDraftRejected(
                    "research wall clock exceeded while repairing:\n  "
                    + "\n  ".join(found[:3]))
            repair = (
                "Your previous draft cannot be used. It contains vocabulary "
                "that must never reach the proposing agent: nothing about "
                "thresholds, p-values, significance, error rates or rejection, "
                "validation or holdout, out-of-sample behaviour, corrections, "
                "or a number of trials. The papers you were given may use all "
                "of it; your hypothesis must not carry it into the search. "
                "Restate the finding about the MARKET, not about how an answer "
                "would be judged.\n\n"
                "Findings:\n\n" + "\n".join(f"  - {f}" for f in found) +
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