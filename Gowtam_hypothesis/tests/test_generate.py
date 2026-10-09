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
    monkeypatch.setattr(generate, "propose", lambda m, s: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    monkeypatch.setattr(generate, "_refine", lambda r, p: r)

    rec = generate.generate_hypothesis(
        "m", [], max_refine=0, advisory=lambda r: ["ungrounded: soft"])
    assert rec.id == "test"


def test_hard_findings_still_block(monkeypatch):
    monkeypatch.setattr(generate, "propose", lambda m, s: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    monkeypatch.setattr(generate, "_refine", lambda r, p: r)

    with pytest.raises(generate.PassBlocked):
        generate.generate_hypothesis(
            "m", [], max_refine=0, finalize=lambda r: ["hard lint"])


def test_soft_findings_join_refine_prompt(monkeypatch):
    monkeypatch.setattr(generate, "propose", lambda m, s: _rec())
    monkeypatch.setattr(generate, "critique", lambda r: _pass())
    seen = []

    def refine(rec, problems):
        seen.append(problems)
        return rec

    monkeypatch.setattr(generate, "_refine", refine)
    generate.generate_hypothesis(
        "m", [], max_refine=1, advisory=lambda r: ["ungrounded: soft"])
    assert seen and "ungrounded: soft" in seen[0]
