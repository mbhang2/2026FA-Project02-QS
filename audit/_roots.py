"""Where this tree is, found by a marker rather than by counting directories.

**Why this module exists.** Task 44 moved three trees and, between them, broke
**six** relative root walks -- `Path(__file__).resolve().parents[3]` and friends
-- and **not one of them failed loudly**. `download_r2.py` went looking for
`secrets/r2.env` in the wrong directory and would have printed "please fill this
out"; the fixture calendar registry pointed at a directory that does not exist,
so every committed split would have read as "no pre-registered calendar". One
constant in `loop/paths.py` has now been corrected three separate times, twice by
a task whose purpose was something else entirely.

A depth count is a fact about where a file sits **today**. Any move invalidates
it, the invalidation is silent, and the failure surfaces somewhere else looking
like a configuration problem.

**What replaces it.** Two anchors, neither of them a distance:

* `repo_root()` walks up for a marker -- `.git`, or `run.py` beside
  `pyproject.toml` for a checkout without one. Immune to this file moving.
* `tree_root()` is `repo_root() / 'audit'` -- a **name**. Moving this tree means
  editing one string, and a stale string raises at import rather than quietly
  resolving somewhere wrong.

Three near-identical copies of this live in `loop/`, `audit/` and
`alternatives/l1_engine/`, and the duplication is deliberate. These trees import
nothing from each other and nothing from `src.core`; that independence is what
lets any of them be replaced without touching the others, and it is worth more
than twenty lines. A shared helper would also have to sit at the repository root
under a generic name -- the exact hazard Task 44 spent a commit removing.
"""

from __future__ import annotations

from pathlib import Path

TREE = 'audit'


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
        if (d / "run.py").is_file() and (d / "pyproject.toml").is_file():
            return d
    raise RuntimeError(
        f"no repository marker above {here}: looked for .git, and for run.py "
        "beside pyproject.toml"
    )


def tree_root() -> Path:
    root = repo_root() / TREE
    if not root.is_dir():
        raise RuntimeError(
            f"{TREE!r} is not a directory under {repo_root()}. This tree was "
            "renamed or moved and TREE in this file was not updated -- which is "
            "the loud failure this module exists to produce instead of a "
            "silently wrong path."
        )
    return root
