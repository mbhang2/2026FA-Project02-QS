"""The context facade and the run entry point.

The facade is convenience for the agent; it must not be a hole. These tests are
the argument that it is not: queued calls apply after the callback returns, T+1
still holds through the new path, the cap still drops and counts, a stale context
raises, every validation rule has a message, the scorer and metric remain
unreachable, and the pre-refactor blotters reproduce byte for byte.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.context import ContextError, ContextExpired, parse_interval  # noqa: E402
from src.core.datasets.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import OrderStatus, Split  # noqa: E402
from src.strategies.current import MomentumStrategy  # noqa: E402

GOLDEN = json.loads((REPO_ROOT / "tests" / "golden" / "pre_context_refactor.json").read_text())


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


class Base:
    """Subscribes to 1m and does nothing else."""

    def setup(self, ctx):
        ctx.subscribe("1m")

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


# --------------------------------------------------------------------------- #
# the refactor changed no behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: f"{c['fill_model']}-s{c['spec']['signal_strength']}-seed{c['spec']['seed']}")
def test_blotter_is_byte_identical_to_the_pre_refactor_implementation(case):
    """The acceptance criterion for Task 5: a refactor, not a behaviour change."""
    eng = Engine(
        SyntheticDataset(SyntheticSpec(**case["spec"])), MomentumStrategy(),
        split=Split.VALIDATION, warmup_sessions=1,
        fill_model=case["fill_model"], seed=case["spec"]["seed"],
    )
    eng.run()
    assert eng.blotter.digest() == case["blotter_digest"]
    assert len(eng.blotter.rows) == case["rows"]
    assert eng.n_fills == case["fills"] and eng.n_bars == case["bars"]
    assert eng.blotter.final_position == case["final_position"]


# --------------------------------------------------------------------------- #
# queued, not applied
# --------------------------------------------------------------------------- #
def test_a_queued_call_takes_effect_after_the_callback_returns_never_during():
    seen = []

    class Watcher(Base):
        def on_bars(self, ctx):
            if not ctx.actionable or ctx.position or ctx.working_order_ids:
                return
            before = ctx.working_order_ids
            oid = ctx.buy(1, tag="probe")
            after = ctx.working_order_ids
            seen.append((before, oid, after))

    eng = Engine(ds(), Watcher(), split=Split.VALIDATION)
    eng.run()
    assert seen, "the probe never ran"
    before, oid, after = seen[0]
    assert oid is not None, "buy() did not return an order id"
    assert oid not in before and oid not in after, (
        "the order existed during the callback that asked for it"
    )
    assert oid in eng.orders, "the queued order was never applied"


def test_order_ids_are_allocated_at_the_call_and_honoured_on_apply():
    ids = []

    class Two(Base):
        def on_bars(self, ctx):
            if not ctx.actionable or ids:
                return
            ids.append(ctx.buy(1, type="limit", limit=1.0, tif="GTC"))
            ids.append(ctx.buy(1, type="limit", limit=1.0, tif="GTC"))

    eng = Engine(ds(), Two(), split=Split.VALIDATION)
    eng.run()
    assert ids == sorted(ids) and len(set(ids)) == 2
    for oid in ids:
        assert eng.orders[oid].order_id == oid


def test_t_plus_one_still_holds_through_the_context_path():
    marks = []

    class Probe(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not marks:
                marks.append(("submit", ctx._seq))
                ctx.buy(1, tag="probe")

        def on_fill(self, ctx):
            marks.append(("fill", ctx._seq))

    eng = Engine(ds(), Probe(), split=Split.VALIDATION)
    eng.run()
    kinds = dict(marks)
    assert "fill" in kinds, "the probe never filled"
    assert kinds["fill"] > kinds["submit"]
    for row in eng.blotter.rows:
        assert row.seq >= eng.orders[row.order_id].eligible_from_seq


# --------------------------------------------------------------------------- #
# the cap counts queued calls
# --------------------------------------------------------------------------- #
def test_the_action_cap_counts_queued_calls_and_drops_the_rest():
    class Spammer(Base):
        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            self.returned = [ctx.buy(1, type="limit", limit=1.0) for _ in range(10)]

    strat = Spammer()
    eng = Engine(ds(), strat, split=Split.VALIDATION, action_cap=4)
    rec = eng.run()
    assert rec.action_cap_hits > 0
    assert rec.actions_dropped == rec.action_cap_hits * 6
    # a dropped call gets no id, so it consumes none
    assert strat.returned[:4] == [i for i in strat.returned[:4] if i is not None]
    assert strat.returned[4:] == [None] * 6


def test_a_dropped_call_does_not_consume_an_order_id():
    class Spammer(Base):
        def on_bars(self, ctx):
            if not ctx.actionable or getattr(self, "done", False):
                return
            self.done = True
            self.ids = [ctx.buy(1, type="limit", limit=1.0) for _ in range(10)]

    strat = Spammer()
    eng = Engine(ds(), strat, split=Split.VALIDATION, action_cap=3)
    eng.run()
    issued = [i for i in strat.ids if i is not None]
    assert issued == list(range(issued[0], issued[0] + 3)), (
        f"ids are not contiguous, so a dropped call took one: {strat.ids}"
    )


def test_warmup_calls_are_inert_and_counted_rather_than_silently_dropped():
    class Eager(Base):
        def on_bars(self, ctx):
            self.last = ctx.buy(1, tag="always")

    strat = Eager()
    eng = Engine(ds(), strat, split=Split.VALIDATION, warmup_sessions=2)
    rec = eng.run()
    assert rec.warmup_actions_ignored > 0
    assert rec.action_cap_hits == 0, "warmup calls were charged to the action cap"


# --------------------------------------------------------------------------- #
# a context outlives nothing
# --------------------------------------------------------------------------- #
def test_a_stashed_context_raises_when_used_later():
    class Hoarder(Base):
        stashed = None

        def on_bars(self, ctx):
            if self.stashed is None:
                self.stashed = ctx
                return
            with pytest.raises(ContextExpired):
                self.stashed.position
            with pytest.raises(ContextExpired):
                self.stashed.buy(1)
            self.checked = True

    strat = Hoarder()
    Engine(ds(), strat, split=Split.VALIDATION).run()
    assert getattr(strat, "checked", False), "the stale context was never exercised"


def test_the_setup_context_expires_too():
    class Late(Base):
        def setup(self, ctx):
            ctx.subscribe("1m")
            self.setup_ctx = ctx

        def on_bars(self, ctx):
            with pytest.raises(ContextExpired):
                self.setup_ctx.subscribe("5m")
            self.checked = True

    strat = Late()
    Engine(ds(), strat, split=Split.VALIDATION).run()
    assert strat.checked


# --------------------------------------------------------------------------- #
# validation: one test per rule, each with its own message
# --------------------------------------------------------------------------- #
def _raises_on(call, match):
    class Bad(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not getattr(self, "done", False):
                self.done = True
                call(ctx)

    with pytest.raises(ContextError, match=match):
        Engine(ds(), Bad(), split=Split.VALIDATION).run()


def test_size_must_be_a_positive_whole_number():
    _raises_on(lambda c: c.buy(0), "size must be a positive whole number")
    _raises_on(lambda c: c.buy(-1), "use sell\\(\\) rather than a negative size")
    _raises_on(lambda c: c.buy(1.5), "size must be a positive whole number")


def test_a_limit_order_needs_a_limit_price():
    _raises_on(lambda c: c.buy(1, type="limit"), "needs limit=")


def test_a_market_order_cannot_carry_a_limit_price():
    _raises_on(lambda c: c.buy(1, limit=100.0), "market order cannot carry a limit")


def test_a_stop_order_needs_a_trigger_and_the_message_says_what_stop_means():
    _raises_on(lambda c: c.buy(1, type="stop"), "needs trigger=")


def test_a_stop_must_be_below_the_entry_for_a_long():
    _raises_on(lambda c: c.buy(1, stop=c.last_price + 5), "must be below the entry")


def test_a_stop_must_be_above_the_entry_for_a_short():
    _raises_on(lambda c: c.sell(1, stop=c.last_price - 5), "must be above the entry")


def test_a_target_must_be_on_the_other_side_of_the_entry():
    _raises_on(lambda c: c.buy(1, target=c.last_price - 5), "must be above the entry")
    _raises_on(lambda c: c.sell(1, target=c.last_price + 5), "must be below the entry")


def test_bracket_levels_cannot_mix_prices_and_offsets():
    _raises_on(
        lambda c: c.buy(1, stop=c.last_price - 5, target_offset=1.0),
        "not both",
    )


def test_an_offset_is_a_positive_distance():
    _raises_on(lambda c: c.buy(1, stop_offset=-1.0), "positive distance")


def test_a_gtd_order_needs_an_expiry():
    _raises_on(lambda c: c.buy(1, tif="GTD"), "needs expire_ts")


def test_modify_and_cancel_need_an_order_that_is_still_working():
    _raises_on(lambda c: c.cancel(9999), "no order 9999")
    _raises_on(lambda c: c.modify(9999, limit=1.0), "no order 9999")
    _raises_on(lambda c: c.modify(0), "no order 0")


def test_cannot_modify_or_cancel_a_finished_order():
    class Late(Base):
        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if not hasattr(self, "oid"):
                self.oid = ctx.buy(1, tag="probe")
                return
            if ctx.position and self.oid in ctx._engine.orders:
                order = ctx._engine.orders[self.oid]
                if order.status is OrderStatus.FILLED:
                    with pytest.raises(ContextError, match="already"):
                        ctx.cancel(self.oid)
                    with pytest.raises(ContextError, match="cannot modify"):
                        ctx.modify(self.oid, limit=1.0)
                    self.checked = True

    strat = Late()
    Engine(ds(), strat, split=Split.VALIDATION).run()
    assert getattr(strat, "checked", False)


def test_close_with_no_position_says_so():
    _raises_on(lambda c: c.close(), "no position open")


def test_an_unknown_subscription_names_the_ones_that_exist():
    class Wrong(Base):
        def on_bars(self, ctx):
            ctx.history("15m")

    with pytest.raises(KeyError, match="no subscription"):
        Engine(ds(), Wrong(), split=Split.VALIDATION).run()


def test_setup_must_declare_a_subscription():
    class Silent(Base):
        def setup(self, ctx):
            pass

    with pytest.raises(ValueError, match="ctx.subscribe"):
        Engine(ds(), Silent(), split=Split.VALIDATION).run()


def test_returning_subscriptions_from_setup_is_an_error_not_a_silent_no_op():
    class OldShape(Base):
        def setup(self, ctx):
            return [ctx.subscribe("1m")]

    with pytest.raises(TypeError, match="setup\\(\\) returns nothing now"):
        Engine(ds(), OldShape(), split=Split.VALIDATION).run()


def test_interval_strings_parse_and_bad_ones_say_what_is_accepted():
    assert parse_interval("30s") == 30 * 1_000_000_000
    assert parse_interval("5m") == 300 * 1_000_000_000
    assert parse_interval("1h") == 3600 * 1_000_000_000
    with pytest.raises(ContextError, match="'30s', '5m', '1h', '1d'"):
        parse_interval("5 minutes")


# --------------------------------------------------------------------------- #
# what the context does and does not expose
# --------------------------------------------------------------------------- #
def test_the_context_exposes_pnl_but_nothing_the_scorer_computes():
    seen = {}

    class Peek(Base):
        def on_bars(self, ctx):
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tag="probe")
            if ctx.position:
                seen["realized"] = ctx.realized_pnl
                seen["unrealized"] = ctx.unrealized_pnl
                seen["equity"] = ctx.equity
                seen["surface"] = {a for a in dir(ctx) if not a.startswith("_")}

    Engine(ds(), Peek(), split=Split.VALIDATION).run()
    assert "equity" in seen, "the probe never held a position"
    assert seen["equity"] == pytest.approx(seen["realized"] + seen["unrealized"])
    forbidden = {"score", "scorer", "metric", "metrics", "sortino", "sharpe",
                 "split", "seed", "calendar", "dataset"}
    assert not (seen["surface"] & forbidden), (
        f"the context exposes {seen['surface'] & forbidden}"
    )


def test_strategies_still_cannot_import_the_scorer_or_the_metric():
    """Importing the scorer trips the metric's guard first, since the scorer
    imports the metric -- either way the import fails and says why."""
    for module in ("scorer", "metrics"):
        probe = REPO_ROOT / "src" / "strategies" / f"_probe_{module}.py"
        probe.write_text(f"from src.core import {module}  # noqa: F401\n")
        try:
            r = subprocess.run(
                [sys.executable, "-c", f"import src.strategies._probe_{module}"],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            assert r.returncode != 0, f"importing {module} from a strategy succeeded"
            assert "ImportError" in r.stderr
            assert any(
                f"may not import src.core.{m}" in r.stderr for m in ("scorer", "metrics")
            ), r.stderr[-400:]
        finally:
            probe.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# run.py
# --------------------------------------------------------------------------- #
def run_py(*args, ledger: Path):
    return subprocess.run(
        [sys.executable, "run.py", "--ledger", str(ledger), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def ledger_lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_a_run_emits_exactly_one_ledger_record_with_its_trigger(tmp_path):
    led = tmp_path / "ledger.jsonl"
    assert run_py("--quiet", ledger=led).returncode == 0
    rows = ledger_lines(led)
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger"] == "harness" and row["profile"] == "harness"
    assert row["outcome"] == "completed"
    for field in ("dataset", "dataset_digest", "split", "fill_model", "seed",
                  "git_sha", "wall_clock_s", "subscriptions", "score", "diagnostics",
                  "blotter_digest", "action_cap_hits", "end_of_session_positions",
                  "strategy", "strategy_digest"):
        assert field in row, f"the ledger row is missing {field}"


def test_the_ledger_can_tell_two_uncommitted_strategy_edits_apart(tmp_path):
    """git_sha alone does not identify a trial: the agent edits current.py and
    runs it before anything is committed."""
    led = tmp_path / "ledger.jsonl"
    path = REPO_ROOT / "src" / "strategies" / "current.py"
    original = path.read_text()
    run_py("--quiet", ledger=led)
    try:
        path.write_text(original + "\n# an uncommitted edit\n")
        run_py("--quiet", ledger=led)
    finally:
        path.write_text(original)
    a, b = ledger_lines(led)
    assert a["git_sha"] == b["git_sha"]
    assert a["strategy_digest"] != b["strategy_digest"]


def test_the_ledger_is_append_only_and_counts_every_invocation(tmp_path):
    led = tmp_path / "ledger.jsonl"
    for _ in range(3):
        run_py("--quiet", ledger=led)
    run_py("--trigger", "agent", "--quiet", ledger=led)
    rows = ledger_lines(led)
    assert len(rows) == 4, "N is only honest if every run lands in the ledger"
    assert [r["trigger"] for r in rows] == ["harness"] * 3 + ["agent"]


def test_run_refuses_the_holdout_without_an_explicit_unlock_and_still_records(tmp_path):
    led = tmp_path / "ledger.jsonl"
    r = run_py("--split", "holdout", ledger=led)
    assert r.returncode == 2 and "not served until it is explicitly unlocked" in r.stderr
    rows = ledger_lines(led)
    assert len(rows) == 1 and rows[0]["outcome"] == "refused"


def test_the_holdout_opens_only_with_a_reason_that_reaches_the_record(tmp_path):
    led = tmp_path / "ledger.jsonl"
    r = run_py("--split", "holdout", "--unlock-holdout", "mentor sign-off",
               "--quiet", ledger=led)
    assert r.returncode == 0, r.stderr
    row = ledger_lines(led)[0]
    assert row["split"] == "holdout"
    assert row["holdout_unlock_reason"] == "mentor sign-off"


@pytest.mark.parametrize(
    "flag", [("--split", "holdout"), ("--fill-model", "favorable"),
             ("--seed", "42"), ("--profile", "harness")]
)
def test_an_agent_triggered_run_cannot_choose_its_own_conditions(flag, tmp_path):
    led = tmp_path / "ledger.jsonl"
    r = run_py("--trigger", "agent", *flag, ledger=led)
    assert r.returncode != 0
    assert "fixed configuration" in r.stderr
    assert not led.exists() or ledger_lines(led) == []


def test_a_crashing_strategy_still_lands_in_the_ledger(tmp_path):
    led = tmp_path / "ledger.jsonl"
    broken = REPO_ROOT / "src" / "strategies" / "current.py"
    original = broken.read_text()
    broken.write_text(original + '''

class Strategy(MomentumStrategy):
    def on_bars(self, ctx):
        raise RuntimeError("deliberate")
''')
    try:
        r = run_py("--quiet", ledger=led)
        assert r.returncode != 0
        rows = ledger_lines(led)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "crashed"
        assert "deliberate" in (rows[0]["error"] or "")
    finally:
        broken.write_text(original)


def test_the_generated_api_reference_is_current():
    r = subprocess.run(
        [sys.executable, "scripts/gen_context_api.py", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
