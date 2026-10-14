"""The L2 gate's plumbing: a validation null per winner, and a declared family.

No network. `max_of_m` existed and could not be run, because two inputs were
missing. This is what supplies them.

The assertions that matter are about refusing to be convenient:

1. **M is declared, never discovered.** A script that globbed its own family would
   decide the size of the multiplicity it corrects for, silently, and every
   judgement in that glob would move the p-value with nothing in the record.
2. **The null's seed is stable across processes.** `hash()` on a str is salted per
   process, so the first version gave each campaign a different null on every run
   — hidden, because the cache froze whichever happened first.
3. It reports the tension it cannot resolve rather than papering over it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "l2_gate", REPO_ROOT / "scripts" / "l2_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 1. M is declared
# --------------------------------------------------------------------------- #
def test_the_family_must_be_declared_not_discovered():
    """M is the entire content of this correction. §4 says L2 runs until a human
    stops it; the same human says what was in the family."""
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert '"--campaigns"' in src
    assert "M is declared, not discovered" in src
    # the candidate lister is separate from the family, and does not select
    assert "def candidates(" in src
    body = src[src.index("def candidates("):src.index("def build_validation_null")]
    assert "max_of_m" not in body, "listing candidates must not compute a verdict"


def test_the_declared_family_is_written_into_the_verdict():
    """So the family is recoverable from the artefact, not only from a shell
    history."""
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert 'out["declared_family"] = family' in src
    assert 'out["schema"] = "l2-verdict/1"' in src


def test_a_campaign_with_no_validation_score_cannot_be_in_a_family():
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert "not scored on validation, so cannot be in a family" in src


def test_the_lister_names_the_trap_it_is_protecting_against():
    """Anyone reading `--list` is one step from dropping the campaigns that did
    badly, which is the defect the whole correction exists to prevent."""
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert "including the ones " in src and "that did badly" in src
    assert "excluding crashed trials " in src


# --------------------------------------------------------------------------- #
# 2. the null is reproducible
# --------------------------------------------------------------------------- #
def test_the_null_seed_is_stable_across_processes():
    """**The defect this test exists for.** The first version seeded from
    `abs(hash(name))`. `hash()` on a str is salted per process, so three separate
    interpreters gave 6907, 7517 and 9336 for the same campaign -- a different
    null each run, invisible because the cache froze whichever came first.
    """
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    # the assignment itself, not the comment above it that explains the defect
    seed_lines = [ln.strip() for ln in src.splitlines()
                  if ln.strip().startswith("seed = ")]
    assert len(seed_lines) == 1, seed_lines
    assert "hashlib.sha256(name.encode())" in seed_lines[0]
    assert "hash(name)" not in seed_lines[0], "a salted hash is not a seed"

    # the expression itself, evaluated in three fresh interpreters
    expr = ("import hashlib;print(90_000 + int(hashlib.sha256(b'nq-ml-03')"
            ".hexdigest()[:8], 16) % 10_000)")
    seen = {subprocess.run([sys.executable, "-c", expr], capture_output=True,
                           text=True).stdout.strip() for _ in range(3)}
    assert len(seen) == 1, f"seed differs across processes: {seen}"
    assert seen.pop() == str(
        90_000 + int(hashlib.sha256(b"nq-ml-03").hexdigest()[:8], 16) % 10_000)


def test_the_null_is_cached_with_everything_needed_to_check_it():
    """A cached null that does not say which fill log and which seed produced it
    cannot be verified, and a null nobody can verify is a number to distrust."""
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    body = src[src.index("def build_validation_null"):src.index("def main(")]
    for field in ('"seed": seed', '"fill_log_digest"', '"b": b', '"profile"',
                  '"n_sessions"', '"split": "validation"', '"spec"'):
        assert field in body, field


def test_the_null_is_built_on_validation_not_train():
    """The whole point. `stage_nulls` builds these on train; nothing built one on
    validation, which is why the L2 gate could not run."""
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    body = src[src.index("def build_validation_null"):src.index("def main(")]
    assert "Split.VALIDATION" in body
    assert "Split.TRAIN" not in body
    assert "validation.jsonl" in body, "the validation run's own ledger row"


def test_the_committed_caches_match_their_own_recorded_seed():
    """Every cached validation null in the tree is reproducible from what it
    records. If a cache was written by the salted-hash version, its seed will not
    match the stable one and this fails."""
    caches = list((REPO_ROOT / "runs" / "campaigns").glob("*/validation-null.json"))
    if not caches:
        return
    for c in caches:
        d = json.loads(c.read_text())
        name = c.parent.name
        expected = 90_000 + int(
            hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 10_000
        assert d["seed"] == expected, f"{name}: stale seed {d['seed']}"
        assert d["split"] == "validation"
        assert len(d["sample"]) == d["b"]


# --------------------------------------------------------------------------- #
# 3. what it says it cannot do
# --------------------------------------------------------------------------- #
def test_it_records_the_tension_with_section_4_rather_than_hiding_it():
    """§4 says validation is not scored during the run at all, and all M winners
    are scored at the stop. Our practice scores each campaign's winner
    immediately, and nq-ml-03's was read the day it ran. The script inherits what
    happened and says so."""
    doc = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert "not scored during the run at all" in doc
    assert "Recorded here, not decided" in doc


def test_it_does_not_claim_a_promotion():
    src = (REPO_ROOT / "scripts" / "l2_gate.py").read_text()
    assert "Not a promotion" in src
    assert "human decision on all of the evidence" in src


def test_the_candidate_lister_runs_offline_and_reports_the_real_tree():
    """It reads committed artefacts only -- no dataset, no network -- so it can
    say what the family could be without touching the market data."""
    mod = _mod()
    cands = mod.candidates()
    assert cands, "six campaigns have been validated"
    ids = {c["campaign_id"] for c in cands}
    assert "nq-ml-03" in ids
    for c in cands:
        assert c["validation_primary"] is not None
        assert c["hypothesis"] is not None
    # **One winner is now positive on validation, and this test is how we found
    # out.** It used to assert that none ever had been. `nq-s02-01` broke it on
    # 2026-09-30: train +0.2248 over 466 trades, validation **+0.1765** over 106
    # trades and +941 index points. It is the first time rule (a) has passed
    # anywhere in this project.
    #
    # It did NOT pass the gate. Its own validation null sits at +0.0113 +/-
    # 0.1604, so the winner is +1.03 sd above it and p = 0.1471 -- nowhere near
    # q. The L2 verdict is FAIL and nothing is claimed. Rule (a) is a floor, not
    # a finding, and a positive number that a sign-flip of its own round turns
    # reproduces one time in seven is what "not distinguishable" looks like.
    #
    # Two caveats that belong beside the number, not in a footnote: M = 1,
    # because the five-campaign family it belonged to died on a network timeout
    # after one campaign; and that campaign's committee never ran, for the same
    # reason, so no auditor has read it.
    # **nq-s05b-01 joined this set on 2026-10-04, and it is the more
    # instructive member.** Its L1 gate on TRAIN rejected it at p = 0.8724 and
    # its committee vetoed on robustness -- all profit in two UTC hours, 74% on
    # Sundays, 57% in one month, which is the Globex weekly open and not the RTH
    # open its question asked about. It then validated to +0.0165 and cleared
    # rule (a) anyway.
    #
    # That is the clearest demonstration available of why rule (a) is a FLOOR
    # and not a finding. Eleven campaigns are listed here, two are positive on
    # validation, and one of those two was rejected by every other tier that
    # looked at it.
    # 2026-10-05: session-06 added two more, and one of them should not be here
    # at all. **nq-s06-03 was validated in error** -- it died at trial 36 of 100
    # on a gateway substitution, so `nulls` and `report` never ran and it has no
    # train-side gate, yet it was handed a validation look and its +0.1371 became
    # that session's L2 observed best. `research_session.py` no longer validates a
    # campaign that did not complete, but the artefact exists and the lister reads
    # validation.json, so it stays in this pool. Its status.json says so.
    #
    # Four of fourteen validated campaigns are positive on validation, and two of
    # the four were rejected on train. Rule (a) is a floor, not a finding.
    # **Session 07 added the two that make this list worth reading.**
    # nq-s07-02 is the highest validation score in the project's history
    # (+0.2694) and its TRAIN gate rejected it at p = 1.0, its best candidate
    # coming in below the null's expected maximum. nq-s07-01, which passed train
    # at the Monte Carlo floor, is absent: it validated to -0.2542.
    #
    # Six of seventeen validated campaigns are positive out of sample, and the
    # train ranking of session 07's three was exactly inverted against it. Rule
    # (a) is a floor, not a finding, and this list is the evidence.
    #
    # **nq-t72b-02 joined on 2026-10-14, and it is the most informative entry
    # here precisely because it is not a result.** It is a member of the Task 72b
    # CONTROL arm: three campaigns that ran `intraday-continuation.md` with
    # `refine: false`, the same unchanged question three times, to measure what
    # best-of-N does on its own. Validating them was the point -- the control had
    # train numbers and no out-of-sample ones, so nothing could be compared
    # against it. They came back -0.0827, **+0.0603**, -0.1539.
    #
    # So an unchanged question, refined by nobody, cleared rule (a) one time in
    # three. Rule (a) is a floor in the most literal sense: a coin lands on it.
    # The useful quantity is the ceiling the control reached, +0.0603, because
    # five campaigns in this pool beat it and that is the first statement about
    # validation this project can make that has a denominator.
    positive = [c["campaign_id"] for c in cands if c["rule_a"]]
    assert positive == ["nq-s02-01", "nq-s05b-01", "nq-s06-01", "nq-s06-03",
                        "nq-s07-02", "nq-s07-03", "nq-t72b-02"], (
        f"the set of winners positive on validation changed: {positive}. "
        "Update this deliberately and look closely at whichever one is new -- "
        "every previous campaign's winner lost money on validation.")
