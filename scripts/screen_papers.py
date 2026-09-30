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
TEXT_DIR = ROOT / "data" / "papers" / "text"
PROMPT = ROOT / "config" / "prompts" / "paper-screen.md"

MODEL = "claude-haiku-4-5"
VERDICTS = ("direct", "adaptable", "infeasible")


def _fetch_bodies_for_new_survivors(ids: list[str]) -> None:
    """Stage 0b, immediately, for anything this screen just kept.

    **Why it is here and not left to a human.** `fetch_fulltext.py` was built as
    a standalone command and run once by hand. Nothing triggered it, so the
    moment a later screen produced new survivors the corpus had bodies for the
    old ones and abstracts for the new -- silently, because every consumer falls
    back to the abstract rather than failing. It went unnoticed until someone
    asked why a paper rated `high` had no body.

    This is the one place in the pipeline where "this paper matters" is decided,
    so it is where the body should be fetched. Best-effort: a network failure
    prints and moves on, because a screen verdict is worth keeping even when
    arXiv is down, and `--survivors` can always be re-run.
    """
    if not ids:
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_fulltext", pathlib.Path(__file__).with_name("fetch_fulltext.py"))
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)

    todo = [i for i in ids if not (ff.TEXT_DIR / f"{i}.txt").exists()]
    if not todo:
        return
    print(f"\n  stage 0b: fetching the body of {len(todo)} new survivor(s)")
    if ff.tools_python() is None:
        print("    (no .venv-tools, so a PDF-only paper will be skipped; "
              "scripts/fetch_fulltext.py --setup-tools creates it)")
    reg = json.loads(REGISTRY.read_text())
    ff.TEXT_DIR.mkdir(parents=True, exist_ok=True)
    for pid in todo:
        try:
            text, source = ff.fetch(pid)
        except Exception as exc:                                # noqa: BLE001
            print(f"    {pid}  {type(exc).__name__}: {exc}"[:110]); continue
        print(f"    {pid}  {source:24} {len(text):>8,} chars")
        if not text:
            continue
        (ff.TEXT_DIR / f"{pid}.txt").write_text(text)
        reg["papers"][pid].update(
            fulltext_source=source, fulltext_chars=len(text),
            fulltext_fetched=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%d"))
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


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


# How much of a paper body reaches the screen. The longest extracted paper is
# 199,092 characters; capped so one enormous paper cannot dominate a batch, and
# head-and-tail because the claim and the method are at the front and the
# results at the back, while the middle is mostly related work.
FULLTEXT_CAP = 120_000


def _body(paper_id: str) -> str:
    """The extracted paper body, or "". Stage 0b writes these."""
    f = TEXT_DIR / f"{paper_id}.txt"
    if not f.exists():
        return ""
    t = f.read_text()
    if len(t) <= FULLTEXT_CAP:
        return t
    half = FULLTEXT_CAP // 2
    return (t[:half] + f"\n\n[... middle omitted ({len(t) - FULLTEXT_CAP:,} "
            "characters) ...]\n\n" + t[-half:])


def screen_one(client, paper: dict, body: str = "") -> dict:
    """One paper, one verdict. Raises on anything that is not one of the three.

    **`body` is the whole paper when we hold it.** The screen decided 284
    verdicts, 257 of them rejections, from an abstract of ~1,300 characters --
    the thinnest input in the pipeline driving its most consequential stage. An
    abstract states the headline claim and rarely the data it needed, so a paper
    with a single-instrument section can read as cross-sectional and be rejected
    for a reason the body contradicts.
    """
    user = (f"Title: {paper['title']}\n\n"
            f"Abstract: {paper.get('abstract', '(none)')}\n\n"
            f"Categories: {', '.join(paper.get('categories') or []) or '(none)'}")
    if body:
        user += ("\n\n### The paper itself\n\nJudge against this, not the "
                 "abstract: an abstract states the claim and rarely the data it "
                 "needed.\n\n" + body)
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
    ap.add_argument("--rescreen", action="store_true",
                    help="re-judge already-screened papers we hold a body for. "
                         "The first verdict is preserved as "
                         "feasibility_first_pass when it changes.")
    ap.add_argument("--dry-run", action="store_true",
                    help="screen and print, write nothing back")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}. Run scripts/fetch_papers.py first.")
    reg = json.loads(REGISTRY.read_text())
    if a.rescreen:
        # **Only papers we hold a body for.** Re-running the same input expecting
        # a different answer is not a re-screen, it is a re-roll -- and it is the
        # habit this project is built against. A body is new evidence; an
        # abstract seen twice is not.
        todo = [p for p in reg["papers"].values()
                if p.get("status") == "screened"
                and (TEXT_DIR / f"{p['id']}.txt").exists()][:a.limit]
        if not todo:
            sys.exit("nothing screened has a body yet; run "
                     "scripts/fetch_fulltext.py --survivors first")
    else:
        todo = [p for p in reg["papers"].values()
                if p["status"] == "unread"][:a.limit]
    if not todo:
        print("nothing unread"); return 0

    client, served = _client()
    print(f"screening {len(todo)} papers  model {MODEL} (served {served})", flush=True)

    def work(row):
        raw = RAW / f"{row['id']}.json"
        paper = json.loads(raw.read_text()) if raw.exists() else row
        try:
            return row["id"], screen_one(client, paper, _body(row["id"])), None
        except Exception as exc:                                # noqa: BLE001
            return row["id"], None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(work, todo))

    counts: dict[str, int] = {}
    moved: list[str] = []
    for pid, verdict, err in results:
        if err:
            print(f"  {pid}  ERROR {err}")
            continue
        counts[verdict["feasibility"]] = counts.get(verdict["feasibility"], 0) + 1
        print(f"  {pid}  {verdict['feasibility']:<11} {verdict['feasibility_reason'][:88]}")
        if not a.dry_run:
            row = reg["papers"][pid]
            was = row.get("feasibility")
            if was is not None and was != verdict["feasibility"]:
                # The first verdict is part of the record. A verdict that moves
                # when the model is shown more evidence is a finding about the
                # screen, and overwriting it would erase exactly that.
                row["feasibility_first_pass"] = was
                row["feasibility_first_pass_reason"] = row.get("feasibility_reason")
                moved.append(f"{pid}: {was} -> {verdict['feasibility']}")
            row.update(verdict)
            row["status"] = "screened"
            row["screened_on"] = "fulltext" if (TEXT_DIR / f"{pid}.txt").exists() \
                else "abstract"

    print(f"\n  {counts}")
    if moved:
        print(f"  {len(moved)} verdict(s) MOVED on re-screen:")
        for m in moved:
            print(f"    {m}")
    elif a.rescreen:
        print("  no verdict moved: the abstract was enough for every one of these")
    if a.dry_run:
        print("  dry run: registry not written")
        return 0
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    _fetch_bodies_for_new_survivors(
        [pid for pid, v, e in results if not e and v
         and v["feasibility"] in ("direct", "adaptable")])
    print(f"  wrote {REGISTRY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
