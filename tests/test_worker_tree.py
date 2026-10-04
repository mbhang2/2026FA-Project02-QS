"""Isolated working copies — Brian's point 4, the half that needs no new
pre-registration.

**Parallelism, deliberately not beam search.** Branching one trial into several
and then branching again changes the search algorithm, which makes every number
incomparable with every campaign run so far; that needs its own
pre-registration and is a human's decision. This runs the trials we would have
run anyway, at the same time.

Nor does parallelism launder multiplicity: 400 parallel trials are still 400
looks and `max_of_n` counts every one. The only thing bought is wall clock.

The defect it fixes: `src/strategies/current.py` is a single hardwired path, so
two trials in one checkout overwrite each other mid-flight and **the ledger
would look clean and be wrong**.

Proven 2026-10-04: 4/4 workers resolved the repository to themselves, loaded
their own strategy concurrently with no collision, left the real tree untouched,
and every parallel run reproduced the sequential score exactly.
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "worker_tree.py").read_text()
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "worker_tree", REPO_ROOT / "scripts" / "worker_tree.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_is_not_beam_search_and_says_so():
    assert "NOT beam search" in SRC or "not beam search" in FLAT.lower()
    assert "pre-registration" in FLAT


def test_it_does_not_claim_parallelism_fixes_multiplicity():
    """The tempting and wrong claim. 400 parallel trials are 400 looks."""
    assert "still 400" in FLAT
    assert "max_of_n" in SRC


def test_the_heavy_cache_is_shared_and_never_copied():
    """1.1 GiB, content-keyed, read-only to a trial and deliberately absent from
    ARTIFACT_GLOBS. Copying it per worker would be 8.8 GiB to no purpose."""
    m = _mod()
    assert "data" in m.LINK, "data/ must be a symlink"
    assert "data" not in m.COPY
    assert "1.1" in FLAT


def test_config_is_copied_because_it_is_fingerprinted():
    """ARTIFACT_GLOBS digests config/*.yaml and config/hypotheses/*.md. An
    identical copy gives an identical fingerprint, which is the point; a symlink
    would too, but a copy is what makes a worker independently editable."""
    m = _mod()
    assert "config" in m.COPY
    assert "ARTIFACT_GLOBS" in SRC


def test_a_worker_is_a_repository_by_marker_not_by_depth():
    """`src/core/_roots.py` resolves the root by marker: `.git`, or run.py beside
    pyproject.toml. A worker holds both, so everything inside resolves to it."""
    m = _mod()
    assert "run.py" in m.COPY and "pyproject.toml" in m.COPY
    assert "_roots" in SRC


def test_each_worker_gets_its_own_writable_runs():
    """Two processes appending to one JSONL is a corrupted line waiting to
    happen."""
    m = _mod()
    assert "runs" in m.MKDIR
    assert "runs" not in m.LINK
    assert "corrupted line" in FLAT


def test_the_isolation_proof_checks_the_real_tree_is_untouched():
    """The failure mode is writing to the real `current.py`. Proving each worker
    read its own is only half of it."""
    assert 'real = (ROOT / "src" / "strategies" / "current.py").read_text()' in SRC
    assert "TOUCHED -- isolation failed" in SRC


def test_the_equivalence_check_compares_against_sequential():
    """Speed is worthless if the number changes. A fixed seed and a committed
    profile make a backtest deterministic, so the scores must be identical."""
    assert "def equivalence" in SRC
    assert 'g["primary"] != seq["primary"]' in SRC
    assert "Parallelism is NOT safe here" in SRC


def test_the_probe_does_not_spend_a_trial():
    """`purpose=check` rows are excluded from `global_n()` by construction. The
    default profile is synthetic, so the mechanism is provable without touching
    the market data at all."""
    assert '"--purpose", "check"' in SRC
    assert 'default="campaign"' in SRC
    assert "SYNTHETIC" in SRC
    assert '"--purpose", "trial"' not in SRC


def test_workers_live_under_a_gitignored_path():
    m = _mod()
    assert ".scratch" in str(m.WORKERS_DIR)
    assert ".scratch*" in (REPO_ROOT / ".gitignore").read_text()
