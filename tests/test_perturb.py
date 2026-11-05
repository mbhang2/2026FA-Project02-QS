"""Behaviour that depends on what the question says: `src/core/loop/perturb.py`,
the synthetic options it runs on, and the proposer's repair paths around it.

No network and no subprocess: `run_on_profile` is replaced by a fake that hands
back canned fill logs, so these pin the decisions, not the engine.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.loop import fidelity as F
from src.core.loop import llm
from src.core.loop import perturb as P
from src.core.types import Split

CT = ZoneInfo("America/Chicago")
HYP = ("Entries follow the order flow.\nEntries go against the recent price move.\n"
       "Entries go with the recent price move.\nThe trade fades the move.\n")
FLOW = {"id": "flow", "says": "Entries follow the order flow.", "check": "uses_flow"}
AGAINST = {"id": "against", "says": "Entries go against the recent price move.",
           "check": "trades_move", "direction": "against"}
WITH = {"id": "with", "says": "Entries go with the recent price move.",
        "check": "trades_move", "direction": "with"}


def _cons(tmp_path, items, judged=None):
    f = tmp_path / "x.constraints.yaml"
    doc = {"schema": F.SCHEMA, "constraints": items}
    if judged:
        doc["judged"] = judged
    f.write_text(yaml.safe_dump(doc))
    return F.load(f, HYP)


# --------------------------------------------------------------------------- #
# the synthetic options
# --------------------------------------------------------------------------- #
def test_the_defaults_hash_exactly_as_before():
    """`aggressor` and `session_open_ct` are absent from the digest at their
    defaults, so every dataset already on the ledger keeps its digest."""
    spec = SyntheticSpec(seed=3)
    assert "aggressor" not in spec.as_dict() and "session_open_ct" not in spec.as_dict()


def test_flipped_inverts_sides_and_leaves_prices():
    kw = dict(n_sessions=40, ticks_per_session=120, seed=1)
    a = list(SyntheticDataset(SyntheticSpec(**kw)).ticks(Split.VALIDATION))
    b_ds = SyntheticDataset(SyntheticSpec(**kw, aggressor="flipped"))
    b = list(b_ds.ticks(Split.VALIDATION))
    assert [(t.ts, t.price) for t in a] == [(t.ts, t.price) for t in b]
    assert all((x.side is None and y.side is None) or x.side != y.side for x, y in zip(a, b))
    assert SyntheticDataset(SyntheticSpec(**kw)).digest() != b_ds.digest()


def test_sessions_can_open_at_the_regular_hours_open_across_dst():
    ds = SyntheticDataset(SyntheticSpec(n_sessions=200, ticks_per_session=60, seed=1,
                                        session_open_ct="08:30"))
    opens = [dt.datetime.fromtimestamp(ds.session_bounds(s)[0] / 1e9, CT) for s in range(200)]
    assert {(o.hour, o.minute) for o in opens} == {(8, 30)}
    assert all(o.weekday() < 5 for o in opens)
    assert len({o.utcoffset() for o in opens}) == 2, "should span a DST change"


# --------------------------------------------------------------------------- #
# the decisions, on canned runs
# --------------------------------------------------------------------------- #
def _fill(seq, ts, session, side):
    return [seq, ts, session, seq, side, 1, 100.0, "taker", ""]


def _store(tmp_path, name, rows) -> str:
    f = tmp_path / f"{name}.jsonl.gz"
    with gzip.open(f, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(f)


@pytest.fixture()
def canned(tmp_path, monkeypatch):
    """Prices rise in session 7 all day; `entries` say which side each opens."""
    t0 = int(dt.datetime(2024, 7, 8, 9, 0, tzinfo=CT).timestamp() * 1e9)
    minute = 60 * 10**9
    prices = {7: ([t0 + k * minute for k in range(240)],
                  [100.0 + k for k in range(240)])}
    monkeypatch.setattr(P, "_probe_prices", lambda profile=P.BASE: prices)
    runs = {}

    def fake_run(source, profile, *, ledger, strategy_path=None):
        return runs[(source, profile)]
    monkeypatch.setattr(P, "run_on_profile", fake_run)

    def make(source, entry_sides, flipped_same=True):
        rows = []
        for k, side in enumerate(entry_sides):
            ts = t0 + (90 + 10 * k) * minute
            other = "sell" if side == "buy" else "buy"
            rows += [_fill(2 * k, ts, 7, side), _fill(2 * k + 1, ts + minute, 7, other)]
        log = _store(tmp_path, f"{source}-{len(runs)}", rows)
        base = {"outcome": "completed", "fill_log_source": log, "fill_log_digest": "d1"}
        flip = {"outcome": "completed", "fill_log_source": log,
                "fill_log_digest": "d1" if flipped_same else "d2"}
        runs[(source, P.BASE)] = base
        runs[(source, P.FLIPPED)] = flip
    return make


def test_identical_trades_under_flipped_flow_is_a_violation(tmp_path, canned):
    canned("S", ["buy"] * 6, flipped_same=True)
    v, rep = P.check("S", _cons(tmp_path, [FLOW]), ledger=tmp_path / "l")
    assert [x.id for x in v] == ["flow"] and rep["flow"] == "identical trades"
    canned("T", ["buy"] * 6, flipped_same=False)
    v, rep = P.check("T", _cons(tmp_path, [FLOW]), ledger=tmp_path / "l")
    assert v == [] and rep["flow"] == "trades changed"


def test_buying_a_rally_is_with_the_move(tmp_path, canned):
    canned("B", ["buy"] * 6)
    v, _ = P.check("B", _cons(tmp_path, [AGAINST, WITH]), ledger=tmp_path / "l")
    assert [x.id for x in v] == ["against"]


def test_selling_a_rally_is_against_the_move(tmp_path, canned):
    canned("S", ["sell"] * 6)
    v, rep = P.check("S", _cons(tmp_path, [AGAINST, WITH]), ledger=tmp_path / "l")
    assert [x.id for x in v] == ["with"]
    assert all(val == 1.0 for val in rep["against"].values())


def test_too_few_entries_or_no_trades_is_inconclusive_not_a_violation(tmp_path, canned):
    canned("F", ["sell"] * 2)
    v, rep = P.check("F", _cons(tmp_path, [WITH]), ledger=tmp_path / "l")
    assert v == [] and "inconclusive" in rep["with"]
    canned("N", [])
    v, rep = P.check("N", _cons(tmp_path, [WITH, FLOW]), ledger=tmp_path / "l")
    assert v == [] and "no trades" in rep["inconclusive"]


def test_a_fitted_proposal_is_not_perturbed(tmp_path):
    src = "class Strategy:\n    def fit(self): pass\n"
    v, rep = P.check(src, _cons(tmp_path, [FLOW]), ledger=tmp_path / "l")
    assert v == [] and rep["ran"] is False


def test_nothing_runs_without_probe_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "run_on_profile", lambda *a, **k: pytest.fail("ran"))
    assert P.check("x", F.load(_write(tmp_path), HYP), ledger=tmp_path / "l") == ([], {})


def _write(tmp_path):
    f = tmp_path / "y.constraints.yaml"
    f.write_text(yaml.safe_dump({"schema": F.SCHEMA, "judged": [
        {"id": "fade", "says": "The trade fades the move."}]}))
    return f


def test_the_probe_pair_is_configured_and_differs_only_in_aggressor():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "run.yaml").read_text())
    a, b = cfg["profiles"][P.BASE], cfg["profiles"][P.FLIPPED]
    assert {k: v for k, v in a.items() if k != "dataset"} == \
        {k: v for k, v in b.items() if k != "dataset"}
    da, db = cfg["datasets"][a["dataset"]], cfg["datasets"][b["dataset"]]
    assert {k: v for k, v in da.items()} == {k: v for k, v in db.items() if k != "aggressor"}
    assert db["aggressor"] == "flipped" and da["signal_strength"] == 0.0


# --------------------------------------------------------------------------- #
# the proposer
# --------------------------------------------------------------------------- #
CODE = "class Strategy:\n    def setup(self, ctx): pass\n"


def _agent(tmp_path, replies, cons, **kw):
    os.environ["LITELLM_BASE_URL"] = "https://example.invalid/v1"
    os.environ["LITELLM_API_KEY"] = "not-a-key-and-never-sent"
    agent = llm.LLMAgent(model="test-model", show_history=False, constraints=cons, **kw)
    script = list(replies)
    kinds = []

    def one(prompt, kind):
        agent._pending.append(llm.Call(kind, True, 0.0))
        kinds.append(kind)
        return script.pop(0)
    agent._one = one                                       # type: ignore[method-assign]
    agent._static_ok = lambda src: (True, "")              # type: ignore[method-assign]
    return agent, kinds


def test_a_perturbation_violation_is_repaired(tmp_path):
    cons = _cons(tmp_path, [FLOW])
    agent, kinds = _agent(tmp_path, ["A", "B"], cons)
    agent.perturb = lambda src: (
        ([F.Violation("flow", "uses_flow", FLOW["says"], "identical")], {"flow": "identical trades"})
        if src == "A" else ([], {"flow": "trades changed"}))
    assert agent.propose([]) == "B"
    log = agent.drain_log()
    assert kinds == ["propose", "fidelity_repair"]
    assert log["fidelity_probe"] == {"flow": "trades changed"} and log["off_question_code"] == []


def _verdict(violated: bool):
    v = {"id": "fade", "says": "The trade fades the move.",
         "verdict": "VIOLATED" if violated else "HOLDS",
         "evidence": "ctx.buy()" if violated else "", "why": "buys the rally"}
    return {"ran": True, "verdicts": [v], "violated": ["fade"] if violated else []}


def test_shadow_records_and_does_not_repair(tmp_path):
    cons = _cons(tmp_path, [], judged=[{"id": "fade", "says": "The trade fades the move."}])
    agent, kinds = _agent(tmp_path, ["A"], cons, judge=lambda s: _verdict(True),
                          judge_mode="shadow")
    assert agent.propose([]) == "A" and kinds == ["propose"]
    assert agent.drain_log()["judge"]["violated"] == ["fade"]


def test_repair_mode_sends_the_objection_back_and_never_blocks(tmp_path):
    cons = _cons(tmp_path, [], judged=[{"id": "fade", "says": "The trade fades the move."}])
    agent, kinds = _agent(tmp_path, ["A", "B", "C"], cons,
                          judge=lambda s: _verdict(True), judge_mode="repair")
    out = agent.propose([])
    assert out == "C", "still objected after two repairs, and the trial runs anyway"
    assert kinds == ["propose", "judge_repair", "judge_repair"]
    log = agent.drain_log()
    assert not log["blocked"] and log["judge_repairs"] == 2


def test_a_reviewer_repair_may_not_break_a_mechanical_constraint(tmp_path):
    cons = _cons(tmp_path, [FLOW], judged=[{"id": "fade", "says": "The trade fades the move."}])
    agent, _ = _agent(tmp_path, ["A", "B"], cons, judge=lambda s: _verdict(s == "A"),
                      judge_mode="repair")
    agent.perturb = lambda src: (([F.Violation("flow", "uses_flow", FLOW["says"], "x")], {})
                                 if src == "B" else ([], {}))
    assert agent.propose([]) == "A"


def test_a_reviewer_that_fails_does_not_lose_the_proposal(tmp_path):
    cons = _cons(tmp_path, [], judged=[{"id": "fade", "says": "The trade fades the move."}])

    def boom(src):
        raise RuntimeError("gateway down")
    agent, _ = _agent(tmp_path, ["A"], cons, judge=boom, judge_mode="repair")
    assert agent.propose([]) == "A"
    assert agent.drain_log()["judge"]["ran"] is False
