"""Amendment 18: a crypto pass must show profit after costs. No control arm runs here."""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys
from statistics import NormalDist
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from src.core.loop.campaign import summarise
from src.core.scoring.correction import max_of_m
from src.core.scoring.profitability import (applies, newey_west_lag, newey_west_se,
                                            profitability_bound)

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_lag_is_the_registered_rule():
    assert [newey_west_lag(n) for n in (100, 160, 262, 973)] == [4, 4, 4, 6]


def test_newey_west_matches_a_hand_calculation():
    x = [1.0, -1.0, 2.0, 0.0, 3.0, -2.0]         # lag 2 at T = 6 after the sample cap
    n = len(x)
    lag = newey_west_lag(n)
    e = np.array(x) - np.mean(x)
    var = sum(v * v for v in e) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(e[t] * e[t - k] for t in range(k, n)) / n
    assert newey_west_se(x) == pytest.approx(math.sqrt(max(var, 0) / n))


def test_positive_autocorrelation_widens_the_standard_error():
    rng = np.random.default_rng(1)
    e = rng.normal(size=2000)
    ar = np.empty(2000)
    ar[0] = e[0]
    for t in range(1, 2000):
        ar[t] = 0.6 * ar[t - 1] + e[t]
    assert newey_west_se(ar) > 1.5 * ar.std(ddof=1) / math.sqrt(2000)


def test_the_bound_uses_the_one_sided_critical_value():
    x = np.array([0.02, -0.01, 0.03, 0.0] * 50)
    out = profitability_bound(x, 0.01)
    assert out["z"] == pytest.approx(NormalDist().inv_cdf(0.99))
    assert out["lower_bound"] == pytest.approx(out["mean"] - out["z"] * out["se"])


def test_a_positive_mean_inside_the_noise_fails_and_a_clear_one_passes():
    rng = np.random.default_rng(2)
    assert not profitability_bound(rng.normal(0.0005, 0.02, 300), 0.01)["passes"]
    assert profitability_bound(rng.normal(0.01, 0.02, 300), 0.01)["passes"]


@pytest.mark.parametrize("returns", [[0.01] * 50, [0.01], [0.01, float("nan")]])
def test_a_zero_or_undefined_standard_error_fails(returns):
    assert not profitability_bound(returns, 0.05)["passes"]


def test_only_a_signed_amendment_on_a_notional_profile_applies():
    profiles = yaml.safe_load((REPO / "config" / "run.yaml").read_text())["profiles"]
    for name, profile in profiles.items():
        assert not applies(profile, False), name
        assert applies(profile, True) == (profile.get("scoring_units") == "notional"), name


def _campaign():
    score = {"primary": 0.5, "sharpe": 0.1, "skew": 0.0, "kurtosis": 0.0, "n_sessions": 200,
             "net_pnl": 1.0, "trade_count": 100, "rejected": False, "reject_reason": "",
             "metric": "sortino_per_session"}
    rows = [{"campaign_index": 0, "strategy_digest": "aaaa", "outcome": "completed",
             "score": {**score, "primary": 0.2}},
            {"campaign_index": 1, "strategy_digest": "bbbb", "outcome": "completed",
             "score": score}]
    record = SimpleNamespace(campaign_id="x", budget=2, q=0.05, correction_rule="independent")
    return SimpleNamespace(record=record, record_digest="d", tampered=False, trials=rows,
                           rows=rows, harness_failures=[], unaccounted=[], wall_clock_s=1.0)


@pytest.mark.parametrize("bounds, accept", [
    (None, True),                                  # the floor, as before
    ({"1": {"passes": True}, "0": {"passes": False}}, True),   # the selected trial's bound
    ({"1": {"passes": False}, "0": {"passes": True}}, False),
    ({}, False),                                   # a missing bound fails closed
])
def test_l1_accept_reads_the_selected_trials_bound(bounds, accept):
    nulls = [[-0.5, -0.4, -0.3] * 100, [-0.5, -0.4, -0.3] * 100]
    gate = summarise(_campaign(), dataset="synthetic", null_samples=nulls, q=0.05,
                     profitability=bounds)["gate"]
    assert gate["reject"] and gate["accept"] is accept


def _family(**extra):
    return [{"campaign_id": "a", "validation_primary": 0.5,
             "validation_null": [-0.5, -0.4, -0.3] * 100, **extra}]


def test_l2_is_unchanged_without_a_bound():
    out = max_of_m(_family(), q=0.05)
    assert out["rule_a_pass"] and "rule_a_basis" not in out


def test_l2_uses_the_bound_when_supplied():
    out = max_of_m(_family(profitability_pass=False), q=0.05)
    assert out["rule_a_basis"] == "amendment_18_profitability_bound"
    assert not out["rule_a_pass"] and not out["passes"]


def test_the_holdout_uses_the_bound_when_supplied():
    mod = _load("holdout")
    row = {"score": {"primary": 0.9, "metric": "sortino_per_session"}}
    facts = {"round_turns": 50, "identity_primary": 0.9,
             "identity_metric": "sortino_per_session"}
    assert mod.judge(row, [-0.9] * 400, facts)["confirmed"]
    refused = mod.judge(row, [-0.9] * 400, {**facts, "profitability": {"passes": False}})
    assert not refused["confirmed"]
    assert refused["rule_i_basis"] == "amendment_18_profitability_bound"


def test_the_control_design_is_the_registered_one():
    c = _load("crypto_a18_control")
    assert c.ALPHA == pytest.approx(0.01)
    assert {k: (e, s[0], s[-1], len(s)) for k, (e, s) in c.ARMS.items()} == {
        "unprofitable": (8.0, 300_001, 301_000, 1000),
        "noise": (0.0, 301_001, 301_400, 400),
        "strong": (25.0, 301_401, 301_500, 100)}
    counts = {"unprofitable": 1000, "noise": 400, "strong": 100}
    assert c.verdict({"unprofitable": 36, "noise": 0, "strong": 80}, counts)["accepted"]
    assert not c.verdict({"unprofitable": 37, "noise": 0, "strong": 80}, counts)["accepted"]
    assert not c.verdict({"unprofitable": 0, "noise": 12, "strong": 80}, counts)["accepted"]
    assert not c.verdict({"unprofitable": 0, "noise": 0, "strong": 79}, counts)["accepted"]


def test_one_small_campaign_applies_the_bound():
    gate = _load("crypto_gate_control")
    row = gate.run_campaign(25.0, 900_002, n_sessions=50, b=50,
                            attribution="fill", profit_alpha=0.01)
    assert row["passed"] == (row["rule_b"] and row["profitability"]["passes"])
    assert row["profitability"]["alpha"] == 0.01
