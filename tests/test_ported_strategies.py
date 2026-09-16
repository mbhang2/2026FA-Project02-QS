"""The two strategies ported from `nalin_l1` in Task 40.

They exercise shared code -- the event-driven contract, `RemoteStrategy`, the
engine and the scorer -- so they live here rather than beside the source they
came from.

**What these tests are for.** A port is a claim that two implementations mean the
same thing, and the port of `nalin_l1.strategy.candidate.Momentum` had one place
it could quietly go wrong: the source shifts every signal by one bar to avoid
lookahead, and this API's history already contains only closed bars. Porting the
shift literally would lag every decision by two bars and produce a strategy that
is not the one that was ported. `test_the_port_does_not_double_shift` is the
check that it did not.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine.remote import RemoteStrategy  # noqa: E402
from src.core.runlog import RunTicket  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402

PORTED = ("ported_buy_and_hold", "ported_momentum")
SPEC = SyntheticSpec(n_sessions=24, ticks_per_session=390, vol_per_tick=0.15,
                     seed=1, train_frac=0.5, validation_frac=0.2)


# --------------------------------------------------------------------------- #
# they are valid strategies on the path a scored run actually uses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", PORTED)
def test_the_port_runs_on_the_scored_path(name):
    path = REPO_ROOT / "src" / "strategies" / f"{name}.py"
    with RemoteStrategy(path, "Strategy") as strategy:
        engine = Engine(SyntheticDataset(SPEC), strategy,
                        ticket=RunTicket.for_test(), split=Split.VALIDATION,
                        warmup_sessions=1, fill_model="unfavorable", seed=1)
        record = engine.run()
    assert record.outcome == "completed", record.error
    score(engine.reconstructed(), period=engine.session_calendar())


@pytest.mark.parametrize("name", PORTED)
def test_the_port_passes_the_check_the_agent_would_run(name):
    """These are hand-written, not agent-written, but they live in the tree the
    agent writes to and the same static check applies."""
    from src.core.checks import static_check, type_check

    path = REPO_ROOT / "src" / "strategies" / f"{name}.py"
    report = static_check(path)
    errors = [str(f) for f in report.findings if f.code.startswith("E")]
    assert not errors, errors
    assert report.strategy_class
    result = type_check(path, report.strategy_class, module=f"src.strategies.{name}")
    assert result.ok, result.errors


# --------------------------------------------------------------------------- #
# the momentum port's decision logic, driven bar by bar
# --------------------------------------------------------------------------- #
class _Bar:
    __slots__ = ("close",)

    def __init__(self, close: float) -> None:
        self.close = close


class _History(list):
    def last(self, k):
        return tuple(self[-k:])


class _Ctx:
    """The smallest surface `ported_momentum` touches. Deliberately hand-built:
    driving the real engine to a 5-sigma breakout would mean constructing data
    to order, which tests the data rather than the strategy."""

    def __init__(self) -> None:
        self.hist = _History()
        self.position = 0
        self.working_order_ids = ()
        self.actionable = True
        self.orders: list[tuple[str, int, str]] = []

    def subscribe(self, interval):
        return interval

    def history(self, interval):
        return self.hist

    def buy(self, size=1, tag="", **kw):
        self.orders.append(("buy", size, tag))
        self.position += size

    def sell(self, size=1, tag="", **kw):
        self.orders.append(("sell", size, tag))
        self.position -= size

    def close(self, tag="", **kw):
        self.orders.append(("close", abs(self.position), tag))
        self.position = 0


def _strategy(**kw):
    from src.strategies.ported_momentum import Strategy

    return Strategy(**kw)


def _walk(strategy, ctx, closes):
    for c in closes:
        ctx.hist.append(_Bar(float(c)))
        strategy.on_bars(ctx)


def test_no_entry_before_the_window_is_full():
    s, ctx = _strategy(window=30), _Ctx()
    _walk(s, ctx, [100.0] * 29)
    assert ctx.orders == []


def test_a_break_above_the_upper_band_goes_long():
    s, ctx = _strategy(window=30, band_mult=2.0, span=30), _Ctx()
    _walk(s, ctx, [100.0 + (i % 2) * 0.5 for i in range(40)])   # dispersion
    assert ctx.orders == []
    _walk(s, ctx, [130.0])                                       # a clear break
    assert ctx.orders and ctx.orders[-1][0] == "buy"
    assert ctx.orders[-1][2] == "momentum-long"


def test_the_minimum_hold_is_enforced_before_any_exit():
    s, ctx = _strategy(window=30, band_mult=2.0, min_hold=15), _Ctx()
    _walk(s, ctx, [100.0 + (i % 2) * 0.5 for i in range(40)])
    _walk(s, ctx, [130.0])
    assert ctx.position > 0
    # Immediately back through the EMA: the source holds anyway for 15 bars.
    _walk(s, ctx, [100.0] * 14)
    assert ctx.position > 0, "exited inside the minimum holding period"
    _walk(s, ctx, [100.0])
    assert ctx.position == 0
    assert ctx.orders[-1][2] == "momentum-exit"


def test_the_port_does_not_double_shift():
    """The source applies `.shift(1)` by hand; `ctx.history` is already only
    closed bars. A literal port would lag by two and enter one bar late.

    Driven so the break is the bar that just closed: the decision must be taken
    on that bar, not on the one after it.
    """
    s, ctx = _strategy(window=30, band_mult=2.0), _Ctx()
    _walk(s, ctx, [100.0 + (i % 2) * 0.5 for i in range(40)])
    before = len(ctx.orders)
    _walk(s, ctx, [130.0])
    assert len(ctx.orders) == before + 1, (
        "the break bar produced no order on the callback it closed on -- the "
        "port is lagging, which is what porting `.shift(1)` literally does"
    )


def test_the_ema_recurrence_matches_pandas_adjust_false():
    """EMA is kept incrementally rather than recomputed. It is a recurrence, so
    the two are equal, and this is the check that the alpha is right."""
    closes = [100.0, 101.0, 99.5, 103.0, 98.0, 104.5, 97.0]
    span = 5
    s = _strategy(span=span)
    for c in closes:
        s._update_ema(c)
    alpha = 2.0 / (span + 1.0)
    expected = closes[0]
    for c in closes[1:]:
        expected = alpha * c + (1 - alpha) * expected
    assert math.isclose(s.ema, expected, rel_tol=1e-12)
