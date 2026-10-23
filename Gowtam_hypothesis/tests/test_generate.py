import pytest

from researcher import generate
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
        sources=(SourceClaim(claim="c", source_url="https://a.com/1"),),
        confidence_prior="c",
    )


def _pass():
    return generate.CriticVerdict(verdict="pass", failures=(), notes="")


def test_advisory_soft_findings_never_block(monkeypatch):
    monkeypatch.setattr(generate, "propose", lambda m, s, **kw: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    monkeypatch.setattr(generate, "_refine", lambda r, p: r)

    rec = generate.generate_hypothesis(
        "m", [], max_refine=0, advisory=lambda r: ["ungrounded: soft"])
    assert rec.id == "test"


def test_hard_findings_still_block(monkeypatch):
    monkeypatch.setattr(generate, "propose", lambda m, s, **kw: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    monkeypatch.setattr(generate, "_refine", lambda r, p: r)

    with pytest.raises(generate.PassBlocked):
        generate.generate_hypothesis(
            "m", [], max_refine=0, finalize=lambda r: ["hard lint"])


def test_soft_findings_join_refine_prompt(monkeypatch):
    monkeypatch.setattr(generate, "propose", lambda m, s, **kw: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    seen = []

    def refine(rec, problems):
        seen.append(problems)
        return rec

    monkeypatch.setattr(generate, "_refine", refine)
    generate.generate_hypothesis(
        "m", [], max_refine=1, advisory=lambda r: ["ungrounded: soft"])
    assert seen and "ungrounded: soft" in seen[0]


def test_refine_with_spec_does_not_crash_keyerror(monkeypatch):
    rec = _rec()
    mock_reply = {
        "mechanism": "new mechanism",
        "falsifiable": "if new then true",
        "operationalize": "new op",
        "specification": {
            "signal_def": "x=2",
            "entry_rule": "x>1",
            "exit_rule": "x<1",
            "parameters": [{"name": "p1", "min_val": 1.0, "max_val": 5.0, "step": 1.0}],
            "bar_size": "5m",
            "session_filter": "all",
            "cost_assumption": "1bp",
            "data_fields_required": ["close"]
        },
        "confidence_prior": "c"
    }
    monkeypatch.setattr(generate, "_ask", lambda *args, **kwargs: mock_reply)
    refined = generate._refine(rec, ["fix something"])
    assert refined.id == rec.id
    assert refined.mechanism == "new mechanism"
    assert refined.specification.signal_def == "x=2"
    assert refined.specification.get_total_grid_size() == 5


def test_refine_malformed_raises_passblocked(monkeypatch):
    rec = _rec()
    monkeypatch.setattr(generate, "_ask", lambda *args, **kwargs: {"falsifiable": "invalid; multiple sentences. Here is two."})
    with pytest.raises(generate.PassBlocked):
        generate._refine(rec, ["fix something"])
