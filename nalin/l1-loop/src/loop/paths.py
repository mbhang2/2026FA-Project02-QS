"""Filesystem layout helpers for the per-candidate working directory."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

L1_READY_PATH = PROJECT_ROOT / "Docs" / "reports" / "L1_READY.md"


def candidate_dir(candidate_id: str) -> Path:
    d = PROJECT_ROOT / "candidates" / candidate_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def candidate_file(candidate_id: str, name: str) -> Path:
    return candidate_dir(candidate_id) / name


# Name of the single file the worker builds (the one-file ground rule).
STRATEGY_FILE = "train.py"
