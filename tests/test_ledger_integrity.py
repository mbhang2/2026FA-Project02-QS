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
import re
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
    """Every row names a commit that is in history and whose fingerprint matches.

    **The `UNREPRODUCIBLE` assertion that used to sit here was dead, and is
    gone.** `run_check` does not pass `--replay`, so `reproduces` stays None,
    every row verdicts as `checkable`, and the string could never appear --
    measured on 2026-10-25: zero occurrences without the flag, whatever the
    ledger contains. It read as a reproduction guard and guarded nothing, which
    is the same shape as the `setdefault` endpoint bug and the boundary sweep
    that "walked only public names and so never saw it".

    Reproduction is checked by the two tests below instead, and what it means
    is narrower than the dead assertion implied -- see them for why.
    """
    r = run_check(str(LEDGER))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "UNVERIFIABLE" not in r.stdout


def test_the_unreproducible_verdict_exists_and_needs_a_replay_to_reach():
    """The guard can fire. Proving that is the point.

    A check that cannot report a failure is worse than no check, because it
    reports clean. This pins both halves: `verdict` DOES return UNREPRODUCIBLE
    when a replay disagrees, and it CANNOT without one.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ledger_check_mod", REPO_ROOT / "scripts" / "ledger_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    base = {"git_sha": "0" * 40, "sha_in_history": True,
            "code_fingerprint_matches": True,
            "environment_drifted": False, "notes": []}
    assert mod.verdict({**base, "reproduces": False}) == "UNREPRODUCIBLE"
    assert mod.verdict({**base, "reproduces": True}) == "reproduces"
    # without --replay the field is never set away from None, which is exactly
    # why the old assertion could not fire
    assert mod.verdict({**base, "reproduces": None}) == "checkable"


def test_rows_recorded_at_head_reproduce(tmp_path):
    """**Reproduction here is contemporaneous, not cross-version.**

    A replay runs CURRENT code against a row recorded under the code of its own
    commit, so a row from an older sha may legitimately differ once the engine
    changes -- 4 of the 9 replayable rows in `runs/ledger.jsonl` do, the oldest
    from 2026-09-20 and the newest from 2026-10-20, none of them at HEAD. That
    is not rot: the checker has an `unpinned` verdict for exactly it, and a
    campaign verifies its own reproduction at its own sha (Task 37 reported
    97/97 exact that way).

    What must hold is the narrower thing: a row written under the code that is
    checked out now has to reproduce now. This skips when there are none rather
    than passing silently, so it becomes live the moment a run is recorded at
    HEAD -- which is also what makes it a usable acceptance signal for a
    refactor.
    """
    import json
    import subprocess

    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, cwd=REPO_ROOT).stdout.strip()
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    # **Trials, because the guarantee is about the p-value column.** A
    # `check` row is a diagnostic that `global_n()` does not count and that
    # nothing is concluded from, and it is written by hand against whatever
    # configuration was being explored at the time -- the five options control
    # runs of 2026-10-25 name two surface sizes that the committed config no
    # longer describes, which is a true statement about an exploratory run and
    # not a reproduction failure. Scoping to trials is what makes a failure
    # here mean what the docstring says it means.
    at_head = [r for r in rows
               if r.get("git_sha") == head and r.get("purpose") == "trial"]
    if not at_head:
        pytest.skip(f"no trial in {LEDGER.name} was recorded at HEAD ({head[:12]})")

    # **Only the HEAD rows, and only the ones this suite can afford.**
    #
    # This passed `--replay` over the WHOLE ledger, which is not what the
    # docstring above claims and not what the test is for. It was invisible
    # while no row sat at HEAD and the test always skipped; the first run that
    # recorded one turned it into a replay of every historical row, which fails
    # on the NQ rows for want of bucket credentials and would re-execute
    # several hundred sessions of options pricing besides.
    #
    # The budget is a real constraint rather than a preference: a row's own
    # recorded wall clock says what replaying it costs, and a guard that adds
    # twenty-five minutes to every suite run is a guard that gets deselected.
    # When every HEAD row is too expensive, say so and name the command --
    # skipping with a reason beats both a slow suite and a silent pass.
    BUDGET_S = 60.0
    affordable = [r for r in at_head
                  if (r.get("wall_clock_s") or 0.0) <= BUDGET_S
                  and r.get("fill_log_source")]
    if not affordable:
        slowest = max((r.get("wall_clock_s") or 0.0) for r in at_head)
        pytest.skip(
            f"{len(at_head)} row(s) at HEAD, none replayable inside {BUDGET_S:.0f}s "
            f"(cheapest is {slowest:.0f}s). Run it by hand: "
            f"python scripts/ledger_check.py runs/ledger.jsonl --replay")

    sub = tmp_path / "at-head.jsonl"
    sub.write_text("".join(json.dumps(r) + "\n" for r in affordable))
    r = run_check(str(sub), "--replay")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "UNREPRODUCIBLE" not in r.stdout, r.stdout


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
    # "no-sha" since Task 47: this fixture's `git_sha` is "" incidentally --
    # the subject here is drift -- and a row recording no sha at all is now told
    # apart from one recording a sha nobody has. Either is acceptable; what the
    # test is asserting is the two lines around it.
    assert ledger_check.verdict(result) in (
        "UNVERIFIABLE", "env-drifted", "no-sha")
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


# --------------------------------------------------------------------------- #
# recomputing a fingerprint from history, cheaply
# --------------------------------------------------------------------------- #
def test_the_narrow_archive_hashes_exactly_what_the_whole_tree_did():
    """`code_fingerprint_at` asks git only for the paths it hashes.

    The unfiltered archive of this repository is 751 MB, nearly all of it the
    committed campaign records, and the fingerprint reads 0.66 MB of it. That
    was costing the check ~1.98 TB of tar to learn 57 distinct answers.

    The saving is only legitimate if the hash does not move, so this recomputes
    the pre-change implementation and compares. Two commits rather than all 57:
    the full archive is the expensive thing being removed, and the exhaustive
    57-sha comparison was run once when the change was made.
    """
    rows = ledger_check.load_rows(LEDGER)
    shas = [r["git_sha"] for r in rows if r.get("git_sha") and r.get("code_fingerprint")]
    assert shas, "no row names a commit; this test proves nothing"

    def whole_tree(sha):
        import tempfile
        archive = subprocess.run(["git", "archive", sha], cwd=REPO_ROOT,
                                 capture_output=True)
        assert archive.returncode == 0, sha
        with tempfile.TemporaryDirectory() as tmp:
            assert subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout,
                                  capture_output=True).returncode == 0
            return integrity.code_fingerprint(Path(tmp))

    for sha in ("HEAD", shas[0]):
        assert ledger_check.code_fingerprint_at(sha) == whole_tree(sha), (
            f"narrowing the archive changed the fingerprint at {sha[:12]}; the "
            f"pathspec no longer covers everything SOURCE_GLOBS hashes")


def test_the_pathspec_cannot_fall_behind_source_globs():
    """The failure this guards is silent and in the safe direction looking
    backwards: a glob added to SOURCE_GLOBS but not to the archive's pathspec
    would hash files that were never extracted, so every historical row would
    be recomputed as a mismatch and reported as uncommitted edits. Deriving the
    pathspec from the globs is what makes that impossible, so assert it is
    derived and not transcribed."""
    src = (REPO_ROOT / "scripts" / "ledger_check.py").read_text()
    body = src.split("def _code_fingerprint_at_uncached", 1)[1].split("\ndef ", 1)[0]
    body = re.sub(r"^\s*#.*$", "", body, flags=re.M)
    assert "integrity.SOURCE_GLOBS" in body, (
        "the pathspec is no longer derived from SOURCE_GLOBS; a glob added "
        "there would stop being archived")
    for glob in integrity.SOURCE_GLOBS:
        root = glob.split("*", 1)[0].rstrip("/") or glob
        out = subprocess.run(["git", "archive", "HEAD", "--", root],
                             cwd=REPO_ROOT, capture_output=True)
        assert out.returncode == 0, f"{root!r} is not archivable at HEAD"


def test_a_commit_is_fingerprinted_once_however_many_rows_name_it():
    """2,700 archive-eligible rows across the ledgers name 57 commits. The
    answer is a function of the commit, so the row count must not be what
    drives git."""
    calls = []
    real = ledger_check.subprocess.run

    def counting(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "archive"]:
            calls.append(cmd[2])
        return real(cmd, *a, **kw)

    ledger_check._FP_CACHE.clear()
    ledger_check.subprocess.run = counting
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True).stdout.strip()
        first = ledger_check.code_fingerprint_at(sha)
        for _ in range(12):
            assert ledger_check.code_fingerprint_at(sha) == first
    finally:
        ledger_check.subprocess.run = real
        ledger_check._FP_CACHE.clear()
    assert len(calls) == 1, (
        f"13 lookups of one commit ran {len(calls)} archives; the memo is gone")


def test_the_history_check_is_memoised_too():
    calls = []
    real = ledger_check.subprocess.run

    def counting(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "merge-base"]:
            calls.append(cmd)
        return real(cmd, *a, **kw)

    ledger_check._HIST_CACHE.clear()
    ledger_check.subprocess.run = counting
    try:
        for _ in range(20):
            assert ledger_check.sha_is_in_history("HEAD") is True
    finally:
        ledger_check.subprocess.run = real
        ledger_check._HIST_CACHE.clear()
    assert len(calls) == 1, f"20 lookups ran {len(calls)} merge-base calls"
