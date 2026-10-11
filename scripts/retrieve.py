#!/usr/bin/env python3
"""The retrieval pipeline as one command: fetch, screen, rank, and stop.

    python scripts/retrieve.py --term "intraday momentum" --term breakout --dry-run
    python scripts/retrieve.py --term "trend reversal" --asking config/hypotheses/x.md
    python scripts/retrieve.py --asking config/hypotheses/x.md --distil 2605.04004v3

Stages 0, 0b, 1 and 2 were four separate commands run in the right order by
somebody remembering the right order. This runs them as one, which is what
"between campaigns" needs.

## Where it stops, and why there

It **ranks and stops.** It does not distil unless you name a paper with
`--distil`, and it never registers a campaign.

That is Amendment 8 rather than caution. A machine may write a hypothesis; the
commit is the human act, and it is where §6a's feasibility sanity check actually
happens. A pipeline that fetched, ranked, distilled and registered in one command
would put a model's judgement about what is worth several hundred looks at the
data beyond review. So the last thing this prints is which paper to distil and
the command to do it.

## What `--asking` is for

`rank_papers.py` judges a paper against what past campaigns **set aside**. That is
silent about the present: a paper on order-flow absorption arriving while we chase
trend reversal gets marked down for being a closed question, which is true of the
past and useless now. Passing the hypothesis we are working on puts the live
question in the ranking context.

It is optional, and its absence is printed, because ranking the corpus with no
live question is a legitimate thing to do -- it is what produced the two-axis
re-rank -- and the record should say which happened.

## What it costs in N: nothing

Design §4: fetching, screening, ranking and distilling never look at our market
data, so none of them is a look. **The moment a candidate hypothesis is
backtested to decide whether to keep it, it is a look and it counts.** This
script therefore cannot run a campaign, and the one after it is a human typing a
register command.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
PY = sys.executable


def _stage(argv: list[str], *, label: str) -> int:
    print(f"\n--- {label} " + "-" * max(0, 62 - len(label)))
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT))
    out = (proc.stdout or "").rstrip()
    # The stage scripts already print a readable summary; show its tail rather
    # than every paper, and the whole thing on failure.
    lines = out.splitlines()
    for line in (lines if proc.returncode else lines[-14:]):
        print(f"  {line}")
    if proc.returncode:
        print(f"  {(proc.stderr or '')[-800:]}")
    return proc.returncode


def counts() -> dict:
    if not REGISTRY.exists():
        return {}
    p = json.loads(REGISTRY.read_text())["papers"]
    feas = [v for v in p.values() if v.get("feasibility") in ("direct", "adaptable")]
    return {
        "papers": len(p),
        "unread": sum(1 for v in p.values() if v.get("status") != "screened"),
        "survivors": len(feas),
        "ranked": sum(1 for v in feas if v.get("gate_relevance")),
        "with_body": sum(1 for v in feas if v.get("fulltext_source")),
        "distilled": sum(1 for v in p.values() if v.get("hypothesis_path")),
    }


def worth_distilling() -> list[dict]:
    """Survivors rated high on either axis and not yet distilled, best first."""
    if not REGISTRY.exists():
        return []
    order = {"high": 0, "medium": 1, "low": 2, None: 3}
    rows = [v for v in json.loads(REGISTRY.read_text())["papers"].values()
            if v.get("feasibility") in ("direct", "adaptable")
            and not v.get("hypothesis_path")
            and not v.get("duplicate_of")      # the registry marks the copies
            and (v.get("relevance") == "high" or v.get("gate_relevance") == "high")]
    rows.sort(key=lambda v: (order.get(v.get("relevance")),
                             order.get(v.get("gate_relevance"))))
    # **One row per claim.** 40 duplicate titles across 82 registry rows
    # (measured 2026-10-04): Zenodo mints a DOI per version and an arXiv
    # preprint and its journal version are the same claim twice. The registry
    # keeps every row -- a row carries decisions and a re-fetch must not erase
    # them -- but suggesting one paper twice invites two campaigns on one idea.
    import re as _re
    seen: set[str] = set()
    out = []
    for v in rows:
        key = " ".join(_re.sub(r"[^a-z0-9 ]+", " ",
                               (v.get("title") or "").lower()).split())
        if key and key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", action="append", default=[],
                    help="repeatable topic filter for the fetch. Omit to skip "
                         "fetching and only screen and rank what is already held.")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD submitted-after bound")
    ap.add_argument("--max-results", type=int, default=100)
    ap.add_argument("--asking", type=pathlib.Path, default=None,
                    help="the hypothesis we are working on, added to the ranking "
                         "context so a paper is judged against the live question "
                         "and not only against what past campaigns set aside")
    ap.add_argument("--distil", metavar="PAPER_ID", default=None,
                    help="after ranking, distil this paper into a hypothesis. "
                         "Writes a file and stops -- Amendment 8 makes the commit "
                         "the human act.")
    ap.add_argument("--model", default=None, help="model for the distil step")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.asking is not None and not a.asking.exists():
        sys.exit(f"no hypothesis at {a.asking}")

    before = counts()
    print(f"registry before: {before}")
    if a.asking:
        print(f"asking: {a.asking.name}")
    else:
        print("asking: NO current question -- papers will be judged only against "
              "what past campaigns set aside")
    if a.dry_run:
        print("\ndry run: would fetch"
              + (f" on {len(a.term)} term(s)" if a.term else " nothing")
              + ", then screen (which fetches bodies), then rank"
              + (f" with {a.asking.name} in context" if a.asking else "")
              + ". Nothing would be distilled or registered.")
        return 0

    # ---- stage 0: fetch ----------------------------------------------------
    if a.term:
        argv = [PY, "scripts/fetch_papers.py", "--max-results", str(a.max_results)]
        for t in a.term:
            argv += ["--term", t]
        if a.since:
            argv += ["--since", a.since]
        if _stage(argv, label="stage 0  fetch"):
            print("\nfetch failed; nothing downstream ran")
            return 1
    else:
        print("\n--- stage 0  fetch: skipped (no --term) " + "-" * 24)

    # ---- stage 1: screen (which now fetches bodies for survivors) ----------
    if _stage([PY, "scripts/screen_papers.py", "--limit", str(a.limit)],
              label="stage 1  screen (+ stage 0b bodies)"):
        print("\nscreen failed; ranking skipped")
        return 1

    # ---- stage 2: rank -----------------------------------------------------
    argv = [PY, "scripts/rank_papers.py", "--limit", str(a.limit)]
    if a.asking:
        argv += ["--asking", str(a.asking)]
    if _stage(argv, label="stage 2  rank"):
        print("\nranking failed")
        return 1

    after = counts()
    print(f"\nregistry after:  {after}")
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    print(f"changed:         { {k: v for k, v in delta.items() if v} }")

    # ---- stage 3: only when asked, and only ever one --------------------
    if a.distil:
        argv = [PY, "scripts/distil_paper.py", "--paper", a.distil]
        if a.model:
            argv += ["--model", a.model]
        code = _stage(argv, label=f"stage 3  distil {a.distil}")
        if code:
            return 1
        print("\nA file was written and NOTHING was registered. Read it, then "
              "commit it:\n  the commit is the pre-registration (Amendment 8).")
        return 0

    picks = worth_distilling()
    print("\nranked and stopped. Nothing was distilled and nothing registered.")
    if picks:
        print(f"\n{len(picks)} paper(s) rated high and not yet distilled:")
        for v in picks[:6]:
            print(f"  {v['id']}  campaign={v.get('relevance')} "
                  f"gate={v.get('gate_relevance')}  {v['title'][:56]}")
        print(f"\nTo turn one into a question:\n"
              f"  python scripts/retrieve.py --distil {picks[0]['id']}"
              + (f" --asking {a.asking}" if a.asking else ""))
    else:
        print("\nno paper is rated high on either axis and undistilled.\n"
              "That used to be reported here as a finding about our data: the "
              "campaign axis\nhad never rated one high in 284 papers, because "
              "the single-instrument\nconstraint removes almost every signal "
              "paper. Measured 2026-10-04, that was\nhalf right. The other half "
              "was the SOURCE: arXiv q-fin is mostly method\npapers, and 3 of "
              "284 were directly testable here (1.1%). Adding OpenAlex and\n"
              "Crossref gave 22 of 153 (14.4%) and 3 of 95, so the corpus now "
              "holds 28\ndirectly testable papers instead of 3. If this list is "
              "empty, run the ranker\n(stage 2) over the new survivors before "
              "concluding anything about our data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
