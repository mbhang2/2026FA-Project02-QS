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

from src.core.calibration import AcceptRule, acceptance_rate  # noqa: E402
from src.core.datasets.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
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
from src.core.view import HistoryView  # noqa: E402
from src.strategies.current import MomentumStrategy  # noqa: E402

M1 = Subscription(interval_ns=60 * NS_PER_SECOND)
M5 = Subscription(interval_ns=5 * 60 * NS_PER_SECOND)


def dataset(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1)
    base.update(kw)
    # 20 sessions is too few for the default fractions; use a wider train share
    base.setdefault("train_frac", 0.5)
    base.setdefault("validation_frac", 0.2)
    return SyntheticDataset(SyntheticSpec(**base))


class Recorder:
    """Minimal strategy that records what it was handed."""

    def __init__(self, subs=(M5,), submit_on_first_bar=False):
        self.subs = list(subs)
        self.submit_on_first_bar = submit_on_first_bar
        self.bar_events: list[BarEvent] = []
        self.fills = []
        self.lifecycle = []
        self._submitted = False

    def setup(self, ctx):
        return self.subs

    def on_bars(self, ev, ctx) -> list[Action]:
        self.bar_events.append(ev)
        if self.submit_on_first_bar and not self._submitted and ctx.actionable:
            self._submitted = True
            return [Submit(side=Side.BUY, size=1, order_type=OrderType.MARKET,
                           tif=TimeInForce.DAY, tag="probe")]
        return []

    def on_fill(self, ev, ctx):
        self.fills.append(ev)
        return []

    def on_lifecycle(self, ev, ctx):
        self.lifecycle.append(ev)
        return []


# --------------------------------------------------------------------------- #
# 1. reaching at or beyond the current index raises
# --------------------------------------------------------------------------- #
def test_history_view_refuses_to_reach_forward():
    v = HistoryView([10, 20, 30])
    assert v[-1] == 30 and v[0] == 10 and v[:3] == [10, 20, 30]
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
        def on_bars(self, ev, ctx):
            h = ctx.bars[M5.sub_id]
            return [] if len(h) == 0 else h[len(h)] and []

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
    strat = Recorder(subs=(M1, M5))
    Engine(dataset(), strat, split=Split.VALIDATION).run()

    multi = [ev for ev in strat.bar_events if len(ev.bars) > 1]
    assert multi, "no timestamp closed both subscriptions"
    for ev in multi:
        assert len({b.sub_id for b in ev.bars}) == len(ev.bars)
        assert [b.close_ts for b in ev.bars] == [ev.bars[0].close_ts] * len(ev.bars)

    # and never two callbacks at the same timestamp
    stamps = [ev.ts for ev in strat.bar_events]
    assert len(stamps) == len(set(stamps)), "more than one BarEvent at a timestamp"


def test_bars_are_stamped_by_open_and_delivered_on_close():
    strat = Recorder(subs=(M5,))
    Engine(dataset(), strat, split=Split.VALIDATION).run()
    for ev in strat.bar_events:
        for b in ev.bars:
            assert b.close_ts > b.open_ts
            assert b.close_ts - b.open_ts == M5.interval_ns
            assert ev.ts >= b.close_ts, "delivered before it closed"


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
    probe.write_text("from src.core import scorer  # noqa: F401\n")
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
    assert violations(["src/core/engine.py"]) == ["src/core/engine.py"]
    assert violations(["src/core/scorer.py"]) == ["src/core/scorer.py"]
    assert violations(["tests/test_simulator.py", "src/strategies/x.py"]) == [
        "tests/test_simulator.py"
    ]


def test_agent_path_check_exits_nonzero_on_a_core_diff():
    r = subprocess.run(
        [sys.executable, "scripts/check_agent_paths.py", "--paths", "src/core/scorer.py"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 1
    assert "src/core/scorer.py" in r.stderr


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
    from src.core.fills import FavorableFill, UnfavorableFill
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
    from src.core.scorer import score

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
def test_the_sweep_turns_over_at_high_signal_and_the_cause_is_the_payoff():
    """The screening criterion fails at the strongest planted signal, and this
    records why rather than trimming the sweep to where it passes.

    `MomentumStrategy` has a fixed 0.75 bracket, so every win is capped -- while a
    stop gapped through under the pessimistic fill model loses more the stronger
    the trend. Past a point the loss tail grows faster than the edge, session
    dispersion outruns session mean, and any mean-over-risk ratio must fall. That
    is the metric behaving correctly on a payoff that genuinely got riskier;
    `test_sortino_is_monotone_in_edge_when_the_risk_profile_is_held_fixed` in
    tests/test_metrics.py shows the metric itself is monotone.

    If someone gives the strategy a volatility-scaled bracket this test should
    start failing, and the sweep should then be monotone through 1.0.
    """
    import statistics as st

    from src.core import metrics

    stats = {}
    for sig in (0.75, 1.0):
        wins, losses = [], []
        for seed in (1, 2, 3, 4):
            eng = Engine(
                SyntheticDataset(SyntheticSpec(n_sessions=175, ticks_per_session=390,
                                               vol_per_tick=0.15, signal_strength=sig,
                                               seed=seed)),
                MomentumStrategy(), split=Split.VALIDATION, warmup_sessions=1,
            )
            eng.run()
            closes = [r.realized_pnl - r.commission for r in eng.blotter.rows
                      if r.realized_pnl != 0.0]
            wins += [c for c in closes if c > 0]
            losses += [c for c in closes if c <= 0]
        stats[sig] = (st.mean(wins), st.mean(losses))

    (win_lo, loss_lo), (win_hi, loss_hi) = stats[0.75], stats[1.0]
    assert win_hi == pytest.approx(win_lo, rel=0.05), (
        "the upside is no longer capped by the bracket, so the diagnosis has changed"
    )
    assert loss_hi < loss_lo * 1.5, (
        f"the loss tail did not widen with signal strength: {loss_lo} -> {loss_hi}"
    )
