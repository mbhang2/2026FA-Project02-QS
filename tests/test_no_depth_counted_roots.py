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

**`src/core/**` is covered too, since Task 47.** It was not at first, and the
reason it was not turned out to be wrong, which is worth keeping rather than
quietly deleting. Nine modules there counted depth at three different depths --
`parents[2]`, `parents[3]`, `parents[4]` -- and Task 45 left them alone on the
belief that editing a fingerprinted path would restate `code_fingerprint` and
stale every committed ledger row.

Task 46 measured it instead. `scripts/ledger_check.py` compares a row's
fingerprint against the one recomputed from the tree at the commit **that row
names**, via `git archive <sha>` into a scratch directory; the comparison never
touches HEAD. A committed row cannot be staled by a later edit. With no cost
there was no reason left, and the nine are now eight imports of
`src.core._roots` plus one inline walk.

**The one inline walk is `src/core/engine/remote/child.py`.** It computes the
root *in order to put it on* `sys.path`, so it is the bootstrap and there is
nothing importable at that point by definition. It carries the marker walk
inline, and the test below checks that it is a walk rather than a count -- which
is the property that matters; importing was never the point.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The trees this covers. `scripts/` and `tests/` are flat, one level below the
# root, so their `parent.parent` is not a depth through a hierarchy and has
# nowhere to drift to.
COVERED = ("loop", "audit", "alternatives", "src/core")

# Files inside a covered tree that resolve the root without importing anything,
# because they run before an import is possible. Each one must still WALK.
BOOTSTRAPS = ("src/core/engine/remote/child.py",)

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
            if path.relative_to(REPO_ROOT).as_posix() in BOOTSTRAPS:
                continue                     # checked by its own test, below
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


def test_src_core_counts_depth_nowhere_at_all():
    """Nine, then zero. The number is asserted so it cannot creep back.

    The nine were `runlog.py`, `checks/typecheck.py`, `scoring/integrity.py`,
    `scoring/randomization/profile_null.py`, `engine/remote/child.py`,
    `engine/remote/adapter.py`, `loop/agents.py`, `loop/campaign.py` and
    `loop/llm.py`. Eight import `src.core._roots`; the ninth walks inline and is
    checked by `test_the_bootstrap_walks_rather_than_counts`.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "src" / "core").rglob("*.py")):
        if path.name == "_roots.py":
            continue
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                segment = ast.get_source_segment(source, node.value) or ""
                if any(ROOT_NAME.search(n) for n in names) and DEPTH_CALL.search(segment):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"{names[0]} = {segment}")
    assert offenders == [], (
        "a root found by counting directories under src/core:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `src.core._roots.repo_root()`. There is no fingerprint cost: "
        "ledger_check recomputes a row's fingerprint from the tree at the commit "
        "the row names, never from HEAD, so a committed row cannot be staled by "
        "editing src/core.")


def test_the_bootstrap_walks_rather_than_counts():
    """`child.py` cannot import a helper -- it builds the import path itself.

    So the requirement on it is not "imports `_roots`" but "does not count", and
    that is what is checked: a loop over `parents`, a marker test, and no
    `parents[n]` subscript anywhere in the file.
    """
    for rel in BOOTSTRAPS:
        source = (REPO_ROOT / rel).read_text()
        # Over the AST, not the text. The first draft grepped for the string and
        # failed on the file's own docstring, which quotes the `parents[4]` it
        # replaced -- a check that cannot tell code from prose about code would
        # force the fix to be undocumented to pass.
        subscripted = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
        ]
        assert not subscripted, (
            f"{rel} counts directories at line "
            f"{subscripted[0].lineno if subscripted else '?'}. It is the "
            "bootstrap, so it cannot import `_roots` -- but it can walk, and "
            "walking is the property that survives the file being moved.")
        assert "for d in (here, *here.parents)" in source, (
            f"{rel} no longer contains the marker walk")
        assert '(d / ".git").exists()' in source, (
            f"{rel} no longer tests for a repository marker")


def test_every_module_that_can_import_roots_does_so():
    """The eight. Named, so a tenth arriving by copy-paste is visible."""
    expected = {
        "src/core/checks/typecheck.py",
        "src/core/engine/remote/adapter.py",
        "src/core/loop/agents.py",
        "src/core/loop/campaign.py",
        "src/core/loop/llm.py",
        "src/core/runlog.py",
        "src/core/scoring/integrity.py",
        "src/core/scoring/randomization/profile_null.py",
    }
    for rel in sorted(expected):
        source = (REPO_ROOT / rel).read_text()
        assert "_roots import repo_root" in source, f"{rel} does not import it"
        assert "REPO_ROOT = repo_root()" in source, f"{rel} does not use it"
