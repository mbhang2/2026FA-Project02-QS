"""Structured trial-result serialization (Task 4.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nalin_l1.config.experiment_spec import ExperimentSpec


def write_trial_log(
    result: dict[str, Any], spec: ExperimentSpec, output_path: str
) -> str:
    """Write a structured trial log record and return its filesystem path.

    Includes placeholder fields for information L1 does not produce yet but that
    later stages (debate gate, KB-2, committee) will need — reserving the shape of
    the eventual results-knowledge-base entry now, so later stages extend this
    schema rather than migrate it.
    """
    record = {
        "hypothesis": None,
        "source_paper_ref": None,
        "structural_fingerprint": None,
        "verdict": None,
        "spec": spec.to_dict(),
        "result": result,
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2, allow_nan=False)
    return str(path)
