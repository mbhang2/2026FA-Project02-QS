"""The simulated crypto gate control: its market, its bounds and one small campaign."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "crypto_gate_control.py"
SPEC = importlib.util.spec_from_file_location("crypto_gate_control", PATH)
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def test_simulated_prices_stay_positive_and_sessions_chain():
    ds = control.CryptoControlDataset(control.spec_for(25.0, 3, n_sessions=50))
    ticks = [t for s in range(5) for t in ds._session_ticks(s)]
    assert min(t.price for t in ticks) > 0
    assert ds.session_open_prices([0])[0] == pytest.approx(control.START_PRICE, rel=1e-3)


def test_funding_settles_three_times_a_session():
    ds = control.CryptoControlDataset(control.spec_for(0.0, 3, n_sessions=50))
    events = ds.funding_series([0, 1])
    assert len(events) == 6
    assert {e.rate for e in events} == {control.FUNDING_RATE}


def test_wilson_interval():
    low, high = control.wilson(0, 100)
    assert low == 0.0 and high == pytest.approx(0.037, abs=1e-3)
    low, high = control.wilson(2, 100)
    assert low == pytest.approx(0.0055, abs=1e-3) and high == pytest.approx(0.0700, abs=1e-3)


def test_criteria_use_the_fixed_thresholds():
    arm = lambda rate, ci: {"rule_b": rate, "rule_b_ci": ci, "gate": rate, "gate_ci": ci}
    verdicts = control.criteria({0.0: arm(0.03, (0.01, 0.05)), 5.0: arm(0.02, (0.006, 0.07)),
                                 25.0: arm(0.9, (0.8, 0.95))})
    assert verdicts["C1 noise, rule (b) upper <= 0.08"]
    assert verdicts["C2 noise, full gate upper <= 0.05"]
    assert not verdicts["C3 edge below fees, full gate upper <= 0.05"]
    assert verdicts["C4 25 bp edge, full gate >= 0.80"]


def test_one_small_campaign_runs_through_the_real_gate():
    row = control.run_campaign(25.0, 1, n_sessions=50, b=50)
    assert len(row["trades"]) == len(control.VARIANTS)
    assert 0 < row["p"] <= 1
    assert row["passed"] == (row["rule_b"] and row["best"] > 0)
