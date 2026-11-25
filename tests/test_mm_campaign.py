"""Amendment 24: market-making campaigns use the net-profit gate and the native runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from src.core.loop import campaign as C  # noqa: E402


class _Record:
    campaign_id, budget, q = "mm", 3, 0.05
    correction_rule = None


class _Result:
    def __init__(self, rows):
        self.record, self.record_digest, self.tampered, self.wall_clock_s = _Record(), "d", False, 1.0
        self.rows = self.trials = rows
        self.harness_failures, self.unaccounted = [], []


def _rows():
    return [{"campaign_index": i, "outcome": "completed",
             "score": {"primary": p, "rejected": False, "n_sessions": 100}} for i, p in enumerate((0.1, 0.4, 0.2))]


def _summary(bounds, **kw):
    import src.core.scoring.calibration.null as nullmod

    original = nullmod.null_path_for
    nullmod.null_path_for = lambda dataset: Path("/nonexistent")
    try:
        return C.summarise(_Result(_rows()), dataset="d", null_samples=[[], [], []], q=0.05,
                           profitability=bounds, gate_kind="spread_capture", **kw)
    finally:
        nullmod.null_path_for = original


def test_the_selected_trial_passes_on_its_bound():
    summary = _summary({"0": {"passes": False}, "1": {"passes": True}, "2": {"passes": False}})
    gate = summary["gate"]
    assert gate["kind"] == "spread_capture" and gate["selected_index"] == 1 and gate["accept"] is True
    assert "p" not in gate


def test_a_failing_or_missing_bound_rejects():
    assert _summary({"1": {"passes": False}})["gate"]["accept"] is False
    assert _summary({})["gate"]["accept"] is False
    assert _summary(None)["gate"]["accept"] is False


def test_excluding_the_best_selects_the_next():
    summary = _summary({"1": {"passes": True}, "2": {"passes": False}}, exclude={1})
    assert summary["gate"]["selected_index"] == 2 and summary["gate"]["accept"] is False


def test_the_gate_is_registered_only_once_amendment_24_is_signed():
    import nq_campaign

    assert nq_campaign.gate_for({"gate": "spread_capture"}, in_force=True) == "spread_capture"
    assert nq_campaign.gate_for({}, in_force=True) is None
    with pytest.raises(SystemExit):
        nq_campaign.gate_for({"gate": "spread_capture"}, in_force=False)


def test_a_native_trial_goes_through_the_native_runner(tmp_path, monkeypatch):
    calls = []
    ledger = tmp_path / "ledger.jsonl"

    def fake_run(argv, **kw):
        calls.append(argv)
        ledger.write_text(json.dumps({"outcome": "completed", "score": None}) + "\n")

    monkeypatch.setattr(C.subprocess, "run", fake_run)
    row = C.run_trial("def kernel(event, book, state, orders):\n    return 0\n", profile="btc-okx-mm-train",
                      ledger=ledger, campaign_id="mm", index=7, native=True)
    argv = calls[0]
    assert argv[0] == C.NATIVE_PYTHON and argv[1] == "scripts/run_native_trial.py"
    assert argv[argv.index("--profile") + 1] == "btc-okx-mm-train"
    kernel = Path(argv[argv.index("--kernel") + 1])
    assert kernel.read_text().startswith("def kernel") and kernel.name == "mm-0007.py"
    assert row["campaign_index"] == 7 and row["campaign_id"] == "mm"


def test_a_native_trial_without_a_row_is_a_harness_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(C.subprocess, "run", lambda argv, **kw: None)
    row = C.run_trial("x", profile="p", ledger=tmp_path / "ledger.jsonl", campaign_id="mm", index=0, native=True)
    assert row["outcome"] == "harness_failed"
