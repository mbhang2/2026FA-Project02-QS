#!/usr/bin/env python3
"""Stage 1 of the retrieval pipeline: can this paper be tested on our data?

    python scripts/screen_papers.py --limit 20
    python scripts/screen_papers.py --limit 5 --dry-run

Design: `docs/design/paper-retrieval.md` stage 1. Prompt:
`config/prompts/paper-screen.md`.

**This is the load-bearing stage.** The first five live `q-fin.TR` results were
order-level shadowing, market making with concentrated liquidity, index
reconstitution, HFT concurrency infrastructure and prediction-market decidability
-- five for five needing book depth or a cross-section we do not have. The
retrieval half is easy; deciding what is testable here is the work.

Three verdicts, not two: `direct`, `adaptable` (with the adaptation stated), and
`infeasible` (with the binding constraint named). Instrument substitution and
timeframe aggregation are usually adaptations rather than blockers -- we hold
ticks, so any bar interval is constructible -- but **384 train sessions is a hard
ceiling on observations**, so a paper needing years of daily data is infeasible
even though the bars can be built.

Reads and writes `data/papers/registry.json`, which is bookkeeping rather than a
fingerprinted artifact: screening cannot invalidate a running trial.

Build-time, like the other scripts: nothing under `src/core/` may import it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
RAW = ROOT / "data" / "papers" / "raw"
PROMPT = ROOT / "config" / "prompts" / "paper-screen.md"

MODEL = "claude-haiku-4-5"
VERDICTS = ("direct", "adaptable", "infeasible")


def _client():
    """The committee's client, reused deliberately.

    `audit/committee/llm.py` imports nothing from `src.core` -- that separation is
    enforced by a test and exists so the audit tier stays a separate tier. A
    build-time script has no such constraint, and a third copy of the same HTTP
    call would be a third place for a timeout default to drift.
    """
    from audit.committee.llm import Client, preflight

    served = preflight(MODEL)
    return Client(model=MODEL, temperature=0.0, max_tokens=1024), served


def screen_one(client, paper: dict) -> dict:
    """One paper, one verdict. Raises on anything that is not one of the three."""
    user = (f"Title: {paper['title']}\n\n"
            f"Abstract: {paper.get('abstract', '(none)')}\n\n"
            f"Categories: {', '.join(paper.get('categories') or []) or '(none)'}")
    out = client.ask(PROMPT.read_text(), user)
    v = str(out.get("feasibility", "")).strip().lower()
    if v not in VERDICTS:
        raise ValueError(f"not one of {VERDICTS}: {out.get('feasibility')!r}")
    if v == "adaptable" and not out.get("adaptation"):
        # Section 1 of the design: an adaptation changes the claim, and a
        # downstream reader must be able to see what is no longer being tested.
        raise ValueError("adaptable without a stated adaptation")
    return {"feasibility": v,
            "feasibility_reason": str(out.get("reason") or "").strip(),
            "adaptation": out.get("adaptation"),
            "claim": str(out.get("claim") or "").strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true",
                    help="screen and print, write nothing back")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}. Run scripts/fetch_papers.py first.")
    reg = json.loads(REGISTRY.read_text())
    todo = [p for p in reg["papers"].values() if p["status"] == "unread"][:a.limit]
    if not todo:
        print("nothing unread"); return 0

    client, served = _client()
    print(f"screening {len(todo)} papers  model {MODEL} (served {served})", flush=True)

    def work(row):
        raw = RAW / f"{row['id']}.json"
        paper = json.loads(raw.read_text()) if raw.exists() else row
        try:
            return row["id"], screen_one(client, paper), None
        except Exception as exc:                                # noqa: BLE001
            return row["id"], None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(work, todo))

    counts: dict[str, int] = {}
    for pid, verdict, err in results:
        if err:
            print(f"  {pid}  ERROR {err}")
            continue
        counts[verdict["feasibility"]] = counts.get(verdict["feasibility"], 0) + 1
        print(f"  {pid}  {verdict['feasibility']:<11} {verdict['feasibility_reason'][:88]}")
        if not a.dry_run:
            reg["papers"][pid].update(verdict)
            reg["papers"][pid]["status"] = "screened"

    print(f"\n  {counts}")
    if a.dry_run:
        print("  dry run: registry not written")
        return 0
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {REGISTRY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
