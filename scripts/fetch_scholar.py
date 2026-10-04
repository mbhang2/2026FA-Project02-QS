#!/usr/bin/env python3
"""Stage 0, beyond arXiv: OpenAlex and Crossref into the same registry.

    python scripts/fetch_scholar.py --term "intraday momentum futures" --dry-run
    python scripts/fetch_scholar.py --term "opening range breakout" --max-results 50
    python scripts/fetch_scholar.py --term "order flow imbalance" --source crossref

Brian's point 3, second half: look for papers outside arXiv. It is the same
registry, the same row shape and the same idempotency guard as
`fetch_papers.py` -- `register()` and `load_registry()` are imported from it
rather than reimplemented, so there is one definition of a paper row.

## Why this exists, measured rather than assumed

The corpus was **284 of 284 from arXiv**, and `retrieve.py` prints a standing
finding that no paper has ever been rated high on the campaign axis in all 284,
"because our single-instrument constraint removes almost every signal paper".
That reasoning was half right. The other half is that arXiv q-fin is mostly
method papers, and the empirical intraday-futures literature is in journals.

Measured 2026-10-04 on `intraday futures momentum`:

| source | result |
|---|---|
| arXiv, 284 papers held | nothing rated high on the campaign axis |
| OpenAlex, one query | 58 works, first hit *"Nasdaq-100 Index Futures: Intraday Momentum or Reversal?"* |

That title is this project's question on this project's instrument, and we did
not have it.

## The limit, stated rather than discovered later

**Half of what OpenAlex knows about, we cannot read.** Same query: 58 works, 30
with an abstract, 33 open access, 16 both. A paywalled work with no abstract
cannot be screened -- the screen reads the abstract and, when we hold it, the
body -- so `--require-abstract` defaults to **true** and the rest are not
registered at all. They are not hidden: the run prints how many were skipped
for being unreadable, because "we found 58 and took 30" is a different fact
from "there were 30".

Bodies: `fetch_fulltext.fetch_url` pulls the open-access link when there is
one. A work with an abstract and no body is still registered and is screened on
the abstract alone, which is the weaker path the repo already documents for
pre-stage-0b arXiv papers.

## What it costs in N: nothing

Same as every other retrieval stage. Nothing here looks at our market data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAPERS = ROOT / "data" / "papers"
RAW = PAPERS / "raw"

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"

# Both APIs ask for a contact address in the agent and give higher rate limits
# for it ("the polite pool"). This is the same address the full-text fetcher
# already sends.
MAILTO = "mbhang2@illinois.edu"
USER_AGENT = f"2026FA-Project02-2-QS/0.1 (practicum research; mailto:{MAILTO})"
RATE_LIMIT_S = 1.0

# OpenAlex types that are a paper. `dataset`, `peer-review` and the rest are not
# claims we can test.
TYPES = ("article", "preprint", "book-chapter", "dissertation", "report")


def _get(url: str, timeout: float = 45.0) -> dict:
    """`requests`, not `urllib`, and the reason is certificates.

    `fetch_papers.py` reaches arXiv with `urllib` and that works, so this started
    the same way. Crossref failed with CERTIFICATE_VERIFY_FAILED: the system
    Python has no usable issuer chain for it. `requests` ships certifi, which is
    why the full-text fetcher already uses it.
    """
    import requests
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": USER_AGENT,
                              "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def safe_id(prefix: str, raw: str) -> str:
    """A registry id that is also a legal filename.

    `screen_papers.py` reads `data/papers/raw/{id}.json` directly, so an id with
    a slash in it would write outside the directory. DOIs are full of slashes.
    The real identifier is kept in the row as `doi` / `url`; this is the key.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    # Runs of dots collapse. No legitimate DOI or OpenAlex id contains `..`, and
    # a `..` in something that becomes a filename is the shape of a traversal
    # even when the slashes are already gone.
    safe = re.sub(r"\.{2,}", ".", safe).strip("._-")
    return prefix + "-" + safe


def abstract_from_index(aii: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}. Rebuild the text.

    Not a nicety: it is the only abstract OpenAlex gives, and the screen cannot
    run without one.
    """
    if not aii:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in aii.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[k] for k in sorted(pos))


def _strip_jats(s: str) -> str:
    """Crossref abstracts are JATS XML fragments. Take the text."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    return " ".join(s.split())


def search_openalex(terms: tuple[str, ...], since: str | None, limit: int,
                    *, require_abstract: bool) -> tuple[list[dict], int]:
    """`(papers, skipped_unreadable)`."""
    out: list[dict] = []
    skipped = 0
    select = ("id,doi,title,publication_date,authorships,"
              "abstract_inverted_index,open_access,topics,type,"
              "primary_location,cited_by_count")
    for term in terms:
        # Quoted: a bare space in the filter is an illegal URL character and
        # urllib raised on it before requests ever saw the query.
        filters = [f"title_and_abstract.search:{urllib.parse.quote(term)}"]
        if since:
            filters.append(f"from_publication_date:{since}")
        if require_abstract:
            filters.append("has_abstract:true")
        url = (f"{OPENALEX}?filter={','.join(filters)}"
               f"&per-page={min(limit, 100)}&select={select}"
               f"&mailto={urllib.parse.quote(MAILTO)}")
        data = _get(url)
        for w in data.get("results", []):
            if w.get("type") not in TYPES:
                continue
            ab = abstract_from_index(w.get("abstract_inverted_index"))
            if not ab and require_abstract:
                skipped += 1
                continue
            oid = (w.get("id") or "").rsplit("/", 1)[-1]
            if not oid:
                continue
            oa = w.get("open_access") or {}
            loc = w.get("primary_location") or {}
            out.append({
                "id": safe_id("openalex", oid),
                "source": "openalex",
                "openalex_id": oid,
                "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
                "title": " ".join((w.get("title") or "").split()),
                "abstract": ab,
                "published": w.get("publication_date") or "",
                "updated": w.get("publication_date") or "",
                "authors": [(a.get("author") or {}).get("display_name", "")
                            for a in (w.get("authorships") or [])],
                "categories": [t.get("display_name", "")
                               for t in (w.get("topics") or [])],
                "abs_url": w.get("id"),
                "oa_url": oa.get("oa_url") or loc.get("pdf_url"),
                "is_oa": bool(oa.get("is_oa")),
                "venue": ((loc.get("source") or {}) or {}).get("display_name"),
                "cited_by_count": w.get("cited_by_count"),
            })
        time.sleep(RATE_LIMIT_S)
    return out, skipped


def search_crossref(terms: tuple[str, ...], since: str | None, limit: int,
                    *, require_abstract: bool) -> tuple[list[dict], int]:
    """Crossref's value over OpenAlex is SSRN: `10.2139/ssrn.*` is where a lot
    of the empirical intraday-futures work actually lives."""
    out: list[dict] = []
    skipped = 0
    for term in terms:
        params = {"query.bibliographic": term, "rows": str(min(limit, 100)),
                  "select": "DOI,title,abstract,issued,type,container-title,author,URL",
                  "mailto": MAILTO}
        if since:
            params["filter"] = f"from-pub-date:{since}"
        data = _get(f"{CROSSREF}?{urllib.parse.urlencode(params)}")
        for w in data.get("message", {}).get("items", []):
            ab = _strip_jats(w.get("abstract", ""))
            if not ab and require_abstract:
                skipped += 1
                continue
            doi = w.get("DOI")
            if not doi:
                continue
            parts = ((w.get("issued") or {}).get("date-parts") or [[]])[0]
            published = "-".join(f"{p:02d}" if i else str(p)
                                 for i, p in enumerate(parts)) if parts else ""
            out.append({
                "id": safe_id("doi", doi),
                "source": "crossref",
                "openalex_id": None,
                "doi": doi,
                "title": " ".join((w.get("title") or [""])[0].split()),
                "abstract": ab,
                "published": published,
                "updated": published,
                "authors": [" ".join(filter(None, [a.get("given"), a.get("family")]))
                            for a in (w.get("author") or [])],
                "categories": [w.get("type") or ""],
                "abs_url": w.get("URL") or f"https://doi.org/{doi}",
                "oa_url": None,
                "is_oa": False,
                "venue": (w.get("container-title") or [None])[0],
                "cited_by_count": None,
            })
        time.sleep(RATE_LIMIT_S)
    return out, skipped


def title_key(title: str) -> str:
    """A title reduced to what makes it the same paper.

    **DOI dedup is not enough.** Measured 2026-10-04 after the first real fetch:
    40 duplicate titles across 82 registry rows. Zenodo mints a DOI per version,
    so one paper arrived as `10.5281/zenodo.22892891` and
    `...892`; an arXiv preprint and its journal version are the same claim under
    two identifiers. A corpus that holds a paper twice spends two screens and two
    rank calls on it, and can offer the same claim twice as a question to
    register -- which would be two campaigns on one idea, counted as two.
    """
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return " ".join(t.split())


def _fetch_papers_mod():
    spec = importlib.util.spec_from_file_location(
        "fetch_papers", pathlib.Path(__file__).with_name("fetch_papers.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", action="append", required=True,
                    help="repeatable; searched in title and abstract")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD published-after bound")
    ap.add_argument("--max-results", type=int, default=50,
                    help="per term, per source")
    ap.add_argument("--source", choices=("openalex", "crossref", "both"),
                    default="both")
    ap.add_argument("--require-abstract", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="skip works with no abstract. ON by default: the screen "
                         "reads the abstract, so a work without one cannot be "
                         "screened and would sit in the registry forever as "
                         "unread.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    fp = _fetch_papers_mod()
    terms = tuple(a.term)
    query = (f"scholar terms={'|'.join(terms)} since={a.since or 'any'} "
             f"source={a.source} require_abstract={a.require_abstract}")
    print(f"querying: {query}  max={a.max_results}/term/source", flush=True)

    papers: list[dict] = []
    skipped = 0
    for name, fn in (("openalex", search_openalex), ("crossref", search_crossref)):
        if a.source not in (name, "both"):
            continue
        try:
            got, miss = fn(terms, a.since, a.max_results,
                           require_abstract=a.require_abstract)
        except Exception as exc:                                 # noqa: BLE001
            # One source failing must not throw away the other's results.
            print(f"  {name} FAILED: {type(exc).__name__}: {exc}")
            continue
        print(f"  {name}: {len(got)} usable, {miss} skipped (no abstract)")
        papers += got
        skipped += miss

    # The same work can arrive from both sources. OpenAlex wins: it carries the
    # topics, the open-access link and the citation count.
    by_doi: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    deduped: list[dict] = []
    for p in sorted(papers, key=lambda x: x["source"] != "openalex"):
        d = (p.get("doi") or "").lower()
        tk = title_key(p.get("title", ""))
        if (d and d in by_doi) or (tk and tk in by_title):
            continue
        if d:
            by_doi[d] = p
        if tk:
            by_title[tk] = p
        deduped.append(p)
    if len(deduped) != len(papers):
        print(f"  {len(papers) - len(deduped)} duplicate(s) across sources dropped")

    reg = fp.load_registry()
    before = len(reg["papers"])
    known_dois = {(v.get("doi") or "").lower()
                  for v in reg["papers"].values() if v.get("doi")}
    known_titles = {title_key(v.get("title", ""))
                    for v in reg["papers"].values() if v.get("title")}
    fresh = [p for p in deduped
             if (p.get("doi") or "").lower() not in known_dois
             and title_key(p.get("title", "")) not in known_titles]
    if len(fresh) != len(deduped):
        print(f"  {len(deduped) - len(fresh)} already in the registry "
              f"(by DOI or title)")

    if a.dry_run:
        print(f"\ndry run: would consider {len(fresh)} new work(s); "
              f"{skipped} were unreadable and would NOT be registered")
        for p in fresh[:12]:
            oa = "OA" if p["is_oa"] else "  "
            print(f"  {oa} {p['source']:9} {p['id'][:34]:34} {p['title'][:52]}")
        print("\nNothing written.")
        return 0

    added = 0
    RAW.mkdir(parents=True, exist_ok=True)
    for p in fresh:
        if fp.register(reg, p, query):
            added += 1
            row = reg["papers"][p["id"]]
            # Fields the arXiv row has no use for, so `register()` does not know
            # about them. Written here rather than widening the shared row.
            row.update({"doi": p["doi"], "openalex_id": p["openalex_id"],
                        "oa_url": p["oa_url"], "is_oa": p["is_oa"],
                        "venue": p["venue"],
                        "cited_by_count": p["cited_by_count"]})
            (RAW / f"{p['id']}.json").write_text(json.dumps(p, indent=2) + "\n")

    fp.REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"\nregistry {before} -> {len(reg['papers'])}  (+{added})")
    print(f"  {skipped} work(s) were found and NOT registered: no abstract, so "
          f"they cannot be screened")
    print(f"  wrote {fp.REGISTRY.relative_to(ROOT)} and {RAW.relative_to(ROOT)}")
    print("\nNothing was screened or ranked. Next:\n"
          "  python scripts/retrieve.py --asking <hypothesis>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
