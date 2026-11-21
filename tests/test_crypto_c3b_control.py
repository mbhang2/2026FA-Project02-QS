"""C3b's fixed design and decision rules. Neither real stage runs here."""

from __future__ import annotations

import importlib.util
import pathlib
import statistics
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "crypto_c3b_control", REPO / "scripts" / "crypto_c3b_control.py")
c3b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c3b)


def test_the_design_is_the_preregistered_one():
    assert c3b.EDGES_BP == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
    assert (c3b.TRUTH_SEEDS[0], c3b.TRUTH_SEEDS[-1], len(c3b.TRUTH_SEEDS)) == (100_001, 100_200, 200)
    assert (c3b.TEST_SEEDS[0], c3b.TEST_SEEDS[-1], len(c3b.TEST_SEEDS)) == (200_001, 201_000, 1000)
    assert c3b.MAX_PASSES == 36
    assert c3b.T_CRITICAL_99 == 2.601
    assert not set(c3b.TRUTH_SEEDS) & set(c3b.TEST_SEEDS)


def test_the_pass_limit_is_the_largest_count_under_the_wilson_bound():
    assert c3b.gate.wilson(36, 1000)[1] <= 0.05 < c3b.gate.wilson(37, 1000)[1]
    assert c3b.verdict(36, 1000)["c3b_passed"]
    assert not c3b.verdict(37, 1000)["c3b_passed"]
    with pytest.raises(ValueError):
        c3b.verdict(0, 999)


def test_upper_bound_is_the_t_interval():
    values = [-3.0, -2.0, -1.0, -2.0]
    want = -2.0 + c3b.T_CRITICAL_99 * statistics.stdev(values) / 2
    assert c3b.upper_bound(values) == pytest.approx(want)


def test_the_largest_edge_with_every_variant_losing_is_selected():
    assert c3b.select_edge({1.0: [-2, -1], 3.0: [-1, -0.1], 5.0: [-1, 0.1], 8.0: [0.2, 0.3]}) == 3.0


def test_an_edge_with_a_bound_at_zero_does_not_qualify():
    assert c3b.select_edge({1.0: [-1.0, 0.0], 2.0: [0.1, 0.2]}) is None


def test_a_stage_can_be_claimed_only_once(tmp_path):
    path = tmp_path / "nested" / "truth.json"
    c3b.claim(path)
    with pytest.raises(SystemExit, match="already been claimed"):
        c3b.claim(path)


def test_step_two_refuses_without_a_finished_step_one(tmp_path, monkeypatch):
    monkeypatch.setattr(c3b, "provenance", lambda: {})
    with pytest.raises(SystemExit, match="no finished result"):
        c3b.stage_test(tmp_path, 1)
    c3b.claim(tmp_path / "truth.json")          # claimed but crashed before writing
    with pytest.raises(SystemExit, match="no finished result"):
        c3b.stage_test(tmp_path, 1)
    assert not (tmp_path / "test.json").exists()


def test_step_two_stops_when_no_edge_qualified(tmp_path, monkeypatch):
    monkeypatch.setattr(c3b, "provenance", lambda: {})
    (tmp_path / "truth.json").write_text('{"git_sha": "x", "selected_edge_bp": null}')
    with pytest.raises(SystemExit, match="not run"):
        c3b.stage_test(tmp_path, 1)
    assert not (tmp_path / "test.json").exists()


def test_the_variant_measure_runs_on_a_small_market():
    means = c3b.gate.variant_mean_returns(1.0, 900_001, n_sessions=50)
    assert len(means) == len(c3b.gate.VARIANTS)
    assert all(isinstance(x, float) for x in means)
