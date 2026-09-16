"""Datasets: the protocol, the adapters, and the DBN reading rules.

Consumed during a trial. The committed artifacts these read -- `data/manifest.json`,
the published session list, the contract-selection file -- are produced by
`scripts/`, which is deliberately not importable from here: if a trial could
re-derive coverage it could disagree with the manifest, and the manifest is what
makes a ledger row mean something.
"""

from .dataset import (
    Calendar,
    CalendarNotWritten,
    Dataset,
    HoldoutLock,
    SplitBounds,
    calendar_path,
    load_calendar,
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
    "CalendarNotWritten",
    "calendar_path",
    "load_calendar",
    "sessions_for",
]
