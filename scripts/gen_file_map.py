#!/usr/bin/env python3
"""Write docs/FILES.md: what every tracked file does, from the file's own words.

    python scripts/gen_file_map.py           # rewrite docs/FILES.md
    python scripts/gen_file_map.py --check   # exit 1 if it is out of date

**Generated, not written.** A hand-kept list of 200 files is wrong within a
week. Each entry is the first line of the file's own description -- a Python
module's docstring, a markdown file's first heading, a YAML or shell file's
first comment -- so the map says what the file says about itself, and a file
whose description is wrong is fixed in the file. `tests/test_file_map.py` runs
`--check`, so adding a file without regenerating fails the suite.

Grouping is by directory, in the order a reader meets them on a run (see
docs/RUN-SEQUENCE.md). `runs/` is output and `alternatives/`, `research/`,
`docs/reviews/` are summarised as one line each: they are beside the harness,
not in it.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "FILES.md"

# (heading, path prefix, what the group is). Order is the reading order.
GROUPS = [
    ("Entry points", ("run.py",), "The two commands everything else calls."),
    ("Campaigns and sessions", ("scripts/nq_campaign.py", "scripts/research_session.py",
                                "scripts/rehearse.py", "scripts/l2_gate.py",
                                "scripts/holdout.py"),
     "Drive a campaign's stages, a session's campaigns, and the gates at the end."),
    ("Hypotheses: writing, refining, retrieving",
     ("scripts/write_hypothesis.py", "scripts/refine_hypothesis.py",
      "scripts/outcome_retrieval.py", "scripts/review_hypothesis.py",
      "scripts/distil_paper.py", "scripts/grounding.py", "scripts/retrieve.py",
      "scripts/fetch_", "scripts/screen_papers.py", "scripts/rank_papers.py",
      "scripts/fidelity_judge.py", "scripts/open_questions.py",
      "scripts/orchestrate.py", "scripts/bwis_calibrate.py",
      "scripts/knowledge_", "scripts/research_session"),
     "Turn a statement, a paper or a finished campaign into the next question."),
    ("Other scripts", ("scripts/",), "Calibration, checks, inventory and one-off tools."),
    ("The proposer and the loop", ("src/core/loop/",),
     "The LLM agent, the campaign record, and the question-fidelity checks."),
    ("Engine", ("src/core/engine/",), "The backtest walk, fills, and the isolated child process."),
    ("Scoring and nulls", ("src/core/scoring/",), "The metric, the nulls, and the correction."),
    ("Data", ("src/core/data/",), "Datasets, calendars, and where the split is enforced."),
    ("Research helpers", ("src/core/research/",), "Priors, the trajectory whitelist, the outcome lint."),
    ("Core, other", ("src/core/",), "Types, the run log, features and checks."),
    ("Strategies and primitives", ("src/strategies/", "src/primitives/", "src/"),
     "The file the agent edits, its baseline, and the building blocks it may use."),
    ("Committee and audit", ("audit/",), "The four auditors, and the audit engine."),
    ("Configuration", ("config/run.yaml", "config/corrections.yaml",
                       "config/house-constraints.yaml", "config/bwis-priors.json",
                       "config/calendars/"), "Profiles, the correction, and house limits."),
    ("Prompts", ("config/prompts/",), "Every prompt a model is given. Each is fingerprinted."),
    ("Session plans", ("config/sessions/",), "Plans `research_session.py` runs."),
    ("Loop side-car", ("loop/",), "Schemas and the opencode bridge beside the harness."),
    ("Tests", ("tests/",), "What each test file pins."),
]
SUMMARISED = {
    "config/hypotheses/": "Every research question, its constraints file and its provenance; see docs/guide/hypotheses.md.",
    "alternatives/": "Alternative engines beside the harness (l1_engine); not on the run path.",
    "research/": "Exploratory notebooks and notes; not on the run path.",
    "docs/reviews/": "The script behind every number quoted in the docs, one folder per task.",
    "runs/": "Run output: campaigns, ledgers, sessions. Generated; never edited by hand.",
    "data/papers/": "The paper registry (registry.json) and per-paper records the retrieval stages write.",
    "data/calibration/": "Calibration tables the scoring reads; see docs/design/metric.md.",
    "agent-patterns/": "Mentor-contributed agent patterns, kept for reference.",
}
KINDS = (".py", ".md", ".yaml", ".yml", ".sh", ".json")


def describe(path: pathlib.Path) -> str:
    """The file's own first line about itself, or a placeholder that says so."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "(unreadable)"
    first = ""
    if path.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except SyntaxError:
            doc = None
        if doc:
            # First non-empty line that is prose, not a usage example.
            for line in doc.splitlines():
                s = line.strip()
                if s and not s.startswith(("python ", "$ ", "pytest ", ">>>")):
                    first = s
                    break
    elif path.suffix == ".md":
        # A prompt opens with an HTML comment saying what it is, then "## SYSTEM".
        c = re.match(r"\s*<!--\s*(.+?)(?:\n\s*\n|-->)", text, re.S)
        m = re.search(r"^#{1,3}\s+(?!SYSTEM\b|USER\b)(.+)$", text, re.M)
        first = (c.group(1) if c else "") or (m.group(1) if m else "")
    elif path.suffix in (".yaml", ".yml", ".sh"):
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#") and not s.startswith("#!") and len(s) > 3:
                first = s.lstrip("#").strip()
                break
    elif path.suffix == ".json":
        first = "data (JSON)"
    if not first and path.name == "__init__.py":
        first = "package marker" + ("" if not text.strip() else " (re-exports)")
    first = re.sub(r"\s+", " ", first).strip().rstrip(":")
    first = first.replace("|", "\\|")
    if len(first) > 160:
        first = first[:157].rstrip() + "..."
    return first or "*(no description in the file)*"


def tracked() -> list[str]:
    # Tracked files AND new ones not yet committed (but never ignored ones):
    # tracked-only would leave a file out of the map until its first commit,
    # and the map would then be stale the moment it was committed.
    out = subprocess.run(["git", "ls-files", "--cached", "--others",
                          "--exclude-standard"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    return sorted({p for p in out if p.endswith(KINDS) and (ROOT / p).is_file()})


def build() -> str:
    files = tracked()
    seen: set[str] = set()
    lines = [
        "# Every file, and what it does",
        "",
        "**Generated by `scripts/gen_file_map.py` -- do not edit by hand.** Each",
        "description is the first line the file writes about itself (a module",
        "docstring, a first heading, a first comment). If one is wrong, fix the file",
        "and run `python scripts/gen_file_map.py`. `tests/test_file_map.py` fails",
        "when this is out of date.",
        "",
        "For the order these are called in during a run, see",
        "[RUN-SEQUENCE.md](RUN-SEQUENCE.md). For which document explains each part,",
        "see [the documentation map](README.md).",
        "",
    ]
    summary_hit = {k: 0 for k in SUMMARISED}
    for f in files:
        for k in SUMMARISED:
            if f.startswith(k):
                summary_hit[k] += 1
                seen.add(f)
    docs_other = [f for f in files if f.startswith("docs/") and f not in seen
                  and f != "docs/FILES.md"]
    for title, prefixes, blurb in GROUPS:
        members = [f for f in files if f not in seen and not f.startswith("docs/")
                   and any(f == p or f.startswith(p) for p in prefixes)]
        if not members:
            continue
        lines += [f"## {title}", "", blurb, "", "| File | What it does |", "|---|---|"]
        for f in members:
            seen.add(f)
            lines.append(f"| `{f}` | {describe(ROOT / f)} |")
        lines.append("")
    rest = [f for f in files if f not in seen and not f.startswith("docs/")]
    if rest:
        lines += ["## Everything else", "", "| File | What it does |", "|---|---|"]
        lines += [f"| `{f}` | {describe(ROOT / f)} |" for f in rest]
        lines.append("")
    lines += ["## Documentation", "", "| File | Title |", "|---|---|"]
    lines += [f"| `{f}` | {describe(ROOT / f)} |" for f in docs_other]
    # No file counts: they would depend on untracked files in a checkout (a
    # researcher's uncommitted hypotheses), and the map would differ between a
    # working copy and a fresh clone.
    lines += ["", "## Summarised, not listed", "", "| Folder | What it holds |",
              "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in SUMMARISED.items() if summary_hit[k]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = build()
    if a.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print("docs/FILES.md is out of date: run python scripts/gen_file_map.py")
            return 1
        print("docs/FILES.md is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
