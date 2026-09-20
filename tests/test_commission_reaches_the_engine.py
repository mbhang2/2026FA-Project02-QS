"""The rate a profile declares is the rate the engine charges.

Task 60. `commission_per_contract` was declared in `config/run.yaml`, copied into
the campaign record, and printed in the agent's run facts -- and `run.py` never
passed it to `Engine`, which fell back to its own default of 0.01. That default
is scaled for the synthetic generator's price units and is **ten times too cheap
on NQ**.

So `nq-mvp-01` ran at a tenth of its stated cost: 206,710 fills that should have
paid 20,671 index points paid 2,067, and every artefact in the campaign said
0.10. The gap is 18,604 points, about **$372,000**, on one campaign.

Nothing cross-checked the configured rate against the charged total, and the
identity `commission == rate x contract-fills` would have caught it on the first
trial. It is now asserted inside `_run_and_score`, and these tests check both
that the plumbing works and that the assertion bites.
"""

from __future__ import annotations

import pytest

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.types import Split

REFERENCE = "src/strategies/current.py"


def _run(commission: float | None):
    """A short synthetic walk, optionally with an explicit commission."""
    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=40, ticks_per_session=390, vol_per_tick=0.15,
        seed=4_242, signal_strength=0.0))
    ns: dict = {}
    exec(compile(open(REFERENCE).read(), REFERENCE, "exec"), ns)
    # The committed file names its class `MomentumStrategy`; agent-written ones
    # name it `Strategy`. Find whichever class the module defines rather than
    # hardcoding a name this test does not own.
    cls = next(v for k, v in ns.items()
               if isinstance(v, type) and not k.startswith("_"))
    kw = {} if commission is None else {"commission_per_contract": commission}
    engine = Engine.for_test(ds, cls(), split=Split.TRAIN,
                             warmup_sessions=0, fill_model="unfavorable",
                             seed=1, **kw)
    engine.run()
    return engine.reconstructed()


def _charged(blotter):
    return sum(r.commission for r in blotter.rows)


def _contract_fills(blotter):
    return sum(r.size for r in blotter.rows)


def test_the_engine_charges_the_rate_it_is_given():
    for rate in (0.01, 0.10, 0.25):
        b = _run(rate)
        assert _contract_fills(b) > 0, "the reference strategy should trade"
        assert _charged(b) == pytest.approx(rate * _contract_fills(b)), (
            f"asked for {rate} per contract per side and the blotter did not "
            "pay it")


def test_the_default_is_the_synthetic_one_and_is_ten_times_too_cheap_for_nq():
    """Stated rather than left implicit. The default is correct for synthetic
    price units and wrong for NQ, which is why a profile must override it and
    why the override failing silently was expensive."""
    b = _run(None)
    assert _charged(b) == pytest.approx(0.01 * _contract_fills(b))
    assert 0.01 * 10 == pytest.approx(0.10), "NQ's configured rate is 0.10"


def test_changing_the_rate_changes_the_pnl_by_exactly_the_difference():
    """The arithmetic that identified the bug on the real campaign: the net moved
    18,604 points on 206,710 fills, which is exactly 0.09 each."""
    lo, hi = _run(0.01), _run(0.10)
    fills = _contract_fills(lo)
    assert _contract_fills(hi) == fills, (
        "the commission must not change which trades are taken -- if it does, "
        "this comparison is between two different runs")
    delta = _charged(hi) - _charged(lo)
    assert delta == pytest.approx(0.09 * fills)


def test_run_py_passes_the_profile_rate_through():
    """The specific line that was missing. Asserted on the source because the
    alternative is a full subprocess run for a one-line plumbing fact."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "run.py").read_text()
    engine_call = src[src.index("engine = Engine("):]
    engine_call = engine_call[:engine_call.index("\n            )")]
    assert "commission_per_contract" in engine_call, (
        "run.py builds the Engine without the profile's commission again")


def test_the_invariant_is_asserted_where_the_record_is_built():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "run.py").read_text()
    body = src[src.index("def _run_and_score"):src.index("def _failure_record")]
    assert "commission_per_contract" in body and "ScoringMismatch" in body, (
        "the charged-vs-configured check has been removed from _run_and_score")


def test_every_nq_profile_declares_its_commission():
    """The class of bug, not the instance.

    Task 60 fixed `run.py` so a declared rate reaches the engine, and added an
    invariant that charged must equal declared. Neither can see an omission: with
    no key in the profile, `want` falls back to the engine default, charged equals
    want, and the invariant is satisfied by a run costed ten times too cheap.

    `nq-validation` was in exactly that state -- and it is the profile the protocol
    uses to check a campaign's winner against its train result, so the two splits
    would have been priced differently and the gap read as out-of-sample decay.

    Any profile on real NQ data states its own commission. The default belongs to
    the synthetic generator's price units and is never right here.
    """
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config/run.yaml").read_text())
    nq = {name: p for name, p in cfg["profiles"].items()
          if p.get("dataset", "").startswith("nq")}
    assert nq, "no NQ profiles found -- this test is looking in the wrong place"
    missing = [n for n, p in nq.items() if "commission_per_contract" not in p]
    assert not missing, (
        f"NQ profiles without a declared commission: {missing}. They would run at "
        "the engine default of 0.01, which is a tenth of NQ's real cost, and "
        "nothing downstream would notice.")
    rates = {n: p["commission_per_contract"] for n, p in nq.items()}
    assert len(set(rates.values())) == 1, (
        f"NQ profiles disagree on commission: {rates}. Scores from different "
        "splits are compared directly and cannot be priced differently.")
