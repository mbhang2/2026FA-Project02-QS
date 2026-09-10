"""The `Dataset` protocol and the split machinery that enforces pre-registration.

A dataset is the only way data enters the simulator, and it is where the
chronological split, the embargo and the holdout lock are enforced -- not
requested. The NQ/ES/QQQ adapter and the synthetic generator are both just
implementations of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol, Sequence

from ..types import DataCapabilities, HoldoutLocked, Split, Tick, Ts


@dataclass(frozen=True, slots=True)
class SplitBounds:
    """Half-open in session index: [start, stop)."""

    start: int
    stop: int

    def __post_init__(self) -> None:
        if self.stop < self.start:
            raise ValueError(f"empty or reversed split bounds {self.start}..{self.stop}")

    def __len__(self) -> int:
        return self.stop - self.start

    def contains(self, session: int) -> bool:
        return self.start <= session < self.stop


@dataclass(frozen=True)
class Calendar:
    """Pre-registered, chronological, never random.

    Random splits leak: adjacent observations are correlated, so a randomly
    held-out point sits between two training points and is nearly free to
    predict. The embargo is at least the longest feature lookback plus the
    longest holding period, and the simulator enforces the gap rather than
    trusting the split boundaries to be far enough apart.
    """

    train: SplitBounds
    validation: SplitBounds
    holdout: SplitBounds
    embargo_sessions: int
    n_sessions: int

    def __post_init__(self) -> None:
        order = [self.train, self.validation, self.holdout]
        for a, b in zip(order, order[1:]):
            if b.start < a.stop:
                raise ValueError("splits must be chronological and non-overlapping")
            if b.start - a.stop < self.embargo_sessions:
                raise ValueError(
                    f"embargo of {self.embargo_sessions} session(s) not satisfied "
                    f"between {a.stop} and {b.start}"
                )
        if self.holdout.stop > self.n_sessions:
            raise ValueError("holdout runs past the end of the data")

    def bounds(self, split: Split) -> SplitBounds:
        return {
            Split.TRAIN: self.train,
            Split.VALIDATION: self.validation,
            Split.HOLDOUT: self.holdout,
        }[split]

    def as_dict(self) -> dict:
        return {
            "train": [self.train.start, self.train.stop],
            "validation": [self.validation.start, self.validation.stop],
            "holdout": [self.holdout.start, self.holdout.stop],
            "embargo_sessions": self.embargo_sessions,
            "n_sessions": self.n_sessions,
        }


CALENDAR_DIR = "config/calendars"


def calendar_path(name: str) -> str:
    """Where the pre-registered split for a dataset lives, derived from its name.

    `Dataset.name` may carry an instrument suffix (`nq-trades:NQ`); the calendar is
    per dataset, not per instrument, so the suffix is dropped. Derived rather than
    written down because the adapter, the error message it raises and the file a
    human is meant to create had already drifted to two different names -- and the
    only symptom of that drift is a pre-registration nobody reads.
    """
    return f"{CALENDAR_DIR}/{name.split(':', 1)[0]}.yaml"


class Dataset(Protocol):
    name: str

    def capabilities(self) -> DataCapabilities: ...
    def calendar(self) -> Calendar: ...
    def ticks(self, split: Split, *, warmup: int = 0) -> Iterator[Tick]: ...
    def session_bounds(self, session: int) -> tuple[Ts, Ts]: ...
    def digest(self) -> str: ...


class HoldoutLock:
    """One-way, in-process, and the reason lands in the run record.

    Cannot be re-locked: a flag that can be toggled back is a flag someone
    toggles back.
    """

    __slots__ = ("_unlocked", "_reason")

    def __init__(self) -> None:
        self._unlocked = False
        self._reason = ""

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    @property
    def reason(self) -> str:
        return self._reason

    def unlock(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("unlocking the holdout requires a reason for the record")
        self._unlocked = True
        self._reason = reason.strip()

    def check(self, split: Split) -> None:
        if split is Split.HOLDOUT and not self._unlocked:
            raise HoldoutLocked(
                "the holdout is not served until it is explicitly unlocked, once. "
                "Call dataset.unlock_holdout(reason) and expect the reason to appear "
                "in the run record."
            )


def warmup_start(bounds: SplitBounds, warmup: int) -> int:
    """Warmup reaches backward across a boundary; decisions never reach forward.

    At the first served session the strategy needs lookback history, and that
    history legitimately comes from before the split start. Starting cold instead
    would waste warmup-length data out of the scarce resource.
    """
    if warmup < 0:
        raise ValueError("warmup must be non-negative")
    return max(0, bounds.start - warmup)


def sessions_for(
    calendar: Calendar, split: Split, warmup: int
) -> tuple[Sequence[int], Sequence[int]]:
    """(warmup sessions, actionable sessions) for a split."""
    b = calendar.bounds(split)
    start = warmup_start(b, warmup)
    return range(start, b.start), range(b.start, b.stop)
