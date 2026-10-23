"""Unit tests for Tier 1, 2, and 3 enhancements.

Tests:
1. Tier 1A: summarise() handling datasets with missing empirical null tables (returns far=1.0, deflation note).
2. Tier 1B: Idempotent stage skipping and resumption in run_single_campaign.
3. Tier 2A: BWIS backtest feedback logging and promote_priors aggregation.
4. Tier 2B: Champion Feature Memory recording and context retrieval.
5. Tier 3A: Parallel campaign execution.
6. Tier 3B: Early stopping condition check in stage_run.
"""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from Gowtam_hypothesis.campaign_loop import (
    CampaignOutcome,
    evaluate_promotion,
    run_single_campaign,
    slugify,
)
from Gowtam_hypothesis.researcher import bwis
from Gowtam_hypothesis.researcher.feature_memory import FeatureEntry, FeatureMemory
from Gowtam_hypothesis.researcher.schemas import (
    DataRequires,
    HypothesisRecord,
    ParameterDef,
    SourceClaim,
    SpecData,
)
from src.core.loop.campaign import CampaignRecord, CampaignResult, summarise


@pytest.fixture
def sample_hypothesis() -> HypothesisRecord:
    return HypothesisRecord(
        id="test-vwap-meanreversion",
        mechanism="Intraday VWAP mean reversion on price stretch",
        falsifiable="If price stretches 2 ATR from VWAP, it reverts within the session.",
        operationalize="5m bar close relative to VWAP bands",
        specification=SpecData(
            signal_def="(close - vwap) / atr_14",
            entry_rule="enter long when signal < -2.0",
            exit_rule="exit at vwap cross or session close",
            parameters=(ParameterDef("stretch_atr", 1.5, 3.0, 0.5),),
            bar_size="5m",
            session_filter="London open",
            cost_assumption="0.06 round trip per unit",
            data_fields_required=("close", "vwap", "high", "low"),
        ),
        data_requires=DataRequires(interval="5m"),
        sources=(
            SourceClaim(
                claim="Mean reversion to VWAP is documented in intraday gold futures.",
                source_url="https://arxiv.org/abs/2101.99999",
                source_title="VWAP Microstructure Paper",
            ),
        ),
        bwis_score=0.68,
    )


# ---------------------------------------------------------------------------
# Tier 1A Tests: Graceful Missing Tabulated Null Handling
# ---------------------------------------------------------------------------

def test_summarise_with_missing_empirical_null():
    """Verify summarise() does not raise FileNotFoundError when empirical null is absent."""
    record = CampaignRecord(
        campaign_id="test-camp-01",
        profile="xauusd-research",
        budget=2,
        q=0.05,
        agent="dummy",
        dataset="xauusd",
        split="train",
        hypothesis_path="config/hypotheses/test.md",
        hypothesis_digest="abc123",
    )
    rows = [
        {
            "campaign_index": 0,
            "strategy_digest": "strat1",
            "outcome": "completed",
            "score": {
                "primary": 0.15,
                "sharpe": 0.15,
                "sortino": 0.18,
                "skew": 0.0,
                "kurtosis": 3.0,
                "n_sessions": 40,
                "net_pnl": 1200.0,
                "trade_count": 25,
                "rejected": False,
                "reject_reason": None,
                "metric": "sortino_per_session",
                "downside_sessions": 10,
            },
        },
        {
            "campaign_index": 1,
            "strategy_digest": "strat2",
            "outcome": "completed",
            "score": {
                "primary": 0.05,
                "sharpe": 0.05,
                "sortino": 0.06,
                "skew": 0.0,
                "kurtosis": 3.0,
                "n_sessions": 40,
                "net_pnl": 300.0,
                "trade_count": 15,
                "rejected": False,
                "reject_reason": None,
                "metric": "sortino_per_session",
                "downside_sessions": 12,
            },
        },
    ]
    result = CampaignResult(record=record, record_digest="digest123", rows=rows)
    # Using null_samples simulates sign-flip MC null from stage nulls
    null_samples = [[0.01, 0.02, 0.03], [0.01, 0.015, 0.025]]

    summary = summarise(result, dataset="xauusd", null_samples=null_samples, q=0.05)
    assert "gate" in summary
    assert "deflation" in summary
    # Deflation view caught FileNotFoundError and set expected_max_under_null to None
    assert summary["deflation"]["expected_max_under_null"] is None
    assert "no tabulated null" in summary["deflation"]["note"]


# ---------------------------------------------------------------------------
# Tier 1B Tests: Idempotency & Resumption in run_single_campaign
# ---------------------------------------------------------------------------

def test_run_single_campaign_dry_run_idempotent(tmp_path, sample_hypothesis):
    """Verify run_single_campaign runs cleanly in dry-run mode and respects outcomes."""
    hyp_file = tmp_path / "hyp.md"
    hyp_file.write_text("# Test Hypothesis")

    outcome = run_single_campaign(
        rec=sample_hypothesis,
        hyp_path=hyp_file,
        profile="xauusd-research",
        trials=5,
        stages=("register", "run", "nulls", "report"),
        dry_run=True,
    )
    assert outcome.hypothesis_id == sample_hypothesis.id
    assert outcome.bwis_band == "high"
    assert outcome.gate_accept is True
    assert outcome.promotion_verdict == "PROMOTE"


def test_stage_skipping_when_artifacts_exist(tmp_path, sample_hypothesis):
    """Verify run_single_campaign skips already-executed stages."""
    hyp_file = tmp_path / "hyp.md"
    hyp_file.write_text("# Test Hypothesis")

    campaign_name = f"{sample_hypothesis.id}-resumable"
    camp_dir = tmp_path / "runs" / "campaigns" / campaign_name
    camp_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate artifacts
    (camp_dir / "campaign.json").write_text(json.dumps({"budget": 2, "profile": "xauusd-research"}))
    (camp_dir / "ledger.jsonl").write_text('{"trial": 0, "outcome": "completed"}\n{"trial": 1, "outcome": "completed"}\n')
    (camp_dir / "nulls.json").write_text(json.dumps({"samples": []}))
    (camp_dir / "summary.json").write_text(json.dumps({
        "n_trials": 2,
        "outcomes": {"completed": 2},
        "gate": {"accept": True, "observed_best": 0.42, "p": 0.01},
    }))
    (camp_dir / "tier2-summary.json").write_text(json.dumps({"dummy": 1}))

    with patch("Gowtam_hypothesis.campaign_loop.REPO_ROOT", tmp_path), \
         patch("subprocess.run") as mock_sub:
        outcome = run_single_campaign(
            rec=sample_hypothesis,
            hyp_path=hyp_file,
            profile="xauusd-research",
            trials=2,
            stages=("register", "run", "nulls", "report"),
            dry_run=False,
            resume=True,
        )
        # All stages should have been skipped, so subprocess.run should NEVER have been called!
        assert mock_sub.call_count == 0
        assert outcome.gate_accept is True
        assert outcome.observed_best == 0.42


# ---------------------------------------------------------------------------
# Tier 2A Tests: Empirical Backtest Feedback into BWIS
# ---------------------------------------------------------------------------

def test_record_backtest_feedback(tmp_path):
    """Verify backtest outcomes are logged to bwis_feedback.jsonl."""
    feedback_file = tmp_path / "bwis_feedback.jsonl"
    sources = [{"source_url": "https://arxiv.org/abs/2101.00001", "backend": "arxiv"}]

    bwis.record_backtest_feedback(
        hypothesis_id="hypo-1",
        theme="Mean Reversion",
        sources=sources,
        observed_best=0.35,
        gate_accept=True,
        feedback_file=feedback_file,
    )

    bwis.record_backtest_feedback(
        hypothesis_id="hypo-2",
        theme="Mean Reversion",
        sources=sources,
        observed_best=-0.12,
        gate_accept=False,
        feedback_file=feedback_file,
    )

    assert feedback_file.exists()
    lines = [json.loads(x) for x in feedback_file.read_text().splitlines() if x.strip()]
    assert len(lines) == 2
    assert lines[0]["empirical_verdict"] == "accept"
    assert lines[1]["empirical_verdict"] == "flat_or_loss"

    # Verify aggregation in promote_priors
    from Gowtam_hypothesis.bwis_probe.promote_priors import aggregate_feedback
    weights = aggregate_feedback(feedback_file)
    assert "by_class" in weights
    assert "academic" in weights["by_class"]
    assert weights["by_class"]["academic"]["count"] == 2


# ---------------------------------------------------------------------------
# Tier 2B Tests: Shared Champion Feature Memory
# ---------------------------------------------------------------------------

def test_feature_memory(tmp_path, sample_hypothesis):
    """Verify FeatureMemory records outcomes and provides relevant context."""
    mem_file = tmp_path / "feature_memory.json"
    mem = FeatureMemory(path=mem_file)

    outcome = CampaignOutcome(
        hypothesis_id=sample_hypothesis.id,
        hypothesis_title="Test Title",
        mechanism=sample_hypothesis.mechanism,
        bwis_score=0.68,
        bwis_band="high",
        campaign_id="camp-123",
        profile="xauusd-research",
        trials_budget=10,
        n_trials=10,
        observed_best=0.28,
        gate_accept=True,
    )

    entry = mem.record_outcome(sample_hypothesis, outcome, theme="Mean Reversion")
    assert entry is not None
    assert "vwap" in entry.indicators
    assert mem_file.exists()

    # Query context
    context = mem.get_feature_context(theme="Mean Reversion")
    assert "Previous high-performing candidate feature patterns" in context
    assert "GATE ACCEPTED" in context
    assert "vwap" in context


# ---------------------------------------------------------------------------
# Tier 3A Tests: Parallel Campaign Execution
# ---------------------------------------------------------------------------

def test_parallel_campaign_dispatch(tmp_path, sample_hypothesis):
    """Verify concurrent.futures handles multiple hypothesis campaigns concurrently."""
    import concurrent.futures

    hyp_file = tmp_path / "hyp.md"
    hyp_file.write_text("# Test")

    pairs = [(sample_hypothesis, hyp_file), (sample_hypothesis, hyp_file)]
    outcomes = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futs = [
            executor.submit(
                run_single_campaign,
                rec=rec,
                hyp_path=path,
                profile="xauusd-research",
                trials=5,
                stages=("register", "run", "nulls", "report"),
                dry_run=True,
            )
            for rec, path in pairs
        ]
        for f in concurrent.futures.as_completed(futs):
            outcomes.append(f.result())

    assert len(outcomes) == 2
    for o in outcomes:
        assert o.promotion_verdict == "PROMOTE"


# ---------------------------------------------------------------------------
# Tier 3B Tests: Early Stopping
# ---------------------------------------------------------------------------

def test_early_stopping_evaluation():
    """Verify evaluate_promotion rejects negative scores correctly."""
    verdict, reasons = evaluate_promotion(
        observed_best=-0.05,
        gate_accept=False,
        bwis_band_val="high",
        bwis_score_val=0.70,
        tier3_verdict_val="PASS",
    )
    assert verdict == "REJECT"
    assert any("not positive" in r for r in reasons)
