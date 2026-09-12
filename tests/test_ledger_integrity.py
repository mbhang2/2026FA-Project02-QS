"""The ledger as an artifact with its own integrity requirements.

Everything else treats the ledger as an append target. It is also the p-value
column, and false-discovery control over it assumes each row is a calibrated
number from a run someone could re-run. A row nobody can reproduce is a number
with a timestamp, and carrying one silently overcounts N.

Two mechanisms are tested here: `scripts/ledger_check.py`, which asks the
question per row, and supersession, which is what you do with a row that cannot
answer it. Supersession is never deletion -- the fact that a trial happened is
the one thing the ledger cannot afford to lose.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ledger_check  # noqa: E402
from src.core.scoring import integrity  # noqa: E402

LEDGER = REPO_ROOT / "runs" / "ledger.jsonl"


def run_check(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/ledger_check.py", *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


# --------------------------------------------------------------------------- #
# the committed ledger
# --------------------------------------------------------------------------- #
def test_every_committed_row_names_a_commit_that_is_still_in_history():
    """The cheap half of reproducibility, and the half that can be checked in CI
    without re-walking 33 million ticks. A row naming a commit nobody can find
    was produced by code nobody can look at."""
    rows = ledger_check.load_rows(LEDGER)
    assert rows, "the committed ledger is empty; this test proves nothing"
    orphans = [
        (i, r.get("git_sha", "")[:12]) for i, r in enumerate(rows)
        if not ledger_check.sha_is_in_history(r.get("git_sha", ""))
    ]
    assert not orphans, f"rows naming commits not reachable from HEAD: {orphans}"


def test_the_committed_ledger_has_no_unverifiable_rows():
    r = run_check(str(LEDGER))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "UNVERIFIABLE" not in r.stdout
    assert "UNREPRODUCIBLE" not in r.stdout


def test_n_is_a_filter_that_supersession_narrows_and_deletion_never_does(tmp_path):
    led = tmp_path / "ledger.jsonl"
    rows = [
        {"purpose": "trial", "git_sha": "", "blotter_digest": "aaa"},
        {"purpose": "trial", "git_sha": "", "blotter_digest": "bbb"},
        {"purpose": "smoke", "git_sha": "", "blotter_digest": "ccc"},
    ]
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sup = tmp_path / "supersessions.jsonl"
    sup.write_text(json.dumps({
        "row": 1, "blotter_digest": "bbb",
        "reason": "produced by a harness version whose fill model was wrong",
    }) + "\n")

    results = [
        ledger_check.check_row(i, r, replay=False,
                               superseded=ledger_check.load_supersessions(sup))
        for i, r in enumerate(rows)
    ]
    counted = [x for x in results if x["purpose"] == "trial" and "superseded" not in x]
    assert len(counted) == 1
    # and the superseded row is still a row, with its reason attached
    assert "superseded" in results[1]
    assert "fill model" in results[1]["superseded"]
    assert len(ledger_check.load_rows(led)) == 3, "supersession is not deletion"


def test_a_supersession_that_has_drifted_onto_another_row_is_flagged(tmp_path):
    """Entries are keyed by index, and an index is only stable while nothing is
    inserted above it. The digest is the check on that."""
    sup = tmp_path / "supersessions.jsonl"
    sup.write_text(json.dumps({
        "row": 0, "blotter_digest": "the-row-it-meant", "reason": "whatever",
    }) + "\n")
    result = ledger_check.check_row(
        0, {"purpose": "trial", "git_sha": "", "blotter_digest": "a-different-row"},
        replay=False, superseded=ledger_check.load_supersessions(sup),
    )
    assert any("DIFFERENT ROW" in n for n in result["notes"])


# --------------------------------------------------------------------------- #
# the replay loader, which stopped working and nothing noticed
# --------------------------------------------------------------------------- #
def test_a_stored_source_with_a_relative_import_still_loads():
    """Task 17 gave the calibration reference `from ...types import
    NS_PER_SECOND`. `scripts/replay.py` executed stored sources under a bare
    module name, so they had no package, so the import raised and every row
    naming that file became unreplayable. It stayed broken for five tasks
    because nothing replayed a row in between.

    The regression test is the shape of the bug, not the file: any stored source
    that lived inside a package has to come back as a member of that package.
    """
    from replay import load_strategy_from

    reference = REPO_ROOT / "src" / "core" / "scoring" / "calibration" / "reference.py"
    assert "from ..." in reference.read_text(), (
        "the reference no longer uses a relative import; this test now proves "
        "nothing and needs a fixture that does"
    )
    obj = load_strategy_from(reference, "src/core/scoring/calibration/reference.py")
    assert hasattr(obj, "setup") and hasattr(obj, "on_bars")


def test_the_loader_falls_back_when_the_row_does_not_say_where_the_file_lived(tmp_path):
    """Old rows predate nothing here -- `strategy` has always been recorded --
    but a hand-written row or a bare path must not crash the loader."""
    from replay import load_strategy_from

    mod = tmp_path / "standalone.py"
    mod.write_text(
        "class Strategy:\n"
        "    def setup(self, ctx): pass\n"
        "    def on_bars(self, ctx): pass\n"
        "    def on_fill(self, ctx): pass\n"
        "    def on_lifecycle(self, ctx): pass\n"
    )
    assert hasattr(load_strategy_from(mod), "setup")
    assert hasattr(load_strategy_from(mod, "standalone.py"), "setup")


# --------------------------------------------------------------------------- #
# the fingerprints a row carries
# --------------------------------------------------------------------------- #
def test_code_fingerprint_is_recomputable_from_history():
    """The property that makes it worth recording. A fingerprint nobody can
    recompute at an arbitrary commit answers no question about an old row."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                          capture_output=True, text=True).stdout.strip()
    at_head = ledger_check.code_fingerprint_at(head)
    assert at_head, "git archive produced nothing"
    # equal to the working tree's only when the tree is clean, which it is not
    # during development -- so the assertion is that it is stable, not that it
    # matches right now
    assert at_head == ledger_check.code_fingerprint_at(head)
    assert len(at_head) == 64


def test_the_two_fingerprints_answer_different_questions():
    """`fingerprint()` is bytecode of loaded modules and is the only one that can
    see a monkeypatch. `code_fingerprint()` is file contents and is the only one
    that can be recomputed at a commit. Neither substitutes for the other."""
    assert integrity.fingerprint() != integrity.code_fingerprint()
    assert integrity.code_fingerprint(REPO_ROOT) == integrity.code_fingerprint()


def test_an_absent_artifact_is_recorded_rather_than_skipped(tmp_path):
    """Deleting a null table and recreating it with different contents would
    otherwise look exactly like a table that never moved."""
    digests = integrity.artifact_digests(tmp_path)
    assert digests["src/strategies/current.py"] == "absent"
    assert digests["data/manifest.json"] == "absent"
    assert integrity.artifact_fingerprint(digests) != integrity.artifact_fingerprint()


# --------------------------------------------------------------------------- #
# the environment, which no content hash over the repo can reach
# --------------------------------------------------------------------------- #
def test_the_lockfile_is_consistent_with_the_declared_dependencies():
    """The question CI can answer. It cannot ask whether the lockfile matches
    *its* environment, because CI is not the environment that produces scored
    runs -- different platform, installed from requirements-dev.txt. What it can
    ask is whether the lockfile is well formed and contradicts nothing."""
    r = subprocess.run(
        [sys.executable, "scripts/lock_env.py", "--verify"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_environment_digest_moves_when_a_version_does():
    """A guard on the guard: a fingerprint that never changes reports nothing."""
    base = integrity.resolved_environment()
    assert "python" in base and "numpy" in base, sorted(base)[:10]
    moved = dict(base, numpy="0.0.0-not-a-real-version")
    assert integrity.environment_fingerprint(moved) != integrity.environment_fingerprint(base)
    assert integrity.environment_fingerprint(base) == integrity.environment_fingerprint()


def test_a_row_records_the_environment_it_ran_under_and_the_one_it_declared(tmp_path):
    """Two digests, because they answer different questions: what was promised
    and what was used. A machine whose pandas moved without the lockfile being
    regenerated is the case worth catching."""
    import run as runner

    led = tmp_path / "ledger.jsonl"
    r = subprocess.run(
        [sys.executable, "run.py", "--human", "--quiet", "--ledger", str(led)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
    row = ledger_check.load_rows(led)[0]
    assert row["environment_fingerprint"] == integrity.environment_fingerprint()
    assert row["lockfile_digest"] == integrity.lockfile_digest()
    assert row["python_version"] == sys.version.split()[0]
    assert runner.ENVIRONMENT_AT_IMPORT == row["environment_fingerprint"]


def test_environment_drift_is_reported_and_is_not_supersession():
    """A row from before a dependency upgrade may be perfectly good; it just will
    not replay bit-identically. Treating that as a failure would mean
    re-baselining the ledger every time numpy moves."""
    row = {
        "purpose": "trial", "git_sha": "", "blotter_digest": "aaa",
        "environment_fingerprint": "a" * 64,
    }
    result = ledger_check.check_row(0, row, replay=False, superseded={})
    assert result["environment_drifted"] is True
    assert ledger_check.verdict(result) in ("UNVERIFIABLE", "env-drifted")
    assert "superseded" not in result
    # and a row that matches today is not flagged
    same = dict(row, environment_fingerprint=integrity.environment_fingerprint())
    assert ledger_check.check_row(0, same, replay=False,
                                  superseded={})["environment_drifted"] is False


def test_a_replay_is_logged_but_not_into_the_ledger(tmp_path):
    """Every execution is recorded; a replay is not a candidate. Putting replays
    in the ledger would also make `--replay` grow the file it is checking, once
    per row per pass."""
    import replay as replaymod

    assert replaymod.REPLAY_LOG.name == "replays.jsonl"

    led = tmp_path / "ledger.jsonl"
    r = subprocess.run(
        [sys.executable, "run.py", "--human", "--quiet", "--ledger", str(led)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
    row = ledger_check.load_rows(led)[0]

    replays = tmp_path / "replays.jsonl"
    engine = replaymod.replay(row, replay_log=replays)
    assert engine.blotter.digest() == row["blotter_digest"]

    assert len(ledger_check.load_rows(led)) == 1, (
        "the replay appended to the ledger; that file is the denominator of N"
    )
    logged = ledger_check.load_rows(replays)
    assert len(logged) == 1
    assert logged[0]["purpose"] == "replay"
    assert logged[0]["replay_of"]["blotter_digest"] == row["blotter_digest"]
