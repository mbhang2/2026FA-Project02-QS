"""The single look. 73 sessions, once, ever.

This file carries more weight than its size suggests, because **the thing it tests
cannot be tested by using it.** Every other path in this project can be re-run;
the holdout cannot. So the refusals are asserted individually, the pass rule is
asserted against Amendment 9's text, and the plumbing is exercised against the
synthetic `holdout` profile — which exists for exactly this reason.

The refusal that matters most is the first one: until Amendment 9 says IN FORCE
there is no pass rule, and §8 requires the rule to be written *before* the look.
A number from the holdout with no rule to judge it against is worse than no
number, because it invites a rule written afterwards to fit it.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod(monkeypatch=None, tmp_path=None):
    spec = importlib.util.spec_from_file_location(
        "holdout", REPO_ROOT / "scripts" / "holdout.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if monkeypatch is not None:
        monkeypatch.setattr(mod, "L2_VERDICT", tmp_path / "l2-verdict.json")
        monkeypatch.setattr(mod, "RESULT", tmp_path / "holdout-result.json")
        monkeypatch.setattr(mod, "PREREG", tmp_path / "prereg.md")
        monkeypatch.setattr(mod, "CAMPAIGNS", tmp_path / "campaigns")
    return mod


def _prereg(tmp_path, in_force: bool):
    status = "IN FORCE from 2026-09-30" if in_force else "DRAFTED, NOT IN FORCE"
    (tmp_path / "prereg.md").write_text(
        f"## Amendment 9 — the holdout pass rule\n\n**Status: {status}.**\n\nrules\n")


def _l2(tmp_path, verdict="PASS", best="nq-ml-03"):
    (tmp_path / "l2-verdict.json").write_text(json.dumps(
        {"verdict": verdict, "best_campaign": best, "rule_a_pass": verdict == "PASS",
         "p": 0.01, "declared_family": [best, "other"]}))


# --------------------------------------------------------------------------- #
# the refusals, one at a time
# --------------------------------------------------------------------------- #
def test_no_pass_rule_means_no_look(monkeypatch, tmp_path):
    """§8: "the pass rule is written **before** it is opened." A holdout number
    with no rule to judge it invites a rule written afterwards to fit it."""
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=False)
    _l2(tmp_path)
    reasons = mod.blocked(rehearse=False)
    assert any("Amendment 9 is not IN FORCE" in r for r in reasons)
    assert mod.amendment_9_in_force() is False

    _prereg(tmp_path, in_force=True)
    assert mod.amendment_9_in_force() is True
    assert not mod.blocked(rehearse=False)


def test_the_real_amendment_9_is_not_in_force_yet():
    """Asserted against the committed document, so the day it is signed off this
    test fails and someone reads it."""
    mod = _mod()
    assert mod.amendment_9_in_force() is False, (
        "Amendment 9 now says IN FORCE -- update this test, and re-read §8 before "
        "anyone runs scripts/holdout.py --reason")


def test_a_failed_l2_gate_blocks_the_look(monkeypatch, tmp_path):
    """§8: opened "only if the L2 gate accepts. If nothing survived L2, opening the
    holdout confirms nothing and spends 73 sessions that cannot be recovered." """
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=True)
    _l2(tmp_path, verdict="FAIL")
    reasons = mod.blocked(rehearse=False)
    assert any("did not pass" in r for r in reasons)
    assert any("cannot be recovered" in r for r in reasons)


def test_a_missing_l2_verdict_blocks_the_look(monkeypatch, tmp_path):
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=True)
    assert any("no L2 verdict" in r for r in mod.blocked(rehearse=False))


def test_a_look_already_taken_blocks_a_second(monkeypatch, tmp_path):
    """On disk, not in memory. `HoldoutLock` is per-process, so a second process
    would unlock again quite happily; the artefact is what makes it once."""
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=True)
    _l2(tmp_path)
    (tmp_path / "holdout-result.json").write_text("{}")
    reasons = mod.blocked(rehearse=False)
    assert any("already exists" in r and "no second one" in r for r in reasons)


def test_the_real_look_is_refused_today_end_to_end():
    """The whole script, as a subprocess, against the real tree. Two things block
    it right now and it must exit non-zero without opening anything."""
    proc = subprocess.run(
        [sys.executable, "scripts/holdout.py", "--reason", "a test must not do this"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 1, proc.stdout[-2000:]
    assert "REFUSED" in proc.stdout
    assert "Nothing was opened and nothing was written" in proc.stdout
    assert not (REPO_ROOT / "runs" / "holdout-result.json").exists(), (
        "the real look must not have been recorded")


# --------------------------------------------------------------------------- #
# Amendment 9's pass rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("primary,null,rule_i,rule_ii,confirmed", [
    (0.30, [0.0] * 999, True, True, True),        # both hold
    (-0.05, [-0.9] * 999, False, True, False),    # beats its null, still a loss
    (0.30, [0.9] * 999, True, False, False),      # positive, unremarkable
    (-0.05, [0.9] * 999, False, False, False),
    (0.30, [], True, False, False),               # no null is not a pass
])
def test_both_rules_are_required(primary, null, rule_i, rule_ii, confirmed):
    """Rule (i) Sortino > 0 and rule (ii) p ≤ 0.05, both pass conditions.

    The second row is the case rule (i) exists for and it is not hypothetical:
    §3b found a candidate losing 3,275 points at p = 0.0005, because flipping a
    loser's round turns makes it lose more, so its null sits below it.
    """
    mod = _mod()
    out = mod.judge({"score": {"primary": primary}}, null)
    assert out["rule_i_sortino_positive"] is rule_i
    assert out["rule_ii_reject"] is rule_ii
    assert out["confirmed"] is confirmed


def test_everything_except_the_two_rules_is_evidence():
    """Amendment 9: decay, concentration, turnover and the committee's reading are
    evidence and not thresholds. There is no constant to tune here."""
    mod = _mod()
    out = mod.judge({"score": {"primary": 0.3, "trade_count": 400,
                               "net_pnl": 120.0, "n_sessions": 73},
                     "diagnostics": {"by_hour_utc": {"14": 100.0}}}, [0.0] * 999)
    ev = out["evidence_not_thresholds"]
    assert ev["trade_count"] == 400 and ev["net_pnl"] == 120.0
    assert ev["diagnostics"]["by_hour_utc"]["14"] == 100.0
    assert "evidence and not a" in out["note"]
    # and the pass conditions are not hiding in the evidence
    assert "confirmed" not in ev and "rule_i_sortino_positive" not in ev


def test_the_amendment_names_its_own_invalidations():
    text = (REPO_ROOT / "docs" / "design" / "pre-registration.md").read_text()
    a9 = text[text.index("## Amendment 9"):]
    a9 = a9[:a9.index("## Ideas recorded")]
    for required in ("73 sessions, one look, ever", "may not choose the candidate",
                     "may not be repeated", "may not refit",
                     "does not decide promotion", "would invalidate"):
        assert required in a9, required
    assert "no multiplicity correction at this step" in a9, (
        "one look needs no correction, and the amendment must say why")


# --------------------------------------------------------------------------- #
# the profiles and the fitted path
# --------------------------------------------------------------------------- #
def test_the_nq_holdout_profiles_read_the_real_split():
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())["profiles"]
    for name in ("nq-holdout", "nq-holdout-fitted"):
        p = cfg[name]
        assert p["dataset"] == "nq" and p["split"] == "holdout"
        assert p["commission_per_contract"] == 0.10, "costed like every other run"
    assert cfg["nq-holdout-fitted"]["weights_path"] == "runs/current.weights", (
        "the fitted profile must carry weights: without weights_path the engine "
        "would fit, and refitting on the holdout is unrecoverable")
    assert "weights_path" not in cfg["nq-holdout"]
    # and the rehearsal surface is synthetic, which is the only reason this
    # script can be tested at all
    assert cfg["holdout"]["dataset"] == "synthetic"


def test_a_fitted_champion_selects_the_fitted_profile():
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    assert 'profile = "nq-holdout-fitted" if fitted else "nq-holdout"' in src
    # and cannot be rehearsed on a profile that would refit it
    assert "fitted champion cannot be rehearsed on it" in src


def test_the_rehearsal_unlocks_because_that_is_the_part_most_likely_to_break():
    """The first rehearsal skipped the unlock, so the run refused and scored
    nothing -- it tested everything except the lock the real look turns."""
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    assert "A rehearsal unlocks too, and must" in src
    assert 'reason = a.reason or (' in src


def test_the_strategy_file_is_restored_whatever_happens():
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    body = src[src.index("def run_once"):src.index("def holdout_null")]
    assert "finally:" in body
    assert "STRATEGY.write_text(original)" in body
    assert "LIVE_WEIGHTS.unlink(missing_ok=True)" in body


def test_it_refuses_to_score_a_different_strategy_than_l2_ruled_on():
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    assert "Refusing to record a look at a different" in src
    # the candidate comes from the L2 verdict, and --campaign is rehearsal-only
    assert 'campaign = a.campaign if (a.rehearse and a.campaign) else v.get("best_campaign")' in src


# --------------------------------------------------------------------------- #
# the rehearsal, actually run
# --------------------------------------------------------------------------- #
def test_the_rehearsal_runs_the_whole_path_on_synthetic_data(tmp_path):
    """End to end against generated data: unlock, score, null, judge, record,
    restore. Slower than the rest of this file and the only test that proves the
    plumbing rather than the prose."""
    strategy = REPO_ROOT / "src" / "strategies" / "current.py"
    backup = strategy.read_text()
    proc = subprocess.run(
        [sys.executable, "scripts/holdout.py", "--rehearse",
         "--campaign", "nq-flowcont-5m"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert strategy.read_text() == backup, "current.py was not restored"
    assert proc.returncode == 0, proc.stdout[-2500:] + proc.stderr[-1500:]
    assert "REHEARSAL (synthetic)" in proc.stdout

    out = json.loads((REPO_ROOT / "runs" / "holdout-rehearsal.json").read_text())
    assert out["rehearsal"] is True
    assert out["profile"] == "holdout", "synthetic, never nq-holdout"
    assert out["reason"] is None, "a rehearsal records no reason as the real look"
    assert not (REPO_ROOT / "runs" / "holdout-result.json").exists()

    row = [json.loads(x) for x in
           (REPO_ROOT / "runs" / "holdout-rehearsal.jsonl").read_text().splitlines()
           if x.strip()][-1]
    assert row["outcome"] == "completed", "the lock was turned and the walk ran"
    assert "REHEARSAL" in (row.get("holdout_unlock_reason") or "")
    assert row["split"] == "Split.HOLDOUT" or "holdout" in str(row["split"]).lower()
