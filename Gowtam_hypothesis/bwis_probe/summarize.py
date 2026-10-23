"""Aggregate probe results into source-type statistics (markdown to stdout).

    python -m bwis_probe.summarize tmp_bwis_100 [retrieval.jsonl|results.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Coarse source classes by domain, for the "kind of source" question.
ACADEMIC = ("arxiv.org", "semanticscholar.org", "ssrn.com", "papers.ssrn.com",
            "sciencedirect.com", "springer.com", "wiley.com", "jstor.org",
            "researchgate.net", "nber.org", "tandfonline.com", "oup.com",
            "cambridge.org", "doi.org", "mdpi.com", "ieee.org", "acm.org",
            "repec.org", "econstor.eu", "core.ac.uk", "pnas.org", "nature.com")
INSTITUTIONAL = ("federalreserve.gov", "bis.org", "imf.org", "worldbank.org",
                 "ecb.europa.eu", "bankofengland.co.uk", "cmegroup.com",
                 "gold.org", "lbma.org.uk", "cftc.gov", "sec.gov", "frbsf.org",
                 "stlouisfed.org", "newyorkfed.org")
PRACTITIONER = ("quantpedia.com", "quantifiedstrategies.com", "alphaarchitect.com",
                "quantstart.com", "investopedia.com", "tradingview.com",
                "medium.com", "substack.com", "seekingalpha.com", "blogspot.com",
                "wordpress.com", "quantconnect.com", "robotwealth.com",
                "github.com", "stackexchange.com", "wikipedia.org")


def classify(domain: str, backend: str) -> str:
    if backend in ("arxiv", "semantic_scholar"):
        return "academic"
    d = domain.lower()
    for tag, group in (("academic", ACADEMIC), ("institutional", INSTITUTIONAL),
                       ("practitioner", PRACTITIONER)):
        if any(d == g or d.endswith("." + g) for g in group):
            return tag
    return "other web"


def main() -> int:
    out = Path(sys.argv[1])
    fname = sys.argv[2] if len(sys.argv) > 2 else "retrieval.jsonl"
    rows = [json.loads(l) for l in (out / fname).read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["qid"])
    n = len(rows)

    print(f"## Aggregate ({n} questions, {fname})\n")
    oc = Counter(r.get("outcome") for r in rows)
    print("Outcomes: " + ", ".join(f"{k}={v}" for k, v in oc.items()) + "\n")

    be = Counter(c["backend"] for r in rows for c in r["retrieved"])
    tot = sum(be.values())
    print("| Backend | Sources | Share |\n|---|---|---|")
    for k, v in be.most_common():
        print(f"| {k} | {v} | {v/tot:.0%} |")

    cls = Counter(classify(c["domain"], c["backend"]) for r in rows for c in r["retrieved"])
    print("\n| Source class | Sources | Share |\n|---|---|---|")
    for k, v in cls.most_common():
        print(f"| {k} | {v} | {v/tot:.0%} |")

    dom = Counter(c["domain"] for r in rows for c in r["retrieved"]
                  if c["backend"] == "searxng")
    print("\nTop SearXNG domains: " + ", ".join(f"{d} ({c})" for d, c in dom.most_common(15)))

    print("\n## By theme\n")
    print("| Theme | Qs | Avg sources | arXiv | S2 | SearXNG | Academic % |\n|---|---|---|---|---|---|---|")
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r["theme"]].append(r)
    for th, rs in by.items():
        c = Counter(x["backend"] for r in rs for x in r["retrieved"])
        t = sum(c.values()) or 1
        ac = sum(1 for r in rs for x in r["retrieved"]
                 if classify(x["domain"], x["backend"]) == "academic")
        print(f"| {th} | {len(rs)} | {t/len(rs):.1f} | {c['arxiv']} | "
              f"{c['semantic_scholar']} | {c['searxng']} | {ac/t:.0%} |")

    print("\n## Per question\n")
    print("| ID | Question | Src | arXiv | S2 | Web | Top source |\n|---|---|---|---|---|---|---|")
    for r in rows:
        c = Counter(x["backend"] for x in r["retrieved"])
        top = r["retrieved"][0]["title"][:60].replace("|", "/") if r["retrieved"] else "-"
        print(f"| {r['qid']} | {r['question']} | {r['n_retrieved']} | {c['arxiv']} | "
              f"{c['semantic_scholar']} | {c['searxng']} | {top} |")

    if any(r.get("outcome") == "emitted" for r in rows):
        em = [r for r in rows if r.get("outcome") == "emitted"]
        cb = Counter(s["backend"] for r in em for s in r["cited"])
        print("\n## Cited in emitted hypotheses\n")
        print(", ".join(f"{k}={v}" for k, v in cb.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
