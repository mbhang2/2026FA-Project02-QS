"""Filesystem layout helpers for the per-candidate working directory."""

from __future__ import annotations

from pathlib import Path

# Two `parent`s from `alternatives/l1_engine/<sub>/<file>.py` is the
# package root. It was three when this lived at
# `nalin/l1-loop/nalin_l1/`, which had one more level above it (Task 44).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Was `PROJECT_ROOT / "Docs"` -- capital D against the repo's lowercase `docs/`,
# which case-resolves on macOS and does not on Linux. Corrected here.
#
# **The target still does not exist**, and never has: `L1_READY.md` appears in
# no branch of this repository. It is left pointing where its author meant it to
# point rather than re-aimed at some other document, because guessing what the
# planner was supposed to read is not a filing decision. `context_files()`
# below makes the absence visible instead of silent.
L1_READY_PATH = PROJECT_ROOT / "docs" / "reports" / "L1_READY.md"


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
    d = PROJECT_ROOT / "candidates" / candidate_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def candidate_file(candidate_id: str, name: str) -> Path:
    return candidate_dir(candidate_id) / name


# Name of the single file the worker builds (the one-file ground rule).
STRATEGY_FILE = "train.py"
