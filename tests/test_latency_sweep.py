"""The latency analysis, and the nanosecond rules around it. Synthetic data only."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import prereg  # noqa: E402
import run as runner  # noqa: E402

_spec = importlib.util.spec_from_file_location("latency_sweep", REPO / "scripts" / "latency_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)
NQ_CAMPAIGN = REPO / "scripts" / "nq_campaign.py"


def _point(latency, passes, mean=0.1, outcome="completed"):
    return {"latency_ns": latency, "outcome": outcome,
            "bound": {"passes": passes, "mean": mean, "lower_bound": 0.0}}


def _signed(monkeypatch):
    real = prereg.in_force
    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: n == 19 or real(n, text))


def test_the_grids_are_ascending_and_start_at_zero():
    for grid in sweep.DEFAULT_GRIDS.values():
        assert sweep.check_grid(grid) == grid and grid[0] == 0
    assert all(x == 0 or x >= 1_000_000 for x in sweep.DEFAULT_GRIDS["crypto"])


@pytest.mark.parametrize("text", ["5", "10,5", "1,1", "-1,5", "1.5,2", "a,b"])
def test_a_bad_grid_is_refused(text):
    with pytest.raises(SystemExit):
        sweep.parse_grid(text)


def test_the_summary_finds_the_profitable_range():
    s = sweep.summarise([_point(0, True), _point(10, True), _point(20, False, -0.1),
                         _point(30, False, -0.2)])
    assert (s["lowest_profitable_ns"], s["highest_profitable_ns"]) == (0, 10)
    assert s["profitable_range_is_contiguous"]
    assert s["positive_mean_latencies_ns"] == [0, 10]


def test_the_summary_reports_a_gap_and_a_later_start():
    s = sweep.summarise([_point(0, False), _point(10, True), _point(20, False), _point(30, True)])
    assert s["lowest_profitable_ns"] == 10 and s["highest_profitable_ns"] == 30
    assert not s["profitable_range_is_contiguous"]


def test_the_summary_when_nothing_is_profitable_or_a_point_crashed():
    s = sweep.summarise([_point(0, False, -1.0), _point(10, False, None, outcome="crashed")])
    assert s["lowest_profitable_ns"] is None and s["n_completed"] == 1
    assert not s["profitable_range_is_contiguous"]


def test_it_is_refused_until_amendment_19_is_signed(tmp_path, monkeypatch):
    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: False)
    with pytest.raises(SystemExit, match="not in force"):
        sweep.sweep("nq-train", REPO / "src/strategies/current.py", (0, 10), tmp_path / "s")


@pytest.mark.parametrize("profile", ["crypto-btc-validation", "nq-validation"])
def test_it_refuses_anything_but_train(tmp_path, monkeypatch, profile):
    _signed(monkeypatch)
    with pytest.raises(SystemExit, match="train profile"):
        sweep.sweep(profile, REPO / "src/strategies/current.py", (0, 10_000_000), tmp_path / "s")


def test_it_refuses_a_crypto_latency_below_the_timestamps(tmp_path, monkeypatch):
    _signed(monkeypatch)
    with pytest.raises(SystemExit, match="resolution"):
        sweep.sweep("crypto-btc-train", REPO / "src/strategies/current.py", (0, 999_999),
                    tmp_path / "s")


def test_a_profile_below_the_crypto_resolution_is_refused(monkeypatch):
    _signed(monkeypatch)
    with pytest.raises(ValueError, match="resolution"):
        runner.execution_terms({"dataset": "crypto-btc", "latency_ns": 500_000})
    want = {"dataset": "crypto-btc", "latency_ns": 1_000_000, "allow_overnight": True}
    assert runner.execution_terms(want)["latency_ns"] == 1_000_000
    assert runner.execution_terms({"dataset": "nq", "latency_ns": 1})["latency_ns"] == 1


@pytest.mark.parametrize("value", [1.5, -1, True])
def test_a_profile_latency_must_be_whole_nanoseconds(monkeypatch, value):
    _signed(monkeypatch)
    with pytest.raises(ValueError, match="whole number"):
        runner.execution_terms({"latency_ns": value})


def test_the_override_is_for_train_check_runs_only():
    with pytest.raises(SystemExit, match="train-split check"):
        runner.main(["--human", "--purpose", "smoke", "--profile", "nq-train"],
                    latency_override_ns=1)
    with pytest.raises(SystemExit, match="train-split check"):
        runner.main(["--human", "--purpose", "check", "--profile", "nq-validation"],
                    latency_override_ns=1)


def test_a_train_and_validation_pair_must_share_the_terms(monkeypatch):
    spec = importlib.util.spec_from_file_location("nq_campaign", NQ_CAMPAIGN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pair = {"dataset": "d", "fill_model": "unfavorable", "commission_per_contract": 0.0}
    config = {"profiles": {"x-train": {**pair, "split": "train", "latency_ns": 100},
                           "x-validation": {**pair, "split": "validation", "latency_ns": 50}}}
    monkeypatch.setattr(mod, "cfg", lambda: config)
    with pytest.raises(SystemExit, match="latency_ns"):
        mod.validation_profile_for("x-train", False)


def test_a_real_sweep_on_synthetic_data(tmp_path, monkeypatch):
    _signed(monkeypatch)
    monkeypatch.setattr(sweep, "_BOOTSTRAP", (
        "import prereg; _real = prereg.in_force; "
        "prereg.in_force = lambda n, text=None: n == 19 or _real(n, text)"))
    grid = (0, 45_000_000_000)
    result = sweep.sweep("campaign", REPO / "src/strategies/current.py", grid, tmp_path / "s")
    assert [p["latency_ns"] for p in result["points"]] == list(grid)
    assert all(p["outcome"] == "completed" and p["trades"] for p in result["points"])
    assert result["points"][0]["blotter_digest"] != result["points"][1]["blotter_digest"]
    assert result["alpha_per_point"] == pytest.approx(0.025)
    rows = [json.loads(x) for x in (tmp_path / "s" / "ledger.jsonl").read_text().splitlines()]
    assert [r["purpose"] for r in rows] == ["check", "check"]
    assert "execution" not in rows[0]
    assert rows[1]["execution"] == {**runner.DEFAULT_EXECUTION, "latency_ns": 45_000_000_000}
    with pytest.raises(SystemExit, match="not empty"):
        sweep.sweep("campaign", REPO / "src/strategies/current.py", grid, tmp_path / "s")
