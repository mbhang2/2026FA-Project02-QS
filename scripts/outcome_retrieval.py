#!/usr/bin/env python3
"""Between campaigns: what went wrong, and which papers speak to it.

    python scripts/outcome_retrieval.py --campaign nq-cvd-01            # diagnose + search
    python scripts/outcome_retrieval.py --campaign nq-cvd-01 --fetch-new  # + arXiv

**The gap it closes.** `refine_hypothesis.py` already reads what campaign k did
and what its auditors objected to, and it already offers the refiner papers --
but it chose them by word overlap with the *question*, so the same three papers
arrived however the campaign went. A campaign whose profit sat in four sessions
and one whose strategy never traded were offered identical reading. Here the
papers are chosen for the *outcome*:

1. **Diagnose.** One model call reads exactly the evidence the refiner reads --
   `refine_hypothesis.evidence`, which already withholds the gate's p-value,
   verdict and the best score -- and names at most three problems, each tied to
   a line of that evidence, with a search query for each.
2. **Retrieve.** Each query is ranked (BM25) against the registry's usable
   papers, over the screener's and ranker's own words about each one -- title,
   claim, what a strategy would do differently, how it adapts to our data. The
   registry holds no full text in most checkouts, so these fields are the
   corpus; they are also the part that says what to *do*.
3. Optionally, **fetch new papers** first: the queries become `retrieve.py`
   terms, so arXiv is searched, screened and ranked into the registry, then
   step 2 runs again. Off by default: it is network calls and two model passes
   over up to a hundred papers.

Each paper comes back with the problem it was retrieved for, and the refiner is
asked to apply only what answers a diagnosed problem.

**What it may not see is what the refiner may not see.** The diagnosis is built
from the same redacted evidence and nothing else, so it adds no route for a
score to reach the question; it changes which papers are read, not what is
known about the data. Nothing here reads validation (the refiner refuses first
if anything is readable), and nothing here is a look: no market data is read.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
PY = sys.executable
MAX_PROBLEMS = 3
PER_PROBLEM = 2            # papers kept per diagnosed problem
MAX_PAPERS = 4             # in total, after de-duplication
FIELDS = ("title", "claim", "acts_on", "adaptation", "relevance_reason",
          "abstract", "speaks_to_deferral")
STOP = set("""the and that this with from what does for are its not but has was
which would than paper data session sessions question strategy strategies when
into over under more less only also their there they them then these those such
used using use can may might will shall been being have had how why who whom
very much most some many each other between within without about across""".split())

DIAGNOSE_SYSTEM = """You read what one research campaign did and what its four
auditors objected to, and you name what most needs fixing in the QUESTION it
asked -- not in any one strategy. You are not told how the study scored it, and
you must not guess.

Return ONLY a JSON object:
{"problems": [{"what": "one sentence: the problem",
               "evidence": "the exact line of the evidence it rests on",
               "look_for": "one sentence: what kind of idea would address it",
               "query": "4-8 words to search a library of finance papers"}]}

At most three problems, most important first. Each must rest on a line of the
EVIDENCE section -- what the strategy did, or what an auditor objected to --
copied word for word into "evidence". A line of the question itself is not
evidence: it says what was asked, not what happened. If nothing in the evidence
supports a problem, return {"problems": []}. Queries
describe methods and market mechanisms ("volatility regime filter for order
flow signals"), never dates, prices, or what a market did."""


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())
            if w not in STOP]


def corpus(registry: pathlib.Path = REGISTRY) -> list[dict]:
    """The papers a refinement may use: screened usable on our data."""
    if not registry.exists():
        return []
    return [v for v in json.loads(registry.read_text())["papers"].values()
            if v.get("feasibility") in ("direct", "adaptable")]


def bm25(query: str, docs: list[dict], k1: float = 1.5, b: float = 0.75
         ) -> list[tuple[float, dict]]:
    """Rank `docs` for `query` over FIELDS. Crude and deterministic on
    purpose: an embedding model would be a second thing to pin and fingerprint,
    and the screener's words are already short and on-topic."""
    toks = [_tokens(" ".join(str(d.get(f) or "") for f in FIELDS)) for d in docs]
    if not toks:
        return []
    n, avg = len(toks), (sum(map(len, toks)) / len(toks)) or 1.0
    df: dict[str, int] = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    q = set(_tokens(query))
    out = []
    for d, t in zip(docs, toks):
        tf: dict[str, int] = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in q & tf.keys():
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        if s > 0:
            out.append((s, d))
    out.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    return out


RETRY_NOTE = ("\n\n# Your last answer was set aside\n\nEvery problem you named "
              "quoted something that is not in the evidence section -- most "
              "often a line of the question. Quote what the strategy did or "
              "what an auditor said, word for word.")


def diagnose(client, parent: str, evidence_text: str) -> list[dict]:
    """The problems, each with its evidence line and a query. [] if none.

    **Asked twice at most, and only when every problem was dropped.** Measured
    on nq-orderflow-absorption-02 (2026-11-12): one run in three quoted the
    QUESTION's own lines as evidence, all three problems were correctly dropped,
    and the refiner lost its whole diagnosis to a quoting slip. A model that
    names no problem is answered, not re-asked.
    """
    user = f"# The question\n\n{parent}\n\n# The evidence\n\n{evidence_text}"
    probs = _grounded_problems(client.ask(DIAGNOSE_SYSTEM, user), evidence_text)
    if probs is None:
        probs = _grounded_problems(client.ask(DIAGNOSE_SYSTEM, user + RETRY_NOTE),
                                   evidence_text) or []
    return probs


def _grounded_problems(out: dict, evidence_text: str) -> list[dict] | None:
    """The problems that rest on the evidence; None if some were named and
    every one was dropped (worth one re-ask), [] if none were named."""
    named = [p for p in (out.get("problems") or [])[:MAX_PROBLEMS]
             if isinstance(p, dict)]
    probs = []
    for p in named:
        q, what = str(p.get("query") or "").strip(), str(p.get("what") or "").strip()
        ev = str(p.get("evidence") or "").strip()
        # **Tied to the evidence, or dropped.** A problem the evidence does not
        # contain is the model's prior about strategies, and a search for it
        # retrieves what it already believed.
        if q and what and ev and _grounded(ev, evidence_text):
            probs.append({"what": what, "evidence": ev, "query": q,
                          "look_for": str(p.get("look_for") or "").strip()})
    return None if named and not probs else probs


def _grounded(line: str, evidence_text: str) -> bool:
    """Most of the cited line's words appear in the evidence."""
    words = set(_tokens(line))
    return bool(words) and len(words & set(_tokens(evidence_text))) >= 0.6 * len(words)


def retrieve(problems: list[dict], docs: list[dict]) -> list[dict]:
    """Papers per problem, best first, each knowing what it was retrieved for."""
    picked: dict[str, dict] = {}
    for prob in problems:
        for score, d in bm25(prob["query"], docs)[:PER_PROBLEM]:
            pid = d["id"]
            if pid not in picked:
                picked[pid] = {"paper": d, "for": [], "score": score}
            picked[pid]["for"].append(prob["what"])
            picked[pid]["score"] = max(picked[pid]["score"], score)
    ranked = sorted(picked.values(), key=lambda x: (-len(x["for"]), -x["score"]))
    return [{**shape(x["paper"]), "retrieved_for": x["for"],
             "retrieval_score": round(x["score"], 3)} for x in ranked[:MAX_PAPERS]]


VET_SYSTEM = """For each paper, decide whether it offers a usable idea for the
problem it was retrieved for -- on its own words, not its title. A paper that
shares words with the problem but is about something else does not.

Return ONLY a JSON object:
{"papers": [{"id": "<id>", "addresses": true or false,
             "how": "one sentence: the idea, and how it answers the problem"}]}"""


def vet(client, problems: list[dict], papers: list[dict]) -> list[dict]:
    """Keep only papers a reader says actually answer their problem.

    Measured on nq-orderflow-absorption-02 (2026-11-12): BM25 over the
    screener's fields matched "volume" to a volatility-volume classifier and
    "holding period" to two Nikkei reversal papers -- shared words, other
    subjects. The registry has nothing on volume-profile construction, so the
    honest answer for that problem was "no paper", and a refiner handed four
    loose matches is invited to use them anyway. One call, so the cost is
    bounded whatever the count.
    """
    if not papers:
        return []
    brief = [{"id": p["id"], "title": p.get("title"), "claim": p.get("claim"),
              "acts_on": p.get("acts_on"), "adaptation": p.get("adaptation"),
              "retrieved_for": p["retrieved_for"]} for p in papers]
    out = client.ask(VET_SYSTEM, "# Problems\n\n" + json.dumps(
        [{"what": q["what"], "look_for": q.get("look_for")} for q in problems],
        indent=1) + "\n\n# Papers\n\n" + json.dumps(brief, indent=1))
    verdict = {str(v.get("id")): v for v in (out.get("papers") or [])
               if isinstance(v, dict)}
    kept = []
    for p in papers:
        v = verdict.get(p["id"])
        # **No verdict is no.** A paper the reader skipped has not been shown
        # to answer anything, and silence is not a recommendation.
        if v and v.get("addresses") is True and str(v.get("how") or "").strip():
            kept.append({**p, "how": str(v["how"]).strip()})
    return kept


def shape(v: dict) -> dict:
    """The fields `refine_hypothesis.context` renders, as `papers_for` gives them."""
    body = ""
    f = ROOT / "data" / "papers" / "text" / f"{v['id']}.txt"
    if f.exists():
        body = f.read_text()[:6_000]
    return {"id": v["id"], "title": v.get("title"),
            "campaign_axis": v.get("relevance"), "gate_axis": v.get("gate_relevance"),
            "acts_on": v.get("acts_on"), "adaptation": v.get("adaptation"),
            "speaks_to_deferral": v.get("speaks_to_deferral"),
            "claim": v.get("claim"),
            "abstract": (v.get("abstract") or "")[:1_500], "body": body}


def fetch_new(problems: list[dict], parent_path: pathlib.Path) -> int:
    """`retrieve.py` with the diagnosed queries as terms: fetch, screen, rank.

    It ranks with `--asking` the current question, so a paper is judged against
    the live question rather than only against what past campaigns set aside.
    It never distils and never registers. Returns its exit code."""
    argv = [PY, "scripts/retrieve.py", "--max-results", "25"]
    for p in problems:
        argv += ["--term", p["query"]]
    argv += ["--asking", str(parent_path)]
    return subprocess.run(argv, cwd=str(ROOT)).returncode


def plan(client, parent: str, evidence_text: str, *, parent_path: pathlib.Path,
         fetch: bool = False) -> dict:
    """Diagnose, optionally fetch, retrieve. What the refiner and the commit get."""
    problems = diagnose(client, parent, evidence_text)
    fetched = None
    if problems and fetch:
        fetched = fetch_new(problems, parent_path)
    candidates = retrieve(problems, corpus()) if problems else []
    papers = vet(client, problems, candidates)
    return {"problems": problems, "papers": papers, "fetched": fetched,
            "rejected": [p["id"] for p in candidates
                         if p["id"] not in {k["id"] for k in papers}]}


def render(found: dict) -> str:
    """The diagnosis, for the refiner's context. Papers render in `context`."""
    if not found.get("problems"):
        return ""
    lines = ["\n\n# What the last campaign suggests needs work\n",
             "A diagnosis from the evidence above, not a score. Where a paper was "
             "judged to answer a problem it is named, and its idea is below. Where "
             "none was, address the problem on your own judgement. Either way, keep "
             "the question the same question, and give no number you were not "
             "given.\n"]
    for i, p in enumerate(found["problems"], 1):
        ids = [x["id"] for x in found.get("papers", [])
               if p["what"] in x.get("retrieved_for", [])]
        lines.append(f"\n{i}. **{p['what']}**\n   evidence: {p['evidence']}\n"
                     f"   would help: {p['look_for']}\n   "
                     + (f"answered by: {', '.join(ids)}\n" if ids else
                        "no paper answers this -- use your own judgement\n"))
    return "".join(lines)


def own_judgement(found: dict) -> list[str]:
    """The diagnosed problems no kept paper answers: the refiner's to decide."""
    covered = {w for x in found.get("papers", []) for w in x.get("retrieved_for", [])}
    return [p["what"] for p in found.get("problems", []) if p["what"] not in covered]


def commit_lines(found: dict) -> str:
    """What `git log` should say about why the question changed."""
    if not found or not found.get("problems"):
        return ("Retrieval: no problem diagnosed from the evidence; papers chosen "
                "by overlap with the question.\n")
    out = ["Diagnosis (from train behaviour and auditors only):\n"]
    for p in found["problems"]:
        out.append(f"  - {p['what']}  [query: {p['query']}]\n")
    mine = own_judgement(found)
    if mine:
        out.append("Left to the refiner's own judgement (no paper answered):\n")
        for w in mine:
            out.append(f"  - {w}"[:220] + "\n")
    if found["papers"]:
        out.append("Retrieved for it, and judged to answer it:\n")
        for x in found["papers"]:
            out.append(f"  - {x['id']} -- {x.get('how') or '; '.join(x['retrieved_for'])}"[:220] + "\n")
    else:
        out.append("No paper in the registry answered any of it; every change "
                   "is the refiner's own judgement.\n")
    if found.get("rejected"):
        out.append(f"Retrieved but judged not to answer it: "
                   f"{', '.join(found['rejected'])}\n")
    if found.get("fetched") is not None:
        out.append(f"New papers fetched first: retrieve.py exit {found['fetched']}\n")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--fetch-new", action="store_true")
    a = ap.parse_args()
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "refine_hypothesis", ROOT / "scripts" / "refine_hypothesis.py")
    rh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rh)
    leak = rh.validation_readable(a.campaign)
    if leak:
        sys.exit("REFUSED: a validation artefact is readable for "
                 f"{a.campaign}: {leak}")
    d = rh.CAMPAIGNS / a.campaign
    record = json.loads((d / "campaign.json").read_text()).get("record", {})
    parent_path = ROOT / record["hypothesis_path"]
    parent = parent_path.read_text()
    ev_text = rh.evidence_text(rh.evidence(a.campaign))
    from audit.committee.llm import Client, preflight
    model = a.model or rh.MODEL
    preflight(model)
    found = plan(Client(model=model, temperature=0.0, max_tokens=8_192), parent,
                 ev_text, parent_path=parent_path, fetch=a.fetch_new)
    print(render(found) or "no problem diagnosed")
    print(commit_lines(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
