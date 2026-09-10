"""Datasets: the protocol, the adapters, and the DBN reading rules.

Consumed during a trial. The committed artifacts these read -- `data/manifest.json`,
the published session list, the contract-selection file -- are produced by
`scripts/`, which is deliberately not importable from here: if a trial could
re-derive coverage it could disagree with the manifest, and the manifest is what
makes a ledger row mean something.
"""

from .dataset import (
    Calendar,
    Dataset,
    HoldoutLock,
    SplitBounds,
    calendar_path,
    sessions_for,
)
from .synthetic import SyntheticDataset, SyntheticSpec

__all__ = [
    "Calendar",
    "Dataset",
    "HoldoutLock",
    "SplitBounds",
    "SyntheticDataset",
    "SyntheticSpec",
    "calendar_path",
    "sessions_for",
]
