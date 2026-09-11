"""Task 7: tick events, bracket offsets, reduce_only, the budget, and replay.

Each of these is a place where the interface said one thing and did another --
an aggregation choice made for the agent, a validation that could not be
complete, a parameter accepted and ignored, a budget read and not enforced, a
digest that identified without reproducing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.engine.context import ContextError  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import BudgetExceeded, Engine  # noqa: E402
from src.core.types import (  # noqa: E402
    LifecycleKind,
    OrderStatus,
    Side,
    Split,
    Subscription,
)

STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


class Base:
    subs = ("1m",)

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
# 1. tick subscription
# --------------------------------------------------------------------------- #
def test_the_subscription_type_can_express_a_tick_stream():
    """The type, not only the implementation. A time-interval-only type is what
    would have become an API break later."""
    tick = Subscription(kind="tick")
    assert tick.sub_id == "tick" and tick.interval_ns is None and not tick.is_bars
    with pytest.raises(ValueError, match="no interval"):
        Subscription(kind="tick", interval_ns=5)
    with pytest.raises(ValueError, match="positive interval_ns"):
        Subscription()
    with pytest.raises(NotImplementedError, match="unknown subscription kind"):
        Subscription(kind="volume")


def test_ticks_are_delivered_with_price_size_side_and_timestamp():
    class T(Base):
        subs = ("tick",)

        def __init__(self):
            self.seen = []

        def on_tick(self, ctx):
            self.seen.append(ctx.tick)

    s = T()
    rec = Engine(ds(), s, split=Split.VALIDATION).run()
    assert s.seen and rec.tick_events_delivered == len(s.seen)
    ev = s.seen[5]
    assert ev.price > 0 and ev.size > 0 and ev.ts > 0
    assert {t.side for t in s.seen} & {Side.BUY, Side.SELL}, "no aggressor was ever set"
    # and the history view is populated and refuses to reach forward
    assert len(s.seen) == rec.tick_events_delivered


def test_a_tick_precedes_the_bar_it_closes():
    """Pinned the same way bar ordering is. The tick that closes a bar belongs to
    the next bar, so delivering it after the close would report it out of order.
    """
    class Both(Base):
        subs = ("tick", "1m")

        def __init__(self):
            self.order = []

        def on_tick(self, ctx):
            self.order.append(("tick", ctx.ts))

        def on_bars(self, ctx):
            self.order.append(("bars", ctx.ts))

        def on_fill(self, ctx):
            self.order.append(("fill", ctx.ts))

    s = Both()
    Engine(ds(), s, split=Split.VALIDATION).run()
    by_ts = defaultdict(list)
    for kind, ts in s.order:
        by_ts[ts].append(kind)
    shared = [k for k in by_ts.values() if "tick" in k and "bars" in k]
    assert shared, "no timestamp delivered both"
    for kinds in shared:
        assert kinds.index("tick") < kinds.index("bars")


def test_a_fill_is_delivered_before_the_tick_that_caused_it():
    """A strategy should learn about its own fill before being asked to react to
    the trade that caused it."""
    class Trader(Base):
        subs = ("tick", "1m")

        def __init__(self):
            self.order = []
            self.done = False

        def on_tick(self, ctx):
            self.order.append(("tick", ctx.ts))
            if ctx.actionable and not self.done and not ctx.position:
                self.done = True
                ctx.buy(1, tag="probe")

        def on_fill(self, ctx):
            self.order.append(("fill", ctx.ts))

    s = Trader()
    Engine(ds(), s, split=Split.VALIDATION).run()
    fills = [i for i, (k, _) in enumerate(s.order) if k == "fill"]
    assert fills
    for i in fills:
        ts = s.order[i][1]
        same = [j for j, (k, t) in enumerate(s.order) if t == ts and k == "tick"]
        assert all(i < j for j in same), "a tick was delivered before its own fill"


def test_subscribing_to_ticks_without_an_on_tick_callback_is_an_error():
    class Silent(Base):
        subs = ("tick",)

    with pytest.raises(TypeError, match=r"on_tick\(ctx\) is missing"):
        Engine(ds(), Silent(), split=Split.VALIDATION).run()


def test_train_bars_refuses_a_tick_subscription_and_says_what_to_use():
    class S(Base):
        def setup(self, ctx):
            ctx.subscribe("1m")
            ctx.train_bars(Subscription(kind="tick"))

    with pytest.raises(ContextError, match="ctx.ticks"):
        Engine(ds(), S(), split=Split.VALIDATION).run()


# --------------------------------------------------------------------------- #
# 2. bracket offsets resolve at the fill
# --------------------------------------------------------------------------- #
def test_offsets_resolve_against_the_fill_price_not_last_price():
    """Which is the whole reason offsets exist: a market order has no entry price
    at submission, so an absolute level can arm on the wrong side after a T+1
    fill at a different price."""
    class Bracketed(Base):
        def __init__(self):
            self.submitted_at = None

        def on_bars(self, ctx):
            if not ctx.actionable or ctx.position or ctx.working_order_ids:
                return
            if self.submitted_at is None:
                self.submitted_at = ctx.last_price
                ctx.buy(1, stop_offset=0.5, target_offset=0.5, tag="e")

    s = Bracketed()
    eng = Engine(ds(signal_strength=0.5), s, split=Split.VALIDATION)
    eng.run()
    entry = eng.blotter.rows[0]
    legs = [o for o in eng.orders.values() if o.parent_id == entry.order_id]
    assert len(legs) == 2
    stop = next(o for o in legs if o.stop_price is not None)
    target = next(o for o in legs if o.limit_price is not None)
    assert stop.stop_price == pytest.approx(entry.price - 0.5)
    assert target.limit_price == pytest.approx(entry.price + 0.5)
    if s.submitted_at != entry.price:
        # the fill gapped away from the estimate, and the bracket followed it
        assert stop.stop_price != pytest.approx(s.submitted_at - 0.5)


def test_absolute_levels_are_still_available_where_the_entry_is_known():
    class Limit(Base):
        def on_bars(self, ctx):
            if not ctx.actionable or getattr(self, "done", False):
                return
            self.done = True
            px = ctx.last_price
            self.oid = ctx.buy(1, type="limit", limit=px, stop=px - 1.0,
                               target=px + 1.0, tag="lim")

    s = Limit()
    eng = Engine(ds(), s, split=Split.VALIDATION)
    eng.run()
    assert s.oid in eng.orders


# --------------------------------------------------------------------------- #
# 3. reduce_only, and its siblings
# --------------------------------------------------------------------------- #
def test_reduce_only_is_rejected_when_flat():
    class Flat(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not getattr(self, "done", False) and not ctx.position:
                self.done = True
                ctx.sell(1, reduce_only=True, tag="ro")

    s = Flat()
    eng = Engine(ds(), s, split=Split.VALIDATION)
    eng.run()
    assert not eng.blotter.rows, "a reduce-only order opened a position"
    assert any(
        e.kind is LifecycleKind.ORDER_REJECTED and "no position" in e.detail
        for e in s.lifecycle
    )


def test_reduce_only_refuses_to_increase_exposure():
    class Adder(Base):
        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tag="open")
            elif ctx.position > 0 and not getattr(self, "tried", False):
                self.tried = True
                ctx.buy(5, reduce_only=True, tag="more")

    s = Adder()
    eng = Engine(ds(), s, split=Split.VALIDATION)
    eng.run()
    assert getattr(s, "tried", False)
    assert max(abs(r.position_after) for r in eng.blotter.rows) == 1
    assert any(
        e.kind is LifecycleKind.ORDER_REJECTED and "increase" in e.detail
        for e in s.lifecycle
    )


def test_reduce_only_clamps_an_oversized_order_to_the_position():
    class Over(Base):
        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tag="open")
            elif ctx.position > 0 and not getattr(self, "tried", False):
                self.tried = True
                ctx.sell(9, reduce_only=True, tag="close")

    s = Over()
    eng = Engine(ds(), s, split=Split.VALIDATION)
    eng.run()
    assert getattr(s, "tried", False)
    closing = [r for r in eng.blotter.rows if r.side is Side.SELL]
    assert closing and all(r.size <= 1 for r in closing)
    assert all(abs(r.position_after) <= 1 for r in eng.blotter.rows), (
        "a reduce-only order flipped the position"
    )


def test_gtd_expires_mid_session_not_only_at_the_boundary():
    """It was checked only at a session close, so an order expiring at noon
    rested all afternoon -- accepted, carried, quietly not done."""
    class Timed(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not getattr(self, "done", False):
                self.done = True
                self.expiry = ctx.ts + 5 * 60 * 1_000_000_000
                self.oid = ctx.buy(1, type="limit", limit=1.0, tif="GTD",
                                   expire_ts=self.expiry, tag="gtd")

    s = Timed()
    eng = Engine(ds(), s, split=Split.VALIDATION)
    eng.run()
    order = eng.orders[s.oid]
    assert order.status is OrderStatus.EXPIRED
    expired = [
        e for e in s.lifecycle
        if e.kind is LifecycleKind.GTD_EXPIRED and e.order_id == s.oid
    ]
    assert expired, "the GTD order never expired"
    ev = expired[0]
    assert ev.ts >= s.expiry
    # and it expired inside its session, not at the boundary
    session_start, session_end = eng.dataset.session_bounds(
        next(k for k in range(50)
             if eng.dataset.session_bounds(k)[0] <= ev.ts <= eng.dataset.session_bounds(k)[1])
    )
    assert ev.ts < session_end, "the expiry waited for the session close"


# --------------------------------------------------------------------------- #
# 4 & 5. budget and replay, through run.py
# --------------------------------------------------------------------------- #
def run_py(*args, ledger: Path, cwd: Path = REPO_ROOT):
    return subprocess.run(
        [sys.executable, "run.py", "--human", "--ledger", str(ledger), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_a_slow_strategy_is_abandoned_and_the_trial_is_counted(tmp_path):
    led = tmp_path / "ledger.jsonl"
    original = STRATEGY_PATH.read_text()
    cfg = REPO_ROOT / "config" / "run.yaml"
    cfg_original = cfg.read_text()
    STRATEGY_PATH.write_text(original + (
        "\n\nimport time as _t\n\n\n"
        "class Strategy(MomentumStrategy):\n"
        "    def on_bars(self, ctx):\n"
        "        _t.sleep(0.02)\n"
        "        super().on_bars(ctx)\n"
    ))
    cfg.write_text(cfg_original.replace("wall_clock_budget_s: 900",
                                        "wall_clock_budget_s: 2"))
    try:
        r = run_py(ledger=led)
        assert r.returncode == 3
        assert "abandoned" in r.stderr
        recorded = rows(led)
        assert len(recorded) == 1, "an abandoned run is a trial and must be counted"
        assert recorded[0]["outcome"] == "abandoned_on_budget"
        assert recorded[0]["wall_clock_budget_s"] == 2
        assert recorded[0]["wall_clock_s"] < 8, "the budget overshot badly"
    finally:
        STRATEGY_PATH.write_text(original)
        cfg.write_text(cfg_original)


def test_the_budget_is_not_enforced_when_none_is_set():
    eng = Engine(ds(), Base(), split=Split.VALIDATION, wall_clock_budget_s=None)
    rec = eng.run()
    assert rec.outcome == "completed" and rec.wall_clock_budget_s is None


def test_a_ledger_row_replays_from_its_stored_source(tmp_path):
    """A digest identifies a trial; the stored source reproduces it."""
    led = tmp_path / "ledger.jsonl"
    assert run_py("--quiet", ledger=led).returncode == 0
    row = rows(led)[0]
    assert row["strategy_source"], "the ledger row does not point at a source"
    stored = Path(row["strategy_source"])
    if not stored.is_absolute():
        stored = REPO_ROOT / stored
    assert stored.exists()

    r = subprocess.run(
        [sys.executable, "scripts/replay.py", str(led), "0", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "identical" in r.stdout


def test_an_edited_strategy_replays_from_the_source_it_ran_with(tmp_path):
    """The case that matters: the source at that moment was uncommitted and is
    now gone from the working tree."""
    led = tmp_path / "ledger.jsonl"
    original = STRATEGY_PATH.read_text()
    STRATEGY_PATH.write_text(
        original.replace("stop: float = 0.75", "stop: float = 0.4")
    )
    try:
        assert run_py("--quiet", ledger=led).returncode == 0
    finally:
        STRATEGY_PATH.write_text(original)

    r = subprocess.run(
        [sys.executable, "scripts/replay.py", str(led), "0", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- #
# 6. continuous session prices
# --------------------------------------------------------------------------- #
def test_a_session_opens_where_the_previous_one_closed():
    d = ds(n_sessions=30)
    for k in (1, 5, 12):
        prev, cur = d._session_ticks(k - 1), d._session_ticks(k)
        assert cur[0].price == pytest.approx(prev[-1].price, abs=0.26), (
            "the generator still restarts sessions at a fixed price"
        )


def test_serving_one_split_gives_the_same_prices_as_a_full_walk():
    """Session opens chain, so a late split must not depend on whether the
    earlier ones were asked for."""
    a = ds(n_sessions=30)
    full = {t.ts: t.price for k in range(30) for t in a._session_ticks(k)}
    b = ds(n_sessions=30)
    served = {t.ts: t.price for t in b.ticks(Split.VALIDATION)}
    assert served and all(full[k] == v for k, v in served.items())


def test_the_generator_can_still_reset_sessions_if_asked():
    d = ds(continuous_sessions=False)
    assert d._session_ticks(3)[0].price == pytest.approx(100.0, abs=0.26)


# --------------------------------------------------------------------------- #
# adversarial review, Task 20: replay on a real row
# --------------------------------------------------------------------------- #
def _replay_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "replay_script", REPO_ROOT / "scripts" / "replay.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.live
def test_a_real_data_ledger_row_replays_byte_identically():
    """The review's finding: "any trial can be replayed from its stored source"
    was true on synthetic and false for the one real row that existed.

    `replay.py` reconstructed a *synthetic* dataset from whatever profile the row
    mentioned, so the `nq-smoke` row died with `KeyError: 'nq-smoke'` -- the
    machinery was built and verified where the dataset was reconstructable and
    never exercised where it is not. The row is the authority now: `dataset`
    names the adapter, `dataset_digest` pins the bytes.
    """
    ledger = REPO_ROOT / "runs" / "ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    real = [r for r in rows if not r["dataset"].startswith("synthetic")]
    if not real:
        pytest.skip("no real-data row in the committed ledger")

    module = _replay_module()
    for row in real:
        engine = module.replay(row)
        assert engine.blotter.digest() == row["blotter_digest"], row["dataset"]


def test_replay_refuses_when_the_data_has_changed_since_the_trial():
    """A digest that no longer matches is a refusal, loudly. Replaying against
    data that has since changed is worse than not replaying, because it produces
    a number."""
    module = _replay_module()
    cfg = {"datasets": {"synthetic": {"kind": "synthetic", "n_sessions": 40,
                                      "ticks_per_session": 390,
                                      "vol_per_tick": 0.15,
                                      "signal_strength": 0.0}}}
    row = {"dataset": "synthetic:s=0:seed=1", "seed": 1,
           "dataset_digest": "not-the-digest-it-has-now"}
    with pytest.raises(SystemExit, match="no longer hashes"):
        module.rebuild_dataset(row, cfg)


def test_replay_rebuilds_the_dataset_the_row_names_not_the_profiles():
    """The second half of the same finding: a dataset override replayed against
    the profile's current choice, producing a digest mismatch that looked like
    non-determinism."""
    module = _replay_module()
    cfg = {"datasets": {
        "a": {"kind": "synthetic", "n_sessions": 40, "ticks_per_session": 390,
              "vol_per_tick": 0.15, "signal_strength": 0.0},
        "b": {"kind": "synthetic", "n_sessions": 40, "ticks_per_session": 390,
              "vol_per_tick": 0.15, "signal_strength": 0.5},
    }}
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec

    spec = {k: v for k, v in cfg["datasets"]["b"].items() if k != "kind"}
    wanted = SyntheticDataset(SyntheticSpec(**spec, seed=3))
    row = {"dataset": wanted.name, "seed": 3, "dataset_digest": wanted.digest()}
    rebuilt = module.rebuild_dataset(row, cfg)
    assert rebuilt.name == wanted.name
    assert rebuilt.digest() == wanted.digest()
