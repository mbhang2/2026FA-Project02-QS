#!/usr/bin/env python3
"""Hypothesis k+1, from hypothesis k and what campaign k actually did.

    python scripts/refine_hypothesis.py --campaign nq-s01-continuation --dry-run
    python scripts/refine_hypothesis.py --campaign nq-s01-continuation --commit

This is the L1.5 the pre-registration anticipated: the step that makes an
overnight session a *search* rather than a list of questions written in advance.
Governed by **Amendment 11**, which is what lets it commit without a human
reading the result first, and which states plainly what that costs.

## What it is shown, and the one thing it is not

It is shown the parent question, **what the strategy did**, and **what the four
auditors objected to**. It is not shown the gate's p-value, the gate's verdict,
or the campaign's best score.

That is deliberate and it is the most important decision in this file. Task 36
measured an LLM shown its own prior scores learning to improve them -- on a
dataset with no edge in it, trend p = 0.0031, +1.59 SD over a history-withheld
control. A refiner shown the gate's output is being taught to write questions
that score well. A refiner shown turnover, concentration and four auditors'
objections is being taught to write questions that survive scrutiny. Only one of
those is the thing we want.

## Validation is unreadable, by construction

Section 4: campaign k+1 may depend on prior campaigns' **train** results, never
on their validation results. That is not enforced here by instruction. This
script **refuses to run** if the campaign it was given has any validation
artefact on disk, because the honest reading of "may not depend on" is that the
information must not be in the room.

This works because `scripts/research_session.py` validates at the stop: during
phase 1 no validation file exists for any campaign, so the refiner runs, and
after phase 2 it will not run at all. The ordering enforces the rule.

## The five gates of Amendment 11

Lint, grounding, a recorded diff against the parent, the family unchanged, and no
validation readable. All five, or nothing is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import prereg  # noqa: E402

CAMPAIGNS = ROOT / "runs" / "campaigns"
HYPOTHESES = ROOT / "config" / "hypotheses"
PROMPT = ROOT / "config" / "prompts" / "hypothesis-refine.md"
REGISTRY = ROOT / "data" / "papers" / "registry.json"

MODEL = "deepseek-v4-pro"
REFINE_MAX_TOKENS = 32_768
MIN_WORDS = 350
MAX_WORDS = 1_125
MAX_LINT_REPAIRS = 3
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$")
PAPER_CHARS = 6_000
MAX_PAPERS = 3


# --------------------------------------------------------------------------- #
# gate 5: validation must not be in the room
# --------------------------------------------------------------------------- #
def validation_readable(campaign: str) -> list[str]:
    """Any validation artefact on this campaign. Non-empty means refuse."""
    d = CAMPAIGNS / campaign

    def rel(x: pathlib.Path) -> str:
        try:
            return str(x.relative_to(ROOT))
        except ValueError:
            return str(x.relative_to(d.parent))   # tmp dirs in the tests

    return sorted(rel(x) for x in d.glob("validation*") if x.is_file())


# --------------------------------------------------------------------------- #
# what the refiner is shown
# --------------------------------------------------------------------------- #
def evidence(campaign: str) -> dict:
    """What the strategy DID and what the auditors said. Never the gate's score.

    `redacted` is returned alongside so the record shows what was withheld rather
    than leaving a reader to infer it.
    """
    d = CAMPAIGNS / campaign
    summary = json.loads((d / "tier2-summary.json").read_text())
    best = (summary.get("best_candidate") or {})
    score = dict(best.get("score") or {})
    diags = dict(best.get("diagnostics") or {})

    # Withheld on purpose -- see the module docstring.
    for k in ("primary", "primary_uncharged", "sharpe", "sortino",
              "significance", "rejected", "reject_reason"):
        score.pop(k, None)

    reasons = []
    cv = d / "committee-verdict.json"
    if cv.exists():
        v = json.loads(cv.read_text())
        for a in v.get("auditors") or []:
            reasons.append({"axis": a.get("axis"), "verdict": a.get("verdict"),
                            "reasons": a.get("reasons") or []})

    n = summary.get("n") or {}
    return {
        "campaign": campaign,
        "what_the_strategy_did": {
            "trades": score.get("trade_count"),
            "sessions_scored": score.get("n_sessions"),
            "downside_sessions": score.get("downside_sessions"),
            "net_pnl_index_points": score.get("net_pnl"),
            "skew": score.get("skew"), "kurtosis": score.get("kurtosis"),
        },
        "concentration": {k: diags.get(k) for k in
                          ("by_month", "by_hour_utc", "by_weekday",
                           "best_session_share", "avg_holding_period_ns")
                          if diags.get(k) is not None},
        "auditors": reasons,
        "candidates_tried": (n.get("trials") if isinstance(n, dict) else n),
        "redacted": ["the gate's p-value", "the gate's verdict",
                     "every candidate's score, including the best"],
    }


def papers_for(limit: int = MAX_PAPERS) -> list[dict]:
    """Survivors rated high on either axis, with a slice of body text."""
    if not REGISTRY.exists():
        return []
    rows = [v for v in json.loads(REGISTRY.read_text())["papers"].values()
            if v.get("feasibility") in ("direct", "adaptable")
            and (v.get("relevance") == "high" or v.get("gate_relevance") == "high")]
    out = []
    for v in rows[:limit]:
        body = ""
        p = ROOT / "data" / "papers" / "text" / f"{v['id']}.txt"
        if p.exists():
            body = p.read_text()[:PAPER_CHARS]
        out.append({"id": v["id"], "title": v.get("title"),
                    "abstract": (v.get("abstract") or "")[:2_000], "body": body})
    return out


def context(parent_text: str, ev: dict, papers: list[dict]) -> str:
    parts = ["# The question as it stands\n", parent_text,
             "\n\n# What a strategy written against it actually did\n",
             json.dumps({k: ev[k] for k in
                         ("what_the_strategy_did", "concentration",
                          "candidates_tried")}, indent=2, default=str),
             "\n\n# What the auditors objected to\n"]
    for a in ev["auditors"]:
        parts.append(f"\n## {a['axis']} -- {a['verdict']}\n")
        for r in a["reasons"]:
            parts.append(f"- {r}\n")
        if not a["reasons"]:
            parts.append("- (no objection recorded)\n")
    parts.append("\n\n# Deliberately withheld from you\n")
    for r in ev["redacted"]:
        parts.append(f"- {r}\n")
    parts.append(
        "\nYou are not told how the study scored this, because a question "
        "written to score well is not the same as a question that is true.\n")
    if papers:
        parts.append("\n\n# Papers held as relevant\n")
        for p in papers:
            parts.append(f"\n## {p['id']} -- {p['title']}\n{p['abstract']}\n")
            if p["body"]:
                parts.append(f"\n{p['body']}\n")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# gates 1, 2, 4
# --------------------------------------------------------------------------- #
def lint_findings(md: str) -> list[str]:
    from src.core.loop.hypothesis import findings
    return findings(md)


def grounding_findings(md: str, sources: list[str]) -> list[str]:
    import grounding
    return grounding.findings(md, *sources)


def family_changed(parent: str, child: str) -> str:
    """Gate 4, and the README is honest that it is the weakest of the five.

    A title that no longer overlaps the parent's, or a body sharing almost
    nothing with it, is a new question wearing an old name. Measured as a
    similarity floor rather than asserted, so the number is arguable in the open.
    """
    ph = parent.lstrip().splitlines()[0].lstrip("# ").lower()
    ch = child.lstrip().splitlines()[0].lstrip("# ").lower()
    stop = {"does", "do", "a", "an", "the", "on", "in", "of", "and", "or", "it",
            "is", "are", "for", "to", "that", "what", "when", "give", "back"}
    pw = {w for w in re.findall(r"[a-z]+", ph) if w not in stop and len(w) > 2}
    cw = {w for w in re.findall(r"[a-z]+", ch) if w not in stop and len(w) > 2}
    overlap = len(pw & cw) / max(1, len(pw))
    body = difflib.SequenceMatcher(None, parent, child).ratio()
    if overlap < 0.34 and body < 0.30:
        return (f"the revision looks like a different question: its heading "
                f"shares {overlap:.0%} of the parent's content words and the "
                f"bodies are {body:.0%} similar.\n    parent: {ph}\n    child:  "
                f"{ch}")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", required=True,
                    help="the campaign whose outcome informs the revision")
    ap.add_argument("--parent", type=pathlib.Path, default=None,
                    help="the hypothesis to refine; defaults to the campaign's")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--commit", action="store_true",
                    help="commit it (Amendment 11). Without this, it writes the "
                         "file and stops.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not prereg.in_force(11) and a.commit:
        sys.exit("Amendment 11 is not in force, so a machine may not commit a "
                 "hypothesis. Drop --commit, or sign the amendment.")

    d = CAMPAIGNS / a.campaign
    if not (d / "tier2-summary.json").exists():
        sys.exit(f"{a.campaign} has no tier2-summary.json: it has not been "
                 f"reported yet, so there is nothing to refine against.")

    # ---- gate 5, before anything is read -----------------------------------
    leak = validation_readable(a.campaign)
    if leak:
        sys.exit(
            "REFUSED. A validation artefact is readable for "
            f"{a.campaign}:\n  " + "\n  ".join(leak) + "\n\n"
            "Section 4: campaign k+1 may depend on prior campaigns' train "
            "results, never on their validation results. The honest reading is "
            "that the information must not be in the room, so this refuses "
            "rather than promising not to look. Refine during phase 1, before "
            "validation runs at the stop.")

    record = json.loads((d / "campaign.json").read_text()).get("record", {})
    parent_path = a.parent or (ROOT / record["hypothesis_path"])
    parent = parent_path.read_text()

    ev = evidence(a.campaign)
    papers = papers_for()
    user = context(parent, ev, papers)
    print(f"parent      {parent_path.relative_to(ROOT)}")
    print(f"evidence    {len(ev['auditors'])} auditor(s), "
          f"{ev['what_the_strategy_did'].get('trades')} trades, "
          f"{len(papers)} paper(s)")
    print(f"withheld    {', '.join(ev['redacted'])}")
    print(f"context     {len(user)} chars")
    if a.dry_run:
        print("\ndry run: nothing was asked of a model and nothing written.")
        return 0

    # The committee's client, as stages 1-3 of retrieval use it and for the same
    # reason distil_paper.py gives: a fourth copy of the same HTTP call is a
    # fourth place for a default to drift. It also preflights, so a substituted
    # model is caught before a question is written under the wrong name.
    from audit.committee.llm import Client, preflight
    served = preflight(a.model)
    print(f"model       asked {a.model!r}, served {served!r}")
    client = Client(model=a.model, temperature=0.7,
                    max_tokens=REFINE_MAX_TOKENS)
    sources = [parent, user] + [p["body"] + p["abstract"] for p in papers]

    out = client.ask(PROMPT.read_text(), user)
    for attempt in range(MAX_LINT_REPAIRS):
        md = out.get("markdown") or ""
        leaks = lint_findings(md) if md else ["empty"]
        loose = grounding_findings(md, sources) if md else []
        fam = family_changed(parent, md) if md else ""
        words = len(md.split())
        length = ("" if MIN_WORDS <= words <= MAX_WORDS else
                  f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}")
        if not (leaks or loose or fam or length):
            break
        note = ["\n\n# Your previous attempt was refused. Fix ALL of it at once.\n"]
        if leaks:
            note.append("\n## It tells the strategy agent how it is judged\n")
            note += [f"- {x}\n" for x in leaks[:10]]
        if loose:
            note.append("\n## Numbers or quotes that are in no source\n")
            note += [f"- {x}\n" for x in loose[:10]]
        if fam:
            note.append(f"\n## It is no longer the same question\n- {fam}\n")
        if length:
            note.append(f"\n## Length\n- {length}\n")
        out = client.ask(PROMPT.read_text(), user + "".join(note))

    md = str(out.get("markdown") or "")
    slug = str(out.get("slug") or "").strip().lower()
    problems = []
    if not SLUG.match(slug):
        problems.append(f"slug is not 2-6 kebab-case words: {slug!r}")
    if not md.lstrip().startswith("#"):
        problems.append("markdown does not start with a '#' heading")
    problems += [f"lint: {x}" for x in lint_findings(md)]
    problems += [f"grounding: {x}" for x in grounding_findings(md, sources)]
    fam = family_changed(parent, md)
    if fam:
        problems.append(f"family: {fam}")
    words = len(md.split())
    if not (MIN_WORDS <= words <= MAX_WORDS):
        problems.append(f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}")
    if problems:
        print("\nNOT WRITTEN. Amendment 11's gates:")
        for p in problems[:14]:
            print(f"  - {p}")
        return 1

    dest = HYPOTHESES / f"{slug}.md"
    if dest.exists():
        dest = HYPOTHESES / f"{slug}-{dt.datetime.now():%m%d%H%M}.md"
    diff = "\n".join(difflib.unified_diff(
        parent.splitlines(), md.splitlines(),
        fromfile=str(parent_path.relative_to(ROOT)),
        tofile=str(dest.relative_to(ROOT)), lineterm=""))
    dest.write_text(md if md.endswith("\n") else md + "\n")
    print(f"\nwrote {dest.relative_to(ROOT)}  ({words} words)")
    print(f"  all five gates passed: lint, grounding, family, length, "
          f"no validation readable")

    if not a.commit:
        print("\nNOT committed. Read it, then commit it -- or pass --commit, "
              "which Amendment 11 permits.")
        return 0

    msg = (
        f"hypothesis: refine {parent_path.stem} after {a.campaign}\n\n"
        f"MACHINE-WRITTEN AND MACHINE-COMMITTED under Amendment 11. No human "
        f"read this before it was committed.\n\n"
        f"Parent: {parent_path.relative_to(ROOT)}\n"
        f"Informed by: {a.campaign} -- train results and four auditors' "
        f"objections only.\n"
        f"Withheld from the refiner: {'; '.join(ev['redacted'])}.\n"
        f"Model: {a.model} (served {served})\n"
        f"Papers cited: {', '.join(p['id'] for p in papers) or 'none'}\n\n"
        f"Gates passed: hypothesis lint, grounding, family-unchanged, length, "
        f"and no validation artefact was readable for {a.campaign} when this "
        f"ran.\n\n"
        f"Amendment 11's containment: this may spend trials and may NOT support "
        f"a claim. If a family containing it reaches L2 and passes, the pass is "
        f"held until a human has read every machine-written question in it.\n\n"
        f"--- diff against the parent ---\n{diff[:4000]}\n\n"
        f"Machine-Written: {a.campaign}\n")
    subprocess.run(["git", "add", str(dest)], cwd=str(ROOT), check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(ROOT), check=True)
    print(f"  committed, marked `Machine-Written: {a.campaign}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
