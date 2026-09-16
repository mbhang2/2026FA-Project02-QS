"""No tree outside `src/core/**` finds its root by counting directories.

**Why this exists.** Task 44 moved three trees and broke **six** relative root
walks between them. Not one failed loudly:

* `download_r2.py` looked for `secrets/r2.env` one directory too high, where it
  would have printed "please ensure r2.env is correctly filled out" -- a path bug
  wearing a configuration bug's clothes;
* the fixture calendar registry pointed at a directory that does not exist, so
  every committed split would have read as "no pre-registered calendar";
* `audit/config.py` pointed one level above the repository;
* `loop/paths.py`'s constant was corrected for the **third** time, twice by tasks
  whose purpose was something else.

A depth count is a fact about where a file sits today. Any move invalidates it,
silently, and the damage shows up somewhere else. So the six were replaced with
`_roots.py` in each tree -- a marker for the repository and a *name* for the tree
-- and this is the check that a seventh does not appear.

**It deliberately does not cover `src/core/**`, and that is a finding rather than
an exemption.** Nine modules there count depth, at three different depths
(`parents[2]`, `parents[3]`, `parents[4]`), so moving a scoring module one level
would silently change `REPO_ROOT` for the artifact fingerprint, the ledger and
the remote child. Fixing them edits a **fingerprinted path**: `SOURCE_GLOBS`
covers `src/core/**/*.py`, so the change would restate `code_fingerprint` and
make every committed ledger row's recorded fingerprint stale. That is a
deliberate, costed decision and not one to make inside a cleanup commit.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The trees this covers. `src/core/**` is excluded above, with reasons; `scripts/`
# and `tests/` are flat, one level below the root, so their `parent.parent` is
# not a depth through a hierarchy and has nowhere to drift to.
COVERED = ("loop", "audit", "alternatives")

ROOT_NAME = re.compile(r"(^|_)(root|dir|home|base)$", re.I)
DEPTH_CALL = re.compile(r"parents\[\d+\]|\.parent\.parent")


def _py_files(tree: str) -> list[Path]:
    """Every .py in the tree, read off disk rather than out of the index.

    An earlier draft asked `git ls-files`, and it passed by finding nothing --
    the modules under test were not staged yet. A check on new code that is
    silent until the code is committed is a check that has the failure mode it
    was written to catch.
    """
    files = [p for p in sorted((REPO_ROOT / tree).rglob("*.py"))
             if "__pycache__" not in p.parts]
    assert files, f"{tree}: no Python files found -- did this tree move?"
    return files


def test_the_covered_trees_anchor_on_a_marker_not_a_depth():
    offenders: list[str] = []
    for tree in COVERED:
        for path in _py_files(tree):
            if path.name == "_roots.py":
                continue                     # the marker walk itself lives here
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Assign):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not any(ROOT_NAME.search(n) for n in names):
                    continue
                segment = ast.get_source_segment(source, node.value) or ""
                if DEPTH_CALL.search(segment):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"{names[0]} = {segment}")
    assert not offenders, (
        "a root found by counting directories:\n  " + "\n  ".join(offenders)
        + "\n\nUse this tree's `_roots.py`: `repo_root()` walks up for a marker "
        "and `tree_root()` is a name. Six of these broke silently in Task 44 and "
        "this check exists so the seventh does not."
    )


def test_every_covered_tree_has_a_roots_module_naming_itself():
    for tree in COVERED:
        modules = [p for p in _py_files(tree) if p.name == "_roots.py"]
        assert modules, f"{tree} has no _roots.py"
        for module in modules:
            # Loaded from its path, not exec'd into a bare dict: `repo_root`
            # starts its walk at `__file__`, so a namespace without one tests
            # something other than the module.
            spec = importlib.util.spec_from_file_location(
                f"_roots_under_test_{tree.replace('/', '_')}", module)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            assert (REPO_ROOT / mod.TREE).is_dir(), (
                f"{module.relative_to(REPO_ROOT)} names TREE={mod.TREE!r}, "
                "which is not a directory")
            assert mod.repo_root() == REPO_ROOT
            assert mod.tree_root() == REPO_ROOT / mod.TREE


def test_a_renamed_tree_fails_loudly_rather_than_resolving_somewhere_wrong():
    """The property that makes a name better than a depth: a wrong one raises."""
    import importlib

    roots = importlib.import_module("loop._roots")
    original = roots.TREE
    try:
        roots.TREE = "a-tree-that-does-not-exist"
        try:
            roots.tree_root()
        except RuntimeError as exc:
            assert "was renamed or moved" in str(exc)
        else:
            raise AssertionError("a stale TREE resolved silently")
    finally:
        roots.TREE = original


def test_src_core_still_counts_depth_and_that_is_recorded_not_forgotten():
    """Nine modules, three different depths, behind the fingerprint boundary.

    Asserted so the number cannot quietly grow, and so that fixing them is a
    decision somebody makes rather than a thing nobody remembers.
    """
    found = []
    for path in sorted((REPO_ROOT / "src" / "core").rglob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                segment = ast.get_source_segment(source, node.value) or ""
                if any(ROOT_NAME.search(n) for n in names) and DEPTH_CALL.search(segment):
                    found.append(str(path.relative_to(REPO_ROOT)))
    assert len(found) == 9, (
        f"src/core has {len(found)} depth-counted roots, not the 9 recorded in "
        f"Task 45: {found}. Fixing them changes `code_fingerprint`, which makes "
        "every committed ledger row's recorded fingerprint stale -- a costed "
        "decision, not a cleanup."
    )
