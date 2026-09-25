"""Fetch a URL and extract readable text, stdlib only.

HTML→text is a deliberate, *lossy* extraction: strip scripts/styles, keep title
and paragraph-ish text. It is not a reader; it is enough for the researcher to
bind a claim to a source and for a human to audit the bind. No `bs4` — this
workstream stays dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

import requests


class _TextExtractor(HTMLParser):
    """Collect title + body text, dropping script/style/head blocks."""

    _DROPPED = {"script", "style", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._title_depth = 0
        self._drop_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._DROPPED:
            self._drop_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROPPED and self._drop_depth:
            self._drop_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        else:
            self.parts.append(text)


@dataclass(frozen=True, slots=True)
class Extracted:
    title: str
    raw_text: str
    url: str


class FetchError(RuntimeError):
    """The URL could not be fetched."""


def fetch_and_extract(url: str, *, timeout: float = 60.0) -> Extracted:
    resp = requests.get(url, timeout=timeout,
                        headers={"User-Agent": "researcher/0.1 (research)"})
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for {url}")
    content_type = resp.headers.get("content-type", "")
    charset = None
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";")[0].strip()
    resp.encoding = charset or resp.apparent_encoding or "utf-8"
    return extract_text(resp.text, url=url)


def extract_text(html: str, *, url: str = "") -> Extracted:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # A malformed document should not sink the whole retrieval pass.
        pass
    title = " ".join(parser.title_parts).strip()
    body = "\n".join(parser.parts).strip()
    return Extracted(title=title, raw_text=body, url=url)
