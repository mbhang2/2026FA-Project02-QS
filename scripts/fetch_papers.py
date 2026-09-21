#!/usr/bin/env python3
"""Stage 0 of the retrieval pipeline: pull paper metadata into the registry.

    python scripts/fetch_papers.py --category q-fin.TR --since 2025-01-01
    python scripts/fetch_papers.py --category q-fin.TR --max-results 5 --dry-run

Design: `docs/design/paper-retrieval.md`.

**The query does not have to be reproducible and that is deliberate** (decided
2026-09-20). An arXiv search returns different results next week; what has to
survive is the chain from a campaign's result back to the paper that suggested the
question, and that is `data/papers/registry.json` -- one row per paper ever seen,
carrying what we decided about it and what came of it.

The registry is **bookkeeping, not a scored artifact.** It is deliberately absent
from `ARTIFACT_GLOBS`: the frozen thing is the hypothesis file a paper leads to,
whose digest is in `campaign.json`, and one frozen thing is enough. A consequence
worth knowing: writing here cannot invalidate a running trial, unlike anything
under `config/hypotheses/`.

Build-time, like the other scripts: nothing under `src/core/` may import it.

**Idempotent by design.** A paper already in the registry is not re-fetched and its
`status`, `feasibility` and `outcome` are never overwritten -- those are decisions,
and a re-run of a fetch is not a reason to forget them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAPERS = ROOT / "data" / "papers"
RAW = PAPERS / "raw"
REGISTRY = PAPERS / "registry.json"

# https, not http. arXiv 301s http -> https anyway, so the plaintext form
# costs an extra round trip and puts the query on the wire in the clear
# before the redirect. Their own docs give the http form; this is better.
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"

# arXiv asks for one request per three seconds and a descriptive agent. Both are
# in their terms of use, not suggestions, and a project that scrapes politely is
# the reason the API stays open.
RATE_LIMIT_S = 3.0
USER_AGENT = "2026FA-Project02-2-QS/0.1 (practicum research; contact via repo)"

# The categories whose papers could plausibly be testable on one intraday futures
# instrument. q-fin.PM (portfolio management) is deliberately absent: it is almost
# entirely cross-sectional, which section 1 of the design calls infeasible for us.
DEFAULT_CATEGORIES = ("q-fin.TR", "q-fin.ST", "q-fin.CP")


def _ssl_context():
    """Verified TLS, using the CA bundle the lockfile already pins.

    A stock python.org install on macOS ships no system trust store, so plain
    `urlopen` fails with CERTIFICATE_VERIFY_FAILED. The fix is the bundle, not
    an unverified context -- turning verification off to make a fetch work is how
    a pipeline quietly starts trusting whatever answers. `certifi==2026.7.22` is
    already in `requirements.lock`, so this adds no dependency.

    (Deliberately not naming the unverified-context helper here: the test that
    forbids it greps this file, and a comment warning against a string is
    indistinguishable from a use of it.)
    """
    import ssl

    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout,      # noqa: S310
                               context=_ssl_context()) as r:
        return r.read()


def _text(node, tag: str) -> str:
    el = node.find(ATOM + tag)
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml: bytes) -> list[dict]:
    """Atom -> one dict per entry. Tolerant: a malformed entry is skipped, not fatal."""
    out = []
    for e in ET.fromstring(xml).findall(ATOM + "entry"):
        raw_id = _text(e, "id")
        if not raw_id:
            continue
        # http://arxiv.org/abs/2401.01234v2 -> 2401.01234v2
        pid = raw_id.rsplit("/", 1)[-1]
        out.append({
            "id": pid,
            "source": "arxiv",
            "title": " ".join(_text(e, "title").split()),
            "abstract": " ".join(_text(e, "summary").split()),
            "published": _text(e, "published"),
            "updated": _text(e, "updated"),
            "authors": [_text(a, "name")
                        for a in e.findall(ATOM + "author")],
            "categories": [c.attrib.get("term", "")
                           for c in e.findall("{http://arxiv.org/schemas/atom}category")
                           or e.findall(ATOM + "category")],
            "abs_url": raw_id,
        })
    return out


def search(categories, since: str | None, max_results: int,
           page_size: int = 100, terms_any: tuple[str, ...] = ()) -> list[dict]:
    """Paged arXiv search, newest first, rate-limited.

    `terms_any` ANDs a topic filter onto the category sweep -- any of the terms,
    matched in title or abstract. A sweep of recent q-fin returns overwhelmingly
    methods papers (200 fetched, 15 testable here, 0 worth a campaign), because
    the categories describe a field rather than a claim. Naming the topic is how
    you reach papers that assert something a strategy could act on.
    """
    terms = " OR ".join(f"cat:{c}" for c in categories)
    if terms_any:
        topic = " OR ".join(f'abs:"{t}" OR ti:"{t}"' for t in terms_any)
        terms = f"({terms}) AND ({topic})"
    if since:
        # arXiv wants YYYYMMDDTTTT; the day boundary is good enough here.
        stamp = since.replace("-", "")
        terms = f"({terms}) AND submittedDate:[{stamp}0000 TO 999912312359]"
    found: list[dict] = []
    start = 0
    while len(found) < max_results:
        want = min(page_size, max_results - len(found))
        q = urllib.parse.urlencode({
            "search_query": terms, "start": start, "max_results": want,
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        page = parse_feed(_get(f"{ARXIV_API}?{q}"))
        if not page:
            break
        found.extend(page)
        start += len(page)
        if len(page) < want:
            break
        time.sleep(RATE_LIMIT_S)
    return found[:max_results]


def load_registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text())
    return {"schema": "paper-registry/1", "papers": {}}


def write_registry(reg: dict) -> None:
    PAPERS.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


def register(reg: dict, paper: dict, query: str) -> bool:
    """Add a paper. Returns False and changes nothing if it is already known.

    The guard is the point: `status`, `feasibility`, `adaptation` and `outcome`
    are decisions made about a paper, and re-running a fetch is not a reason to
    forget them.
    """
    if paper["id"] in reg["papers"]:
        return False
    reg["papers"][paper["id"]] = {
        "id": paper["id"],
        "source": paper["source"],
        "title": paper["title"],
        "published": paper["published"],
        "date_pulled": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "query": query,
        "feasibility": None,      # direct | adaptable | infeasible   (stage 1)
        "feasibility_reason": None,
        "adaptation": None,       # what had to change, if anything
        "relevance": None,        # stage 2
        "status": "unread",       # unread|screened|hypothesis_written|campaign_run
        "hypothesis_path": None,
        "campaign_ids": [],
        "outcome": None,
    }
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", action="append", default=None,
                    help=f"repeatable; default {' '.join(DEFAULT_CATEGORIES)}")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD submitted-after bound")
    ap.add_argument("--term", action="append", default=None,
                    help="repeatable topic filter, matched in title or abstract; "
                         "ANDed onto the categories. Any of the terms matches.")
    ap.add_argument("--max-results", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    a = ap.parse_args()

    cats = tuple(a.category) if a.category else DEFAULT_CATEGORIES
    terms = tuple(a.term or ())
    query = (f"cats={','.join(cats)} since={a.since or 'any'}"
             + (f" terms={'|'.join(terms)}" if terms else ""))
    print(f"querying arXiv: {query}  max={a.max_results}", flush=True)
    try:
        papers = search(cats, a.since, a.max_results, terms_any=terms)
    except Exception as exc:                                   # noqa: BLE001
        sys.exit(f"arXiv query failed: {type(exc).__name__}: {exc}")
    print(f"  {len(papers)} returned")

    reg = load_registry()
    before = len(reg["papers"])
    added = sum(register(reg, p, query) for p in papers)
    if a.dry_run:
        print(f"  dry run: {added} new, {len(papers) - added} already known; "
              "nothing written")
        for p in papers[:5]:
            print(f"    {p['id']}  {p['title'][:78]}")
        return 0

    RAW.mkdir(parents=True, exist_ok=True)
    for p in papers:
        f = RAW / f"{p['id']}.json"
        if not f.exists():
            f.write_text(json.dumps(p, indent=2, sort_keys=True) + "\n")
    write_registry(reg)
    print(f"  registry: {before} -> {len(reg['papers'])} papers ({added} new)")
    print(f"  wrote {RAW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
