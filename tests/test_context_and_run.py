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

from src.core.engine.context import ContextError, ContextExpired, parse_interval  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
import run  # noqa: E402
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
    eng = Engine.for_test(
        SyntheticDataset(SyntheticSpec(**case["spec"])), MomentumStrategy(),
        split=Split.VALIDATION, warmup_sessions=1,
        fill_model=case["fill_model"], seed=case["spec"]["seed"],
    )
    eng.run()
    assert eng.blotter.digest() == case["blotter_digest"]
    assert len(eng.blotter.rows) == case["rows"]
    assert eng.n_fills == case["fills"] and eng.n_bars == case["bars"]
    assert eng.blotter.final_position == case["final_position"]
    # Task 21 moved the score's input from the accumulator to the fill log. The
    # golden digests are the assertion that it moved *where the number comes
    # from* and not the number: the reconstruction has to land on the same
    # digest, not merely a close one.
    assert eng.reconstructed().digest() == case["blotter_digest"]


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

    eng = Engine.for_test(ds(), Watcher(), split=Split.VALIDATION)
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

    eng = Engine.for_test(ds(), Two(), split=Split.VALIDATION)
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

    eng = Engine.for_test(ds(), Probe(), split=Split.VALIDATION)
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
    eng = Engine.for_test(ds(), strat, split=Split.VALIDATION, action_cap=4)
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
    eng = Engine.for_test(ds(), strat, split=Split.VALIDATION, action_cap=3)
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
    eng = Engine.for_test(ds(), strat, split=Split.VALIDATION, warmup_sessions=2)
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
    Engine.for_test(ds(), strat, split=Split.VALIDATION).run()
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
    Engine.for_test(ds(), strat, split=Split.VALIDATION).run()
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
        Engine.for_test(ds(), Bad(), split=Split.VALIDATION).run()


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
            # The context no longer holds the engine (Task 20), so the test
            # asks the way a strategy would: the order is gone from the working
            # set once it has filled.
            if ctx.position and self.oid not in ctx.working_order_ids:
                with pytest.raises(ContextError, match="already"):
                    ctx.cancel(self.oid)
                with pytest.raises(ContextError, match="cannot modify"):
                    ctx.modify(self.oid, limit=1.0)
                self.checked = True

    strat = Late()
    Engine.for_test(ds(), strat, split=Split.VALIDATION).run()
    assert getattr(strat, "checked", False)


def test_close_with_no_position_says_so():
    _raises_on(lambda c: c.close(), "no position open")


def test_an_unknown_subscription_names_the_ones_that_exist():
    """A ContextError, not a KeyError: every way of naming a subscription now
    routes through one resolver, so there is one exception type for one mistake.
    It used to depend on which entry point you happened to use -- `ctx.history`
    raised a bare KeyError while `"15m" in ctx.bars` was silently False."""
    class Wrong(Base):
        def on_bars(self, ctx):
            ctx.history("15m")

    with pytest.raises(ContextError, match="no subscription"):
        Engine.for_test(ds(), Wrong(), split=Split.VALIDATION).run()


def test_setup_must_declare_a_subscription():
    class Silent(Base):
        def setup(self, ctx):
            pass

    with pytest.raises(ValueError, match="ctx.subscribe"):
        Engine.for_test(ds(), Silent(), split=Split.VALIDATION).run()


def test_returning_subscriptions_from_setup_is_an_error_not_a_silent_no_op():
    class OldShape(Base):
        def setup(self, ctx):
            return [ctx.subscribe("1m")]

    with pytest.raises(TypeError, match="setup\\(\\) returns nothing now"):
        Engine.for_test(ds(), OldShape(), split=Split.VALIDATION).run()


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

    Engine.for_test(ds(), Peek(), split=Split.VALIDATION).run()
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
        probe.write_text(f"from src.core.scoring import {module}  # noqa: F401\n")
        try:
            r = subprocess.run(
                [sys.executable, "-c", f"import src.strategies._probe_{module}"],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            assert r.returncode != 0, f"importing {module} from a strategy succeeded"
            assert "ImportError" in r.stderr
            assert any(
                f"may not import src.core.scoring.{m}" in r.stderr for m in ("scorer", "metrics")
            ), r.stderr[-400:]
        finally:
            probe.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# run.py
# --------------------------------------------------------------------------- #
def run_py(*args, ledger: Path):
    """`--human` because a test is a person running the harness by hand.

    Since Task 20 `--ledger`, `--purpose` and `--unlock-holdout` are accepted
    only in that mode: every automated caller takes its configuration from the
    committed profile, so there is nothing to override and nowhere else to write.
    """
    return subprocess.run(
        [sys.executable, "run.py", "--human", "--ledger", str(ledger), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def agent_check(*args, ledger: Path):
    return subprocess.run(
        [sys.executable, "scripts/agent_check.py", "--ledger", str(ledger), *args],
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
    assert row["trigger"] == "human" and row["profile"] == "harness"
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
    agent_check(ledger=led)
    rows = ledger_lines(led)
    assert len(rows) == 4, "N is only honest if every run lands in the ledger"
    assert [r["trigger"] for r in rows] == ["human"] * 3 + ["agent"]


def test_run_refuses_the_holdout_without_an_explicit_unlock_and_still_records(tmp_path):
    led = tmp_path / "ledger.jsonl"
    # A profile, not a flag: the split is not on the command line any more, so
    # aiming a run at the holdout is an edit to a committed file.
    r = run_py("--profile", "holdout", ledger=led)
    assert r.returncode == 2 and "not served until it is explicitly unlocked" in r.stderr
    rows = ledger_lines(led)
    assert len(rows) == 1 and rows[0]["outcome"] == "refused"


def test_the_holdout_opens_only_with_a_reason_that_reaches_the_record(tmp_path):
    led = tmp_path / "ledger.jsonl"
    r = run_py("--profile", "holdout", "--unlock-holdout", "mentor sign-off",
               "--quiet", ledger=led)
    assert r.returncode == 0, r.stderr
    row = ledger_lines(led)[0]
    assert row["split"] == "holdout"
    assert row["holdout_unlock_reason"] == "mentor sign-off"


def test_the_evaluation_conditions_are_not_on_the_command_line_at_all():
    """The review's finding, closed structurally rather than by a longer
    forbidden-list.

    `--dataset` and `--ledger` were missing from AGENT_FORBIDDEN and the
    supplied-argument check tested truthiness, so `--seed 0` read as "not
    supplied". A strategy could evaluate itself against the planted-signal
    dataset, retry on seeds and write the results somewhere that never counts --
    not a loophole in honest N, a deletion of it.

    Adding names to the list is a race against whoever adds the next flag. These
    arguments do not exist now: every evaluation condition comes from the
    committed profile.
    """
    import argparse

    # The options section, not the prose: the docstring explains what used to be
    # here and why it is not, which is worth keeping and would otherwise fail
    # this check.
    help_text = subprocess.run(
        [sys.executable, "run.py", "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout
    options = help_text[help_text.index("options:"):]
    for gone in ("--dataset", "--split", "--seed", "--fill-model", "--trigger"):
        assert gone not in options, f"{gone} is still a run.py argument"
    del argparse


@pytest.mark.parametrize("flag", ["--purpose", "--ledger", "--unlock-holdout"])
def test_the_hand_run_only_flags_are_refused_without_human(flag, tmp_path):
    r = subprocess.run(
        [sys.executable, "run.py", flag, "smoke" if flag == "--purpose" else "x"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode != 0
    assert "running the harness by hand" in r.stderr


def test_the_agent_entry_point_reports_no_number(tmp_path):
    """The agent's only entry point. A check that yields no score cannot be used
    for selection: nothing to optimise against, nothing to retry for, nothing to
    forge."""
    led = tmp_path / "ledger.jsonl"
    r = agent_check(ledger=led)
    assert r.returncode == 0, r.stderr
    for forbidden in ("sortino", "sharpe", "significance", "far", "pnl", "net"):
        assert forbidden not in r.stdout.lower(), r.stdout

    row = ledger_lines(led)[0]
    assert row["purpose"] == "check" and row["trigger"] == "agent"
    assert row["score"] is None and row["diagnostics"] is None
    assert row["outcome"].startswith("check_")


def test_the_agent_entry_point_cannot_reach_the_scored_path():
    """It takes one optional argument, for tests, and nothing that selects a
    dataset, a split, a seed or a fill model."""
    help_text = subprocess.run(
        [sys.executable, "scripts/agent_check.py", "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout
    options = help_text[help_text.index("options:"):]
    for gone in ("--dataset", "--split", "--seed", "--fill-model", "--purpose",
                 "--profile", "--trigger", "--unlock-holdout"):
        assert gone not in options, f"{gone} is reachable from the agent's entry point"



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


# --------------------------------------------------------------------------- #
# purpose: classify a run rather than delete it afterwards
# --------------------------------------------------------------------------- #
def test_every_row_carries_a_purpose_and_it_defaults_to_trial(tmp_path):
    """A hand-run harness invocation defaults to `smoke`; an automated one is a
    trial by construction and cannot say otherwise."""
    led = tmp_path / "ledger.jsonl"
    assert run_py("--quiet", ledger=led).returncode == 0
    assert ledger_lines(led)[0]["purpose"] == "smoke"
    assert run.resolve_purpose("loop", None) == "trial"


def test_purpose_is_derived_from_the_trigger_not_accepted_from_it():
    """N is the count of `trial` rows, so a caller that can choose its own label
    can choose not to be counted. Only a human running the harness by hand can be
    telling the truth about a run not being a candidate, so only a human may
    say."""
    for trigger in ("harness", "loop"):
        assert run.resolve_purpose(trigger, None) == "trial"
        with pytest.raises(SystemExit, match="always a"):
            run.resolve_purpose(trigger, "smoke")
    # the agent's entry point is a check, and it cannot call itself a trial
    assert run.resolve_purpose("agent", None) == "check"
    with pytest.raises(SystemExit, match="always a"):
        run.resolve_purpose("agent", "trial")
    # and the default for a hand-run harness is the safe direction: a smoke run
    # unless the person says otherwise, because a human claiming a trial can say
    # so and a script quietly claiming a smoke run cannot be caught.
    assert run.resolve_purpose("human", None) == "smoke"
    assert run.resolve_purpose("human", "trial") == "trial"


def test_a_non_trial_run_is_labelled_not_removed(tmp_path):
    """The reason this field exists. A smoke run is still an invocation and still
    lands in the ledger; what changes is that it is classified, so nobody has to
    decide later whether to delete it. A record edited by the party being
    measured against it is not a record, and the first deletion is always the
    defensible one."""
    led = tmp_path / "ledger.jsonl"
    assert run_py("--quiet", "--purpose", "smoke", ledger=led).returncode == 0
    assert run_py("--quiet", "--purpose", "trial", ledger=led).returncode == 0
    assert agent_check(ledger=led).returncode == 0
    rows = ledger_lines(led)
    assert [r["purpose"] for r in rows] == ["smoke", "trial", "check"]
    assert len(rows) == 3, "a classified run is still a row"
    # N is a filter over the ledger, never a shorter ledger
    assert sum(1 for r in rows if r["purpose"] == "trial") == 1


def test_the_agent_cannot_relabel_what_it_ran():
    """Otherwise N is decided by the party with the incentive. The agent's runs
    are checks by construction and it has no flag to say otherwise."""
    with pytest.raises(SystemExit, match="always a"):
        run.resolve_purpose("agent", "trial")
    with pytest.raises(SystemExit, match="always a"):
        run.resolve_purpose("agent", "smoke")





def test_the_ledger_stores_the_source_of_whatever_actually_ran(tmp_path):
    """The first real run produced a row naming the calibration reference in
    `strategy` while digesting and storing `src/strategies/current.py` -- so
    replaying it would have replayed code that never ran. The harness runs
    immutable apparatus as well as the agent's file, and the row has to say
    which."""
    other = REPO_ROOT / "src" / "core" / "scoring" / "calibration" / "reference.py"
    led = tmp_path / "ledger.jsonl"

    row = run._row({}, "harness", "nq-smoke", led, purpose="smoke",
                                strategy_path=other)
    assert row["strategy"] == "src/core/scoring/calibration/reference.py"
    assert row["strategy_digest"] == run.strategy_digest(other)
    stored = (led.parent / "sources" / f"{row['strategy_digest']}.py").read_text()
    assert stored == other.read_text()

    default = run._row({}, "harness", "harness", led)
    assert default["strategy"] == "src/strategies/current.py"
    assert default["strategy_digest"] != row["strategy_digest"]


# --------------------------------------------------------------------------- #
# the irregular generator's own golden set
# --------------------------------------------------------------------------- #
IRREGULAR_GOLDEN = json.loads(
    (REPO_ROOT / "tests" / "golden" / "irregular_generator.json").read_text()
)


@pytest.mark.parametrize(
    "case", IRREGULAR_GOLDEN["cases"],
    ids=lambda c: f"{c['fill_model']}-s{c['spec']['signal_strength']}-seed{c['spec']['seed']}",
)
def test_the_irregular_generator_has_its_own_behaviour_reference(case):
    """The set above pins `uniform=True` and is the long-lived reference carried
    since Task 5. This one covers the path that actually finds things: Task 17
    surfaced four defects on the irregular generator and none on the uniform one,
    so leaving it without a golden set left the more productive half unprotected.

    A digest moving here means behaviour moved. Name which cases and why before
    regenerating.
    """
    engine = Engine.for_test(
        SyntheticDataset(SyntheticSpec(**case["spec"])), MomentumStrategy(),
        split=Split.VALIDATION, warmup_sessions=1,
        fill_model=case["fill_model"], seed=case["spec"]["seed"],
    )
    record = engine.run()
    assert engine.blotter.digest() == case["blotter_digest"]
    assert engine.reconstructed().digest() == case["blotter_digest"]
    assert len(engine.blotter.rows) == case["rows"]
    assert engine.n_fills == case["fills"] and engine.n_bars == case["bars"]
    assert engine.blotter.final_position == case["final_position"]
    left_open = sum(1 for v in record.end_of_session_positions.values() if v)
    assert left_open == case["sessions_left_open"]


def test_the_two_golden_sets_cover_different_worlds():
    """A guard on the guards: if the irregular set ever ends up pinning
    `uniform=True` it is a duplicate of the other one and protects nothing."""
    for case in IRREGULAR_GOLDEN["cases"]:
        assert not case["spec"].get("uniform"), "the irregular set went uniform"
    for case in GOLDEN["cases"]:
        assert case["spec"].get("uniform") is True, "the uniform set went irregular"
    # and the naive strategy carries positions overnight on irregular data, which
    # the uniform world could never have shown
    assert any(c["sessions_left_open"] > 0 for c in IRREGULAR_GOLDEN["cases"])


# --------------------------------------------------------------------------- #
# adversarial review, second pass, finding 7: the fingerprint covered the
# wrong set
# --------------------------------------------------------------------------- #
def test_the_scoring_fingerprint_does_not_see_a_rewritten_null_table():
    """The finding, kept as an assertion rather than a memory.

    `integrity.fingerprint()` hashes scoring *modules*. A null table is a file,
    so moving a threshold moves every accept/reject decision in the harness and
    the fingerprint reports everything as unchanged. This test pins the gap so
    that nobody later reads `scoring_fingerprint` as covering more than it does.
    """
    from src.core.scoring import integrity

    table = REPO_ROOT / "data" / "calibration" / "empirical-null.json"
    original = table.read_bytes()
    before_code = integrity.fingerprint()
    before_files = integrity.artifact_digests()
    try:
        table.write_bytes(original + b"\n")
        assert integrity.fingerprint() == before_code, (
            "the scoring fingerprint moved; this test no longer pins the gap it "
            "was written for"
        )
        # and the artifact fingerprint is the thing that does see it
        after_files = integrity.artifact_digests()
        assert after_files != before_files
        with pytest.raises(integrity.ArtifactsMutated, match="empirical-null.json"):
            integrity.check_artifacts(before_files, after_files)
    finally:
        table.write_bytes(original)
    assert table.read_bytes() == original
    assert integrity.artifact_digests() == before_files


def test_a_mid_run_write_to_a_calibration_table_invalidates_the_trial(tmp_path):
    """The demonstration, end to end through `run.py`.

    The strategy rewrites a null table from `setup()` -- after the baseline was
    taken at import, which is the window that matters -- and the run has to come
    back invalidated rather than scored. The table is restored whatever happens,
    and the test asserts the restoration rather than trusting the `finally`.
    """
    led = tmp_path / "ledger.jsonl"
    table = REPO_ROOT / "data" / "calibration" / "empirical-null.json"
    strategy = REPO_ROOT / "src" / "strategies" / "current.py"
    original_table = table.read_bytes()
    original_strategy = strategy.read_text()
    strategy.write_text(original_strategy + '''

class Strategy(MomentumStrategy):
    def setup(self, ctx):
        import pathlib
        p = (pathlib.Path(__file__).resolve().parents[2]
             / "data" / "calibration" / "empirical-null.json")
        p.write_bytes(p.read_bytes() + b"\\n")
        return super().setup(ctx)
''')
    try:
        r = run_py("--quiet", ledger=led)
        assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
        rows = ledger_lines(led)
        assert len(rows) == 1, "an invalidated trial is still a row"
        row = rows[0]
        assert row["outcome"] == "invalidated_artifacts_mutated"
        assert "empirical-null.json" in row["error"]
        assert row["score"] is None
        # both numbers, so the row says what changed and not only that something did
        assert row["artifacts"]["data/calibration/empirical-null.json"] != ""
    finally:
        table.write_bytes(original_table)
        strategy.write_text(original_strategy)
    assert table.read_bytes() == original_table


def test_every_row_states_the_artifact_versions_that_produced_it(tmp_path):
    """The point of recording the set rather than only the hash. A row whose
    fingerprint differs from today's is useless if it cannot say *which* file
    moved."""
    from src.core.scoring import integrity

    led = tmp_path / "ledger.jsonl"
    assert run_py("--quiet", ledger=led).returncode == 0
    row = ledger_lines(led)[0]

    assert row["artifacts"] == integrity.artifact_digests()
    assert row["artifact_fingerprint"] == integrity.artifact_fingerprint()
    assert row["code_fingerprint"] == integrity.code_fingerprint()
    for expected in ("config/run.yaml", "config/calendars/nq-trades.yaml",
                     "data/calibration/empirical-null.json", "data/manifest.json",
                     "src/strategies/current.py"):
        assert expected in row["artifacts"], expected


def test_the_artifact_path_list_is_not_something_a_caller_supplies():
    """`ARTIFACT_GLOBS` is a module constant inside the scoring package, which a
    strategy cannot import. A set assembled by the caller is a set the caller can
    shorten, and the caller closest to the incentive is the one running the
    strategy."""
    import inspect

    from src.core.scoring import integrity

    src = inspect.getsource(integrity.artifact_digests)
    assert "ARTIFACT_GLOBS" in src
    assert "root" in inspect.signature(integrity.artifact_digests).parameters
    # `root` exists for tests; there is no parameter that changes *which* globs
    assert list(inspect.signature(integrity.artifact_digests).parameters) == ["root"]
