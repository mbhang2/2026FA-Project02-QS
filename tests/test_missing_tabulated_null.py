"""A dataset with no tabulated null loses a diagnostic and keeps its gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.core.loop.campaign import summarise
from src.core.scoring.calibration import null as nullmod

ROOT = Path(__file__).resolve().parents[1]


def _score(primary: float) -> dict:
    return {"primary": primary, "sharpe": primary, "skew": 0.0, "kurtosis": 3.0, "n_sessions": 50,
            "net_pnl": 10.0, "trade_count": 20, "rejected": False, "reject_reason": "",
            "metric": "sortino_per_session", "sortino": primary, "downside_sessions": 20}


def _result():
    rows = [{"campaign_index": i, "strategy_digest": f"{i:08d}", "outcome": "completed",
             "score": _score(p)} for i, p in enumerate((0.05, 0.21, 0.12))]
    record = SimpleNamespace(campaign_id="c", q=0.05, budget=3, correction_rule="independent")
    return SimpleNamespace(trials=rows, rows=rows, record=record, record_digest="d" * 64,
                           tampered=False, harness_failures=[], unaccounted=[], wall_clock_s=1.0)


def _nulls():
    rng = np.random.default_rng(7)
    return [rng.normal(0.0, 0.1, 2_000) for _ in range(3)]


def test_the_gate_is_the_same_with_and_without_the_table(tmp_path, monkeypatch):
    with_table = summarise(_result(), dataset="synthetic", null_samples=_nulls(), q=0.05)
    monkeypatch.setattr(nullmod, "null_path_for", lambda name: tmp_path / "absent.json")
    without = summarise(_result(), dataset="synthetic", null_samples=_nulls(), q=0.05)
    assert without["gate"] == with_table["gate"]
    assert without["gate"]["is_the_gate"] is True
    assert without["per_trial_accepts_at_0.001"] is None
    assert without["diagnostics_only"]["correction"] is None
    assert "no tabulated null" in without["diagnostics_only"]["unavailable"]
    assert "uncorrected" not in without and "deflation" not in without
    assert "unavailable" not in with_table["diagnostics_only"]


def test_the_report_renders_without_the_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(nullmod, "null_path_for", lambda name: tmp_path / "absent.json")
    summary = summarise(_result(), dataset="synthetic", null_samples=_nulls(), q=0.05)
    spec = importlib.util.spec_from_file_location("campaign_script", ROOT / "scripts" / "campaign.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert "THE GATE" in script.render(summary)


def test_with_no_nulls_a_missing_table_is_still_an_error():
    with pytest.raises(FileNotFoundError):
        summarise(_result(), dataset="no-such-dataset", null_samples=None, q=0.05)
