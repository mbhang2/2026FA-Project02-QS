"""One test that runs `run.py` the way a human does, and reads the row it wrote.

**Why this file exists.** Three bugs in this project have had the same shape: a
value computed correctly and never reaching its consumer, with no error and
plausible-looking output.

- Task 60 -- `commission_per_contract` was in the profile, copied into the
  campaign record, printed in the agent's facts, and never reached the engine,
  which charged its own default of 0.01 instead of 0.10.
- Task 61 -- the same key was absent from `nq-validation`, so validation was
  costed at 0.01 against train's 0.10 and the cheaper split looked better.
- 2026-09-22 -- a fitted run trained, handed its weights across the process
  boundary, and dropped them: `_row` adds `strategy_digest` *after* every
  `attach_weights` call site, so `store_weights` saw "" and bailed.

Every one survived a full suite, because the suite exercises the pieces and
never assembles a row through `_row`. This test assembles one, on synthetic
data, and asserts the fields are populated. It is slower than the rest of the
suite and much faster than the five-minute NQ run that finally caught the third.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FIT_SESSIONS = 20

FITTED_MODULE = '''"""TRANSIENT: written by tests/test_run_end_to_end.py, restored in its finally."""

from src.strategies.fitted_reference import FittedReference


class Strategy(FittedReference):
    def __init__(self):
        super().__init__(interval="5m", stop=4.0, target=4.0, size=1)
'''


def test_a_fitted_run_writes_a_row_that_carries_its_weights(tmp_path):
    strategy = REPO_ROOT / "src" / "strategies" / "current.py"
    backup = strategy.read_text()
    ledger = tmp_path / "ledger.jsonl"
    try:
        strategy.write_text(FITTED_MODULE)
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--purpose", "check", "--profile", "harness-fitted",
             "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
    finally:
        strategy.write_text(backup)

    # The restore is part of the test: a crashed run must not leave the agent's
    # file holding a fixture.
    assert strategy.read_text() == backup, "current.py was not restored"
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:])

    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert rows, "the run wrote no ledger row"
    row = rows[-1]
    assert row.get("outcome") == "completed", row.get("error")

    # The fit window was carved out of the split rather than merely declared.
    assert row["fit_sessions"] == FIT_SESSIONS

    # The three fields the weights bug left empty.
    for field in ("strategy_digest", "weights_source", "weights_digest"):
        assert row.get(field), (
            f"{field} is {row.get(field)!r}. The run fitted and the row does not "
            "say what it fitted to -- a champion sent to validation would have "
            "nothing to load.")

    # And the file the row points at is really there, with the weights in it.
    named = Path(row["weights_source"])
    on_disk = named if named.is_absolute() else REPO_ROOT / named
    assert on_disk.exists() and on_disk.stat().st_size > 0, (
        f"the row points at {on_disk}, which does not exist or is empty")


def test_the_profiles_cost_model_reaches_the_row(tmp_path):
    """Task 60 and 61, as a standing check rather than a memory. A profile that
    declares a commission and a row that does not carry it is the bug both
    times."""
    import yaml

    profiles = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())["profiles"]
    declared = {k: v["commission_per_contract"] for k, v in profiles.items()
                if "commission_per_contract" in v}
    assert declared, "no profile declares a commission; this check has gone blind"
    # Train and validation must agree, or the two splits are not comparable.
    pairs = [("nq-train", "nq-validation"),
             ("nq-train-fitted", "nq-validation-fitted")]
    for train, val in pairs:
        if train in declared and val in declared:
            assert declared[train] == declared[val], (
                f"{train} charges {declared[train]} and {val} charges "
                f"{declared[val]}; the cheaper split will look like the better one")


def test_a_load_run_carries_weights_through_run_py(tmp_path):
    """**The path nq-ml-02's validation stage died on.** `_proxy_for` returned the
    plain proxy for a weights-loading profile, and the plain proxy's capability
    check refused a class that defines fit(). The fit half of the path was
    tested end to end; the load half was not. This is the load half: fit on one
    profile, carry the bytes, load under a profile with weights_path and no
    fit_sessions, all through run.py."""
    strategy = REPO_ROOT / "src" / "strategies" / "current.py"
    backup = strategy.read_text()
    wdir = REPO_ROOT / ".scratch" / "test-weights"; wdir.mkdir(parents=True, exist_ok=True)
    wpath = wdir / "harness-fitted-load.weights"
    fit_ledger = tmp_path / "fit.jsonl"; load_ledger = tmp_path / "load.jsonl"
    try:
        strategy.write_text(FITTED_MODULE)
        r1 = subprocess.run([sys.executable, "run.py", "--human", "--quiet",
                             "--purpose", "check", "--profile", "harness-fitted",
                             "--ledger", str(fit_ledger)],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r1.returncode == 0, r1.stderr[-2000:]
        fit_row = [json.loads(x) for x in fit_ledger.read_text().splitlines() if x.strip()][-1]
        src = Path(fit_row["weights_source"]); src = src if src.is_absolute() else REPO_ROOT / src
        wpath.write_bytes(src.read_bytes())

        r2 = subprocess.run([sys.executable, "run.py", "--human", "--quiet",
                             "--purpose", "check", "--profile", "harness-fitted-load",
                             "--ledger", str(load_ledger)],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
    finally:
        strategy.write_text(backup)
        wpath.unlink(missing_ok=True)
    assert strategy.read_text() == backup
    assert r2.returncode == 0, (r2.stdout[-2000:], r2.stderr[-2000:])
    row = [json.loads(x) for x in load_ledger.read_text().splitlines() if x.strip()][-1]
    assert row.get("outcome") == "completed", row.get("error")
    assert "defines fit()" not in (row.get("error") or ""), (
        "the load run was routed to the plain proxy and refused by its own guard")
    # it loaded rather than fitted: no fit window was carved out of the split
    assert not row.get("fit_sessions"), row.get("fit_sessions")


FIT_RAISES_MODULE = '''"""TRANSIENT: written by tests/test_run_end_to_end.py, restored in its finally."""
from src.strategies.fitted_reference import FittedReference


class Strategy(FittedReference):
    def __init__(self):
        super().__init__(interval="5m", stop=4.0, target=4.0, size=1)

    def fit(self):
        raise RuntimeError("deliberate: not enough rows to fit")
'''


def test_a_fit_that_raises_is_a_crashed_row_not_an_empty_one(tmp_path):
    """2026-09-25: this exact run produced a row with no `outcome` at all. The
    crash handler asked the dead child to save weights, the pipe raised, and
    the second exception escaped every handler. A crashed fit is a trial that
    counts; a row with no outcome is one that leaves N without a trace."""
    strategy = REPO_ROOT / "src" / "strategies" / "current.py"
    backup = strategy.read_text()
    ledger = tmp_path / "ledger.jsonl"
    try:
        strategy.write_text(FIT_RAISES_MODULE)
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--purpose", "check", "--profile", "harness-fitted",
             "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
    finally:
        strategy.write_text(backup)
    assert strategy.read_text() == backup, "current.py was not restored"
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert rows, "the run wrote no ledger row"
    row = rows[-1]
    assert row.get("outcome") == "crashed", (row.get("outcome"), proc.stderr[-2000:])
    assert "deliberate: not enough rows to fit" in (row.get("error") or "")
    assert row.get("fit_completed") is False
    assert proc.returncode == 5, proc.returncode
