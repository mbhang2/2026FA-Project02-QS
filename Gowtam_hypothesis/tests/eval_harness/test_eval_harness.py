import pytest
from unittest.mock import patch, MagicMock

from researcher.schemas import SourceClaim, HypothesisRecord, DataRequires, SpecData, ParameterDef
from researcher import generate, driver, leak_lint
from researcher.retrieval import SourceClip
from src.core.loop.hypothesis import HypothesisRejected
from researcher.render import render

# Eval Harness for Researcher Add-ons E1
# Tests lint recall and checks against adversarial behaviors like hallucinated citations.

@pytest.fixture
def clean_record():
    return HypothesisRecord(
        id="test-clean",
        mechanism="Test mechanism",
        falsifiable="If true then X.",
        operationalize="Test op",
        specification=SpecData(
            signal_def="x = close", 
            entry_rule="x > 0", 
            exit_rule="x < 0",
            parameters=(ParameterDef("p1", 1.0, 10.0, 1.0),),
            bar_size="5m", 
            session_filter="all", 
            cost_assumption="1bp",
            data_fields_required=("close",)
        ),
        data_requires=DataRequires(),
        sources=(SourceClaim("claim1", "http://arxiv.org/123", "Title1"),),
        confidence_prior="test"
    )

def test_leak_lint_recall_adversarial(clean_record):
    """Test if the leak lint catches planted outcome leaks."""
    leaky_text = render(clean_record) + "\nIn our backtest, NQ declined 4%."
    with pytest.raises(leak_lint.OutcomeLeak):
        leak_lint.lint(leaky_text, source="test")

def test_gate_vocabulary_lint_recall(clean_record):
    """Test if the rail's deterministic lint catches gate vocabulary."""
    from src.core.loop.hypothesis import lint as rail_lint
    leaky_text = render(clean_record) + "\nThe p-value was significant, so we reject the null."
    with pytest.raises(HypothesisRejected):
        rail_lint(leaky_text, source="test")

def test_driver_blocks_fake_citation(clean_record):
    """Test if driver blocks when model cites a URL not in the retrieved sources."""
    clips = [SourceClip("Title2", "http://arxiv.org/OTHER", "Summary2", "arxiv")]
    errors = driver._lint_findings(clean_record, clips)
    assert any("bad_source: cited" in e for e in errors)
    assert any("http://arxiv.org/123" in e for e in errors)

def test_driver_accepts_valid_citation(clean_record):
    """Test if driver allows citations that accurately reflect the retrieved clips."""
    clips = [SourceClip("Title1", "http://arxiv.org/123", "Summary1", "arxiv")]
    errors = driver._lint_findings(clean_record, clips)
    assert not any("bad_source" in e for e in errors)
