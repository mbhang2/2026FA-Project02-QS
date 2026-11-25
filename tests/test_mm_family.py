"""Amendment 24 D8: a family with spread_capture campaigns, and native validation's purposes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import l2_gate  # noqa: E402


def _spread(name, primary, passes):
    return {"campaign_id": name, "validation_primary": primary, "passes": passes}


def test_a_passing_sign_flip_test_keeps_its_best():
    out = l2_gate.combine_spread({"verdict": "PASS", "best_campaign": "a"}, [_spread("mm", 0.5, True)], 4)
    assert out["verdict"] == "PASS" and out["best_campaign"] == "a" and out["m_family"] == 4


def test_a_passing_spread_campaign_passes_the_family():
    out = l2_gate.combine_spread({"verdict": "FAIL", "best_campaign": "a"},
                                 [_spread("mm1", 0.2, True), _spread("mm2", 0.4, True), _spread("mm3", 0.9, False)], 4)
    assert out["verdict"] == "PASS" and out["best_campaign"] == "mm2" and out["passed_by"] == "spread_capture"


def test_nothing_passing_fails():
    out = l2_gate.combine_spread({"verdict": None, "withheld": "no sign-flip campaigns"},
                                 [_spread("mm", 0.9, False)], 1)
    assert out["verdict"] == "FAIL"


def test_the_bounds_split_q_so_the_family_stays_within_it():
    """D8's arithmetic: m sign-flip campaigns at q*m/M plus one bound per spread campaign at q/M."""
    q, m_family = 0.05, 4
    for m in range(0, m_family + 1):
        spread = m_family - m
        assert abs(q * m / m_family + spread * q / m_family - q) < 1e-12


def test_native_validation_and_audit_rows_are_not_trials():
    text = (ROOT / "scripts/run_native_trial.py").read_text()
    assert ('purpose = ("audit" if deep else "check" if split is Split.VALIDATION else "trial") '
            'if signed else "check"') in text
