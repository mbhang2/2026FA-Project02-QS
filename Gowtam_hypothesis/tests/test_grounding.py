from researcher import grounding
from researcher.retrieval import SourceClip
from researcher.schemas import DataRequires, HypothesisRecord, SpecData, ParameterDef, SourceClaim


def _rec():
    return HypothesisRecord(
        id="test",
        mechanism="m",
        falsifiable="if X then Y",
        operationalize="op",
        specification=SpecData(
        signal_def="x=1",
        entry_rule="x>0",
        exit_rule="x<0",
        parameters=(ParameterDef("p1", 1, 10, 1),),
        bar_size="5m",
        session_filter="all",
        cost_assumption="1bp",
        data_fields_required=("close",)
    ),
        data_requires=DataRequires(),
        sources=(SourceClaim(claim="CPI is 2.7%", source_url="https://a.com/1",
                             source_title="A"),),
        confidence_prior="c",
    )


def _clips():
    return [SourceClip(title="A", url="https://a.com/1", summary="doc text",
                       backend="arxiv")]


def test_disabled_without_base_url(monkeypatch):
    monkeypatch.delenv("MINICHECK_API_BASE", raising=False)
    assert grounding.check_claims(_rec(), _clips()) == []


def test_ungrounded_claim_flagged(monkeypatch):
    monkeypatch.setenv("MINICHECK_API_BASE", "http://localhost:8090/v1")
    monkeypatch.setattr(grounding, "_ask", lambda doc, claim: False)
    findings = grounding.check_claims(_rec(), _clips())
    assert any("ungrounded" in f for f in findings)


def test_grounded_claim_clean(monkeypatch):
    monkeypatch.setenv("MINICHECK_API_BASE", "http://localhost:8090/v1")
    monkeypatch.setattr(grounding, "_ask", lambda doc, claim: True)
    assert grounding.check_claims(_rec(), _clips()) == []


def test_ambiguous_verdict_clean(monkeypatch):
    monkeypatch.setenv("MINICHECK_API_BASE", "http://localhost:8090/v1")
    monkeypatch.setattr(grounding, "_ask", lambda doc, claim: None)
    assert grounding.check_claims(_rec(), _clips()) == []


def test_endpoint_trouble_degrades(monkeypatch):
    monkeypatch.setenv("MINICHECK_API_BASE", "http://localhost:8090/v1")

    def boom(doc, claim):
        raise grounding.GroundingUnavailable("down")

    monkeypatch.setattr(grounding, "_ask", boom)
    assert grounding.check_claims(_rec(), _clips()) == []


def test_hallucinated_citation_skipped(monkeypatch):
    # A URL not among the clips is the deterministic lint's job, not grounding's.
    monkeypatch.setenv("MINICHECK_API_BASE", "http://localhost:8090/v1")
    calls = []

    def spy(doc, claim):
        calls.append(claim)
        return False

    monkeypatch.setattr(grounding, "_ask", spy)
    rec = HypothesisRecord(
        id="test", mechanism="m", falsifiable="if X then Y",
        operationalize="op", specification=SpecData(
        signal_def="x=1",
        entry_rule="x>0",
        exit_rule="x<0",
        parameters=(ParameterDef("p1", 1, 10, 1),),
        bar_size="5m",
        session_filter="all",
        cost_assumption="1bp",
        data_fields_required=("close",)
    ), data_requires=DataRequires(),
        sources=(SourceClaim(claim="c", source_url="https://not-retrieved.com/x"),),
        confidence_prior="c",
    )
    assert grounding.check_claims(rec, _clips()) == []
    assert calls == []
