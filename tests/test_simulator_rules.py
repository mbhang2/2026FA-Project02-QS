"""The spec decisions, tested where they would otherwise only exist in prose.

Empty bars, session-boundary order handling, OCO, the holdout lock, the embargo
and the action cap were all judgement calls recorded in
`docs/design/simulator-interface.md`. A decision nothing asserts is a decision
that drifts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.engine.bars import BarBuilder  # noqa: E402
from src.core.data.dataset import Calendar, SplitBounds  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import (  # noqa: E402
    NS_PER_SECOND,
    BracketSpec,
    Cancel,
    HoldoutLocked,
    LifecycleKind,
    OrderStatus,
    OrderType,
    Side,
    Split,
    Submit,
    Subscription,
    Tick,
    TimeInForce,
)


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


class Noop:
    """Subscribes, records lifecycle events, and optionally fires a fixed set of
    context calls on its first actionable bar."""

    def __init__(self, subs, calls=()):
        self.subs = list(subs)
        self._calls = list(calls)
        self._fired = False
        self.lifecycle = []
        self.bar_events = []
        self.order_ids = {}

    def setup(self, ctx) -> None:
        for s in self.subs:
            ctx.subscribe(s)

    def on_bars(self, ctx) -> None:
        self.bar_events.append(ctx.closed)
        if self._calls and not self._fired and ctx.actionable:
            self._fired = True
            for name, call in self._calls:
                self.order_ids[name] = call(ctx)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        self.lifecycle.append(ctx.event)


# --------------------------------------------------------------------------- #
# empty bars
# --------------------------------------------------------------------------- #
def _quiet_ticks(interval_ns: int) -> list[Tick]:
    """One tick, then a long silence, then one more -- so whole intervals are
    empty in between."""
    return [
        Tick(ts=0, price=100.0, size=1, session=0),
        Tick(ts=5 * interval_ns + 1, price=101.0, size=1, session=0),
    ]


def test_empty_bars_are_skipped_by_default():
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)
    b = BarBuilder(sub)
    b.start_session(0, 0)
    out = []
    for t in _quiet_ticks(sub.interval_ns):
        out.extend(b.bars_closed_by(t))
        b.add(t)
    assert [x.tick_count for x in out] == [1], "an empty interval produced a bar"


def test_emit_empty_carries_the_previous_close_forward():
    sub = Subscription(interval_ns=60 * NS_PER_SECOND, emit_empty=True, sub_id="grid")
    b = BarBuilder(sub)
    b.start_session(0, 0)
    out = []
    for t in _quiet_ticks(sub.interval_ns):
        out.extend(b.bars_closed_by(t))
        b.add(t)
    assert len(out) == 5, f"expected a regular grid, got {len(out)} bars"
    assert out[0].tick_count == 1
    for bar in out[1:]:
        assert bar.tick_count == 0 and bar.volume == 0
        assert bar.open == bar.high == bar.low == bar.close == 100.0


def test_emit_empty_cannot_invent_a_bar_before_the_first_tick():
    """There is no previous close to carry, so leading empties are skipped under
    either setting."""
    sub = Subscription(interval_ns=60 * NS_PER_SECOND, emit_empty=True, sub_id="grid")
    b = BarBuilder(sub)
    b.start_session(0, 0)
    late = Tick(ts=3 * sub.interval_ns + 1, price=100.0, size=1, session=0)
    assert b.bars_closed_by(late) == []


# --------------------------------------------------------------------------- #
# session boundary
# --------------------------------------------------------------------------- #
def test_day_orders_are_cancelled_at_the_session_close_and_gtc_survives():
    strat = Noop(["1m"], calls=[
        ("day", lambda c: c.buy(1, type="limit", limit=1.0, tif="DAY", tag="day")),
        ("gtc", lambda c: c.buy(1, type="limit", limit=1.0, tif="GTC", tag="gtc")),
    ])
    eng = Engine.for_test(ds(), strat, split=Split.VALIDATION)
    eng.run()

    day, gtc = eng.orders[strat.order_ids["day"]], eng.orders[strat.order_ids["gtc"]]
    assert day.status is OrderStatus.EXPIRED, "a DAY order survived the session close"
    assert gtc.is_working, "a GTC order did not survive the session close"
    assert any(e.kind is LifecycleKind.DAY_EXPIRED for e in strat.lifecycle)


def test_positions_are_not_flattened_at_a_session_boundary_but_are_recorded():
    """A strategy that wants to be flat overnight has to say so; the record makes
    an unintended hold visible rather than silent."""
    strat = Noop(["1m"], calls=[("entry", lambda c: c.buy(1, tag="entry"))])
    eng = Engine.for_test(ds(), strat, split=Split.VALIDATION)
    rec = eng.run()

    assert eng.position == 1, "the position did not survive to the end of the run"
    held = [s for s, p in rec.end_of_session_positions.items() if p != 0]
    assert held, "an overnight position was not recorded"


# --------------------------------------------------------------------------- #
# brackets and OCO
# --------------------------------------------------------------------------- #
def test_a_bracket_is_armed_at_fill_time_and_its_legs_are_oco():
    strat = Noop(["1m"], calls=[
        ("entry", lambda c: c.buy(1, stop_offset=0.5, target_offset=0.5, tag="entry"))
    ])
    eng = Engine.for_test(ds(signal_strength=0.5), strat, split=Split.VALIDATION)
    eng.run()

    legs = [o for o in eng.orders.values() if o.parent_id is not None]
    assert len(legs) == 2, f"expected a stop and a target, got {len(legs)}"
    assert len({o.oco_group for o in legs}) == 1, "bracket legs are not one OCO group"
    assert all(o.reduce_only for o in legs)
    assert {o.order_type for o in legs} == {OrderType.STOP, OrderType.LIMIT}

    filled = [o for o in legs if o.status is OrderStatus.FILLED]
    if filled:
        sibling = [o for o in legs if o is not filled[0]][0]
        assert sibling.status is OrderStatus.CANCELLED, (
            "the OCO sibling was not cancelled when its partner filled"
        )
        assert any(e.kind is LifecycleKind.OCO_CANCELLED for e in strat.lifecycle)


def test_a_bracket_leg_cannot_fill_on_the_tick_that_armed_it():
    strat = Noop(["1m"], calls=[
        ("e", lambda c: c.buy(1, stop_offset=0.25, target_offset=0.25, tag="e"))
    ])
    eng = Engine.for_test(ds(signal_strength=0.5), strat, split=Split.VALIDATION)
    eng.run()
    entry_row = eng.blotter.rows[0]
    for row in eng.blotter.rows[1:]:
        assert row.seq > entry_row.seq


# --------------------------------------------------------------------------- #
# splits, embargo, holdout lock
# --------------------------------------------------------------------------- #
def test_holdout_is_refused_until_explicitly_unlocked():
    d = ds()
    with pytest.raises(HoldoutLocked):
        list(d.ticks(Split.HOLDOUT))
    with pytest.raises(ValueError):
        d.unlock_holdout("   ")
    d.unlock_holdout("final evaluation, pre-registered 2026-09-08")
    assert list(d.ticks(Split.HOLDOUT))
    assert d.holdout_lock.unlocked


def test_the_holdout_unlock_reason_reaches_the_run_record():
    d = ds()
    d.unlock_holdout("mentor sign-off")
    eng = Engine.for_test(d, Noop(["1m"]), split=Split.HOLDOUT)
    rec = eng.run()
    assert rec.holdout_unlock_reason == "mentor sign-off"
    assert rec.split == "holdout"


def test_splits_must_be_chronological_and_respect_the_embargo():
    with pytest.raises(ValueError, match="chronological"):
        Calendar(SplitBounds(0, 50), SplitBounds(40, 60), SplitBounds(70, 80),
                 embargo_sessions=0, n_sessions=80)
    with pytest.raises(ValueError, match="embargo"):
        Calendar(SplitBounds(0, 50), SplitBounds(51, 60), SplitBounds(70, 80),
                 embargo_sessions=5, n_sessions=80)
    Calendar(SplitBounds(0, 50), SplitBounds(55, 60), SplitBounds(70, 80),
             embargo_sessions=5, n_sessions=80)


def test_warmup_reaches_backward_but_is_not_actionable():
    class Watcher(Noop):
        def __init__(self):
            super().__init__(["1m"])
            self.sessions_seen = []
            self.actionable_sessions = set()

        def on_bars(self, ctx):
            self.sessions_seen.append(ctx.session)
            if ctx.actionable:
                self.actionable_sessions.add(ctx.session)

    d = ds()
    w = Watcher()
    Engine.for_test(d, w, split=Split.VALIDATION, warmup_sessions=2).run()
    val = d.calendar().validation
    assert min(w.sessions_seen) < val.start, "warmup did not reach back across the boundary"
    assert min(w.actionable_sessions) == val.start, "a warmup session was actionable"


# --------------------------------------------------------------------------- #
# action cap
# --------------------------------------------------------------------------- #
def test_actions_beyond_the_cap_are_dropped_counted_and_reported():
    class Spammer(Noop):
        def __init__(self):
            super().__init__(["1m"])

        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            for _ in range(10):
                ctx.buy(1, type="limit", limit=1.0)

    eng = Engine.for_test(ds(), Spammer(), split=Split.VALIDATION, action_cap=4)
    rec = eng.run()
    assert rec.action_cap_hits > 0
    assert rec.actions_dropped == rec.action_cap_hits * 6
    assert rec.action_cap == 4


# --------------------------------------------------------------------------- #
# the per-run record
# --------------------------------------------------------------------------- #
def test_the_run_record_pins_what_the_trial_actually_ran_on():
    d = ds(signal_strength=0.5)
    eng = Engine.for_test(d, Noop(["1m"]), split=Split.VALIDATION,
                 fill_model="unfavorable", seed=11)
    rec = eng.run()
    got = rec.as_dict()
    for field in (
        "dataset", "dataset_digest", "split", "fill_model", "capabilities", "seed",
        "git_sha", "wall_clock_s", "subscriptions", "action_cap", "action_cap_hits",
        "ticks", "outcome", "blotter_digest", "end_of_session_positions",
    ):
        assert field in got, f"the ledger row is missing {field}"
    assert rec.dataset_digest == d.digest()
    # `has_options` joined the set on 2026-10-24. The equality is kept rather
    # than loosened to a subset: this assertion exists so that a capability
    # appearing or disappearing from the RECORD is a test failure, and rows
    # written before that date carry three keys while rows after carry four --
    # which is the record saying truthfully what the harness could offer at the
    # time, not a schema drift to paper over.
    assert rec.capabilities == {"has_trades": True, "has_quotes": False,
                                "has_book": False, "has_options": False}
    assert rec.outcome == "completed" and rec.error is None


def test_the_dataset_digest_changes_with_the_data_and_not_with_the_run():
    a, b = ds(signal_strength=0.0), ds(signal_strength=0.5)
    assert a.digest() != b.digest()
    assert ds(signal_strength=0.0).digest() == a.digest()


def test_a_fill_model_requiring_absent_capabilities_fails_at_wiring_time():
    from src.core.engine.fills import UnfavorableFill
    from src.core.types import DataCapabilities

    model = UnfavorableFill()
    model.requires = DataCapabilities(has_trades=True, has_quotes=True)
    with pytest.raises(ValueError, match="requires"):
        Engine.for_test(ds(), Noop(["1m"]), fill_model=model)


# --------------------------------------------------------------------------- #
# adversarial review, Task 20
# --------------------------------------------------------------------------- #
def test_reduce_only_cannot_open_an_opposite_position(tmp_path):
    """The review's reproduction. `reduce_only` was checked when the order was
    queued and never again, so two `ctx.close()` calls against a long went
    +1 -> 0 -> -1 with both orders marked reduce-only: the second was legitimate
    when it was written and had become an opening order by the time it filled."""
    seen = {"positions": []}

    class DoubleClose:
        def setup(self, ctx):
            ctx.subscribe("1m")

        def on_bars(self, ctx):
            seen["positions"].append(ctx.position)
            if not ctx.actionable:
                return
            if not ctx.position and not ctx.working_order_ids and "opened" not in seen:
                seen["opened"] = ctx.buy(1, tag="entry")
                return
            if ctx.position > 0 and "closed" not in seen:
                seen["closed"] = True
                ctx.close()
                ctx.close()          # the second one is the attack

        def on_fill(self, ctx):
            seen["positions"].append(ctx.fill.position)

        def on_lifecycle(self, ctx):
            pass

    engine = Engine.for_test(ds(), DoubleClose(), split=Split.VALIDATION)
    engine.run()
    assert seen.get("closed"), "the probe never got a position to close twice"
    assert min(seen["positions"]) >= 0, (
        f"a reduce-only order opened a short: {seen['positions']}"
    )
    assert engine.blotter.final_position >= 0


def test_a_fragmented_bracket_entry_is_protected_at_every_point():
    """The review's second reproduction. Brackets armed only when the parent was
    *fully* filled and sized the children from the final fragment, so a 10-lot
    entry filling 4+1+2+3 got a 3-lot stop and a 3-lot target -- seven contracts
    naked, for the whole sequence.

    The invariant is protected == filled at every point, not only at the end,
    which is why this walks the sequence rather than checking the result.
    """
    from src.core.types import Fill, Order, OrderStatus, OrderType, TimeInForce

    engine = Engine.for_test(ds(), Noop(subs=("1m",)), split=Split.VALIDATION)
    engine.run()

    parent = Order(
        order_id=9_000, side=Side.BUY, size=10, order_type=OrderType.MARKET,
        tif=TimeInForce.DAY, bracket=BracketSpec(stop_offset=1.0, target_offset=1.0),
        status=OrderStatus.WORKING,
    )
    engine.orders[parent.order_id] = parent
    engine._book.working[parent.order_id] = parent
    engine.position = 0

    protected = []
    for size in (4, 1, 2, 3):
        tick = Tick(ts=1, price=100.0, size=size, session=0, seq=len(protected) + 1)
        engine._apply_fill(parent, Fill(price=100.0, size=size), tick)
        legs = [o for o in engine._book.working.values()
                if o.parent_id == parent.order_id]
        by_type = {o.order_type: o.size for o in legs}
        protected.append((parent.filled_size, dict(by_type)))

    for filled, legs in protected:
        assert legs, f"no bracket after {filled} contracts filled"
        assert set(legs) == {OrderType.STOP, OrderType.LIMIT}, legs
        assert all(size == filled for size in legs.values()), (
            f"{filled} contracts filled but the bracket protects {legs}"
        )
    assert len({id(o) for o in engine._book.working.values()
                if o.parent_id == parent.order_id}) == 2, (
        "each fragment armed its own bracket instead of resizing the first"
    )
