"""Filesystem layout helpers for the per-candidate working directory."""

from __future__ import annotations

from pathlib import Path
from loop._roots import repo_root, tree_root

# Anchored on a marker and a name, never on a depth (see `loop/_roots.py`).
# This constant was wrong three separate times when it counted directories.
PROJECT_ROOT = tree_root()
REPO_ROOT = repo_root()

# Was `PROJECT_ROOT / "Docs" / "References" / "context-api.md"` -- capital D
# against the repo's lowercase `docs/`, and a path that has never existed in any
# branch. On macOS `Docs` case-resolves to `docs` and on Linux it does not, so
# the reference read differently depending on where the loop ran; under
# `PROJECT_ROOT` it resolved to nothing either way, and `invoke_opencode` passes
# a missing `-f` path to OpenCode without complaint.
#
# The generated context API reference is a real document in this repo and is
# exactly what the name asks for.
CONTEXT_API_PATH = REPO_ROOT / "docs" / "design" / "context-api.md"


def context_files(*candidates: Path | str) -> list[str]:
    """The ones that exist, as strings; anything missing is reported.

    `invoke_opencode` passes each of these to `opencode run -f`, and a path that
    is not there is accepted without complaint -- so a dangling reference means
    the agent silently runs with less context than the code says it has. Both
    `Docs/` paths in this repository were dangling in exactly that way, one of
    them for its whole history, and nothing surfaced it.
    """
    import sys

    out = []
    for c in candidates:
        path = Path(c)
        if path.is_file():
            out.append(str(path))
        else:
            print(f"context file missing, not passed to the agent: {path}",
                  file=sys.stderr)
    return out


def candidate_dir(candidate_id: str) -> Path:
    # In integration, this might point to a temp build dir, 
    # but the final file must land in src/strategies/current.py
    d = PROJECT_ROOT / "candidates" / candidate_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def candidate_file(candidate_id: str, name: str) -> Path:
    return candidate_dir(candidate_id) / name


# Name of the single file the worker builds (the one-file ground rule).
STRATEGY_FILE = "current.py"
