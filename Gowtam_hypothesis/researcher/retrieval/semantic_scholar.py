"""Semantic Scholar Graph API search (free, no key; optional key for rate).

REST + JSON via `requests`. The unauthenticated tier is rate-limited, so this
module backs off on HTTP 429 rather than failing the run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,abstract,url,externalIds,year"


class SemanticScholarError(RuntimeError):
    """The Graph API did not answer a usable payload."""


@dataclass(frozen=True, slots=True)
class Paper:
    title: str
    abstract: str
    url: str
    year: int | None = None


def _from_item(item: dict) -> Paper:
    return Paper(
        title=(item.get("title") or "").strip(),
        abstract=(item.get("abstract") or "").strip(),
        url=(item.get("url") or "").strip(),
        year=item.get("year"),
    )


def search(query: str, *, limit: int = 10, max_retries: int = 1) -> list[Paper]:
    """Search with a *fast* retry. A rate-limited backend must not stall the run.

    The unauthenticated tier rate-limits aggressively (HTTP 429); a long
    exponential backoff adds seconds of dead time the loop cannot afford, and
    `retrieve`'s query relaxation already provides coverage resilience. So this
    backs off once, briefly, then raises -- `retrieve` swallows it and carries on
    with arXiv alone.
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                ENDPOINT,
                params={"query": query, "limit": limit, "fields": FIELDS},
                timeout=60,
            )
        except requests.RequestException as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(1)
                continue
            raise SemanticScholarError(f"request failed: {exc}") from None

        if resp.status_code == 429:
            last_err = SemanticScholarError("rate limited (429)")
            if attempt < max_retries:
                time.sleep(1)
                continue
            raise SemanticScholarError("rate limited (429)")
        if resp.status_code != 200:
            raise SemanticScholarError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SemanticScholarError(f"unparseable JSON: {exc}") from None
        return [_from_item(it) for it in payload.get("data", []) if it]

    raise SemanticScholarError(f"no answer: {last_err}")
