"""Where the repository is, for the harness. A marker, never a directory count.

The fourth copy of this file, and the duplication is still deliberate: `loop/`,
`audit/` and `alternatives/l1_engine/` import nothing from each other or from
here, and a shared helper at the repo root under a generic name is the coupling
Task 44 removed. See `loop/_roots.py` for the longer version of that argument.

**Why this one arrived three tasks later than the others.** Task 45 fixed the six
root walks that broke when Task 44 moved three trees, and deliberately left the
nine under `src/core/**` alone, on the belief that touching them would restate
`code_fingerprint` and make every committed ledger row's recorded fingerprint
stale. Task 46 established that the belief was wrong: `scripts/ledger_check.py`
compares a row's fingerprint against the one recomputed from the tree at the
commit **that row names**, by `git archive <sha>` into a scratch directory, and
that comparison never touches HEAD. A committed row cannot be staled by later
edits. There was no boundary cost, so there was no reason left to keep nine
modules resolving the repository by counting directories at three different
depths -- `parents[2]`, `parents[3]` and `parents[4]`, which is itself the
evidence that the count is a fact about where a file sits rather than about
where the repository is.

**One module does not use this and cannot.**
`src/core/engine/remote/child.py` computes the root *in order to put it on
`sys.path`*; it is the bootstrap, so there is nothing importable yet by
definition. It carries the same marker walk inline instead, and
`tests/test_no_depth_counted_roots.py` checks that it is a walk and not a count.
"""

from __future__ import annotations

from pathlib import Path

TREE = 'src/core'


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
        if (d / "run.py").is_file() and (d / "pyproject.toml").is_file():
            return d
    raise RuntimeError(
        f"no repository marker above {here}: looked for .git, and for run.py "
        "beside pyproject.toml")


def tree_root() -> Path:
    root = repo_root() / TREE
    if not root.is_dir():
        raise RuntimeError(
            f"{TREE!r} is not a directory under {repo_root()}. This tree was "
            "renamed or moved and TREE in this file was not updated -- which is "
            "the loud failure this module exists to produce instead of a "
            "silently wrong path.")
    return root
