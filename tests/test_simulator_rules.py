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

from src.core.bars import BarBuilder  # noqa: E402
from src.core.dataset import Calendar, SplitBounds  # noqa: E402
from src.core.datasets.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
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
    def __init__(self, subs, actions_on_first_bar=()):
        self.subs = list(subs)
        self._actions = list(actions_on_first_bar)
        self._fired = False
        self.lifecycle = []
        self.bar_events = []

    def setup(self, ctx):
        return self.subs

    def on_bars(self, ev, ctx):
        self.bar_events.append(ev)
        if self._actions and not self._fired and ctx.actionable:
            self._fired = True
            return list(self._actions)
        return []

    def on_fill(self, ev, ctx):
        return []

    def on_lifecycle(self, ev, ctx):
        self.lifecycle.append(ev)
        return []


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
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)
    far_below = Submit(side=Side.BUY, size=1, order_type=OrderType.LIMIT,
                       tif=TimeInForce.DAY, limit_price=1.0, tag="day")
    far_below_gtc = Submit(side=Side.BUY, size=1, order_type=OrderType.LIMIT,
                           tif=TimeInForce.GTC, limit_price=1.0, tag="gtc")
    strat = Noop([sub], actions_on_first_bar=[far_below, far_below_gtc])
    eng = Engine(ds(), strat, split=Split.VALIDATION)
    eng.run()

    by_tag = {tag: oid for oid, tag in eng._order_tags.items()}
    day, gtc = eng.orders[by_tag["day"]], eng.orders[by_tag["gtc"]]
    assert day.status is OrderStatus.EXPIRED, "a DAY order survived the session close"
    assert gtc.is_working, "a GTC order did not survive the session close"
    assert any(e.kind is LifecycleKind.DAY_EXPIRED for e in strat.lifecycle)


def test_positions_are_not_flattened_at_a_session_boundary_but_are_recorded():
    """A strategy that wants to be flat overnight has to say so; the record makes
    an unintended hold visible rather than silent."""
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)
    entry = Submit(side=Side.BUY, size=1, order_type=OrderType.MARKET,
                   tif=TimeInForce.DAY, tag="entry")
    strat = Noop([sub], actions_on_first_bar=[entry])
    eng = Engine(ds(), strat, split=Split.VALIDATION)
    rec = eng.run()

    assert eng.position == 1, "the position did not survive to the end of the run"
    held = [s for s, p in rec.end_of_session_positions.items() if p != 0]
    assert held, "an overnight position was not recorded"


# --------------------------------------------------------------------------- #
# brackets and OCO
# --------------------------------------------------------------------------- #
def test_a_bracket_is_armed_at_fill_time_and_its_legs_are_oco():
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)
    entry = Submit(side=Side.BUY, size=1, order_type=OrderType.MARKET,
                   tif=TimeInForce.DAY,
                   bracket=BracketSpec(stop_offset=0.5, target_offset=0.5), tag="entry")
    strat = Noop([sub], actions_on_first_bar=[entry])
    eng = Engine(ds(signal_strength=0.5), strat, split=Split.VALIDATION)
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
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)
    entry = Submit(side=Side.BUY, size=1, order_type=OrderType.MARKET,
                   tif=TimeInForce.DAY,
                   bracket=BracketSpec(stop_offset=0.25, target_offset=0.25), tag="e")
    strat = Noop([sub], actions_on_first_bar=[entry])
    eng = Engine(ds(signal_strength=0.5), strat, split=Split.VALIDATION)
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
    eng = Engine(d, Noop([Subscription(interval_ns=60 * NS_PER_SECOND)]),
                 split=Split.HOLDOUT)
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
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)

    class Watcher(Noop):
        def __init__(self):
            super().__init__([sub])
            self.sessions_seen = []
            self.actionable_sessions = set()

        def on_bars(self, ev, ctx):
            self.sessions_seen.append(ctx.session)
            if ctx.actionable:
                self.actionable_sessions.add(ctx.session)
            return []

    d = ds()
    w = Watcher()
    Engine(d, w, split=Split.VALIDATION, warmup_sessions=2).run()
    val = d.calendar().validation
    assert min(w.sessions_seen) < val.start, "warmup did not reach back across the boundary"
    assert min(w.actionable_sessions) == val.start, "a warmup session was actionable"


# --------------------------------------------------------------------------- #
# action cap
# --------------------------------------------------------------------------- #
def test_actions_beyond_the_cap_are_dropped_counted_and_reported():
    sub = Subscription(interval_ns=60 * NS_PER_SECOND)

    class Spammer(Noop):
        def __init__(self):
            super().__init__([sub])

        def on_bars(self, ev, ctx):
            if not ctx.actionable:
                return []
            return [
                Submit(side=Side.BUY, size=1, order_type=OrderType.LIMIT,
                       tif=TimeInForce.DAY, limit_price=1.0)
                for _ in range(10)
            ]

    eng = Engine(ds(), Spammer(), split=Split.VALIDATION, action_cap=4)
    rec = eng.run()
    assert rec.action_cap_hits > 0
    assert rec.actions_dropped == rec.action_cap_hits * 6
    assert rec.action_cap == 4


# --------------------------------------------------------------------------- #
# the per-run record
# --------------------------------------------------------------------------- #
def test_the_run_record_pins_what_the_trial_actually_ran_on():
    d = ds(signal_strength=0.5)
    eng = Engine(d, Noop([Subscription(interval_ns=60 * NS_PER_SECOND)]),
                 split=Split.VALIDATION, fill_model="unfavorable", seed=11)
    rec = eng.run()
    got = rec.as_dict()
    for field in (
        "dataset", "dataset_digest", "split", "fill_model", "capabilities", "seed",
        "git_sha", "wall_clock_s", "subscriptions", "action_cap", "action_cap_hits",
        "ticks", "outcome", "blotter_digest", "end_of_session_positions",
    ):
        assert field in got, f"the ledger row is missing {field}"
    assert rec.dataset_digest == d.digest()
    assert rec.capabilities == {"has_trades": True, "has_quotes": False, "has_book": False}
    assert rec.outcome == "completed" and rec.error is None


def test_the_dataset_digest_changes_with_the_data_and_not_with_the_run():
    a, b = ds(signal_strength=0.0), ds(signal_strength=0.5)
    assert a.digest() != b.digest()
    assert ds(signal_strength=0.0).digest() == a.digest()


def test_a_fill_model_requiring_absent_capabilities_fails_at_wiring_time():
    from src.core.fills import UnfavorableFill
    from src.core.types import DataCapabilities

    model = UnfavorableFill()
    model.requires = DataCapabilities(has_trades=True, has_quotes=True)
    with pytest.raises(ValueError, match="requires"):
        Engine(ds(), Noop([Subscription(interval_ns=60 * NS_PER_SECOND)]),
               fill_model=model)
