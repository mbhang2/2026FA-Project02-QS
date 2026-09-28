#!/usr/bin/env python3
"""Stage 3 of the retrieval pipeline: one paper becomes one registerable question.

    python scripts/distil_paper.py --paper 2605.04004v3 --dry-run
    python scripts/distil_paper.py --paper 2605.04004v3

Design: `docs/design/paper-retrieval.md` stage 3. Prompt:
`config/prompts/paper-distil.md`. Governed by Amendment 8 of
`docs/design/pre-registration.md`, which is what makes a machine-written
hypothesis a valid pre-registration.

**This is the only stage that writes something a campaign can be registered on.**
Stages 0-2 fill in a registry; nothing downstream of them had ever produced a
file, so no paper in 224 had ever reached a campaign. This closes that.

## Three things this script refuses to do

**1. It will not write a file that mentions the gate.** The output is linted with
`src.core.loop.hypothesis.lint` -- the same function `nq_campaign.py` runs at
registration -- **before** anything is written, and a failure is a refusal with
every finding named. A machine writing prose that goes verbatim into the
strategy agent's prompt is the sharpest version of the leak the lint exists for:
a human who writes "check it holds out of sample" is one careless sentence, a
model that learns the phrasing writes it every time.

**2. It will not write while a campaign is in flight.** `config/hypotheses/*.md`
is in `ARTIFACT_GLOBS`, so **adding a file to that directory changes the artifact
fingerprint of every subsequent run** -- and the fingerprint is compared at load
and again after the run, so a file appearing mid-campaign invalidates the trial
that straddles it. A stray file in that directory already did this once, on
`nq-orderflow-02`. The guard is a process check, not a note.

**3. It will not touch an existing hypothesis file.** A hypothesis's commit date
is its pre-registration date. Overwriting one would re-date a question after its
campaign had run, so a colliding slug is an error and the human picks another.

## What it costs in N: nothing, and why that is not obvious

Section 4 of the design: retrieval, screening, ranking and distillation never
look at our market data, so none of them is a look. **The moment a candidate
hypothesis is backtested to decide whether to keep it, it is a look and it
counts.** This script therefore produces a file and stops. It does not score it,
rank it against another distillation, or run anything -- and a pipeline that
generated ten hypotheses and registered the most promising would have taken ten
campaigns' worth of looks while reporting one.

Build-time, like the other three stages: nothing under `src/core/` may import it.
It imports `src.core.loop.hypothesis` in the other direction, which is the
allowed one -- a build-time script may read the run-time contract it must satisfy.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = ROOT / "data" / "papers" / "registry.json"
RAW = ROOT / "data" / "papers" / "raw"
PROMPT = ROOT / "config" / "prompts" / "paper-distil.md"
HYPOTHESES = ROOT / "config" / "hypotheses"
MODEL = "claude-haiku-4-5"

# The budget from the design, in words rather than tokens because a word count is
# what a human can check. ~450-700 words is ~600-950 tokens; the design's stated
# ceiling is 1,500 tokens, and the largest hypothesis already in the repo is
# `auction-market-features.md` at 1,422 words, which is over it and predates the
# budget. Enforced as a refusal at the top end because the file's length is paid
# for on every prompt of the campaign.
MIN_WORDS = 350
MAX_WORDS = 900

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$")


def _client():
    """The committee's client, as stages 1 and 2 use it and for the same reason:
    a fourth copy of the same HTTP call is a fourth place for a default to drift."""
    from audit.committee.llm import Client, preflight
    served = preflight(MODEL)
    return Client(model=MODEL, temperature=0.0, max_tokens=8192), served


def campaign_in_flight() -> str:
    """The name of a running campaign, or "". A process check, deliberately.

    `config/hypotheses/*.md` is fingerprinted, so a new file there changes
    `artifact_fingerprint` for every run that starts afterwards and invalidates
    one that is mid-flight. Asking the OS is the only way to know; a lock file
    would be a second thing to keep in sync.
    """
    out = subprocess.run(["pgrep", "-fl", "nq_campaign.py --stage run"],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return ""
    m = re.search(r"--name\s+(\S+)", out)
    return m.group(1) if m else "a campaign"


def context(paper: dict, row: dict) -> str:
    """What the distiller is given: the paper, and stage 1's adaptation.

    Stage 2's relevance is deliberately NOT included. It is a judgement about
    whether to spend a campaign, and a distiller told "this was rated highly"
    would write a more confident question than the evidence supports.
    """
    parts = [
        "### The paper", "",
        f"id: {row['id']}",
        f"Title: {paper.get('title', row.get('title', '(none)'))}", "",
        f"Abstract: {paper.get('abstract', '(none)')}", "",
        f"Its claim, as stage 1 recorded it: {row.get('claim') or '(not recorded)'}",
        "",
        f"Stage 1 judged our data **{row.get('feasibility')}** for it.",
    ]
    if row.get("adaptation"):
        parts += ["", "### How stage 1 said it must be adapted to be testable here",
                  "", str(row["adaptation"])]
    if row.get("feasibility_reason"):
        parts += ["", "### Why stage 1 judged it that way", "",
                  str(row["feasibility_reason"])]
    return "\n".join(parts)


def distil_one(client, paper: dict, row: dict) -> dict:
    """One call, one hypothesis. Validated here; written by the caller."""
    out = client.ask(PROMPT.read_text(), context(paper, row))

    slug = str(out.get("slug") or "").strip().lower()
    if not SLUG.match(slug):
        raise ValueError(f"slug is not 2-6 kebab-case words: {slug!r}")

    md = str(out.get("markdown") or "")
    if not md.lstrip().startswith("#"):
        raise ValueError("markdown does not start with a '#' heading")

    words = len(md.split())
    if not (MIN_WORDS <= words <= MAX_WORDS):
        raise ValueError(
            f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}. This file is pasted "
            "into every prompt of the campaign, so its length is paid for once per "
            "trial; too short and it is not a question.")

    # **The lint, before anything is written.** Same function the campaign runs at
    # registration, so a file that passes here cannot fail there.
    from src.core.loop.hypothesis import findings
    leaks = findings(md)
    if leaks:
        raise ValueError(
            "the distilled hypothesis tells the strategy agent about the gate:\n    "
            + "\n    ".join(leaks[:12])
            + f"\n  ({len(leaks)} finding(s)). Every byte of this file reaches the "
              "agent. Not written.")

    # The chain back to the source. Without it a result cannot be traced to the
    # paper that proposed it, and the corpus would have to be frozen instead.
    if row["id"] not in md:
        raise ValueError(
            f"the file does not cite the paper id {row['id']}, so a result could "
            "not be traced back to its source")

    return {"slug": slug,
            "title": str(out.get("title") or "").strip(),
            "markdown": md if md.endswith("\n") else md + "\n",
            "adapted_claim": out.get("adapted_claim"),
            "cannot_test": out.get("cannot_test"),
            "sets_aside": out.get("sets_aside") or [],
            "words": words}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paper", required=True, help="a registry id, e.g. 2605.04004v3")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the file and the checks; write nothing")
    a = ap.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"no registry at {REGISTRY}; run the earlier stages first")
    reg = json.loads(REGISTRY.read_text())
    row = reg["papers"].get(a.paper)
    if row is None:
        sys.exit(f"{a.paper} is not in the registry")
    if row.get("feasibility") not in ("direct", "adaptable"):
        sys.exit(f"{a.paper} was screened {row.get('feasibility')!r}; stage 1 said "
                 "its claim cannot be tested on our data")
    if row.get("hypothesis_path"):
        sys.exit(f"{a.paper} already has {row['hypothesis_path']}. A hypothesis's "
                 "commit date is its pre-registration date, so it is not rewritten.")

    running = campaign_in_flight()
    if running and not a.dry_run:
        sys.exit(
            f"{running} is running. config/hypotheses/*.md is in ARTIFACT_GLOBS, so "
            "adding a file there changes artifact_fingerprint and invalidates the "
            "trial that straddles it -- this happened to nq-orderflow-02. Wait for "
            "the campaign, or use --dry-run.")

    f = RAW / f"{a.paper}.json"
    paper = json.loads(f.read_text()) if f.exists() else row

    client, served = _client()
    print(f"distilling {a.paper}  model {MODEL} (served {served})")
    got = distil_one(client, paper, row)

    dest = HYPOTHESES / f"{got['slug']}.md"
    if dest.exists():
        sys.exit(f"{dest} already exists. A hypothesis is written once; pick "
                 f"another slug or delete nothing.")

    print(f"  slug        {got['slug']}")
    print(f"  words       {got['words']}  (budget {MIN_WORDS}-{MAX_WORDS})")
    print(f"  lint        clean")
    print(f"  cites id    yes")
    print(f"  adapted     {str(got['adapted_claim'])[:100]}")
    if got["cannot_test"]:
        print(f"  cannot test {str(got['cannot_test'])[:100]}")
    for x in got["sets_aside"][:4]:
        print(f"  sets aside  {str(x)[:92]}")

    if a.dry_run:
        print("\n" + "-" * 78)
        print(got["markdown"])
        print("-" * 78)
        print("dry run: nothing written")
        return 0

    dest.write_text(got["markdown"])
    row["hypothesis_path"] = str(dest.relative_to(ROOT))
    row["distilled"] = {"slug": got["slug"], "words": got["words"],
                        "adapted_claim": got["adapted_claim"],
                        "cannot_test": got["cannot_test"],
                        "sets_aside": got["sets_aside"],
                        "model": MODEL, "model_served": served}
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    print(f"\n  wrote {dest.relative_to(ROOT)}")
    print(f"  wrote {REGISTRY.relative_to(ROOT)}  ({a.paper}.hypothesis_path)")
    print("\nNOT registered. Read the file, commit it, and only then:")
    print(f"  python scripts/nq_campaign.py --stage register --name <id> \\")
    print(f"         --hypothesis {dest.relative_to(ROOT)} ...")
    print("The commit is the pre-registration. A human reads it first "
          "(Amendment 8).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
