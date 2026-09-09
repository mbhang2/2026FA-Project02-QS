"""Train / validation / held-out decay split (Task 2.4)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.config.experiment_spec import ExperimentSpec


@dataclass(frozen=True)
class DateRange:
    start: pd.Timestamp
    end: pd.Timestamp


def split_periods(spec: ExperimentSpec) -> tuple[DateRange, DateRange, DateRange]:
    """Derive ``(train, validation, holdout)`` from an ``ExperimentSpec``.

    Everything after ``val_end`` up to ``end`` is the held-out decay window. The
    holdout exists on *every* experiment by construction, because it comes from
    the spec — a strategy author never has to remember to request it.
    """
    start = pd.Timestamp(spec.start, tz="UTC")
    train_end = pd.Timestamp(spec.train_end, tz="UTC")
    val_end = pd.Timestamp(spec.val_end, tz="UTC")
    end = pd.Timestamp(spec.end, tz="UTC")

    train = DateRange(start, train_end)
    validation = DateRange(train_end, val_end)
    holdout = DateRange(val_end, end)
    return (train, validation, holdout)
