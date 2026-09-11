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
        str(p.relative_to(REPO_ROOT))
        for p in SRC.rglob("*.py")
        if p.name != "__init__.py"
    )


def packages() -> list[str]:
    return sorted(
        str(p.parent.relative_to(REPO_ROOT)) + "/"
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
    text = DOC.read_text()
    missing = [m for m in modules() if m not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd it to the section it belongs to. If it genuinely needs no "
        "explanation, say that in one line -- an unexplained module is the thing "
        "this check exists to surface."
    )


def test_architecture_doc_names_every_package():
    text = DOC.read_text()
    missing = [p for p in packages() if p not in text]
    assert not missing, (
        "docs/design/architecture.md does not mention these packages:\n  "
        + "\n  ".join(missing)
    )


def test_the_doc_links_rather_than_restating():
    """The brief for this document was 'a map, not a duplicate'. The linked
    documents are the authority on rationale; if a link disappears the map has
    probably started absorbing what it was supposed to point at."""
    text = DOC.read_text()
    for target in (
        "docs/design/simulator-interface.md",
        "docs/design/metric.md",
        "docs/datasets/nq-es-qqq.md",
        "docs/agent/strategy-api.md",
        "docs/agent/data.md",
    ):
        assert target in text, f"architecture.md no longer points at {target}"
