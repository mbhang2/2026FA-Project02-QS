"""Reproducibility metadata (PLAN_NEXT B.5).

Captures, at run time, the durable record block every candidate package needs:
random seed, git commit, python/dep versions, dataset hash, and the experiment
config. This is the audit trail the fully-agentic pipeline (no human gate)
depends on — the record *is* the audit trail.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from src.schemas import ApprovedCandidate

FALLBACK_HASH = "n/a"


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return FALLBACK_HASH


def _dataset_hash() -> str:
    # First available tick parquet, for a stable fingerprint of the corpus head.
    try:
        from src.data.ticks import ticks_path

        # Walk the earliest few days; ignore gaps.
        for year in range(2020, 2027):
            for month in range(1, 13):
                for day in range(1, 6):
                    p = ticks_path(year, month, day)
                    if p.exists():
                        return hashlib.md5(open(p, "rb").read(4096)).hexdigest()
    except Exception:
        pass
    return FALLBACK_HASH


def capture_reproducibility(candidate: ApprovedCandidate, seed: int = 0) -> dict:
    """Return the reproducibility block dict, computed at call time."""
    return {
        "random_seed": seed,
        "git_commit": _git_commit(),
        "execution_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python_version": sys.version.split()[0],
        "key_dependencies": {
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "dataset_hash": _dataset_hash(),
        "config": {
            "candidate_id": candidate.candidate_id,
            "asset_ids": candidate.asset_ids,
            "start": candidate.start,
            "train_end": candidate.train_end,
            "val_end": candidate.val_end,
            "end": candidate.end,
        },
    }
