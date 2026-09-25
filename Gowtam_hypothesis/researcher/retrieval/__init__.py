"""Backend orchestration, de-duplication, and source binding.

The retrieval layer is the *only* place the researcher touches the network, and
it is deterministic Python -- no model, no prompt, so its behaviour is fully
auditable. The generation passes only ever see the `SourceClip`s this layer
produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from . import arxiv, semantic_scholar
from .fetch import Extracted, FetchError, fetch_and_extract


@dataclass(frozen=True, slots=True)
class SourceClip:
    """One source, normalized and bound: the unit a claim cites."""

    title: str
    url: str
    summary: str
    backend: str  # "arxiv" | "semantic_scholar"


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _norm_url(url: str) -> str:
    return re.sub(r"https?://", "", url).rstrip("/").lower()


_STOP = {"the", "a", "an", "in", "of", "for", "on", "and", "or", "within",
         "during", "across", "using", "via", "with", "to", "from", "at", "by"}


def _relax(query: str) -> str:
    """Drop the most constraining keyword, floor of one. Broadens a thin query."""
    words = [w for w in query.split() if w.lower() not in _STOP]
    if len(words) <= 2:
        return " ".join(words[:1])
    return " ".join(words[:-1])


def _search_backends(query: str, per_backend: int) -> list[SourceClip]:
    """One query across both backends, no cross-query de-dup."""
    clips: list[SourceClip] = []
    try:
        for e in arxiv.search(query, max_results=per_backend):
            clips.append(SourceClip(title=e.title, url=e.url, summary=e.summary,
                                    backend="arxiv"))
    except arxiv.ArxivError:
        pass
    try:
        for p in semantic_scholar.search(query, limit=per_backend):
            clips.append(SourceClip(title=p.title, url=p.url, summary=p.abstract,
                                    backend="semantic_scholar"))
    except semantic_scholar.SemanticScholarError:
        pass
    return clips


def _dedup(clips: list[SourceClip]) -> list[SourceClip]:
    out: list[SourceClip] = []
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    for c in clips:
        if not c.title and not c.url:
            continue
        t, u = _norm_title(c.title), _norm_url(c.url)
        if t and t in seen_titles:
            continue
        if u and u in seen_urls:
            continue
        seen_titles.add(t)
        seen_urls.add(u)
        out.append(c)
    return out


def retrieve(query: str, *, per_backend: int = 8, fetch_fulltext: bool = False,
             min_sources: int = 2) -> list[SourceClip]:
    """Query arxiv + semantic scholar with query relaxation, de-dup, return clips.

    Relaxation handles the thin-corpus case: a family phrased too precisely for
    the quantitative-finance literature ("intraday momentum in equity index
    futures") may return nothing, so each round drops the most constraining word
    until some sources appear. A failed backend is not a failed retrieval -- it
    narrows coverage.
    """
    clips = _search_backends(query, per_backend)
    relaxed = query
    while len(clips) < min_sources:
        nxt = _relax(relaxed)
        if nxt == relaxed or not nxt:
            break
        relaxed = nxt
        clips = _dedup(clips + _search_backends(relaxed, per_backend))

    clips = _dedup(clips)

    if fetch_fulltext:
        clips = [_with_fulltext(c) for c in clips]

    return clips


def _with_fulltext(clip: SourceClip) -> SourceClip:
    """Optionally enrich an abstract-only clip with fetched body text.

    Fetch failures are swallowed by design: a clip that stays abstract-only is
    still usable; fetching is best-effort enrichment, never a gate.
    """
    if not clip.url:
        return clip
    try:
        ex: Extracted = fetch_and_extract(clip.url)
    except (FetchError, requests.RequestException):
        return clip
    summary = ex.raw_text[:4000] if ex.raw_text else clip.summary
    return SourceClip(title=ex.title or clip.title, url=clip.url,
                      summary=summary, backend=clip.backend)
