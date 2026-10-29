"""Every module under `src/` must be named in `docs/design/architecture.md`.

Same principle as the generated references: make staleness fail loudly rather
than rot quietly. A map that silently stops covering the territory is worse than
no map, because it is still read.

This one cannot be generated -- the whole value of the document is the prose
about *why* each part exists, which no generator can write. So the check is the
weaker, honest version: the doc must at least mention every module. It cannot
tell you the description is still true; it can tell you a module was added and
nobody wrote one.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "design" / "architecture.md"
SRC = REPO_ROOT / "src"


def modules() -> list[str]:
    """Repo-relative paths of every module under `src/`, `__init__.py` aside.

    Package `__init__.py` files are covered by the package requirement below
    instead: they are re-export surfaces, and naming each one in the prose would
    be noise. The one that carries a real decision -- `src/core/scoring/`
    re-exporting nothing -- is discussed there under its package.
    """
    return sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in SRC.rglob("*.py")
        if p.name != "__init__.py"
    )


def packages() -> list[str]:
    return sorted(
        p.parent.relative_to(REPO_ROOT).as_posix() + "/"
        for p in SRC.rglob("__init__.py")
    )


def test_the_module_list_is_not_empty():
    """A guard on the guard: this whole file passes vacuously if the glob breaks,
    and a restructure is exactly when a glob breaks."""
    mods = modules()
    assert len(mods) >= 12, mods
    assert "src/core/engine/engine.py" in mods
    assert "src/core/scoring/metrics.py" in mods
    assert "src/strategies/current.py" in mods


def test_architecture_doc_names_every_module():
    text = DOC.read_text(encoding="utf-8")
    missing = [m for m in modules() if m not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd it to the section it belongs to. If it genuinely needs no "
        "explanation, say that in one line -- an unexplained module is the thing "
        "this check exists to surface."
    )


def source_trees() -> list[str]:
    """Top-level directories holding Python that is not `src/`, `scripts/` or
    `tests/` — i.e. the ones the two checks above cannot see."""
    import subprocess

    # `git ls-files`, not `rglob`: the map documents the repository, and
    # `runs/tmp-*/` holds generated strategy sources that are gitignored on
    # purpose. Asking the filesystem would put scratch in the map.
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True).stdout.split()
    out = set()
    for rel in (Path(x) for x in tracked):
        top = rel.parts[0]
        if top in {"src", "scripts", "tests"} or len(rel.parts) == 1:
            continue                     # covered by the two checks above
        out.add("/".join(rel.parts[:2]) if len(rel.parts) > 2 else top)
    return sorted(out)


def test_architecture_doc_names_every_source_tree():
    """The two checks above glob `src/` and `scripts/`. Nothing globbed anything
    else — so when the `merge-test-michael` merge added `Gowtam/L1/` and
    `alternatives/l1_engine/`, **86 Python files across two top-level directories landed
    outside the map and outside the check that exists to notice that.**

    Same shape as the defect Task 36 found in `test_examples.py`: a completeness
    check pointed at the wrong paths reports clean while the thing it guards
    rots. This one asks the weaker, honest question — is each tree named at all
    — because demanding a line per file would be 86 lines of noise, and a tree
    nobody documented is what actually goes wrong.
    """
    text = DOC.read_text(encoding="utf-8")
    missing = [t for t in source_trees() if t not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention:\n  "
        + "\n  ".join(missing)
        + "\n\nA top-level tree holding Python belongs in the map, even if the "
        "answer is 'this is not harness code and nothing imports it' — that is "
        "the sentence a reader needs."
    )


def test_architecture_doc_names_every_script():
    """Scripts drifted out of the map before the modules did: `session_closes.py`
    was added in Task 18 and the build-time table did not grow with it. The table
    is the only place that says what a script produces and who consumes it, so a
    script missing from it is a committed artifact nobody can trace."""
    text = DOC.read_text(encoding="utf-8")
    scripts = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "scripts").glob("*.py")
    )
    assert len(scripts) >= 8, scripts
    missing = [s for s in scripts if s not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention:\n  " + "\n  ".join(missing)
        + "\n\nAdd it to the build-time table with what it produces and what "
        "consumes it."
    )


def test_architecture_doc_names_every_package():
    text = DOC.read_text(encoding="utf-8")
    missing = [p for p in packages() if p not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention these packages:\n  "
        + "\n  ".join(missing)
    )


def test_the_doc_links_rather_than_restating():
    """The brief for this document was 'a map, not a duplicate'. The linked
    documents are the authority on rationale; if a link disappears the map has
    probably started absorbing what it was supposed to point at."""
    text = DOC.read_text(encoding="utf-8")
    for target in (
        "docs/design/simulator-interface.md",
        "docs/design/metric.md",
        "docs/datasets/nq-es-qqq.md",
        "docs/agent/strategy-api.md",
        "docs/agent/data.md",
    ):
        assert target in text, f"architecture.md no longer points at {target}"
