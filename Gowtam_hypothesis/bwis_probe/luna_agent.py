"""GPT-5.6 Luna evaluation agent for BWIS arbitrary source starting weights.

Takes the 100 questions, retrieved sources, and generated hypotheses, and asks
gpt-5.6-luna to assign arbitrary quality/impressiveness weights (0.00 to 1.00)
to each source. Aggregates per-source, per-class, per-theme, and global averages
to establish the arbitrary starting prior for BWIS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from researcher import llm
from .summarize import classify

MODEL = "gpt-5.6-luna"

SYSTEM_PROMPT = """You are an expert quantitative research evaluator and Bayesian prior calibrator for an algorithmic trading system.
Your task is to critically evaluate sources retrieved for quantitative trading questions and assign rigorous, calibrated quality weights (0.00 to 1.00).

Scoring Calibration:
- 0.85 - 1.00: High-rigor empirical research, top-tier peer-reviewed literature (econometrics, quantitative finance), or primary quantitative whitepapers with concrete methodology.
- 0.70 - 0.84: Solid empirical research, reputable institutional notes (central banks, exchanges, quant asset managers), or rigorous practitioner studies.
- 0.50 - 0.69: Decent practitioner analysis, industry articles with sound concepts but limited empirical/statistical validation.
- 0.30 - 0.49: Basic educational guides, retail trading walkthroughs, or superficial overviews without statistical evidence.
- 0.00 - 0.29: Irrelevant homonyms (e.g. non-financial topics), unverified promotional content, or clickbait.

Return valid JSON strictly matching the schema:
{
  "source_scores": [
    {
      "url": "https://...",
      "weight": 0.85,
      "rationale": "One-line justification"
    }
  ],
  "question_impressiveness": 0.80,
  "summary": "One-line critique of the research quality for this topic"
}
"""


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def evaluate_question(row: dict, hypothesis_text: str | None = None) -> dict:
    """Evaluate one question and its sources using GPT-5.6 Luna."""
    qid = row["qid"]
    theme = row.get("theme", "")
    question = row.get("question", "")
    outcome = row.get("outcome", "")
    retrieved = row.get("retrieved", [])
    cited_urls = {s.get("url") for s in row.get("cited", [])}

    if not retrieved:
        return {
            "qid": qid,
            "theme": theme,
            "question": question,
            "outcome": outcome,
            "source_scores": [],
            "question_impressiveness": 0.0,
            "summary": "No sources retrieved.",
        }

    sources_formatted = []
    for i, s in enumerate(retrieved):
        is_cited = s.get("url") in cited_urls
        sources_formatted.append(
            f"[{i+1}] Title: {s.get('title')}\n"
            f"    URL: {s.get('url')}\n"
            f"    Backend: {s.get('backend')} | Domain: {s.get('domain')} | Cited in hypothesis: {'YES' if is_cited else 'NO'}"
        )

    user_prompt = (
        f"Research Question [{qid}]: {question}\n"
        f"Theme: {theme}\n"
        f"Pipeline Outcome: {outcome}\n\n"
    )

    if hypothesis_text:
        user_prompt += f"Generated Hypothesis:\n```markdown\n{hypothesis_text[:1200]}\n```\n\n"

    user_prompt += (
        f"Retrieved Sources ({len(retrieved)} total):\n"
        + "\n\n".join(sources_formatted)
        + "\n\nEvaluate each source and return the JSON object."
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        comp = llm.call(
            MODEL,
            messages,
            temperature=0.2,
            max_tokens=2048,
            check_substitution=False,
        )
        data = llm.extract_json(comp.text)
        if not data:
            raise ValueError("No JSON extracted from response")
    except Exception as exc:
        # Fallback heuristic if call fails
        return {
            "qid": qid,
            "theme": theme,
            "question": question,
            "outcome": outcome,
            "source_scores": [
                {
                    "url": s.get("url"),
                    "title": s.get("title", ""),
                    "domain": s.get("domain", ""),
                    "backend": s.get("backend", ""),
                    "source_class": classify(s.get("domain", ""), s.get("backend", "")),
                    "is_cited": s.get("url") in cited_urls,
                    "weight": 0.50,
                    "rationale": f"Fallback: {exc}"
                }
                for s in retrieved
            ],
            "question_impressiveness": 0.50,
            "summary": f"Evaluation error: {exc}",
        }

    # Match returned scores to retrieved sources
    score_by_url = {
        s.get("url"): s
        for s in data.get("source_scores", [])
        if isinstance(s, dict) and s.get("url")
    }

    final_scores = []
    for s in retrieved:
        url = s.get("url")
        match = score_by_url.get(url)
        weight = float(match.get("weight", 0.50)) if match else 0.50
        rationale = str(match.get("rationale", "Standard relevance")) if match else "Unrated"
        sclass = classify(s.get("domain", ""), s.get("backend", ""))
        final_scores.append({
            "url": url,
            "title": s.get("title", ""),
            "domain": s.get("domain", ""),
            "backend": s.get("backend", ""),
            "source_class": sclass,
            "is_cited": url in cited_urls,
            "weight": round(weight, 4),
            "rationale": rationale,
        })

    return {
        "qid": qid,
        "theme": theme,
        "question": question,
        "outcome": outcome,
        "source_scores": final_scores,
        "question_impressiveness": round(float(data.get("question_impressiveness", 0.5)), 4),
        "summary": str(data.get("summary", "")),
    }


def aggregate_weights(evaluations: list[dict]) -> dict:
    """Compute global, per-class, and per-theme average arbitrary weights."""
    all_scores = []
    cited_scores = []
    class_scores = defaultdict(list)
    theme_scores = defaultdict(list)
    backend_scores = defaultdict(list)

    for ev in evaluations:
        th = ev.get("theme", "Unknown")
        for sc in ev.get("source_scores", []):
            w = float(sc.get("weight", 0.50))
            c = sc.get("source_class") or classify(sc.get("domain", ""), sc.get("backend", ""))
            b = sc.get("backend", "unknown")
            all_scores.append(w)
            class_scores[c].append(w)
            theme_scores[th].append(w)
            backend_scores[b].append(w)
            if sc.get("is_cited"):
                cited_scores.append(w)

    def avg(lst):
        return round(sum(lst) / len(lst), 4) if lst else 0.0

    return {
        "overall_bwis_score": avg(all_scores),
        "cited_bwis_score": avg(cited_scores),
        "total_sources_scored": len(all_scores),
        "total_cited_scored": len(cited_scores),
        "by_class": {k: {"avg_weight": avg(v), "count": len(v)} for k, v in class_scores.items()},
        "by_theme": {k: {"avg_weight": avg(v), "count": len(v)} for k, v in theme_scores.items()},
        "by_backend": {k: {"avg_weight": avg(v), "count": len(v)} for k, v in backend_scores.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="tmp_bwis_100", help="Directory containing results.jsonl and hypotheses")
    ap.add_argument("--workers", type=int, default=3, help="Concurrent worker threads")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of questions to score")
    args = ap.parse_args()

    probe_dir = Path(args.dir)
    results_file = probe_dir / "results.jsonl"
    if not results_file.exists():
        print(f"Error: {results_file} not found.", file=sys.stderr)
        return 1

    hypo_dir = probe_dir / "hypotheses"
    out_file = probe_dir / "luna_weights.jsonl"
    summary_file = probe_dir / "bwis_starting_weights.json"

    done_qids = set()
    if out_file.exists():
        for line in out_file.read_text().splitlines():
            if line.strip():
                done_qids.add(json.loads(line)["qid"])

    rows = []
    with open(results_file) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))

    todo = [r for r in rows if r["qid"] not in done_qids]
    if args.limit:
        todo = todo[:args.limit]

    print(f"GPT-5.6 Luna Evaluator: {len(done_qids)} done, {len(todo)} to evaluate -> {out_file}", flush=True)

    def work(r):
        qid = r["qid"]
        hypo_file = hypo_dir / f"{qid}.md"
        hypo_text = hypo_file.read_text() if hypo_file.exists() else None
        t0 = time.time()
        ev = evaluate_question(r, hypo_text)
        elapsed = round(time.time() - t0, 1)
        with open(out_file, "a") as f:
            f.write(json.dumps(ev) + "\n")
        print(f"[{qid}] Scored {len(ev['source_scores'])} sources (avg: {round(sum(s['weight'] for s in ev['source_scores'])/max(len(ev['source_scores']),1), 2)}) in {elapsed}s", flush=True)
        return ev

    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, todo))

    # Read all evaluations and aggregate
    all_evals = []
    with open(out_file) as f:
        for line in f:
            if line.strip():
                all_evals.append(json.loads(line))

    agg = aggregate_weights(all_evals)
    summary_file.write_text(json.dumps(agg, indent=2))
    print(f"\nCompleted GPT-5.6 Luna Evaluation.")
    print(f"Overall Arbitrary Score for BWIS: {agg['overall_bwis_score']:.4f}")
    print(f"Cited Sources Score for BWIS: {agg['cited_bwis_score']:.4f}")
    print(f"Aggregated summary written to {summary_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
