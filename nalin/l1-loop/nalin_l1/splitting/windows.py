"""Train / validation / held-out decay split — read, never derived.

**This module used to compute the split from the `ExperimentSpec` at run time.**
`split_periods(spec)` took `spec.start`, `spec.train_end`, `spec.val_end` and
`spec.end` and cut three contiguous windows out of them, with the comment that
the holdout "exists on *every* experiment by construction, because it comes from
the spec".

That is the thing this project most specifically does not do. A split chosen
while a run is in progress is chosen after the data has been seen, whatever the
intention; `config/calendars/nq-trades.yaml` says it in the file itself — *the
pre-registration date is the commit date* — and `CLAUDE.md` lists the calendar
first among the things not to decide unilaterally. There is no version of this
project in which the split is computed during a run.

So the split is now **read from a committed calendar**, and an experiment with no
registered calendar is refused rather than served a derived one. Two registries
are searched, in order:

1. `config/calendars/<name>.yaml` — the harness's, and the one a real dataset
   belongs in.
2. `nalin/l1-loop/calendars/<name>.yaml` — this engine's own, for datasets that
   exist only to give it something to walk.

The second exists because the first is inside `ARTIFACT_GLOBS`: adding a file
there changes `artifact_digests()` and re-dates the provenance on every
committed ledger row. A placeholder dataset has no business doing that.

**The embargo is now real.** The derived split had validation beginning on the
exact day training ended — adjacent observations on either side of a boundary,
which is the leak the harness's `Calendar` constructor refuses outright. A gap
between windows is not an inconvenience; it is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from nalin_l1.config.experiment_spec import ExperimentSpec

# `nalin/l1-loop/`, then the repository above it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent

REGISTRIES = (
    REPO_ROOT / "config" / "calendars",     # the harness's; real datasets
    PROJECT_ROOT / "calendars",             # this engine's; fixtures
)


class NoRegisteredCalendar(FileNotFoundError):
    """No committed split exists for this dataset, so the run does not start.

    Deliberately not a fallback. The alternative — deriving a split from the
    spec — is what this module was changed to stop doing.
    """


@dataclass(frozen=True)
class DateRange:
    start: pd.Timestamp
    end: pd.Timestamp


def calendar_path(name: str) -> Path:
    for registry in REGISTRIES:
        candidate = registry / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(r / f"{name}.yaml") for r in REGISTRIES)
    raise NoRegisteredCalendar(
        f"no pre-registered calendar for dataset {name!r}. Searched:\n  {searched}\n"
        "A split is committed by a human before outcomes are observed; it is not "
        "derived from the spec at run time. Write the calendar, commit it, and "
        "run again."
    )


def registered_split(name: str) -> tuple[DateRange, DateRange, DateRange]:
    """`(train, validation, holdout)` exactly as committed, embargo included."""
    doc = yaml.safe_load(calendar_path(name).read_text())
    splits = doc["splits"]
    out = []
    for key in ("train", "validation", "holdout"):
        window = splits[key]
        out.append(DateRange(pd.Timestamp(window["start"], tz="UTC"),
                             pd.Timestamp(window["end"], tz="UTC")))
    train, validation, holdout = out
    for earlier, later in ((train, validation), (validation, holdout)):
        if later.start <= earlier.end:
            raise ValueError(
                f"{calendar_path(name)} has windows that touch or overlap: "
                f"{earlier.end.date()} -> {later.start.date()}. Adjacent "
                "observations across a split boundary are the leak the embargo "
                "exists to prevent."
            )
    return train, validation, holdout


def split_periods(spec: ExperimentSpec) -> tuple[DateRange, DateRange, DateRange]:
    """The committed split for `spec.calendar`.

    The spec's own `start` / `train_end` / `val_end` / `end` are no longer the
    source of the split. They remain on the spec as the window of data to load,
    and they are **checked** against the calendar rather than trusted: a spec
    that asks for data the calendar's holdout runs past is a spec that would
    silently score on bars it does not have.
    """
    if not getattr(spec, "calendar", ""):
        raise NoRegisteredCalendar(
            "ExperimentSpec.calendar is empty. Name the dataset whose "
            "pre-registered calendar this experiment runs against; the split is "
            "not derived from the spec's dates."
        )
    train, validation, holdout = registered_split(spec.calendar)
    if holdout.end > pd.Timestamp(spec.end, tz="UTC"):
        raise ValueError(
            f"calendar {spec.calendar!r} holds out to {holdout.end.date()} but "
            f"the spec loads data only to {spec.end}. The holdout would be "
            "scored on bars that were never loaded."
        )
    return train, validation, holdout
