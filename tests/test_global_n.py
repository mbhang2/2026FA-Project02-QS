"""Two N, the union that derives the global one, and the empty pre-registrations.

Task 48. `ledger_check` reported **N = 0** while 400 trials sat in campaign
directories beside it, because campaign trials are written to the campaign's own
ledger and never to `runs/ledger.jsonl`, and nothing ever read both.

The fix is a definition rather than a migration: the global count is the union
over every ledger, derived by `src.core.runlog.global_n`. Nothing is dual-written,
so there is nothing to keep in sync and no 400-row backfill appending historical
rows under today's timestamps.

**The two numbers answer different questions and only one of them is a bar.** The
correction is campaign-scoped -- `max_of_n` over that campaign's per-candidate
nulls, `deflation_view` handed that campaign's `len(trials)`. The global count is
the honesty number and nothing computes a bar from it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.core import runlog

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_CHECK = REPO_ROOT / "scripts" / "ledger_check.py"


# --------------------------------------------------------------------------- #
# the union
# --------------------------------------------------------------------------- #

def test_ledger_paths_finds_the_main_ledger_and_every_campaign_one():
    paths = [p.relative_to(REPO_ROOT).as_posix() for p in runlog.ledger_paths()]
    assert paths[0] == "runs/ledger.jsonl", "the main ledger comes first"
    assert any(p.startswith("runs/campaigns/") for p in paths), (
        "no campaign ledger found; the union is the whole point")
    assert all(p.endswith("ledger.jsonl") for p in paths)


def test_replays_and_review_scratch_are_not_ledgers():
    """A replay is not a trial, and a task's scratch output is not the record.

    Counting `docs/reviews/**` would let writing a review move N, which is the
    silliest possible way to inflate a denominator and therefore worth a test.
    """
    paths = [p.relative_to(REPO_ROOT).as_posix() for p in runlog.ledger_paths()]
    assert not any("replays" in p for p in paths)
    assert not any(p.startswith("docs/") for p in paths)


def test_global_n_is_the_union_and_is_not_the_main_ledgers_count():
    main = REPO_ROOT / "runs" / "ledger.jsonl"
    main_trials = sum(
        1 for line in main.read_text().splitlines()
        if line.strip() and json.loads(line).get("purpose") == "trial")
    total = runlog.global_n()
    assert total == len(runlog.trial_rows())
    assert total > main_trials, (
        f"global N is {total} and the main ledger holds {main_trials} trials. "
        "If these are equal the union has stopped finding campaign ledgers, "
        "which is the exact defect Task 48 fixed.")


def test_trial_rows_name_their_ledger_and_index():
    """A row's identity is its position in a specific file: row 7 of one ledger
    is not row 7 of another, and a supersession entry has to say which."""
    for ledger, index, row in runlog.trial_rows():
        assert ledger.startswith("runs/")
        assert isinstance(index, int)
        assert row["purpose"] == "trial"


def test_supersession_excludes_a_row_from_global_n_without_deleting_it(tmp_path):
    ledger = tmp_path / "runs" / "campaigns" / "x" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"purpose": "trial"}) + "\n")
    assert runlog.global_n(tmp_path) == 1

    supersessions = tmp_path / "runs" / "supersessions.jsonl"
    supersessions.write_text(json.dumps({
        "ledger": "runs/campaigns/x/ledger.jsonl", "row": 0,
        "reason": "the child exited before strategy execution",
    }) + "\n")
    assert runlog.global_n(tmp_path) == 0
    assert ledger.read_text(), "supersession is not deletion"


def test_global_n_counts_crashed_and_blocked_trials():
    """Excluding the trials that failed is the cheapest way to cheat a
    correction and the one nobody would look for."""
    outcomes = {row.get("outcome") for _, _, row in runlog.trial_rows()}
    assert "crashed" in outcomes, (
        "no crashed trial is being counted; either the ledgers changed or the "
        "filter got stricter than `purpose == 'trial'`")


def test_ledger_check_agrees_with_runlog():
    """One definition. The reporting tool cross-checks itself against the code
    so the two cannot drift into two answers."""
    out = subprocess.run([sys.executable, str(LEDGER_CHECK)],
                         cwd=REPO_ROOT, capture_output=True, text=True)
    assert "src.core.runlog.global_n() agrees: True" in out.stdout, out.stdout[-2000:]
    assert f"GLOBAL N (the union, every ledger): {runlog.global_n()}" in out.stdout


# --------------------------------------------------------------------------- #
# the correction stays campaign-scoped
# --------------------------------------------------------------------------- #

def test_the_deflation_bar_is_computed_from_the_campaigns_own_trials():
    """Not from the global count. A bar over every trial the project ever ran
    would be a bar for a family nobody selected over, and it would grow every
    night whether or not this campaign looked at anything."""
    source = (REPO_ROOT / "src" / "core" / "loop" / "campaign.py").read_text()
    assert "deflation_view(" in source
    call = source.split("deflation_view(", 1)[1][:120]
    assert "len(trials)" in call, (
        f"deflation_view is no longer handed the campaign's own count: {call!r}")
    assert "global_n" not in source, (
        "campaign.py reaches for the global count; the correction is "
        "campaign-scoped and the global number is not a bar")


def test_nothing_in_scoring_computes_a_bar_from_the_global_count():
    scoring = REPO_ROOT / "src" / "core" / "scoring"
    for path in scoring.rglob("*.py"):
        assert "global_n" not in path.read_text(), (
            f"{path.relative_to(REPO_ROOT)} uses the global count. It is the "
            "honesty number, not a bar.")


# --------------------------------------------------------------------------- #
# empty pre-registrations
# --------------------------------------------------------------------------- #

VALID = ("awaiting_rerun", "abandoned", "complete")


def test_every_ledgerless_campaign_explains_itself():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_lc", LEDGER_CHECK)
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)

    for directory, status in lc.registered_campaigns_without_a_ledger():
        rel = directory.relative_to(REPO_ROOT)
        assert status is not None, (
            f"{rel} has a pre-registration and no ledger and no status.json, so "
            "nothing says whether it ran, was abandoned, or is waiting. An "
            "unexplained empty pre-registration reads as pending, which is the "
            "most flattering of the three and the least likely to be true.")
        assert status["status"] in VALID, (
            f"{rel}: unknown status {status['status']!r}")
        assert status.get("summary"), f"{rel}: a status without a reason"


def test_an_unexplained_empty_pre_registration_fails_the_check(tmp_path):
    """Asserted by construction rather than by trusting the sentence above."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_lc2", LEDGER_CHECK)
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)

    d = tmp_path / "runs" / "campaigns" / "ghost"
    d.mkdir(parents=True)
    (d / "campaign.json").write_text("{}")
    found = lc.registered_campaigns_without_a_ledger(tmp_path)
    assert [(p.name, st) for p, st in found] == [("ghost", None)]

    (d / "status.json").write_text(json.dumps(
        {"status": "abandoned", "summary": "a reason"}))
    found = lc.registered_campaigns_without_a_ledger(tmp_path)
    assert found[0][1]["status"] == "abandoned"


def test_task37_is_recorded_as_waiting_rather_than_left_ambiguous():
    """The specific pair this was written for.

    Recorded `awaiting_rerun` and not `abandoned`: `d607d42` says "The campaign
    resumes when it is not [503]", and the pre-registration was deliberately
    kept untouched so the re-run is still the experiment. Calling it abandoned
    would assert the pre-registration is spent, which nobody has decided.
    """
    for arm in ("s030", "s050"):
        status = json.loads(
            (REPO_ROOT / "runs" / "campaigns" / "task37" / arm
             / "status.json").read_text())
        assert status["status"] == "awaiting_rerun"
        assert status["pre_registration_untouched"] is True
        assert "gemini" in " ".join(status["why"]).lower(), (
            "the reason should name the substitution that caused it")


# --------------------------------------------------------------------------- #
# a blocked proposal names its commit
# --------------------------------------------------------------------------- #

def test_a_row_names_its_commit_even_when_the_engine_never_ran(tmp_path):
    """`git_sha` used to arrive only inside the engine's record, so a run that
    failed at module load -- which is exactly what a blocked proposal is --
    produced a trial with no sha at all. Two such rows are in the Task 36
    campaigns, one per arm.

    The ledger path is `tmp_path`, not the real one: `_row` calls
    `store_source`, which copies the strategy into `<ledger>/../sources/`. The
    first draft of this test passed `runs/ledger.jsonl` and left an untracked
    file in the committed record -- a test that writes to the ledger it is
    checking is the wrong shape whatever it asserts.
    """
    import run as runner

    row = runner._row({"outcome": "crashed", "error": "agent_blocked: x"},
                      "human", "campaign", tmp_path / "ledger.jsonl",
                      purpose="trial")
    assert row["git_sha"], "a row with no engine record still has a commit"
    assert len(row["git_sha"]) == 40


def test_the_engines_own_sha_is_not_overwritten(tmp_path):
    import run as runner

    row = runner._row({"git_sha": "b" * 40}, "human", "campaign",
                      tmp_path / "ledger.jsonl", purpose="trial")
    assert row["git_sha"] == "b" * 40, (
        "the engine recorded the sha at engine time; _row must not replace it")
