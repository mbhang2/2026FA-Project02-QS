#!/usr/bin/env python3
"""Stage 2 of the retrieval pipeline: is this paper worth our next campaign?

    python scripts/rank_papers.py --dry-run
    python scripts/rank_papers.py

Design: `docs/design/paper-retrieval.md` stage 2. Prompt:
`config/prompts/paper-rank.md`.

Stage 1 decided a paper's claim *could* be tested here. This decides whether it is
*worth* testing, and what it would change about the question asked next -- which is
the thing an L2 loop actually consumes.

**The "what we know" block is assembled from committed artifacts, not maintained by
hand.** An earlier draft of the design specified ranking against a list of open
questions and noted that no such list existed. It does: every hypothesis file
carries a *What this is not asking for* section naming what that campaign set
aside, written before the campaign ran. All four of ours defer regime models with
"that is a later and harder question". Those deferrals are the open questions, and
they are already in the repository with dates attached.

Three sources, three purposes:

  config/hypotheses/*.md          what was asked, and what was deliberately not
  runs/campaigns/*/campaign.json  which questions are now closed
  docs/datasets/nq-es-qqq.md      what is falsified and must not be re-explored

**Outcomes are supplied as closure, never as a target.** The prompt says so
explicitly. Ranking papers by resemblance to what scored well would be selection on
data already spent -- retrieval and ranking cost no N precisely because they do not
look at the market, and steering them by past scores would quietly spend it.

Build-time, like the other scripts: nothing under `src/core/` may import it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
RAW = ROOT / "data" / "papers" / "raw"
PROMPT = ROOT / "config" / "prompts" / "paper-rank.md"
HYPOTHESES = ROOT / "config" / "hypotheses"
CAMPAIGNS = ROOT / "runs" / "campaigns"
DATASET_DOC = ROOT / "docs" / "datasets" / "nq-es-qqq.md"

MODEL = "claude-haiku-4-5"
LEVELS = ("high", "medium", "low")


def deferrals() -> list[str]:
    """The `What this is not asking for` bullets, per hypothesis. The open
    questions, written down before each campaign rather than reconstructed after."""
    out = []
    for f in sorted(HYPOTHESES.glob("*.md")):
        body = f.read_text()
        m = re.search(r"## What this is not asking for\n(.*?)(?=\n## |\Z)", body, re.S)
        if not m:
            continue
        bullets = [re.sub(r"\s+", " ", b).strip()
                   for b in re.findall(r"^- (.+?)(?=\n- |\n\n|\Z)", m.group(1), re.S | re.M)]
        if bullets:
            out.append(f"{f.stem}: " + " | ".join(bullets))
    return out


def closed_questions() -> list[str]:
    """What each campaign asked, and that it has been answered. The *question* and
    the *verdict*, deliberately without the scores -- see the module docstring."""
    out = []
    for d in sorted(CAMPAIGNS.iterdir()):
        rec = d / "campaign.json"
        summ = d / "tier2-summary.json"
        if not rec.exists():
            continue
        r = json.loads(rec.read_text()).get("record", {})
        hyp = r.get("hypothesis_path")
        if not hyp:
            continue
        verdict = "no summary yet"
        if summ.exists():
            g = json.loads(summ.read_text()).get("gate") or {}
            verdict = "ACCEPTED" if g.get("accept") else "rejected"
        out.append(f"{d.name}: asked {pathlib.Path(hyp).stem}, {verdict}")
    return out


def falsified() -> list[str]:
    if not DATASET_DOC.exists():
        return []
    m = re.search(r"## Falsified hypothesis families.*?\n(.*?)(?=\n## |\Z)",
                  DATASET_DOC.read_text(), re.S)
    if not m:
        return []
    return [re.sub(r"\s+", " ", b).strip()
            for b in re.findall(r"^- (.+?)(?=\n- |\n\n|\Z)", m.group(1), re.S | re.M)]


def context_block() -> str:
    parts = ["### Questions already asked, and closed", *(f"- {x}" for x in closed_questions()),
             "", "### Set aside deliberately -- the open questions",
             *(f"- {x}" for x in deferrals()),
             "", "### Falsified on this data -- do not re-explore",
             *(f"- {x}" for x in falsified())]
    return "\n".join(parts)


def _client():
    from audit.committee.llm import Client, preflight
    return Client(model=MODEL, temperature=0.0, max_tokens=1024), preflight(MODEL)


def rank_one(client, paper: dict, row: dict, ctx: str) -> dict:
    user = (f"{ctx}\n\n### The paper\n\n"
            f"Title: {paper['title']}\n\nAbstract: {paper.get('abstract','(none)')}\n\n"
            f"Stage 1 judged it **{row['feasibility']}**"
            + (f", adapting it thus: {row['adaptation']}" if row.get("adaptation") else "")
            + f".\nIts claim: {row.get('claim') or '(not recorded)'}")
    out = client.ask(PROMPT.read_text(), user)
    lvl = str(out.get("relevance", "")).strip().lower()
    if lvl not in LEVELS:
        raise ValueError(f"relevance not one of {LEVELS}: {out.get('relevance')!r}")
    if lvl == "high" and not out.get("hypothesis_edit"):
        # A paper worth fifty looks at the data has to change the question. If it
        # would leave the next hypothesis identical, it is not high.
        raise ValueError("high relevance with no hypothesis_edit")
    return {"relevance": lvl,
            "relevance_reason": str(out.get("reason") or "").strip(),
            "acts_on": out.get("acts_on"),
            "hypothesis_edit": out.get("hypothesis_edit"),
            "speaks_to_deferral": out.get("speaks_to_deferral")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}; run fetch_papers.py and screen_papers.py")
    reg = json.loads(REGISTRY.read_text())
    todo = [r for r in reg["papers"].values()
            if r.get("feasibility") in ("direct", "adaptable")
            and r.get("relevance") is None][:a.limit]
    if not todo:
        print("nothing feasible and unranked"); return 0

    ctx = context_block()
    client, served = _client()
    print(f"ranking {len(todo)} feasible papers  model {MODEL} (served {served})")
    print(f"  context: {len(closed_questions())} closed questions, "
          f"{len(deferrals())} deferral sets, {len(falsified())} falsified families\n")

    def work(row):
        f = RAW / f"{row['id']}.json"
        paper = json.loads(f.read_text()) if f.exists() else row
        try:
            return row["id"], rank_one(client, paper, row, ctx), None
        except Exception as exc:                                  # noqa: BLE001
            return row["id"], None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(work, todo))

    counts: dict[str, int] = {}
    for pid, got, err in results:
        if err:
            print(f"  {pid}  ERROR {err}"); continue
        counts[got["relevance"]] = counts.get(got["relevance"], 0) + 1
        print(f"  {pid}  {got['relevance']:<7} {got['relevance_reason'][:84]}")
        if not a.dry_run:
            reg["papers"][pid].update(got)
    print(f"\n  {counts}")
    if a.dry_run:
        print("  dry run: registry not written"); return 0
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {REGISTRY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
