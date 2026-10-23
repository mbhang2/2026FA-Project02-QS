"""Run the 100 BWIS probe questions through retrieval + hypothesis generation.

For each question: retrieve sources (arXiv / Semantic Scholar / SearXNG), propose
-> critique -> refine a hypothesis with the same deterministic lints the driver
uses, and log which sources were retrieved and which were actually cited.

Nothing is written to config/hypotheses/. Output goes to a scratch directory:

    <out>/hypotheses/Qxxx.md      rendered hypotheses (emitted only)
    <out>/results.jsonl           one line per question (resumable)

Usage (from Gowtam_hypothesis/, with LITELLM_API_KEY exported):

    python -m bwis_probe.run [--out tmp_bwis_100] [--workers 3] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from researcher import driver, generate, grounding, llm
from researcher import bwis as _bwis
from researcher.feasibility import DatasetCaps, Infeasible, feasible
from researcher.retrieval import retrieve

from .questions import all_questions

_lock = threading.Lock()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _done_ids(results: Path) -> set[str]:
    if not results.exists():
        return set()
    ids = set()
    for line in results.read_text().splitlines():
        if line.strip():
            ids.add(json.loads(line)["qid"])
    return ids


def _run_one(qid: str, theme: str, question: str, out: Path,
             retrieval_only: bool = False) -> dict:
    t0 = time.time()
    row: dict = {"qid": qid, "theme": theme, "question": question}
    clips = []
    try:
        clips = retrieve(question, per_backend=driver.PER_BACKEND)
    except Exception as exc:  # retrieval must never kill the batch
        row.update(outcome="retrieval_error", reason=f"{type(exc).__name__}: {exc}")
    backend_of = {driver._norm_url(c.url): c.backend for c in clips}
    row["n_retrieved"] = len(clips)
    row["retrieved"] = [
        {"backend": c.backend, "domain": _domain(c.url), "title": c.title, "url": c.url}
        for c in clips
    ]
    if not clips:
        row.setdefault("outcome", "no_sources")
        row["elapsed_s"] = round(time.time() - t0, 1)
        return row
    if retrieval_only:
        row["outcome"] = "retrieved"
        row["elapsed_s"] = round(time.time() - t0, 1)
        return row

    stage = out / "_stage" / qid
    try:
        rec = generate.generate_hypothesis(
            question, clips,
            finalize=lambda r: driver._lint_findings(r, clips),
            advisory=lambda r: grounding.check_claims(r, clips),
            theme=theme)
        if not feasible(rec, DatasetCaps()):
            raise Infeasible("; ".join(driver._caps_violations(rec, DatasetCaps())))
        path = driver._write(rec, stage)
        dest = out / "hypotheses" / f"{qid}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        row.update(
            outcome="emitted", hypothesis_id=rec.id, falsifiable=rec.falsifiable,
            grid_size=rec.specification.get_total_grid_size(),
            bwis_score=round(rec.bwis_score, 4),
            bwis_band=_bwis.bwis_band(rec.bwis_score),
            cited=[{"backend": backend_of.get(driver._norm_url(s.source_url), "?"),
                    "domain": _domain(s.source_url), "title": s.source_title,
                    "url": s.source_url} for s in rec.sources])
    except (generate.PassBlocked, Infeasible, driver.LintRejected,
            driver.RailUnavailable, llm.LLMError, llm.ModelSubstituted) as exc:
        row.update(outcome="blocked", reason=f"{type(exc).__name__}: {exc}"[:400])
        pr = getattr(exc, "record", None)
        if pr is not None:
            row["blocked_cited"] = [
                {"backend": backend_of.get(driver._norm_url(s.source_url), "?"),
                 "url": s.source_url} for s in pr.sources]
    except Exception as exc:  # unexpected: record, keep going
        row.update(outcome="error", reason=f"{type(exc).__name__}: {exc}"[:400])
    row["elapsed_s"] = round(time.time() - t0, 1)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tmp_bwis_100")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--retrieval-only", action="store_true",
                    help="collect sources only; no LLM calls")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = out / ("retrieval.jsonl" if args.retrieval_only else "results.jsonl")
    if not args.retrieval_only:
        llm.preflight(generate.PROPOSE_MODEL)

    done = _done_ids(results)
    todo = [q for q in all_questions() if q[0] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(done)} done, {len(todo)} to run -> {out}", flush=True)

    def work(q):
        row = _run_one(*q, out, retrieval_only=args.retrieval_only)
        with _lock:
            with open(results, "a") as f:
                f.write(json.dumps(row) + "\n")
        print(f"{row['qid']} {row.get('outcome'):<10} "
              f"src={row.get('n_retrieved', 0):<3} {row['elapsed_s']}s  {row['question'][:50]}",
              flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))
    shutil.rmtree(out / "_stage", ignore_errors=True)
    print("complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
