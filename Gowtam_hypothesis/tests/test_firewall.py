import pytest

from researcher import leak_lint, feasibility
from researcher.schemas import DataRequires, HypothesisRecord, SpecData, ParameterDef, SourceClaim


@pytest.mark.parametrize("text", [
    "NQ declined 4% in March",
    "the current price is 21000",
    "over the last 20 sessions ES rose",
    "QQQ rallied 2% during the previous 5 weeks",
    "ES closed down 10 points on 2025-03-14",
])
def test_leak_lint_flags_realized_outcomes(text):
    assert leak_lint.findings(text)
    with pytest.raises(leak_lint.OutcomeLeak):
        leak_lint.lint(text)


@pytest.mark.parametrize("text", [
    "A purely mechanistic claim about how momentum forms.",
    "If the effect is real, then later prices drift back toward the intraday anchor.",
])
def test_leak_lint_allows_mechanism_prose(text):
    assert not leak_lint.findings(text)
    leak_lint.lint(text)  # does not raise


def _rec(interval="5m", ticks=False, spreads=False) -> HypothesisRecord:
    return HypothesisRecord(
        id="x-1", mechanism="m", falsifiable="If real then X.",
        operationalize="o",
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
        data_requires=DataRequires(interval=interval, needs_ticks=ticks,
                                   needs_spreads=spreads),
        sources=(SourceClaim(claim="c", source_url="https://a.b"),),
    )


def test_feasibility_bars_only_default():
    caps = feasibility.DatasetCaps()
    assert feasibility.feasible(_rec(), caps)


def test_feasibility_rejects_ticks():
    assert not feasibility.feasible(_rec(ticks=True), feasibility.DatasetCaps())


def test_feasibility_rejects_bad_interval():
    assert not feasibility.feasible(_rec(interval="1h"), feasibility.DatasetCaps())


def test_feasibility_check_raises_named():
    with pytest.raises(feasibility.Infeasible):
        feasibility.check(_rec(ticks=True), feasibility.DatasetCaps())


def test_feasibility_rejects_excessive_grid_size():
    from researcher.schemas import ParameterDef, SpecData
    # 100 * 100 * 2 = 20,000 > 10,000 limit
    big_params = (
        ParameterDef("p1", 1.0, 100.0, 1.0),
        ParameterDef("p2", 1.0, 100.0, 1.0),
        ParameterDef("p3", 1.0, 2.0, 1.0),
    )
    rec = _rec()
    rec = HypothesisRecord(
        id=rec.id, mechanism=rec.mechanism, falsifiable=rec.falsifiable,
        operationalize=rec.operationalize,
        specification=SpecData(
            signal_def="s", entry_rule="e", exit_rule="x",
            parameters=big_params, bar_size="5m", session_filter="all",
            cost_assumption="1bp", data_fields_required=("close",)
        ),
        data_requires=rec.data_requires, sources=rec.sources,
        confidence_prior=rec.confidence_prior
    )
    caps = feasibility.DatasetCaps()
    assert not feasibility.feasible(rec, caps)
    v = feasibility.violations(rec, caps)
    assert any("search budget limit" in msg for msg in v)


def test_feasibility_rejects_excessive_params():
    from researcher.schemas import ParameterDef, SpecData
    # 5 parameters > 4 limit
    five_params = tuple(ParameterDef(f"p{i}", 1.0, 2.0, 1.0) for i in range(5))
    rec = _rec()
    rec = HypothesisRecord(
        id=rec.id, mechanism=rec.mechanism, falsifiable=rec.falsifiable,
        operationalize=rec.operationalize,
        specification=SpecData(
            signal_def="s", entry_rule="e", exit_rule="x",
            parameters=five_params, bar_size="5m", session_filter="all",
            cost_assumption="1bp", data_fields_required=("close",)
        ),
        data_requires=rec.data_requires, sources=rec.sources,
        confidence_prior=rec.confidence_prior
    )
    caps = feasibility.DatasetCaps()
    assert not feasibility.feasible(rec, caps)
    v = feasibility.violations(rec, caps)
    assert any("parameters count 5 exceeds limit" in msg for msg in v)
