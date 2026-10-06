#!/usr/bin/env python3
"""A reviewer for the candidate code, not the question.

    python scripts/review_strategy.py --campaign nq-s04-03
    python scripts/review_strategy.py --source runs/sources/ad53a4e5.py \
                                      --hypothesis config/hypotheses/x.md
    python scripts/review_strategy.py --campaign nq-s04-03 --json

Brian's point 2, third place: "perhaps the code review between trials". The
per-trial gate that exists is `compile()`, an AST pass and mypy against the
Strategy protocol -- syntax and types. **Nothing has ever read 19 of a
campaign's 20 candidates for whether the code is a fair attempt at the
question.** The four-seat committee reads one champion, after the fact.

## Why this is a separate pass and not a step in the trial loop

Two reasons, and the first is not the interesting one.

**Wall clock.** Twenty candidates times one model call, inside the loop, adds
hours to every campaign. The candidate sources are all kept
(`runs/campaigns/<c>/sources/<digest>.py`, named by the ledger's
`strategy_source`), so the review can run afterwards against exactly the code
that was scored, with nothing reconstructed.

**It is not a gate, and making it one is not mine to decide.** Blocking a
candidate before it is scored changes which candidates the search sees. That is
a change to a registered procedure, not a bug fix, and it needs its own
pre-registration -- the same reason `max_of_n` counts every candidate rather
than the ones we liked. So this records, and a human decides whether a recorded
objection should ever have stopped something.

## Score-blind, deliberately

The scores are on disk next to the sources and this reads none of them. It is
not shown the candidate's score, the campaign's p-value, the gate's verdict,
anything from validation, or **whether this candidate was the champion**. A
reviewer told "this one won" will find reasons it is sound.

That is also what makes the output usable as evidence: if the seat objects to
candidates that scored well and does not object to ones that scored badly, the
objections are about the code and not about the outcome.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Amendment 13: `runs/` unless a rehearsal redirects it. A rehearsal
# writes under `.scratch/`, which `runlog.ledger_paths()` does not read,
# so its trials are invisible to `global_n()` by construction.
import os as _os
_RUNS = pathlib.Path(_os.environ.get("PRACTICUM_RUNS_ROOT") or (ROOT / "runs"))
CAMPAIGNS = _RUNS / "campaigns"
PROMPT = ROOT / "config" / "prompts" / "strategy-review.md"

# **gemini-2.5-pro, not mimo.** Twenty calls per campaign makes mimo's measured
# 738 s a four-hour pass. gemini is a different family from every proposer we
# have run (claude-haiku-4-5, deepseek-v4-pro), so the independence that
# `audit/committee` was fixed for holds here by construction.
MODEL = "gemini-2.5-pro"
MAX_TOKENS = 16_384
WORKERS = 4
VERDICTS = ("OBJECT", "CANNOT_TELL", "NO_OBJECTION")
# **This seat quotes source lines, so its JSON carries code.** Measured on the
# first run over nq-s04-03: 2 of 15 replies came back
# `Unterminated string starting at: line 4` -- a newline inside a JSON string,
# the same fault that ended session-03 and the reason the shared client repairs
# raw control characters. Two of fifteen is a 13% silent loss of coverage, which
# a seat that only records can absorb and should not.
MAX_FORMAT_RETRIES = 2
MAX_SOURCE_CHARS = 24_000


def context(source: str, hypothesis: str) -> str:
    return (
        "# The research question\n" + hypothesis +
        "\n\n# The candidate's source code\n```python\n"
        + source[:MAX_SOURCE_CHARS] +
        "\n```\n\n# Deliberately withheld\n"
        "- this candidate's score, and every other candidate's\n"
        "- the campaign's p-value and the gate's verdict\n"
        "- everything from the validation split\n"
        "- whether this candidate was the one the campaign kept\n\n"
        "You are judging whether this code is a fair attempt at the question "
        "above, not whether it will make money.\n")


def review(source: str, hypothesis: str, *, model: str = MODEL) -> dict:
    """One call, one verdict, normalised the way every other seat here is.

    Retried only for a reply that was not usable JSON, and bounded. A malformed
    reply is not a judgement about the code.
    """
    from audit.committee.llm import Client, LLMUnavailable, preflight
    import requests

    served = preflight(model)
    client = Client(model=model, temperature=0.0, max_tokens=MAX_TOKENS)
    retryable = (LLMUnavailable, requests.exceptions.Timeout,
                 requests.exceptions.ConnectionError)
    base = context(source, hypothesis)
    out = last = None
    for i in range(MAX_FORMAT_RETRIES + 1):
        try:
            out = client.ask(PROMPT.read_text(), base + ("" if i == 0 else
                "\n\n# Your last reply was not usable JSON\n"
                "Return ONLY a JSON object with keys \"verdict\", \"concerns\" "
                "and \"note\". When you quote a line of code inside a string, "
                "every line break must be written \\n and every quote escaped -- "
                "a real line break inside a JSON string is invalid.\n"))
            break
        except retryable as exc:
            last = exc
    if out is None:
        # Reported as a failure, NOT as CANNOT_TELL. The caller separates them:
        # "the seat could not judge this" and "the seat never ran" are different
        # facts and collapsing them hides a coverage hole.
        return {"verdict": "CANNOT_TELL", "concerns": [], "failed": True,
                "note": f"{type(last).__name__}: {last}",
                "model": model, "model_served": served}

    verdict = str(out.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        return {"verdict": "CANNOT_TELL", "concerns": [], "unreadable": True,
                "note": f"the reviewer returned {out.get('verdict')!r}, which is "
                        f"not one of {VERDICTS}",
                "model": model, "model_served": served}
    concerns = [str(c) for c in (out.get("concerns") or [])]
    if verdict == "OBJECT" and not concerns:
        verdict, concerns = "CANNOT_TELL", [
            "the reviewer objected without naming a concern"]
    return {"verdict": verdict, "concerns": concerns,
            "note": str(out.get("note") or ""),
            "model": model, "model_served": served,
            "approval_note": (
                "NO_OBJECTION is not approval. It means nothing in the code made "
                "it an unfair attempt at the question; it says nothing about "
                "whether the strategy works.")}


def candidates(campaign: str) -> list[dict]:
    """Every scored candidate's source, from the ledger. Score NOT included."""
    d = CAMPAIGNS / campaign
    led = d / "ledger.jsonl"
    if not led.exists():
        raise SystemExit(f"no ledger at {led}")
    out, seen = [], set()
    for line in led.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        src = row.get("strategy_source")
        if not src:
            continue
        # The ledger records the path as written at run time; the copy that
        # survives is under the campaign's own sources/.
        name = pathlib.Path(src).name
        if name in seen:
            continue
        f = d / "sources" / name
        if not f.exists():
            f = ROOT / src
        if not f.exists():
            continue
        seen.add(name)
        out.append({"source_name": name, "path": f,
                    "digest": row.get("strategy_digest"),
                    "outcome": row.get("outcome")})
    return out


def hypothesis_for(campaign: str) -> tuple[str, str]:
    """`(text, path)` for the question this campaign was registered on.

    The path lives under `record`, not at the top level of `campaign.json`.
    **The recorded digest is verified**: a hypothesis is a pre-registration, so
    reviewing candidates against a file that has changed since they ran would be
    judging them against a question they were never asked.
    """
    import hashlib
    c = json.loads((CAMPAIGNS / campaign / "campaign.json").read_text())
    rec = c.get("record") or {}
    for src in (rec, c):
        v = src.get("hypothesis_path")
        if isinstance(v, str) and v.endswith(".md"):
            f = ROOT / v
            if not f.exists():
                raise SystemExit(f"{campaign} was registered on {v}, which is gone")
            text = f.read_text()
            want = rec.get("hypothesis_digest") or c.get("hypothesis_digest")
            got = hashlib.sha256(text.encode()).hexdigest()
            if want and got != want:
                raise SystemExit(
                    f"{campaign} was registered on {v} with digest {want[:12]}, "
                    f"but that file now digests to {got[:12]}. Reviewing against "
                    f"it would judge these candidates on a question they were "
                    f"never asked.")
            return text, v
    raise SystemExit(f"{campaign}: cannot find the hypothesis it was registered on")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", help="review every candidate this campaign scored")
    ap.add_argument("--source", type=pathlib.Path, help="one source file")
    ap.add_argument("--hypothesis", type=pathlib.Path,
                    help="required with --source")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.source:
        if not a.hypothesis or not a.hypothesis.is_file():
            sys.exit("--source needs --hypothesis: the seat judges the code "
                     "against the question, and has nothing to say without it")
        r = review(a.source.read_text(), a.hypothesis.read_text(), model=a.model)
        print(json.dumps(r, indent=2) if a.json else
              f"{a.source.name}: {r['verdict']}")
        for c in r["concerns"]:
            print(f"  - {c}")
        return 1 if r["verdict"] == "OBJECT" else 0

    if not a.campaign:
        sys.exit("pass --campaign or --source")
    hyp, hyp_path = hypothesis_for(a.campaign)
    cands = candidates(a.campaign)
    if a.limit:
        cands = cands[:a.limit]
    print(f"{a.campaign}: {len(cands)} candidate(s), question {hyp_path}")
    print(f"  model {a.model}, {a.workers} worker(s), score-blind")
    if a.dry_run:
        for c in cands:
            print(f"    {c['source_name']}  {c['path'].stat().st_size:>6,}B")
        print("\ndry run: nothing asked of a model, nothing written")
        return 0

    def one(c: dict) -> dict:
        try:
            r = review(c["path"].read_text(), hyp, model=a.model)
        except Exception as exc:                                 # noqa: BLE001
            r = {"verdict": "CANNOT_TELL", "concerns": [], "failed": True,
                 "note": f"{type(exc).__name__}: {exc}"}
        return {**c, "path": str(c["path"].relative_to(ROOT)), "review": r}

    def _label(rev: dict) -> str:
        if rev.get("failed"):
            return "SEAT FAILED"
        return {"OBJECT": "OBJECT     ", "CANNOT_TELL": "cannot tell",
                "NO_OBJECTION": "no objection"}.get(rev["verdict"], rev["verdict"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        rows = list(pool.map(one, cands))

    tally: dict[str, int] = {}
    failed = 0
    for row in rows:
        rev = row["review"]
        if rev.get("failed"):
            failed += 1
            tally["SEAT_FAILED"] = tally.get("SEAT_FAILED", 0) + 1
        else:
            v = rev["verdict"]
            tally[v] = tally.get(v, 0) + 1
        print(f"  {_label(rev)}  {row['source_name']}")
        for c in row["review"]["concerns"][:3]:
            print(f"      {c[:150]}")

    dest = CAMPAIGNS / a.campaign / "strategy-reviews.json"
    dest.write_text(json.dumps({
        "schema": "strategy-reviews/1",
        "campaign": a.campaign,
        "hypothesis": hyp_path,
        "model": a.model,
        "score_blind": True,
        "is_a_gate": False,
        "gate_note": (
            "This seat records and does not block. Blocking a candidate before "
            "it is scored changes which candidates the search sees, which is a "
            "change to a registered procedure and needs its own "
            "pre-registration."),
        "approval_note": (
            "NO_OBJECTION is not approval and the counts below are not a pass "
            "rate. This seat cannot say a strategy is good."),
        "tally": tally,
        "seats_failed": failed,
        "coverage_note": (
            f"{failed} of {len(rows)} candidate(s) were never judged: the seat's "
            f"own call failed. That is a coverage hole, not a verdict about the "
            f"code." if failed else ""),
        "candidates": rows,
    }, indent=2) + "\n")
    print(f"\n  {tally}")
    if failed:
        print(f"  ** {failed} of {len(rows)} never judged -- the seat failed, "
              f"which is not a verdict about the code **")
    print(f"  wrote {dest.relative_to(ROOT)}")
    print("\nRECORDED, not gated. No candidate was blocked and no score changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
