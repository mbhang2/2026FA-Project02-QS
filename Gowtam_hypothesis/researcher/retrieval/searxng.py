"""SearXNG metasearch backend (optional, opt-in).

SearXNG is a self-hosted metasearch engine that aggregates arXiv, CrossRef,
Google Scholar, and general web results behind one JSON API. It is the only
backend that reaches the *web* (not just the academic graph), so it can surface
a PDF/HTML the other two cannot.

It is deliberately opt-in and non-authoritative:

- Disabled unless ``SEARXNG_BASE_URL`` is set, so the researcher has no hard
  dependency on a local service.
- When configured but unreachable, ``search`` raises ``SearxngError`` and
  ``retrieve`` swallows it, carrying on with arXiv + Semantic Scholar alone.
- Its noise is bounded client-side: results with an empty title or URL are
  dropped, and the same de-dup + query relaxation the other backends get applies.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

_TIMEOUT = 5.0  # a down SearXNG must not stall the run

DENYLIST_DOMAINS = {"reddit.com", "twitter.com", "x.com", "quora.com", "pastebin.com", "4chan.org"}
ALLOWLIST_DOMAINS = set()

INJECTION_PHRASES = re.compile(
    r"\b(ignore previous|system prompt|you are a|forget everything|disregard|new instructions)\b", 
    re.IGNORECASE
)

def _domain_allowed(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return False
    if any(denied in domain for denied in DENYLIST_DOMAINS):
        return False
    if ALLOWLIST_DOMAINS and not any(allowed in domain for allowed in ALLOWLIST_DOMAINS):
        return False
    return True

def _harden_content(content: str) -> str:
    text = content[:1500]  # Cap characters per source
    text = INJECTION_PHRASES.sub("[REDACTED]", text)
    return text


class SearxngError(RuntimeError):
    """The SearXNG instance is not configured or did not answer usable JSON."""


@dataclass(frozen=True, slots=True)
class SearxngResult:
    title: str
    url: str
    content: str = ""
    content_hash: str = ""


def search(query: str, *, limit: int = 6, academic: bool = False) -> list[SearxngResult]:
    """Search SearXNG; raises ``SearxngError`` when unconfigured or unreachable."""
    base = os.environ.get("SEARXNG_BASE_URL", "").rstrip("/")
    if not base:
        raise SearxngError("SEARXNG_BASE_URL not set; SearXNG backend disabled")

    params = {
        "q": query,
        "format": "json",
        "categories": "science" if academic else "general",
    }
    try:
        resp = requests.get(f"{base}/search", params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise SearxngError(f"request failed: {exc}") from None
    if resp.status_code != 200:
        raise SearxngError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SearxngError(f"unparseable JSON: {exc}") from None

    out: list[SearxngResult] = []
    for item in payload.get("results", []):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        content = (item.get("content") or "").strip()
        if not title or not url or not _domain_allowed(url):
            continue
        if INJECTION_PHRASES.search(title):
            continue
            
        content = _harden_content(content)
        chash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        
        out.append(SearxngResult(title=title, url=url, content=content, content_hash=chash))
        if len(out) >= limit:
            break
    return out
