# Your branch needs re-pointing

**The trunk is now `MVP`.** Not `main` (behind it; PR #1 is open), and not
`merge-test-michael`, which was a bad name for something that became the trunk
and is now just a stale pointer at the same commit.

Task 44 dissolved the name directories. `Gowtam/`, `nalin/` and `daksh/` do not
exist on the trunk. A directory is a role now, never a person — git already
records who wrote something, and what a reader actually needs to know is whether
a file is on the scored path. `docs/design/repo-layout.md` is the full map.

```
Gowtam/L1/          ->  loop/                       the agentic driver
daksh/audit_agent/  ->  audit/                      tier 3
nalin/l1-loop/      ->  alternatives/l1_engine/     a second engine, kept, never scored
```

Git recorded these as renames, so `git log --follow <new-path>` still shows your
history and `git blame` still has your name on every line. Nothing was discarded.

---

## What each of you actually needs to do

I checked each branch against the trunk rather than assuming, so these are three
different situations and not one instruction repeated.

### daksh — `user/daksh/audit_agent_mvp`

**Fully contained in the trunk.** Every commit is an ancestor of `MVP`; nothing
of yours is missing. Start new work from the trunk:

```
git fetch origin && git checkout -b user/daksh/<next-thing> origin/MVP
```

Your imports changed shape: `from daksh.audit_agent.config import REPO_ROOT` is
now `from audit.config import REPO_ROOT`. Your eight tests that need demo
fixtures are in the suite and **skipped with a reason naming what they need** —
they were not deleted and not relaxed. If those fixtures exist somewhere, say so
and they go in; they were not invented, because inventing files whose expected
verdicts your own tests assert would be inventing the evidence.

### nalin — `nalin/l1-loop`

**Contained in the trunk, despite what `git log` suggests.** Your branch reads as
5 commits ahead because the trunk absorbed your tree through an unrelated-history
merge, so your commits are not literally ancestors. I checked the content file by
file rather than trusting the commit graph: every file on your branch has a
counterpart on the trunk, and every difference is the trunk *adding* to your
version, never the reverse.

Where things went:

```
src/**            ->  alternatives/l1_engine/**
.opencode/agent/  ->  alternatives/l1_engine/agents/
Data/raw/         ->  alternatives/l1_engine/market_data/raw/
fixtures/, tests/ ->  tests/l1_engine/
hypotheses/, experiments/  ->  research/
```

Imports: `from src.loop import paths` is now
`from alternatives.l1_engine.loop import paths`.

Two changes to your code are worth reading rather than rediscovering:

- **`splitting/windows.py` no longer computes the split.** It reads a committed
  calendar and checks the `ExperimentSpec` dates against it. A split derived at
  run time is not a pre-registration, and that is the one thing this project
  cannot be sloppy about.
- **`serialization/trial_log.py` is not the trial ledger and its count is not N.**
  It writes one artifact per candidate. N is `runs/ledger.jsonl`, global across
  every dataset, and it counts crashed and abandoned runs too.

Your seven tests that need `pydantic` are skipped with that reason. `pydantic` is
not in the lockfile, and installing it moves `environment_fingerprint` on every
committed row — so it is a decision with a cost, not a `pip install`.

### Gowtam — `Gowtam-L1` — **read this one**

**Your branch has 3 commits the trunk does not contain, and they are not a
re-pointing problem.** Tip is 2026-09-14; the trunk never saw them.

They live under `Gowtam_L1/` — with an underscore — which is a *different
directory* from the `Gowtam/L1/` that Task 44 moved into `loop/`. So there is no
path conflict and nothing was overwritten; the work simply is not there. 77 files
of it, including:

```
src/loop/auditor_step.py          .opencode/agent/auditor.md
src/loop/committee_step.py        .opencode/agent/statistician.md
experiments/benchmark_engine.py   Docs/ORCHESTRATION_FLOW.md
```

None of those filenames exists anywhere on the trunk. **A committee tier is step 7
of the build plan and `CLAUDE.md` currently says it does not exist** — if that is
what `committee_step.py` is, the statement is out of date and the work needs to
land rather than sit on a branch.

This needs a real merge and a decision about where it goes, which is a
conversation and not a command I can hand you. Open it as a PR against `MVP` and
say what the auditor, committee and statistician roles each do. The relevant
constraint is that the trunk already has a tier 3 in `audit/`, arrived at
independently, whose verdict type has no member meaning approval — so the first
question is whether these are the same tier or two.

---

## Rules that are new since you branched

1. **One suite, one count.** `pytest -q` at the root runs everything —
   697 passed, 39 skipped as of Task 45. A test that cannot pass is skipped with a
   reason naming what it needs. None is deleted, none is weakened, and a smaller
   number achieved by deletion is not an improvement.
2. **Don't find a path by counting directories.** No
   `Path(__file__).resolve().parents[3]`. Each tree has a `_roots.py` with
   `repo_root()` and `tree_root()`. Six of these broke silently when Task 44 moved
   things — one of them would have made every committed split read as "no
   pre-registered calendar" without raising — and `tests/test_no_depth_counted_roots.py`
   now fails the build if a seventh appears.
3. **The tiers talk through files, never imports.** `loop/` writes the strategy
   file and invokes `run.py`. `audit/` reads `tier2-summary.json` and writes
   `tier3-verdict.json`. Neither imports `src.core`, and nothing imports `audit`.
   If your change needs an import across that line, the change is wrong.
4. **`src/core/**`, `run.py`, `config/**`, `data/**` are fingerprinted.** Editing
   them restates `code_fingerprint` and makes every committed ledger row's
   recorded fingerprint stale. Task 44 moved 1,200 files and touched none of them,
   deliberately. If your work needs to, that is a conversation first.
5. **Stage by name, never `git add .`.** `secrets/` is gitignored, and one typo is
   the difference between that holding and a credential being in history forever.

## Checking your own branch instead of taking my word for it

```
git fetch origin
git merge-base --is-ancestor origin/<your-branch> origin/MVP && echo contained || echo "has commits the trunk lacks"
git log --oneline origin/<your-branch> ^origin/MVP
```
