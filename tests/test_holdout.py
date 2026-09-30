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


def _prereg(tmp_path, in_force: bool, a10: bool = False):
    status = "IN FORCE from 2026-09-30" if in_force else "DRAFTED, NOT IN FORCE"
    s10 = "IN FORCE from 2026-09-30" if a10 else "DRAFTED, NOT IN FORCE"
    (tmp_path / "prereg.md").write_text(
        f"## Amendment 9 — the holdout pass rule\n\n**Status: {status}.**\n\nrules\n"
        f"\n## Amendment 10 — the L2 statistic\n\n**Status: {s10}.**\n\nrules\n")


def _disagreeing_l2(tmp_path):
    """An L2 verdict whose two statistics name different campaigns."""
    (tmp_path / "l2-verdict.json").write_text(json.dumps({
        "verdict": "PASS", "rule_a_pass": True, "reject": True, "p": 0.001,
        "best_campaign": "nq-ml-03", "declared_family": ["nq-ml-03", "other"],
        "raw_view": {"best_campaign": "nq-ml-03", "p": 0.7321, "reject": False},
        "standardised_view": {"best_campaign": "nq-orderflow-01", "p": 0.0001,
                              "reject": True},
        "disagreement": "the standardised statistic rejects and the raw one "
                        "does not",
    }))


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


def test_the_real_amendment_9_is_signed_and_the_look_is_still_shut():
    """Signed 2026-09-30. This test used to assert the opposite and fired the day
    it was signed, which is what it was for.

    It now asserts the thing that replaces it: signing the pass rule removed ONE
    refusal, and the look must still be blocked by something. Nobody should be
    able to read "the pass rule is in force" as "the holdout is open"."""
    mod = _mod()
    assert mod.amendment_9_in_force() is True
    reasons = mod.blocked(rehearse=False)
    assert not any("Amendment 9" in r for r in reasons)
    assert reasons, (
        "the holdout is now ONE command from being spent. If that is deliberate, "
        "re-read §8 and Amendment 9 first; if it is not, find out what changed.")


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


# --------------------------------------------------------------------------- #
# refusal 7: the two L2 statistics must agree about the family (Task 70)
# --------------------------------------------------------------------------- #
def test_a_disagreement_between_the_two_l2_statistics_is_a_refusal(tmp_path,
                                                                   monkeypatch):
    """There is no second look in which to try the other candidate.

    Task 70 measured the raw maximum as valid but blind. While Amendment 10 has
    not said which statistic decides, a family the gate cannot agree on is not one
    to spend 73 irrecoverable sessions against.
    """
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=True, a10=False)
    _disagreeing_l2(tmp_path)

    reasons = mod.blocked(rehearse=False)
    hit = [r for r in reasons if "two statistics disagree" in r]
    assert hit, reasons
    assert "nq-ml-03" in hit[0] and "nq-orderflow-01" in hit[0], (
        "the refusal must name both candidates, or a reader cannot act on it")
    assert "Amendment 10" in hit[0]
    assert "no second look" in hit[0]


def test_signing_amendment_10_stops_that_refusal_firing(tmp_path, monkeypatch):
    """The refusal exists because the statistic is unsettled, not because the two
    numbers differ. Once the document has chosen, the disagreement stays in the
    record as a fact and stops being an open question."""
    mod = _mod(monkeypatch, tmp_path)
    _disagreeing_l2(tmp_path)

    _prereg(tmp_path, in_force=True, a10=False)
    assert [r for r in mod.blocked(rehearse=False) if "disagree" in r]

    _prereg(tmp_path, in_force=True, a10=True)
    assert not [r for r in mod.blocked(rehearse=False) if "disagree" in r]


def test_agreeing_statistics_do_not_block_the_look(tmp_path, monkeypatch):
    """The refusal must be about disagreement, not about the fields existing."""
    mod = _mod(monkeypatch, tmp_path)
    _prereg(tmp_path, in_force=True, a10=False)
    (tmp_path / "l2-verdict.json").write_text(json.dumps({
        "verdict": "PASS", "rule_a_pass": True, "reject": True, "p": 0.001,
        "best_campaign": "nq-ml-03", "declared_family": ["nq-ml-03", "other"],
        "raw_view": {"best_campaign": "nq-ml-03", "p": 0.001, "reject": True},
        "standardised_view": {"best_campaign": "nq-ml-03", "p": 0.0005,
                              "reject": True},
    }))
    assert not [r for r in mod.blocked(rehearse=False) if "disagree" in r]


def test_the_docstring_counts_its_refusals_correctly():
    """A docstring that says six while the code has seven is how a reader ends up
    trusting a list that is missing the one that would have stopped them."""
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    n = len([1 for i in range(1, 12) if f"\n{i}. **" in src])
    assert "Seven refusals" in src, "the count in the docstring"
    assert n == 7, f"the docstring lists {n} numbered refusals"


# --------------------------------------------------------------------------- #
# Amendment 9's four added conditions
# --------------------------------------------------------------------------- #
def test_rule_ii_is_not_evaluable_below_the_support_the_test_needs():
    """A sign-flip null over k round turns has at most 2**k values, so p <= q is
    unreachable below ceil(log2(1/q)). Not a threshold anybody chose.

    nq-orderflow-01 made two trades in 86 sessions and the machinery reported
    p = 0.0001 where the honest floor was 0.25."""
    mod = _mod()
    assert mod.MIN_ROUND_TURNS == 5, "ceil(log2(1/0.05))"
    row = {"score": {"primary": 0.9, "metric": "sortino_per_session"}}
    null = [-1.0] * 400
    out = mod.judge(row, null, {"round_turns": 2, "identity_primary": 0.9,
                                "identity_metric": "sortino_per_session"})
    assert out["rule_ii_evaluable"] is False
    assert out["rule_ii_p"] is None
    assert out["confirmed"] is False, "rule (i) alone cannot confirm"
    assert "2**k >= 1/q" in out["not_evaluable_because"]


def test_five_round_turns_is_evaluable():
    mod = _mod()
    row = {"score": {"primary": 0.9, "metric": "sortino_per_session"}}
    out = mod.judge(row, [-1.0] * 400, {"round_turns": 5, "identity_primary": 0.9,
                                        "identity_metric": "sortino_per_session"})
    assert out["rule_ii_evaluable"] is True
    assert out["confirmed"] is True


def test_a_null_that_cannot_reproduce_the_observed_yields_no_p():
    """The observed IS the no-flip draw. If re-deriving it disagrees, the two were
    computed under different rules -- which is how nq-orderflow-01's -0.1516 on
    Sharpe came to be compared against a null of Sortinos around -0.87."""
    mod = _mod()
    row = {"score": {"primary": -0.1516, "metric": "sharpe_per_session"}}
    out = mod.judge(row, [-0.9] * 400,
                    {"round_turns": 40, "identity_primary": -0.8455,
                     "identity_metric": "sortino_per_session"})
    assert out["rule_ii_evaluable"] is False
    assert out["confirmed"] is False
    assert "different rules" in out["not_evaluable_because"]
    assert "sharpe_per_session" in out["not_evaluable_because"]


def test_a_metric_disagreement_alone_is_enough_to_refuse():
    """Same number, different leg. Sortino and Sharpe are not comparable even when
    they happen to coincide."""
    mod = _mod()
    row = {"score": {"primary": 0.5, "metric": "sharpe_per_session"}}
    out = mod.judge(row, [-0.9] * 400,
                    {"round_turns": 40, "identity_primary": 0.5,
                     "identity_metric": "sortino_per_session"})
    assert out["rule_ii_evaluable"] is False


def test_the_look_is_claimed_before_the_data_is_opened():
    """A crash after the data is read must not return the look, or "it crashed"
    becomes an unlimited retry channel -- which is what section 8 forbids,
    arriving through a door nobody has to open deliberately."""
    src = (REPO_ROOT / "scripts" / "holdout.py").read_text()
    claim = src.index("RESULT.write_text(json.dumps(claim")
    run = src.index("row = run_once(champ")
    assert claim < run, "the claim must land on disk BEFORE run.py is invoked"
    flat = " ".join(src.split())
    assert "unlimited retry channel" in flat
    assert "CLAIMED -- the run had not finished" in src


def test_rule_i_is_the_charged_primary_and_the_amendment_says_so():
    text = (REPO_ROOT / "docs" / "design" / "pre-registration.md").read_text()
    i = text.index("## Amendment 9")
    body = text[i:text.index("## Amendment 10")]
    flat = " ".join(body.split())
    assert "Not `primary_uncharged`" in flat
    assert "charged `primary`" in flat
    assert "well, uncharged it was positive" in flat, (
        "name the sentence the rule exists to prevent")
