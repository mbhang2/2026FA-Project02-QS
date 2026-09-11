"""Acceptance tests for the backtest simulator.

The eight in Part 3 of the brief, plus the spec decisions that would otherwise
only exist in prose. The two calibration tests are the point: a gate that rejects
everything passes a null test trivially, so calibration has to be two-sided.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring.calibration import AcceptRule, acceptance_rate  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.types import (  # noqa: E402
    NS_PER_SECOND,
    Action,
    BarEvent,
    HoldoutLocked,
    LifecycleKind,
    LookaheadError,
    OrderStatus,
    OrderType,
    Side,
    Split,
    Submit,
    Subscription,
    TimeInForce,
)
from src.core.engine.view import HistoryView  # noqa: E402
from src.strategies.current import MomentumStrategy  # noqa: E402

M1 = Subscription(interval_ns=60 * NS_PER_SECOND)
M5 = Subscription(interval_ns=5 * 60 * NS_PER_SECOND)
M1_ID, M5_ID = M1.sub_id, M5.sub_id


def dataset(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1)
    base.update(kw)
    # 20 sessions is too few for the default fractions; use a wider train share
    base.setdefault("train_frac", 0.5)
    base.setdefault("validation_frac", 0.2)
    return SyntheticDataset(SyntheticSpec(**base))


class Recorder:
    """Minimal strategy that records what it was handed."""

    def __init__(self, subs=("5m",), submit_on_first_bar=False):
        self.subs = list(subs)
        self.submit_on_first_bar = submit_on_first_bar
        self.bar_events: list[BarEvent] = []
        # When each event was *delivered*, which is not when the bars closed: an
        # interval with no ticks in it closes when the next tick arrives, and on
        # irregular data that can be several intervals later.
        self.delivered_at: list[int] = []
        self.fills = []
        self.lifecycle = []
        self._submitted = False

    def setup(self, ctx) -> None:
        for s in self.subs:
            ctx.subscribe(s)

    def on_bars(self, ctx) -> None:
        self.bar_events.append(
            BarEvent(ts=ctx.closed[0].close_ts, seq=ctx._seq, bars=ctx.closed)
        )
        self.delivered_at.append(ctx.ts)
        if self.submit_on_first_bar and not self._submitted and ctx.actionable:
            self._submitted = True
            ctx.buy(size=1, tag="probe")

    def on_fill(self, ctx) -> None:
        self.fills.append(ctx.fill)

    def on_lifecycle(self, ctx) -> None:
        self.lifecycle.append(ctx.event)


# --------------------------------------------------------------------------- #
# 1. reaching at or beyond the current index raises
# --------------------------------------------------------------------------- #
def test_history_view_refuses_to_reach_forward():
    v = HistoryView([10, 20, 30])
    # Slices come back as tuples, not lists: nothing handed to a strategy is
    # mutable, and a returned list invites the habit even where the particular
    # list is a harmless copy.
    assert v[-1] == 30 and v[0] == 10 and v[:3] == (10, 20, 30)
    assert v.last(2) == (20, 30)
    with pytest.raises(LookaheadError):
        v[3]            # the element that does not exist yet
    with pytest.raises(LookaheadError):
        v[99]
    with pytest.raises(LookaheadError):
        v[0:4]          # a slice reaching past the cursor
    with pytest.raises(LookaheadError):
        v[-4]           # before the start of history


def test_strategy_reaching_past_the_cursor_raises_during_a_run():
    class Peeker(Recorder):
        def on_bars(self, ctx):
            h = ctx.history(M5_ID)
            if len(h):
                h[len(h)]   # the element that does not exist yet

    with pytest.raises(LookaheadError):
        Engine(dataset(), Peeker(), split=Split.VALIDATION).run()


# --------------------------------------------------------------------------- #
# 2. an order submitted on the bar closing at T cannot fill at or before T
# --------------------------------------------------------------------------- #
def test_order_cannot_fill_on_the_tick_that_triggered_its_submission():
    strat = Recorder(submit_on_first_bar=True)
    eng = Engine(dataset(), strat, split=Split.VALIDATION)
    eng.run()

    assert strat.fills, "the probe order never filled"
    submit_ev = strat.bar_events[0]
    first_fill = strat.fills[0]
    assert first_fill.seq > submit_ev.seq, (
        f"filled at seq {first_fill.seq}, submitted while handling seq {submit_ev.seq}"
    )
    assert first_fill.ts > submit_ev.bars[0].close_ts, (
        "filled at or before the close of the bar that triggered the submission"
    )
    row = eng.blotter.rows[0]
    assert row.ts > submit_ev.bars[0].close_ts


def test_every_fill_respects_its_order_eligibility():
    strat = MomentumStrategy()
    eng = Engine(dataset(signal_strength=0.5), strat, split=Split.VALIDATION)
    eng.run()
    assert eng.blotter.rows
    for row in eng.blotter.rows:
        order = eng.orders[row.order_id]
        assert row.seq >= order.eligible_from_seq


# --------------------------------------------------------------------------- #
# 3. bars closing at the same timestamp arrive in one callback
# --------------------------------------------------------------------------- #
def test_simultaneous_closes_arrive_in_one_callback():
    strat = Recorder(subs=("1m", "5m"))
    Engine(dataset(), strat, split=Split.VALIDATION).run()

    multi = [ev for ev in strat.bar_events if len(ev.bars) > 1]
    assert multi, "no timestamp closed both subscriptions"
    for ev in multi:
        assert len({b.sub_id for b in ev.bars}) == len(ev.bars)
        assert [b.close_ts for b in ev.bars] == [ev.bars[0].close_ts] * len(ev.bars)

    # One event per close, never two. The rule is about *closes*, not about
    # callbacks: one tick arriving after a quiet stretch closes several intervals
    # at once, and they closed at different instants, so they are delivered as
    # separate events in the order they happened. What must never happen is two
    # events claiming the same close, because then the ordering between the
    # subscriptions inside them is a hidden variable -- which is the thing this
    # rule exists to remove.
    closes = [ev.ts for ev in strat.bar_events]
    assert len(closes) == len(set(closes)), "two events claim the same close"
    assert closes == sorted(closes), "closes delivered out of order"
    for ev in strat.bar_events:
        assert ev.ts == ev.bars[0].close_ts


def test_bars_are_stamped_by_open_and_delivered_on_close():
    strat = Recorder(subs=("5m",))
    Engine(dataset(), strat, split=Split.VALIDATION).run()
    for ev, delivered in zip(strat.bar_events, strat.delivered_at):
        for b in ev.bars:
            assert b.close_ts > b.open_ts
            # A full interval, or less when the session ended inside one. The
            # tail bar used to be stamped at the boundary it never reached, so it
            # was delivered at the session close claiming to close later -- true
            # of the one bar per session where a position is most likely open,
            # and invisible while every synthetic session was an exact multiple
            # of the bar interval.
            assert b.close_ts - b.open_ts <= M5.interval_ns
            assert delivered >= b.close_ts, "delivered before it closed"


# --------------------------------------------------------------------------- #
# 4. determinism
# --------------------------------------------------------------------------- #
def test_same_seed_gives_a_byte_identical_blotter():
    def run(seed):
        eng = Engine(
            dataset(seed=seed, signal_strength=0.5), MomentumStrategy(),
            split=Split.VALIDATION, seed=seed,
        )
        eng.run()
        return eng.blotter

    a, b = run(7), run(7)
    assert a.rows, "nothing traded, so determinism is untested"
    assert a.digest() == b.digest()
    assert [r.as_row() for r in a.rows] == [r.as_row() for r in b.rows]
    assert run(8).digest() != a.digest(), "different seeds produced identical blotters"


# --------------------------------------------------------------------------- #
# 5. strategies cannot import the scorer
# --------------------------------------------------------------------------- #
def test_importing_the_scorer_from_a_strategy_module_raises(tmp_path):
    """Runtime guard: the import itself fails, wherever it is attempted from."""
    probe = REPO_ROOT / "src" / "strategies" / "_import_probe_tmp.py"
    probe.write_text("from src.core.scoring import scorer  # noqa: F401\n")
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import src.strategies._import_probe_tmp"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert r.returncode != 0
        assert "may not import src.core" in r.stderr, r.stderr[-400:]
    finally:
        probe.unlink(missing_ok=True)


def test_no_strategy_source_references_the_scorer():
    """Static guard, for an import deferred to call time where the stack walk
    would not see a strategy module."""
    offenders = []
    for path in sorted((REPO_ROOT / "src" / "strategies").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
            else:
                if isinstance(node, ast.Call) and getattr(
                    getattr(node.func, "value", None), "id", ""
                ) == "importlib":
                    offenders.append((path.name, "dynamic importlib call"))
                continue
            for n in names:
                if "scorer" in n:
                    offenders.append((path.name, n))
    assert not offenders, f"strategy source references the scorer: {offenders}"


# --------------------------------------------------------------------------- #
# 6. CI fails on a diff outside src/strategies/
# --------------------------------------------------------------------------- #
def test_agent_path_allowlist():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_agent_paths import violations

    assert violations(["src/strategies/current.py"]) == []
    assert violations(["src/strategies/a.py", "src/strategies/b/c.py"]) == []
    assert violations(["src/core/engine/engine.py"]) == ["src/core/engine/engine.py"]
    assert violations(["src/core/scoring/scorer.py"]) == ["src/core/scoring/scorer.py"]
    assert violations(["tests/test_simulator.py", "src/strategies/x.py"]) == [
        "tests/test_simulator.py"
    ]


def test_agent_path_check_exits_nonzero_on_a_core_diff():
    r = subprocess.run(
        [sys.executable, "scripts/check_agent_paths.py", "--paths", "src/core/scoring/scorer.py"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 1
    assert "src/core/scoring/scorer.py" in r.stderr


# --------------------------------------------------------------------------- #
# 7 & 8. two-sided calibration
# --------------------------------------------------------------------------- #
# 175 sessions so the validation split clears the pre-registered 30-session floor
# (0.2 x 175 = 35), with half-length sessions so the sweep is affordable in a
# test. The committed curves in data/calibration/ use the full configuration.
CAL = dict(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15)
SEEDS = range(1, 7)


def test_null_the_gate_accepts_essentially_nothing():
    rate, results = acceptance_rate(
        MomentumStrategy, signal_strength=0.0, seeds=SEEDS,
        rule=AcceptRule(t_min=3.0), **CAL,
    )
    assert any(r.score.trade_count > 0 for r in results), "no trades, so no null test"
    assert rate <= 0.05, f"false-acceptance rate on a known null is {rate:.0%}"


def test_power_the_gate_finds_a_planted_signal():
    strong, _ = acceptance_rate(
        MomentumStrategy, signal_strength=0.75, seeds=SEEDS,
        rule=AcceptRule(t_min=3.0), **CAL,
    )
    assert strong >= 0.5, f"only {strong:.0%} acceptance on a strong planted signal"


@pytest.mark.slow
def test_power_curve_is_monotone_in_signal_strength():
    curve = []
    for s in (0.0, 0.25, 0.5, 0.75):
        rate, _ = acceptance_rate(
            MomentumStrategy, signal_strength=s, seeds=SEEDS,
            rule=AcceptRule(t_min=3.0), **CAL,
        )
        curve.append(rate)
    # The screening criterion for any metric: monotone in planted signal
    # strength. If it is not, the metric is wrong -- the threshold does not move
    # to hide it. This is the test the placeholder per-trade t failed.
    assert curve == sorted(curve), f"acceptance is not monotone in signal: {curve}"
    assert curve[0] == 0.0 and curve[-1] > 0.5


def test_unfavorable_never_fills_where_favorable_would_not():
    """The invariant, stated where it actually holds: on the *same* order and the
    *same* trade. Comparing two whole runs is not the test -- different fills give
    different positions and therefore different subsequent trades, so at small
    sample the per-trade means are not comparable.
    """
    from src.core.engine.fills import FavorableFill, UnfavorableFill
    from src.core.types import Order, Tick, TimeInForce

    fav, unf = FavorableFill(), UnfavorableFill()
    prices = [99.5, 99.75, 100.0, 100.25, 100.5]

    for side in (Side.BUY, Side.SELL):
        for otype, kw in (
            (OrderType.LIMIT, {"limit_price": 100.0}),
            (OrderType.STOP, {"stop_price": 100.0}),
        ):
            for px in prices:
                def order():
                    return Order(order_id=1, side=side, size=1, order_type=otype,
                                 tif=TimeInForce.GTC, **kw)

                tick = Tick(ts=0, price=px, size=1, session=0)
                f, u = fav.resolve(order(), tick), unf.resolve(order(), tick)
                if u is not None:
                    assert f is not None, (
                        f"unfavorable filled where favorable did not: "
                        f"{side} {otype} at {px}"
                    )
                    better = (f.price <= u.price) if side is Side.BUY else (f.price >= u.price)
                    assert better, f"favorable filled worse: {f.price} vs {u.price}"


@pytest.mark.slow
def test_the_spread_between_fill_models_has_the_right_sign_at_scale():
    """The spread is a first-class output -- a candidate that collapses between
    the two modes is telling you its edge lives in the optimism. It only means
    something over enough trades for the two paths to be comparable."""
    from src.core.scoring.scorer import score

    for s in (0.0, 0.25, 0.5):
        out = {}
        for model in ("favorable", "unfavorable"):
            eng = Engine(
                SyntheticDataset(SyntheticSpec(n_sessions=200, vol_per_tick=0.15,
                                               signal_strength=s, seed=1)),
                MomentumStrategy(), split=Split.VALIDATION, fill_model=model,
            )
            eng.run()
            out[model] = score(eng.blotter, period=eng.session_calendar())
        assert out["favorable"].net_pnl >= out["unfavorable"].net_pnl, (
            f"unfavorable beat favorable at signal {s}: "
            f"{out['favorable'].net_pnl} vs {out['unfavorable'].net_pnl}"
        )


@pytest.mark.slow
def test_the_loss_tail_no_longer_widens_with_signal_strength():
    """Records the corrected attribution, and fails if it regresses.

    Task 4 measured the fixed-bracket strategy's mean loss growing from 1.54 to
    31.45 as the planted signal strengthened, and attributed it to the bracket
    capping every win while a gapped stop lost more in a stronger trend. Task 6
    accepted that reading and fixed it with a volatility-scaled reference
    strategy. Task 7 made the generator carry price across sessions, and the loss
    tail flattened for the *fixed*-bracket strategy too -- mean loss steady near
    1.02 at every signal strength.

    So the proximate description was right and the root cause was not the
    bracket: it was the generator restarting each session at the same price, and
    a strategy holding overnight realizing the whole of the previous session's
    drift. The vol-scaled reference is still the right apparatus -- a bracket
    that ignores volatility will misbehave on real data whose regimes differ --
    but it was fixing a symptom whose cause was elsewhere.
    """
    import statistics as st

    def shape(strategy, **spec):
        stats = {}
        for sig in (0.0, 0.5, 1.0):
            wins, losses = [], []
            for seed in (1, 2):
                eng = Engine(
                    SyntheticDataset(SyntheticSpec(
                        n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
                        signal_strength=sig, seed=seed, **spec)),
                    strategy(), split=Split.VALIDATION, warmup_sessions=1,
                )
                eng.run()
                closes = [r.realized_pnl - r.commission for r in eng.blotter.rows
                          if r.realized_pnl != 0.0]
                wins += [c for c in closes if c > 0]
                losses += [c for c in closes if c <= 0]
            stats[sig] = (st.mean(wins), st.mean(losses))
        ratios = [w / abs(l) for w, l in stats.values()]
        return stats, max(ratios) / min(ratios)

    stats, spread = shape(MomentumStrategy, uniform=True)
    assert spread < 1.1, (
        f"the payoff shape moves with signal strength again: {stats}. "
        "If the generator stopped carrying price across sessions, that is why."
    )

    # And the part Task 17 added, because it changes the conclusion. Once the
    # generator's volatility clusters the way NQ's does, the fixed bracket's loss
    # tail widens with signal strength again -- mean win pinned by the bracket at
    # 0.74 while the mean loss grows by about a third. Task 7's flattening was
    # real but it was a property of a stationary world, not of the strategy.
    #
    # The vol-scaled reference is measurably less affected on the same data,
    # which is the evidence for keeping it as the apparatus rather than a matter
    # of taste.
    irregular_stats, irregular_spread = shape(MomentumStrategy)
    assert irregular_spread > 1.2, (
        "the fixed bracket's loss tail no longer widens on irregular data. "
        "Either the generator stopped clustering volatility or the bracket "
        f"stopped being fixed: {irregular_stats}"
    )
    _, reference_spread = shape(ReferenceStrategy)
    assert reference_spread < irregular_spread, (
        f"the vol-scaled reference ({reference_spread:.2f}) is no better than the "
        f"fixed bracket ({irregular_spread:.2f}) on irregular data, which is the "
        "only reason it is the calibration apparatus"
    )
    losses = [abs(l) for _, l in stats.values()]
    assert max(losses) / min(losses) < 1.1, (
        f"the loss tail is widening with signal strength again: {stats}"
    )
