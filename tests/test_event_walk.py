"""The event walk must be a pure optimization.

The engine jumps to the next tick at which anything can happen instead of
stepping all ~286,000 in a session. That is only allowed if the events produced,
their order, and every resulting fill are identical to stepping.

So these are **differential** tests: each scenario runs twice on the same data,
once with `event_walk=True` and once with `event_walk=False`, and the blotters
must match row for row. That is stronger than a stored digest, because it checks
the optimization against the implementation it is optimizing rather than against
a snapshot of one strategy's behaviour -- and it covers the cases the fast path
is most likely to get wrong, which are the ones where the strategy is never flat.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import NS_PER_SECOND, Split  # noqa: E402
from src.strategies.current import MomentumStrategy  # noqa: E402


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=40, ticks_per_session=390, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


def both_ways(strategy_factory, *, spec=None, **engine_kw):
    """Run the same thing with and without the jump. Returns (fast, slow)."""
    out = []
    for event_walk in (True, False):
        eng = Engine.for_test(
            SyntheticDataset(spec) if spec else ds(),
            strategy_factory(),
            split=Split.VALIDATION,
            event_walk=event_walk,
            **engine_kw,
        )
        eng.run()
        out.append(eng)
    return out


def assert_identical(fast: Engine, slow: Engine, label: str) -> None:
    assert fast.blotter.digest() == slow.blotter.digest(), (
        f"{label}: the event walk changed the blotter"
    )
    assert [r.as_row() for r in fast.blotter.rows] == [
        r.as_row() for r in slow.blotter.rows
    ], f"{label}: rows differ"
    assert fast.n_ticks == slow.n_ticks, f"{label}: tick counts differ"
    assert fast.n_bars == slow.n_bars, f"{label}: bar counts differ"
    assert fast.n_fills == slow.n_fills, f"{label}: fill counts differ"
    assert fast.blotter.final_position == slow.blotter.final_position
    assert fast.record.end_of_session_positions == slow.record.end_of_session_positions


class Base:
    subs = ("5m",)

    def setup(self, ctx):
        self.lifecycle = []
        for s in self.subs:
            ctx.subscribe(s)

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        self.lifecycle.append(ctx.event)


# --------------------------------------------------------------------------- #
# the strategies that already exist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("signal", [0.0, 0.5, 1.0])
def test_the_reference_strategy_is_unchanged(signal):
    """Never flat for long: a bracket rests almost continuously, which is where
    the fast path has the least room and the most risk."""
    spec = SyntheticSpec(n_sessions=40, ticks_per_session=390, vol_per_tick=0.15,
                         signal_strength=signal, seed=1, train_frac=0.5,
                         validation_frac=0.2)
    fast, slow = both_ways(ReferenceStrategy, spec=spec, warmup_sessions=1)
    assert fast.blotter.rows, "nothing traded, so nothing was compared"
    assert_identical(fast, slow, f"reference signal={signal}")


def test_the_placeholder_strategy_is_unchanged():
    fast, slow = both_ways(MomentumStrategy, warmup_sessions=1)
    assert_identical(fast, slow, "momentum")


# --------------------------------------------------------------------------- #
# the cases the fast path is most likely to get wrong
# --------------------------------------------------------------------------- #
def test_several_resting_triggers_between_the_same_pair_of_bar_closes():
    """The jump must stop at the *first* of them, not the nearest boundary."""

    class Ladder(Base):
        def on_bars(self, ctx):
            if not ctx.actionable or ctx.working_order_ids or ctx.position:
                return
            px = ctx.last_price
            for k in (1, 2, 3, 4):
                ctx.buy(1, type="limit", limit=px - 0.05 * k, tif="GTC")
                ctx.sell(1, type="stop", trigger=px - 0.05 * k, tif="GTC")

    fast, slow = both_ways(Ladder)
    assert fast.blotter.rows
    assert_identical(fast, slow, "ladder")


def test_a_bracket_arming_and_its_sibling_triggering_in_the_same_span():
    class Bracketed(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, stop_offset=0.05, target_offset=0.05, tag="e")

    fast, slow = both_ways(Bracketed)
    assert fast.n_fills > 10
    assert_identical(fast, slow, "tight bracket")


def test_stop_limit_arming_and_filling():
    """Arming and filling must still not be the same trade, on either path."""

    class StopLimit(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not ctx.working_order_ids and not ctx.position:
                px = ctx.last_price
                ctx.buy(1, type="stop_limit", trigger=px + 0.05, limit=px + 0.5,
                        tif="GTC", tag="sl")

    fast, slow = both_ways(StopLimit)
    assert fast.blotter.rows
    assert_identical(fast, slow, "stop-limit")


def test_a_trigger_that_coincides_with_a_bar_boundary():
    """Two candidate events at the same index. The order of the work at that
    tick is fixed and must not depend on which candidate produced it."""

    class OnBoundary(Base):
        subs = ("30s",)   # one tick per bar in this generator, so every tick closes one

        def on_bars(self, ctx):
            if ctx.actionable and not ctx.working_order_ids and not ctx.position:
                ctx.buy(1, type="limit", limit=ctx.last_price, tif="GTC")

    fast, slow = both_ways(OnBoundary)
    assert fast.blotter.rows
    assert_identical(fast, slow, "trigger on a boundary")


def test_gtd_expiry_landing_mid_span():
    """Task 7 made GTD expire on any eligible tick rather than at a session
    boundary. The fast path must not quietly reintroduce boundary-only checking.
    """

    class Timed(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not ctx.working_order_ids:
                ctx.buy(1, type="limit", limit=1.0, tif="GTD",
                        expire_ts=ctx.ts + 137 * NS_PER_SECOND, tag="gtd")

    fast, slow = both_ways(Timed)
    from src.core.types import LifecycleKind

    expiries = [e for e in fast.strategy.lifecycle
                if e.kind is LifecycleKind.GTD_EXPIRED]
    assert expiries, "no GTD order expired, so nothing was tested"
    assert_identical(fast, slow, "gtd expiry")
    slow_expiries = [e for e in slow.strategy.lifecycle
                     if e.kind is LifecycleKind.GTD_EXPIRED]
    assert [e.ts for e in expiries] == [e.ts for e in slow_expiries]


def test_a_session_where_the_strategy_is_never_flat():
    """The fast path is only fast when flat; the correctness risk is where it
    isn't."""

    class AlwaysIn(Base):
        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tag="in")
            elif ctx.position and not ctx.working_order_ids:
                ctx.sell(1, type="limit", limit=ctx.last_price + 100.0, tif="GTC")

    fast, slow = both_ways(AlwaysIn)
    assert fast.blotter.rows
    held = [p for p in fast.record.end_of_session_positions.values() if p]
    assert held, "the strategy was flat after all"
    assert_identical(fast, slow, "never flat")


def test_multiple_subscriptions_with_different_intervals():
    class Multi(Base):
        subs = ("30s", "5m")

        def on_bars(self, ctx):
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, stop_offset=0.2, target_offset=0.2)

    fast, slow = both_ways(Multi)
    assert fast.n_bars > 100
    assert_identical(fast, slow, "two intervals")


def test_an_empty_bar_grid_still_matches():
    class Grid(Base):
        def setup(self, ctx):
            self.lifecycle = []
            ctx.subscribe("5m", emit_empty=True)

        def on_bars(self, ctx):
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, stop_offset=0.3, target_offset=0.3)

    fast, slow = both_ways(Grid)
    assert_identical(fast, slow, "emit_empty")


# --------------------------------------------------------------------------- #
# tick subscriptions pay full price, by design
# --------------------------------------------------------------------------- #
def test_a_tick_subscriber_receives_every_tick():
    """No jumping when ticks are subscribed. That is correct and desirable: the
    cost of tick granularity should be visible in the wall-clock budget rather
    than hidden."""

    class Ticker(Base):
        subs = ("tick", "5m")

        def __init__(self):
            self.seen = 0

        def on_tick(self, ctx):
            self.seen += 1

    fast, slow = both_ways(Ticker)
    assert fast.strategy.seen == fast.n_ticks
    assert fast.strategy.seen == slow.strategy.seen
    assert_identical(fast, slow, "tick subscriber")


# --------------------------------------------------------------------------- #
# the fast path must actually engage
# --------------------------------------------------------------------------- #
def test_the_walk_really_skips_and_the_tests_above_are_not_trivial():
    """Without this, every differential test above could pass by never jumping.

    `ticks_stepped` is what the walk stopped at; the rest were folded into bars
    in bulk. The two numbers also make the speedup a measurement rather than a
    claim.
    """
    class Flat(Base):
        def on_bars(self, ctx):
            pass       # never trades: nothing rests, so only boundaries matter

    fast, slow = both_ways(Flat)
    assert slow.n_stepped == slow.n_ticks, "the slow path skipped something"
    assert fast.n_ticks == slow.n_ticks
    assert fast.n_stepped < 0.2 * fast.n_ticks, (
        f"a strategy that never trades still stepped {fast.n_stepped}/{fast.n_ticks}"
    )

    # and the hard case still skips, just less
    busy, _ = both_ways(ReferenceStrategy, warmup_sessions=1)
    assert busy.n_stepped < busy.n_ticks, "the never-flat case skipped nothing at all"
