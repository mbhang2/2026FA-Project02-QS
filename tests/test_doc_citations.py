"""Every repository path cited in the standing documentation resolves.

**The fourth invariant this project found living only in prose.** The first three
were the split calendar, the import boundary and the depth-counted roots; each was
enforced only by somebody remembering, and each broke. This one broke twice in the
same week:

* `docs/plans/l1-build-plan.md` was deleted by accident in `a2e72e7` ("Clean
  working tree before team-branch merge experiment"), and **four documents went on
  citing it for eight days** — `CLAUDE.md` twice, `README.md`, and
  `docs/datasets/nq-es-qqq.md`. Worse, the build-order numbering that `CLAUDE.md`
  and `docs/design/loop.md` both mean when they say "step 7" exists in no other
  file, so the citation was not decorative: following it was the only way to learn
  what step 7 *was*.
* `docs/design/repo-layout.md` specified `tests/alternatives/`, which the
  execution had to rename to `tests/l1_engine/` because an `__init__.py` there
  shadows the real `alternatives` package.

A test asserting `architecture.md` names every module has existed since Task 12
and catches the opposite direction — code with no prose. This is the direction it
does not cover: prose with no code.

## Scope, and why it stops where it does

The **standing** documentation: the two root files, and `docs/{design,agent,setup,
datasets,plans}`. These describe the system as it is now, so a path in them that
does not resolve is simply wrong.

`docs/reviews/**` and `docs/tasks/**` are deliberately out of scope. They are
historical records of what was true when a task ran, and they legitimately cite
paths that no longer exist or never existed here at all — `docs/reviews/task45/`
discusses `Gowtam_L1/src/loop/committee_step.py`, which lives on another branch.
Rewriting a review to keep a checker happy would be falsifying the record to
protect a test, which is the wrong way round.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCOPE_FILES = ("CLAUDE.md", "README.md", "AGENTS.md")
SCOPE_DIRS = ("docs/design", "docs/agent", "docs/setup", "docs/datasets",
              "docs/plans")

# Top-level names that make a backticked token a repository path rather than a
# module path, a shell word or a fraction.
TOP_LEVEL = {
    "src", "scripts", "config", "data", "runs", "docs", "tests", "loop",
    "audit", "alternatives", "research", "secrets", "agent-patterns",
    ".github",
}

# Cited on purpose, and absent on purpose. Each entry states which, because an
# allowlist without reasons becomes the place failures go to be forgotten.
ABSENT_BY_DESIGN: dict[str, str] = {
    "research/audit_db.json":
        "tier 3's audit ledger, written on the first audit. `audit/config.py` "
        "names it as DEFAULT_LEDGER; it has never been committed, on this "
        "branch or on the one audit/ arrived from.",
    "tests/alternatives/":
        "cited precisely to say it is NOT the name. An `__init__.py` there "
        "makes `tests.alternatives` a package that shadows the real one, which "
        "is why the directory is `tests/l1_engine/`.",
    "data/papers/manual/":
        "designed, not built. The manual-deposit path for SSRN, which has no "
        "public API and whose terms do not permit scraping "
        "(`docs/design/paper-retrieval.md` stage 0).",
    "config/calendar.yaml":
        "the name the v4 plan used, quoted in `docs/datasets/nq-es-qqq.md` as "
        "history. The file is `config/calendars/nq-trades.yaml` and always was "
        "under that name in this repo.",
}

_TOKEN = re.compile(r"`([^`\n]+)`")
# `NN` is not at a word boundary in `docs/reviews/taskNN/`, which is exactly the
# shape it appears in, so this is a substring test and not \bNN\b.
_PLACEHOLDER = re.compile(r"[<>{}]|NN|\bN\b")

# `docs/plans/` holds two kinds of file. `l1-build-plan.md` and
# `00-README-FIRST.md` are standing documents. `task39-...md` and
# `CLAUDE_TASK.md` are task artifacts -- a mapping written against the tree as
# it stood before Task 44 moved it, and the scratch file briefs are pasted into
# -- and they belong with `docs/tasks/` and `docs/reviews/`, out of scope.
_HISTORICAL = ("task", "CLAUDE_TASK")


def _scope() -> list[Path]:
    out = [REPO_ROOT / f for f in SCOPE_FILES]
    for d in SCOPE_DIRS:
        out.extend(p for p in sorted((REPO_ROOT / d).glob("*.md"))
                   if not p.name.startswith(_HISTORICAL))
    return [p for p in out if p.is_file()]


def _citations(text: str) -> set[str]:
    """Backticked tokens that are repository paths, and only those."""
    found = set()
    for match in _TOKEN.finditer(text):
        token = match.group(1).strip()
        if "/" not in token or token.startswith(("http", "#", "/")):
            continue
        # A path has no spaces, and no shell or prose punctuation. This drops
        # command lines, alternations like `a/b | c/d`, and `and/or`.
        if any(c in token for c in " ()|,'\"$"):
            continue
        if token.split("/")[0] not in TOP_LEVEL:
            continue
        # `config/hypotheses/<name>.md`, `docs/reviews/taskNN/`,
        # `candidates/{id}/train.py`: a shape, not a path.
        if _PLACEHOLDER.search(token):
            continue
        found.add(token)
    return found


def test_every_cited_path_resolves():
    broken: list[str] = []
    for doc in _scope():
        rel_doc = doc.relative_to(REPO_ROOT)
        for cited in sorted(_citations(doc.read_text(encoding="utf-8"))):
            if cited in ABSENT_BY_DESIGN:
                continue
            if "*" in cited:
                if not glob.glob(str(REPO_ROOT / cited), recursive=True):
                    broken.append(f"{rel_doc}: `{cited}` matches no file")
                continue
            if not (REPO_ROOT / cited.rstrip("/")).exists():
                broken.append(f"{rel_doc}: `{cited}` does not exist")

    assert not broken, (
        "documentation cites paths that are not there:\n  "
        + "\n  ".join(broken)
        + "\n\nFix the path, restore the file, or -- if it is absent on purpose "
        "-- add it to ABSENT_BY_DESIGN in this file WITH THE REASON. "
        "`docs/plans/l1-build-plan.md` was deleted by accident and cited by four "
        "documents for eight days; this check is why that cannot happen twice."
    )


def test_the_allowlist_has_not_rotted():
    """An entry whose file now exists is an entry that should be deleted.

    Otherwise the allowlist quietly becomes permission to be wrong about paths
    that are, in fact, fine.
    """
    stale = [p for p in ABSENT_BY_DESIGN
             if (REPO_ROOT / p.rstrip("/")).exists()]
    assert not stale, (
        f"these exist now and no longer need an exemption: {stale}")


def test_the_allowlist_is_still_cited():
    """And an entry nobody cites any more is also an entry to delete."""
    everything = "\n".join(p.read_text(encoding="utf-8") for p in _scope())
    cited = _citations(everything)
    unused = [p for p in ABSENT_BY_DESIGN if p not in cited]
    assert not unused, (
        f"no document cites these any more; remove the exemption: {unused}")


def test_the_build_plan_is_present_and_carries_the_step_numbering():
    """The specific failure this test was written for, asserted by name.

    `CLAUDE.md` and `docs/design/loop.md` both say "step 7 of the build plan".
    That numbering exists in exactly one file, and for eight days the file was
    not in the repository.
    """
    plan = REPO_ROOT / "docs" / "plans" / "l1-build-plan.md"
    assert plan.is_file(), "the build plan is gone again"
    text = plan.read_text()
    assert "## Build order" in text
    for n in range(1, 10):
        assert re.search(rf"^{n}\. \*\*\[", text, re.M), (
            f"build order item {n} is missing; CLAUDE.md and loop.md cite this "
            "numbering by number")


def test_the_step_7_claim_agrees_across_documents():
    """CLAUDE.md said the committee tier did not exist while loop.md said it did
    and named `audit/`. They disagreed for eight days and shipped that way into
    PR #1."""
    claude = (REPO_ROOT / "CLAUDE.md").read_text()
    loop = (REPO_ROOT / "docs" / "design" / "loop.md").read_text()
    assert "does not exist" not in claude.split("committee tier")[1][:200], (
        "CLAUDE.md says the committee tier does not exist; loop.md says it is "
        "`audit/` and has been since Task 42")
    assert "step 7" in loop
