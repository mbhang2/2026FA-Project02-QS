"""The split is read from a committed calendar, never derived (Task 2.4, Task 40).

These tests used to pin the opposite behaviour: `split_periods` cut three
contiguous windows out of the spec's own dates, and the test asserted
`train.end == validation.start`. That is a split chosen while a run is in
progress, and an embargo of zero between windows that are known to be
autocorrelated. Both are things this project does not do, so the behaviour went
and the tests that pinned it went with it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nalin_l1.config.experiment_spec import ExperimentSpec
from nalin_l1.splitting.windows import (
    NoRegisteredCalendar,
    calendar_path,
    registered_split,
    split_periods,
)


def _spec(**kw):
    base = dict(
        asset_ids=["a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-29",
        strategy_module_path="x",
    )
    base.update(kw)
    return ExperimentSpec(**base)


def test_a_spec_with_no_calendar_is_refused_rather_than_derived():
    """The whole point. A spec carries dates; dates are not a pre-registration."""
    with pytest.raises(NoRegisteredCalendar, match="not derived from the spec"):
        split_periods(_spec())


def test_an_unknown_dataset_names_where_it_looked():
    with pytest.raises(NoRegisteredCalendar, match="config/calendars"):
        calendar_path("no-such-dataset")


def test_the_committed_split_is_returned_verbatim():
    train, validation, holdout = split_periods(_spec(calendar="placeholder"))
    assert train.start == pd.Timestamp("2024-01-01", tz="UTC")
    assert train.end == pd.Timestamp("2024-02-01", tz="UTC")
    assert holdout.end == pd.Timestamp("2024-04-29", tz="UTC")
    for r in (train, validation, holdout):
        assert r.start < r.end


def test_the_windows_do_not_touch_because_the_embargo_is_real():
    """The defect the old test enshrined: `train.end == validation.start` meant
    the last training bar and the first validation bar were adjacent."""
    train, validation, holdout = registered_split("placeholder")
    assert validation.start > train.end
    assert holdout.start > validation.end
    assert (validation.start - train.end).days == 8
    assert (holdout.start - validation.end).days == 8


def test_a_calendar_whose_windows_touch_is_refused(tmp_path, monkeypatch):
    """The reader enforces it too, so a badly written calendar fails loudly
    rather than reintroducing the leak the derived split had."""
    import nalin_l1.splitting.windows as w

    bad = tmp_path / "touching.yaml"
    bad.write_text(
        "dataset: touching\nsplits:\n"
        "  train: {start: 2024-01-01, end: 2024-02-01}\n"
        "  validation: {start: 2024-02-01, end: 2024-03-01}\n"
        "  holdout: {start: 2024-03-08, end: 2024-04-01}\n"
    )
    monkeypatch.setattr(w, "REGISTRIES", (tmp_path,))
    with pytest.raises(ValueError, match="touch or overlap"):
        w.registered_split("touching")
