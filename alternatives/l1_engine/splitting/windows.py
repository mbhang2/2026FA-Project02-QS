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
2. `alternatives/l1_engine/calendars/<name>.yaml` — this engine's own, for datasets that
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

from alternatives.l1_engine.config.experiment_spec import ExperimentSpec
from alternatives.l1_engine._roots import repo_root, tree_root

# `alternatives/l1_engine/`, then the repository above it.
# Anchored on a marker and a name, never on a depth (see `_roots.py`). When
# this counted directories the Task 44 move pointed it at `alternatives/`, so
# the fixture registry did not exist and every committed split would have read
# as "no pre-registered calendar".
PROJECT_ROOT = tree_root()
REPO_ROOT = repo_root()

REGISTRIES = (
    REPO_ROOT / "config" / "calendars",     # the harness's; real datasets
    PROJECT_ROOT / "calendars",             # this engine's; fixtures ONLY
)

FIXTURE_REGISTRY = PROJECT_ROOT / "calendars"

# **The fixture registry serves these two datasets and refuses everything else.**
#
# Task 40 put this rule in a header comment in each calendar file, and Task 41
# replaced the comment with a check, for the reason Task 35 found in
# `agent-patterns/` and this project has now been caught by twice: an invariant
# that lives only in prose, with nothing comparing it to the configuration, is
# an invariant that has already drifted and nobody knows yet. The critic sharing
# a model family with the coder was a stated rule with no test; so was the
# scripts table in `architecture.md` before `session_closes.py` fell out of it.
#
# What this buys: the fixture registry exists so two toy datasets -- 240 rows of
# generated placeholder and one session of ES -- do not re-date the artifact
# provenance on every committed ledger row, which is what adding a file to
# `config/calendars/` does. That is a reasonable exemption for fixtures and an
# unreasonable one for anything a result depends on. The first time somebody
# points this engine at real data, they should hit a refusal that names the
# trade, not a convenience that silently accepted it.
FIXTURE_DATASETS = frozenset({"placeholder", "es-20250703"})


class NoRegisteredCalendar(FileNotFoundError):
    """No committed split exists for this dataset, so the run does not start.

    Deliberately not a fallback. The alternative — deriving a split from the
    spec — is what this module was changed to stop doing.
    """


@dataclass(frozen=True)
class DateRange:
    start: pd.Timestamp
    end: pd.Timestamp


class FixtureCalendarRefused(RuntimeError):
    """A dataset outside `FIXTURE_DATASETS` was served from the fixture registry.

    Not a missing-file error: the file is there, and that is the problem.
    """


def audit_fixture_registry() -> list[str]:
    """Calendars sitting in the fixture registry that the allowlist does not name.

    Catches the file being *added*, not only the file being *used* -- a calendar
    committed there for a real dataset is already the mistake, whether or not a
    run has reached it yet.
    """
    if not FIXTURE_REGISTRY.is_dir():
        return []
    return sorted(p.stem for p in FIXTURE_REGISTRY.glob("*.yaml")
                  if p.stem not in FIXTURE_DATASETS)


def calendar_path(name: str) -> Path:
    for registry in REGISTRIES:
        candidate = registry / f"{name}.yaml"
        if not candidate.is_file():
            continue
        if registry == FIXTURE_REGISTRY and name not in FIXTURE_DATASETS:
            raise FixtureCalendarRefused(
                f"{candidate} is in the fixture registry, which serves only "
                f"{sorted(FIXTURE_DATASETS)}.\n"
                "That registry exists so two toy datasets do not re-date the "
                "artifact provenance on every committed ledger row. A dataset "
                "whose results anyone will cite does not belong in it.\n"
                f"Register {name!r} in config/calendars/ instead, and accept "
                "that doing so changes artifact_digests() and makes every "
                "existing row's artifact fingerprint stale. That is a human "
                "decision with a stated cost, which is why it is not made here."
            )
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
