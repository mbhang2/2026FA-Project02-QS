"""Amendment 19: execution latency and an overnight rule. Synthetic data only."""

from __future__ import annotations

import pathlib

import pytest
import yaml

import run as runner
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.scoring.calibration.reference import ReferenceStrategy
from src.core.types import LifecycleKind, NS_PER_SECOND, Split

REPO = pathlib.Path(__file__).resolve().parent.parent
TICK_NS = 30 * NS_PER_SECOND


def _ds(**kw) -> SyntheticDataset:
    return SyntheticDataset(SyntheticSpec(**{
        "n_sessions": 40, "ticks_per_session": 120, "vol_per_tick": 0.2, "seed": 5,
        "uniform": True, **kw}))


def _run(strategy, dataset=None, **kw) -> Engine:
    engine = Engine.for_test(dataset or _ds(), strategy, split=Split.TRAIN, warmup_sessions=1,
                             seed=1, **kw)
    engine.run()
    return engine


class Base:
    def setup(self, ctx):
        ctx.subscribe("5m", emit_empty=True)

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


class BuyOnce(Base):
    """One market order, on the first live bar."""

    def __init__(self):
        self.sent_ts = None

    def on_bars(self, ctx):
        if ctx.actionable and self.sent_ts is None:
            self.sent_ts = ctx.ts
            ctx.buy(1, tif="GTC")


class HoldsOvernight(Base):
    """Buys once a session late in the day and never exits."""

    def on_bars(self, ctx):
        if ctx.actionable and not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, tif="GTC")


class CancelsARestingBid(Base):
    """Rests a bid just under the market, then cancels it one bar later."""

    def __init__(self):
        self.oid = None
        self.age = 0

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
        if ctx.position:
            ctx.close()
            return
        if self.oid is not None and self.oid in ctx.working_order_ids:
            self.age += 1
            if self.age == 1:
                ctx.cancel(self.oid)
            return
        last = ctx.history("5m")[-1]
        self.oid, self.age = ctx.buy(1, type="limit", limit=last.close - 0.2, tif="GTC"), 0


class ModifiesAndBrackets(Base):
    def __init__(self):
        self.oid = None

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position:
            return
        last = ctx.history("5m")[-1]
        if self.oid in ctx.working_order_ids:
            ctx.modify(self.oid, limit=last.close - 0.1)
            return
        self.oid = ctx.buy(1, type="limit", limit=last.close - 0.1, tif="GTC",
                           stop_offset=0.6, target_offset=0.6)


class SubmitsAtEveryChance(Base):
    """Tries to reopen from the fill and lifecycle callbacks the close raises."""

    def on_fill(self, ctx):
        if ctx.actionable and not ctx.position:
            ctx.buy(1, tif="GTC")

    def on_lifecycle(self, ctx):
        if ctx.actionable and ctx.event.kind is LifecycleKind.SESSION_CLOSE:
            ctx.buy(1, tif="GTC")

    def on_bars(self, ctx):
        if ctx.actionable and not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, tif="GTC")


@pytest.mark.parametrize("factory", [ReferenceStrategy, CancelsARestingBid, ModifiesAndBrackets])
def test_the_defaults_are_the_engine_as_it_was(factory):
    plain = _run(factory())
    explicit = _run(factory(), latency_ns=0, allow_overnight=True)
    assert plain.blotter.rows
    assert explicit.blotter.digest() == plain.blotter.digest()
    assert "execution" not in explicit.record.as_dict()


@pytest.mark.parametrize("factory", [ReferenceStrategy, CancelsARestingBid, ModifiesAndBrackets,
                                     HoldsOvernight])
@pytest.mark.parametrize("latency_ns", [100_000_000, 45_000_000_000, 95_000_000_000])
@pytest.mark.parametrize("overnight", [True, False])
def test_the_event_walk_matches_stepping(factory, latency_ns, overnight):
    fast = _run(factory(), latency_ns=latency_ns, allow_overnight=overnight, event_walk=True)
    slow = _run(factory(), latency_ns=latency_ns, allow_overnight=overnight, event_walk=False)
    assert slow.blotter.rows
    assert fast.blotter.digest() == slow.blotter.digest()
    assert fast.fill_log_digest() == slow.fill_log_digest()
    assert fast.end_of_session_positions == slow.end_of_session_positions


@pytest.mark.parametrize("latency_ns, ticks_later", [(0, 1), (100_000_000, 1), (30_000_000_000, 1),
                                                     (30_000_000_001, 2), (65_000_000_000, 3)])
def test_an_order_first_matches_at_its_arrival(latency_ns, ticks_later):
    strategy = BuyOnce()
    engine = _run(strategy, latency_ns=latency_ns)
    assert engine.blotter.rows[0].ts == strategy.sent_ts + ticks_later * TICK_NS


def test_a_resting_order_can_fill_while_its_cancel_is_in_flight():
    instant = _run(CancelsARestingBid())
    delayed = _run(CancelsARestingBid(), latency_ns=95_000_000_000)
    assert len(delayed.blotter.rows) != len(instant.blotter.rows)
    cancels = [o for o in delayed.orders.values() if o.cancel_at_ts is not None]
    assert cancels
    filled = [o for o in cancels if o.filled_size]
    assert filled, "the scenario needs a fill that beat its cancel"
    fill_ts = {r.order_id: r.ts for r in delayed.blotter.rows}
    assert all(fill_ts[o.order_id] < o.cancel_at_ts for o in filled)


def test_a_modify_with_a_cancel_in_flight_is_rejected():
    class CancelThenModify(Base):
        def __init__(self):
            self.oid = None
            self.rejected = []

        def on_bars(self, ctx):
            if not ctx.actionable or self.oid is not None:
                return
            last = ctx.history("5m")[-1]
            self.oid = ctx.buy(1, type="limit", limit=last.close - 50.0, tif="GTC")

        def on_lifecycle(self, ctx):
            ev = ctx.event
            if ev.kind is LifecycleKind.ORDER_ACCEPTED and ev.order_id == self.oid:
                ctx.cancel(self.oid)
                ctx.modify(self.oid, limit=1.0)
            elif ev.kind is LifecycleKind.ORDER_REJECTED:
                self.rejected.append(ev.detail)

    strategy = CancelThenModify()
    engine = _run(strategy, latency_ns=100_000_000)
    assert any("cancel in flight" in d for d in strategy.rejected)
    assert engine.orders[strategy.oid].limit_price != 1.0


def test_overnight_off_closes_every_session_flat_and_pays_the_fee():
    held = _run(HoldsOvernight(), commission_per_contract=0.25)
    assert any(v != 0 for v in held.end_of_session_positions.values())
    flat = _run(HoldsOvernight(), allow_overnight=False, commission_per_contract=0.25)
    assert all(v == 0 for v in flat.end_of_session_positions.values())
    closes = [o for o in flat.orders.values() if flat._order_tags.get(o.order_id) == "session-flat"]
    assert closes
    rows = {r.order_id: r for r in flat.blotter.rows}
    assert all(rows[o.order_id].commission == pytest.approx(0.25 * o.size) for o in closes)
    assert all(rows[o.order_id].position_after == 0 for o in closes)
    assert flat.reconciliation()["agree"]


def test_nothing_can_reopen_during_the_close():
    engine = _run(SubmitsAtEveryChance(), allow_overnight=False)
    assert all(v == 0 for v in engine.end_of_session_positions.values())
    assert engine.actions_dropped > 0
    assert not engine._working or all(o.submitted_ts for o in engine._working.values())
    last_close = max(engine.dataset.session_bounds(s)[1] for s in engine.scored_sessions())
    assert not [o for o in engine._working.values() if o.submitted_ts >= last_close]


def test_overnight_on_is_not_flattened():
    engine = _run(HoldsOvernight())
    assert not [o for o in engine.orders.values()
                if engine._order_tags.get(o.order_id) == "session-flat"]


def test_replay_from_the_recorded_terms_reproduces_the_run():
    original = _run(ModifiesAndBrackets(), latency_ns=45_000_000_000, allow_overnight=False)
    execution = original.record.as_dict()["execution"]
    assert execution == {**runner.DEFAULT_EXECUTION, "latency_ns": 45_000_000_000,
                         "allow_overnight": False}
    replayed = _run(ModifiesAndBrackets(), **execution)
    assert replayed.blotter.digest() == original.blotter.digest()
    assert replayed.blotter.digest() != _run(ModifiesAndBrackets()).blotter.digest()


@pytest.mark.parametrize("value", [-1, 1.5, True, float("inf")])
def test_a_bad_latency_is_refused(value):
    with pytest.raises(ValueError, match="latency_ns"):
        Engine.for_test(_ds(), ReferenceStrategy(), split=Split.TRAIN, latency_ns=value)


@pytest.mark.parametrize("kw", [{"latency_ns": 1}, {"allow_overnight": False}])
def test_quote_based_runs_are_refused(kw):
    with pytest.raises(ValueError, match="trade-based"):
        Engine.for_test(_ds(quotes=True), ReferenceStrategy(), split=Split.TRAIN,
                        fill_model="queue-pessimistic", **kw)


def test_unsigned_terms_are_not_applied(monkeypatch, capsys):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: False)
    assert runner.execution_terms({"latency_ns": 100_000_000}) == runner.DEFAULT_EXECUTION
    assert "not in force" in capsys.readouterr().err


def test_signed_terms_are_applied(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: n == 19)
    want = {"latency_ns": 100_000_000, "allow_overnight": False}
    assert runner.execution_terms(want) == {**runner.DEFAULT_EXECUTION, **want}


def test_the_registered_profiles():
    profiles = yaml.safe_load((REPO / "config" / "run.yaml").read_text())["profiles"]
    for name in ("crypto-btc-train", "crypto-btc-validation"):
        assert profiles[name]["latency_ns"] == 100_000_000
        assert profiles[name]["allow_overnight"] is True
    others = {k: v for k, v in profiles.items() if not k.startswith("crypto-")}
    assert others and not [k for k, v in others.items()
                           if "latency_ns" in v or "allow_overnight" in v]
