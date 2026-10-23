"""Unit tests for Gowtam_hypothesis.campaign_loop."""

from __future__ import annotations

import json
import pathlib
import tempfile
import pytest

from Gowtam_hypothesis import campaign_loop
from Gowtam_hypothesis.researcher.schemas import HypothesisRecord


def test_slugify():
    assert campaign_loop.slugify("Build a Profitable Scalping System!") == "build-a-profitable-scalping-system"
    assert campaign_loop.slugify("   ") == "research"
    assert len(campaign_loop.slugify("a" * 100, max_len=20)) <= 20


def test_evaluate_promotion_success():
    verdict, reasons = campaign_loop.evaluate_promotion(
        observed_best=0.35,
        gate_accept=True,
        bwis_band_val="high",
        bwis_score_val=0.68,
        tier3_verdict_val="PASS",
    )
    assert verdict == "PROMOTE"
    assert any("positive" in r for r in reasons)
    assert any("ACCEPT" in r for r in reasons)


def test_evaluate_promotion_negative_score():
    verdict, reasons = campaign_loop.evaluate_promotion(
        observed_best=-0.05,
        gate_accept=True,
        bwis_band_val="high",
        bwis_score_val=0.68,
        tier3_verdict_val="PASS",
    )
    assert verdict == "REJECT"
    assert any("not positive" in r for r in reasons)


def test_evaluate_promotion_gate_reject():
    verdict, reasons = campaign_loop.evaluate_promotion(
        observed_best=0.45,
        gate_accept=False,
        bwis_band_val="high",
        bwis_score_val=0.68,
        tier3_verdict_val="PASS",
    )
    assert verdict == "REJECT"
    assert any("did not accept" in r for r in reasons)


def test_evaluate_promotion_bwis_low():
    verdict, reasons = campaign_loop.evaluate_promotion(
        observed_best=0.45,
        gate_accept=True,
        bwis_band_val="low",
        bwis_score_val=0.42,
        tier3_verdict_val="PASS",
    )
    assert verdict == "REJECT"
    assert any("BWIS" in r and "low" in r for r in reasons)


def test_run_research_phase_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = pathlib.Path(tmpdir) / "hypotheses"
        log_dir = pathlib.Path(tmpdir) / "logs"
        emitted = campaign_loop.run_research_phase(
            question="Scalping question",
            theme="Momentum",
            budget=2,
            output_dir=out_dir,
            log_dir=log_dir,
            dry_run=True,
        )
        assert len(emitted) == 2
        for rec, path in emitted:
            assert isinstance(rec, HypothesisRecord)
            assert path.exists()
            assert rec.id in path.name


def test_run_single_campaign_dry_run(monkeypatch):
    from Gowtam_hypothesis.researcher.schemas import (
        DataRequires,
        ParameterDef,
        SourceClaim,
        SpecData,
    )
    spec = SpecData(
        signal_def="close - open",
        entry_rule="Enter long at close when signal > threshold",
        exit_rule="Exit at session close",
        parameters=(ParameterDef("threshold", 0.1, 0.5, 0.1),),
        bar_size="5m",
        session_filter="London open",
        cost_assumption="0.06 round trip per unit",
        data_fields_required=("open", "close", "vwap"),
    )
    rec = HypothesisRecord(
        id="test-hypo-01",
        mechanism="Test mechanism",
        falsifiable="If momentum persists then buying session breakouts produces positive Sortino.",
        operationalize="5m bar close relative to vwap",
        specification=spec,
        data_requires=DataRequires(interval="5m"),
        sources=(
            SourceClaim(
                claim="Momentum persists intraday in currency and commodities.",
                source_url="https://arxiv.org/abs/2101.00001",
                source_title="Momentum Reference",
            ),
        ),
        bwis_score=0.65,
    )
    hyp_path = pathlib.Path("/nonexistent/test-hypo-01.md")
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(campaign_loop, "REPO_ROOT", pathlib.Path(tmpdir))
        outcome = campaign_loop.run_single_campaign(
            rec=rec,
            hyp_path=hyp_path,
            profile="xauusd-research",
            trials=5,
            stages=("register", "run", "nulls", "report"),
            dry_run=True,
        )
        assert outcome.hypothesis_id == "test-hypo-01"
        assert outcome.bwis_band == "high"
        assert outcome.bwis_score == 0.65
        assert outcome.n_trials == 5
        assert outcome.promotion_verdict == "PROMOTE"
        assert outcome.can_promote is True


def test_write_summary_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = pathlib.Path(tmpdir)
        outcome = campaign_loop.CampaignOutcome(
            hypothesis_id="h1",
            hypothesis_title="Title 1",
            mechanism="Mech 1",
            bwis_score=0.62,
            bwis_band="high",
            campaign_id="c1",
            profile="xauusd-research",
            trials_budget=10,
            n_trials=10,
            observed_best=0.15,
            gate_accept=True,
            can_promote=True,
            promotion_verdict="PROMOTE",
        )
        summary_path = campaign_loop.write_summary_report(
            question="Test Q",
            theme="Test T",
            profile="xauusd-research",
            outcomes=[outcome],
            out_dir=out_dir,
        )
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["question"] == "Test Q"
        assert summary["total_campaigns"] == 1
        assert summary["promoted_count"] == 1
        assert len(summary["campaigns"]) == 1

        jsonl_path = out_dir / "results.jsonl"
        assert jsonl_path.exists()
        lines = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0]["hypothesis_id"] == "h1"


def test_main_dry_run(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(campaign_loop, "REPO_ROOT", pathlib.Path(tmpdir))
        out_path = pathlib.Path(tmpdir) / "run_out"
        code = campaign_loop.main([
            "--question", "Build a scalping system for XAU/USD",
            "--theme", "Trend and momentum",
            "--budget", "2",
            "--trials", "5",
            "--out", str(out_path),
            "--dry-run",
        ])
        assert code == 0
        assert (out_path / "summary.json").exists()
        assert (out_path / "results.jsonl").exists()
        summary = json.loads((out_path / "summary.json").read_text())
        assert summary["total_campaigns"] == 2
