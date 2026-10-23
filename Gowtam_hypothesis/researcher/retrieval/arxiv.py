"""Free scholarly retrieval: arXiv's Atom API.

No key, no third-party library beyond `requests` (already a workstream dep --
`urllib` is rejected by arXiv's edge with HTTP 406 for reasons unrelated to the
query, so `requests` is the reliable client). Atom *parsing* stays on stdlib
`xml.etree` so the feed shape is unit-tested offline from a captured fixture.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"

ENDPOINT = "https://export.arxiv.org/api/query"
_HEADERS = {"User-Agent": "QuantStrat-researcher/0.1 (research; arxiv)"}

# The category scope. `cat:q-fin.*` in the query only *biases* arXiv's ranking
# (it leaks cross-listed physics/cs papers), so the real filter is this prefix
# check on each entry's `primary_category` term, applied client-side.
_CATEGORY_PREFIX = "q-fin."


class ArxivError(RuntimeError):
    """The arXiv endpoint did not answer usable Atom."""


@dataclass(frozen=True, slots=True)
class ArxivEntry:
    entry_id: str
    title: str
    summary: str
    url: str
    published: str = ""
    primary_category: str = ""


def _text(elem, tag: str) -> str:
    child = elem.find(f"{_ATOM}{tag}")
    if child is None or child.text is None:
        return ""
    return " ".join(child.text.split())


def parse_atom(raw: bytes | str) -> list[ArxivEntry]:
    """Parse an Atom feed into entries. Raises on a non-feed body."""
    root = ET.fromstring(raw)
    if root.tag != f"{_ATOM}feed":
        raise ArxivError(f"not an Atom feed (root {root.tag!r})")
    out: list[ArxivEntry] = []
    for entry in root.findall(f"{_ATOM}entry"):
        entry_id = _text(entry, "id")
        cat = entry.find(f"{_ARXIV}primary_category")
        out.append(ArxivEntry(
            entry_id=entry_id,
            title=_text(entry, "title"),
            summary=_text(entry, "summary"),
            url=entry_id,  # arXiv entry ids are the canonical target URL
            published=_text(entry, "published"),
            primary_category=cat.get("term") if cat is not None else "",
        ))
    return out


from .cache import get_cache, set_cache
from .throttle import throttle


def _get(params: dict[str, str | int], max_retries: int = 2) -> bytes:
    for attempt in range(max_retries + 1):
        throttle("arxiv", min_interval_seconds=3.0)
        try:
            resp = requests.get(ENDPOINT, params=params, headers=_HEADERS, timeout=60)
        except requests.RequestException as exc:
            if attempt < max_retries:
                import time
                time.sleep(2 * (attempt + 1))
                continue
            raise ArxivError(f"request failed: {exc}") from None
        if resp.status_code == 429:
            if attempt < max_retries:
                import time
                time.sleep(4 * (attempt + 1))
                continue
            raise ArxivError(f"HTTP 429: rate limited by arXiv")
        if resp.status_code != 200:
            raise ArxivError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content
    raise ArxivError("failed after retries")


def search(query: str, *, max_results: int = 10) -> list[ArxivEntry]:
    """Search the quantitative-finance categories only.

    Overloaded words ("persistence", "oscillation", "reversion", "equity") bleed
    into physics/math unless scope is enforced. `cat:q-fin.*` biases the ranking,
    and a client-side filter on `primary_category` drops every entry that is not
    genuinely quantitative finance -- deterministic, not arXiv's fuzzy AND.
    """
    cached = get_cache("arxiv", f"{query}:{max_results}")
    if cached is not None:
        return [ArxivEntry(**item) for item in cached]

    raw = _get({"search_query": f"all:{query} AND cat:q-fin.*", "start": 0,
                "max_results": max_results})
    entries = parse_atom(raw)
    res = [e for e in entries if e.primary_category.startswith(_CATEGORY_PREFIX)]
    set_cache("arxiv", f"{query}:{max_results}", [
        {"entry_id": e.entry_id, "title": e.title, "summary": e.summary,
         "url": e.url, "published": e.published, "primary_category": e.primary_category}
        for e in res
    ])
    return res


def search_by_id(arxiv_id: str) -> ArxivEntry:
    """Fetch one entry by arXiv id (e.g. '2401.01234'). No category filter."""
    entries = parse_atom(_get({"id_list": arxiv_id}))
    if not entries:
        raise ArxivError(f"no entry for id {arxiv_id!r}")
    return entries[0]
