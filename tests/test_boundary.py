"""Nothing mutable crosses the context boundary, and nothing resolves silently.

Three general tests, each aimed at a class of defect rather than at the one
instance of it that was found:

1. **Immutability.** `ctx.working_orders` handed back the engine's own `Order`
   objects, and `order.size = 99` worked -- skipping the intent queue, the action
   cap and the T+1 rule, and reaching the run record nowhere. The specific fix is
   `OrderView`. The general fix is this test, which walks *every* public
   attribute and method return of *every* context class by introspection, so the
   next accessor somebody adds is covered without anyone remembering to.

2. **Subscription ids.** An id that was never subscribed used to read as absent
   -- `"5m" in ctx.bars` was `False` because ids are canonical -- and a strategy
   that silently never trades burns trials saying nothing. Every id-accepting
   entry point now goes through one resolver; this asserts each of them raises,
   and accepts both the alias and the canonical form.

3. **The protocol.** `types.Strategy` declared the pre-Task-5 shape for four
   tasks because nothing imported it. This asserts the methods it declares are
   exactly the ones the engine calls, by reading the engine's source.
"""

from __future__ import annotations

import ast
import datetime as dt
import enum
import inspect
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine import context as ctxmod  # noqa: E402
from src.core.engine.context import ContextError  # noqa: E402
from src.core.engine.view import BarHistory, HistoryView  # noqa: E402
from src.core.types import (  # noqa: E402
    OrderType,
    Side,
    Split,
    Strategy,
    TickStrategy,
)

INTERVAL = "1m"
CANONICAL = "time:60000000000"


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=20, ticks_per_session=120, vol_per_tick=0.15, seed=1,
                train_frac=0.5, validation_frac=0.2)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


# --------------------------------------------------------------------------- #
# 1. immutability
# --------------------------------------------------------------------------- #
# Read-only view objects that cannot be a tuple because they carry behaviour a
# tuple cannot: HistoryView must raise LookaheadError on a forward read, and the
# closed-bar mapping must raise on an unsubscribed id. They are audited
# separately, below, rather than waved through.
READ_ONLY_VIEWS = (HistoryView, ctxmod._ClosedBars)

IMMUTABLE_SCALARS = (
    str, int, float, bool, complex, bytes, type(None),
    dt.datetime, dt.date, dt.time, dt.timedelta,
)

# Methods that exist to *do* something rather than to return state. Calling them
# in a probe would place orders, so they are excluded here and their return type
# is pinned separately by `test_order_methods_return_an_id_or_none`.
ACTION_METHODS = {"buy", "sell", "close", "modify", "cancel", "cancel_all"}


def is_immutable(value, seen=None) -> tuple[bool, str]:
    """(verdict, why-not). Recurses into tuples and frozen dataclasses."""
    seen = seen or set()
    if id(value) in seen:
        return True, ""
    seen = seen | {id(value)}

    if isinstance(value, IMMUTABLE_SCALARS) or isinstance(value, enum.Enum):
        return True, ""
    if isinstance(value, (frozenset, range)):
        return True, ""
    if isinstance(value, tuple):
        for item in value:
            ok, why = is_immutable(item, seen)
            if not ok:
                return False, f"tuple contains {why}"
        return True, ""
    if is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not params.frozen:
            return False, f"{type(value).__name__} is a mutable dataclass"
        for f in fields(value):
            ok, why = is_immutable(getattr(value, f.name), seen)
            if not ok:
                return False, f"{type(value).__name__}.{f.name} is {why}"
        return True, ""
    if isinstance(value, READ_ONLY_VIEWS):
        return True, ""
    return False, f"a {type(value).__name__}"


class _Probe:
    """Reads every public accessor of every context it is handed, and records
    what came back. Deliberately introspective: a new accessor is covered the
    moment it exists, which is the whole point of a general test."""

    def __init__(self):
        self.seen: list[tuple[str, str, object]] = []
        self.skipped: set[str] = set()
        self.placed = None

    def setup(self, ctx):
        ctx.subscribe(INTERVAL)
        self._read(ctx)

    def _read(self, ctx):
        cls = type(ctx).__name__
        for name in dir(ctx):
            if name.startswith("_"):
                continue
            attr = getattr(type(ctx), name, None)
            if isinstance(attr, property):
                self.seen.append((cls, name, getattr(ctx, name)))
                continue
            member = getattr(ctx, name)
            if not callable(member):
                self.seen.append((cls, name, member))
                continue
            if name in ACTION_METHODS:
                self.skipped.add(name)
                continue
            sig = inspect.signature(member)
            required = [
                p for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            if not required:
                self.seen.append((cls, name, member()))
            elif len(required) == 1:
                # every single-argument reader on a context takes a subscription
                self.seen.append((cls, name, member(INTERVAL)))
            else:
                self.skipped.add(name)

    def on_bars(self, ctx):
        self._read(ctx)
        if ctx.actionable and self.placed is None and not ctx.position:
            self.placed = ctx.buy(1, stop_offset=1.0, target_offset=1.0, tag="probe")

    def on_tick(self, ctx):
        self._read(ctx)

    def on_fill(self, ctx):
        self._read(ctx)

    def on_lifecycle(self, ctx):
        self._read(ctx)


def _probe_run() -> _Probe:
    probe = _Probe()
    Engine(ds(), probe, split=Split.VALIDATION, warmup_sessions=1).run()
    return probe


PROBE = _probe_run()


def test_the_probe_actually_read_something():
    """A guard on the guard. Every assertion below passes vacuously if the
    introspection stopped finding accessors."""
    names = {n for _, n, _ in PROBE.seen}
    classes = {c for c, _, _ in PROBE.seen}
    assert len(names) >= 15, sorted(names)
    assert classes >= {"SetupContext", "BarContext", "FillContext",
                       "LifecycleContext"}, sorted(classes)
    for expected in ("working_orders", "history", "bars", "fills", "closed"):
        assert expected in names, sorted(names)
    assert PROBE.placed is not None, "the probe never placed an order"


def test_everything_a_context_returns_is_immutable():
    bad = []
    for cls, name, value in PROBE.seen:
        ok, why = is_immutable(value)
        if not ok:
            bad.append(f"{cls}.{name} returned {why}")
    assert not bad, (
        "mutable state crossed the context boundary:\n  "
        + "\n  ".join(sorted(set(bad)))
        + "\n\nReturn a frozen dataclass, a tuple, or a primitive. A frozen "
        "dataclass holding a list is still mutable through that list."
    )


def test_the_probe_skipped_only_the_action_methods():
    """If introspection could not call something, that has to be a deliberate
    exclusion rather than a gap nobody noticed."""
    assert PROBE.skipped <= ACTION_METHODS, sorted(PROBE.skipped - ACTION_METHODS)


def test_order_methods_return_an_id_or_none():
    """The accessors excluded above, pinned by hand because calling them has
    effects."""
    seen = {}

    class S:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

        def on_bars(self, ctx):
            if not ctx.actionable or ctx.position or ctx.working_order_ids:
                return
            seen["buy"] = ctx.buy(1, stop_offset=1.0, target_offset=1.0)
            seen["cancel_all"] = ctx.cancel_all()

        def on_fill(self, ctx):
            if ctx.position:
                seen["close"] = ctx.close()

        def on_lifecycle(self, ctx):
            pass

    Engine(ds(), S(), split=Split.VALIDATION).run()
    assert isinstance(seen["buy"], int)
    assert seen["cancel_all"] is None
    assert isinstance(seen.get("close"), (int, type(None)))


@pytest.mark.parametrize("view", READ_ONLY_VIEWS, ids=lambda v: v.__name__)
def test_the_read_only_views_expose_no_mutator(view):
    """They are allowed through `is_immutable` on the grounds that their public
    surface is read-only. That has to be checked, not assumed."""
    mutators = {"append", "extend", "insert", "remove", "pop", "clear", "sort",
                "reverse", "add", "update", "setdefault", "__setitem__",
                "__delitem__", "__iadd__"}
    public = {n for n in dir(view) if not n.startswith("_")} | {
        n for n in dir(view) if n in ("__setitem__", "__delitem__", "__iadd__")
    }
    assert not (public & mutators), sorted(public & mutators)
    # and no public attribute that is itself a mutable container
    assert all(
        not isinstance(getattr(view, n, None), (list, dict, set, bytearray))
        for n in public
    )


def test_a_strategy_cannot_change_an_order_through_what_it_is_given():
    """The specific defect, kept as its own test so the reason the general one
    exists stays legible."""
    outcome = {}

    class Mutator:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

        def on_bars(self, ctx):
            if ctx.working_orders:
                view = ctx.working_orders[0]
                with pytest.raises(Exception):     # FrozenInstanceError
                    view.size = 99
                outcome["size_after"] = ctx.working_orders[0].size
                return
            if ctx.actionable and not ctx.position:
                ctx.buy(1, type="limit", limit=ctx.last_price - 5.0, tif="GTC",
                        tag="resting")

        def on_fill(self, ctx):
            pass

        def on_lifecycle(self, ctx):
            pass

    Engine(ds(), Mutator(), split=Split.VALIDATION).run()
    assert outcome.get("size_after") == 1, outcome


# --------------------------------------------------------------------------- #
# 2. subscription ids
# --------------------------------------------------------------------------- #
# Everything on a context that takes a subscription. Introspected rather than
# listed, so a new one is covered.
def id_accepting_entry_points(ctx) -> dict[str, object]:
    return {
        "history": lambda sub: ctx.history(sub),
        "sub_id": lambda sub: ctx.sub_id(sub),
        "bars.__getitem__": lambda sub: ctx.bars[sub],
        "bars.__contains__": lambda sub: sub in ctx.bars,
    }


def _collect(fn):
    """Run one bar callback and hand it `fn(ctx)`; return what fn recorded."""
    out = {}

    class S:
        done = False

        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

        def on_bars(self, ctx):
            if self.done:
                return
            self.done = True
            fn(ctx, out)

        def on_fill(self, ctx):
            pass

        def on_lifecycle(self, ctx):
            pass

    Engine(ds(), S(), split=Split.VALIDATION).run()
    return out


ENTRY_POINT_NAMES = ["history", "sub_id", "bars.__getitem__", "bars.__contains__"]


@pytest.mark.parametrize("entry", ENTRY_POINT_NAMES)
def test_an_unsubscribed_id_raises_from_every_entry_point(entry):
    def probe(ctx, out):
        try:
            id_accepting_entry_points(ctx)[entry]("15m")
            out["result"] = "no error"
        except Exception as exc:
            out["result"] = (type(exc).__name__, str(exc))

    out = _collect(probe)
    kind, message = out["result"]
    assert kind == "ContextError", out["result"]
    assert "no subscription" in message
    assert CANONICAL in message, "the message must name the ids that do exist"


@pytest.mark.parametrize("entry", ENTRY_POINT_NAMES)
@pytest.mark.parametrize("spelling", [INTERVAL, CANONICAL, "60s"])
def test_a_subscribed_id_is_accepted_by_alias_and_canonical_form(entry, spelling):
    """`"1m"`, `"60s"` and `"time:60000000000"` are the same subscription. The
    strategy should not have to know which spelling the engine prefers."""
    def probe(ctx, out):
        out["value"] = id_accepting_entry_points(ctx)[entry](spelling)

    out = _collect(probe)
    assert "value" in out


def test_the_entry_point_list_covers_every_id_accepting_accessor():
    """Guard: if a new accessor takes a subscription, the parametrization above
    has to grow with it."""
    found = set()
    for name in dir(ctxmod.BaseContext):
        if name.startswith("_"):
            continue
        member = getattr(ctxmod.BaseContext, name)
        target = member.fget if isinstance(member, property) else member
        if not callable(target):
            continue
        params = [
            p.name for p in inspect.signature(target).parameters.values()
            if p.name != "self"
        ]
        if params and params[0] in ("sub", "interval"):
            found.add(name)
    assert found <= {"history", "sub_id"}, (
        f"new subscription-taking accessors {sorted(found)}; add them to "
        "ENTRY_POINT_NAMES"
    )


def test_closed_bars_means_closed_in_this_callback():
    """`ctx.bars` used to mean "the most recent closed bar", which made the
    obvious membership test true whenever any bar had ever closed."""
    seen = {}

    class S:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)
            ctx.subscribe("5m")

        def on_bars(self, ctx):
            seen.setdefault("in_bars", set()).add(
                (INTERVAL in ctx.bars, "5m" in ctx.bars)
            )

        def on_fill(self, ctx):
            seen["fill_bars_empty"] = len(ctx.bars) == 0

        def on_lifecycle(self, ctx):
            seen.setdefault("lifecycle_empty", set()).add(len(ctx.bars) == 0)

    class T(S):
        def on_bars(self, ctx):
            S.on_bars(self, ctx)
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, stop_offset=1.0, target_offset=1.0)

    Engine(ds(), T(), split=Split.VALIDATION).run()
    # a 1m-only close happens far more often than a simultaneous 1m+5m close
    assert (True, False) in seen["in_bars"], seen["in_bars"]
    assert (True, True) in seen["in_bars"], seen["in_bars"]
    assert seen.get("fill_bars_empty") is True
    assert seen["lifecycle_empty"] == {True}


def test_no_context_method_raises_a_non_context_error():
    """Fix 7: one mistake, one exception type. `ctx.history()` used to raise a
    bare KeyError while every other misuse raised ContextError."""
    for bad_id in ("15m", "tick", "time:1"):
        def probe(ctx, out, bad=bad_id):
            try:
                ctx.history(bad)
                out[bad] = None
            except Exception as exc:
                out[bad] = type(exc)

        out = _collect(probe)
        assert out[bad_id] is ContextError, (bad_id, out[bad_id])


# --------------------------------------------------------------------------- #
# 3. the protocol is the contract
# --------------------------------------------------------------------------- #
def callbacks_the_engine_calls() -> set[str]:
    """Every attribute reached on `self.strategy` in the engine, read from the
    source. This is what would have caught four tasks of protocol drift."""
    src = (REPO_ROOT / "src" / "core" / "engine" / "engine.py").read_text()
    tree = ast.parse(src)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "strategy"
            and isinstance(base.value, ast.Name)
            and base.value.id == "self"
        ):
            found.add(node.attr)
    return found


def protocol_callbacks() -> set[str]:
    out = set()
    for proto in (Strategy, TickStrategy):
        out |= {
            n for n, v in vars(proto).items()
            if not n.startswith("_") and callable(v)
        }
    return out


def test_the_protocol_declares_exactly_what_the_engine_calls():
    called = callbacks_the_engine_calls()
    declared = protocol_callbacks()
    assert called, "the AST scan found nothing; the engine was restructured"
    assert called == declared, (
        f"the engine calls {sorted(called)} but types.Strategy/TickStrategy "
        f"declare {sorted(declared)}. The protocol is the only statement of the "
        "contract in the code; a stale one is worse than none."
    )


def test_every_declared_callback_takes_exactly_the_context():
    for proto in (Strategy, TickStrategy):
        for name, fn in vars(proto).items():
            if name.startswith("_") or not callable(fn):
                continue
            params = [p for p in inspect.signature(fn).parameters if p != "self"]
            assert params == ["ctx"], f"{proto.__name__}.{name}{params}"


@pytest.mark.parametrize(
    "missing", ["setup", "on_bars", "on_fill", "on_lifecycle"]
)
def test_a_missing_callback_is_one_clear_wiring_error(missing):
    """All four fail the same way now. `on_tick` used to be the only one with a
    legible message; the others were an AttributeError at the first session
    open, which names nothing useful."""
    ns = {
        "setup": lambda self, ctx: ctx.subscribe(INTERVAL),
        "on_bars": lambda self, ctx: None,
        "on_fill": lambda self, ctx: None,
        "on_lifecycle": lambda self, ctx: None,
    }
    del ns[missing]
    Broken = type("Broken", (), ns)

    with pytest.raises(TypeError, match=f"{missing}\\(ctx\\) is missing"):
        Engine(ds(), Broken(), split=Split.VALIDATION).run()


def test_a_wrong_signature_is_the_same_wiring_error():
    class OldShape:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

        def on_bars(self, ev, ctx):        # the pre-Task-5 shape
            return []

        def on_fill(self, ctx):
            pass

        def on_lifecycle(self, ctx):
            pass

    with pytest.raises(TypeError, match="does not take exactly one argument"):
        Engine(ds(), OldShape(), split=Split.VALIDATION).run()


def test_the_wiring_error_lists_every_problem_at_once():
    """One error naming everything, not one per run: an agent fixing these one
    at a time spends a trial on each."""
    class Bad:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

    with pytest.raises(TypeError) as exc:
        Engine(ds(), Bad(), split=Split.VALIDATION).run()
    message = str(exc.value)
    for name in ("on_bars", "on_fill", "on_lifecycle"):
        assert f"{name}(ctx) is missing" in message


# --------------------------------------------------------------------------- #
# 3b. find_orders
# --------------------------------------------------------------------------- #
def test_find_orders_recovers_an_order_by_its_tag():
    """Tags reached the blotter but lived in an engine-private map, so a strategy
    could only recognise its own orders by inspecting types and prices, or by
    tracking ids by hand across lifecycle events -- which is where a strategy
    holding an id through a DAY expiry crashes."""
    seen = {}

    class S:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if ctx.position and "found" not in seen:
                legs = ctx.find_orders(parent_id=seen["entry_id"])
                stops = ctx.find_orders(tag=f"bracket-stop:{seen['entry_id']}")
                seen["found"] = (len(legs), len(stops))
                if stops:
                    seen["is_stop"] = stops[0].order_type is OrderType.STOP
                    seen["side"] = stops[0].side
                    seen["tagged"] = stops[0].tag
                    seen["matched_entry"] = seen["entry_id"]
                seen["by_type"] = len(ctx.find_orders(order_type="limit"))
                seen["nothing"] = ctx.find_orders(tag="does-not-exist")
                return
            if not ctx.position and not ctx.working_order_ids:
                seen["entry_id"] = ctx.buy(
                    1, stop_offset=1.0, target_offset=1.0, tag="entry"
                )

        def on_fill(self, ctx):
            pass

        def on_lifecycle(self, ctx):
            pass

    Engine(ds(), S(), split=Split.VALIDATION).run()
    assert seen.get("found") == (2, 1), seen
    assert seen["is_stop"] is True
    assert seen["side"] is Side.SELL
    assert seen["tagged"] == f"bracket-stop:{seen['matched_entry']}"
    assert seen["by_type"] == 1
    assert seen["nothing"] == ()


# --------------------------------------------------------------------------- #
# 4. warmup may not reach past the embargo
# --------------------------------------------------------------------------- #
def test_warmup_longer_than_the_embargo_is_refused():
    """Warmup reaches backward from the first session of the split; the embargo
    is the gap in front of it. Warmup within the embargo therefore reaches only
    into embargoed sessions. Exceed it and features are fitted on data the split
    was separated from -- leakage nothing else in the harness would catch."""
    import run

    dataset = SyntheticDataset(SyntheticSpec(n_sessions=60, embargo_sessions=2))
    assert dataset.calendar().embargo_sessions == 2

    run.check_warmup_against_embargo("harness", 2, dataset)   # at the limit, fine

    with pytest.raises(SystemExit) as exc:
        run.check_warmup_against_embargo("harness", 3, dataset)
    message = str(exc.value)
    assert "warmup_sessions=3" in message and "embargo is 2" in message


def test_the_shipped_profiles_satisfy_it():
    """The check is worth nothing if the configuration it guards already fails,
    and worth nothing either if it is inert -- so assert it is live here.

    Profiles on a generated dataset are built and checked directly. A profile on
    a real adapter needs bucket credentials to construct, which CI does not have,
    so its calendar is loaded from the committed pre-registration and the
    published session list instead -- the same file the adapter would read, and
    the same arithmetic, without the network.
    """
    import json

    import run
    from src.core.data.dataset import load_calendar

    cfg = run.load_config()
    checked = 0
    for name, profile in cfg["profiles"].items():
        warmup = profile.get("warmup_sessions", 0)
        spec = cfg["datasets"][profile["dataset"]]
        if spec["kind"] == "synthetic":
            dataset = run.build_dataset(cfg, profile["dataset"], profile["seed"])
            run.check_warmup_against_embargo(name, warmup, dataset)
            embargo = dataset.calendar().embargo_sessions
        else:
            manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text())
            dates = [
                dt.date.fromisoformat(d)
                for d in manifest["instrument_coverage"][spec["symbol"]]["sessions"]["dates"]
            ]
            calendar = load_calendar(
                REPO_ROOT / "config" / "calendars" / f"{spec['kind']}-trades.yaml",
                dates,
            )
            embargo = calendar.embargo_sessions
            assert warmup <= embargo, (
                f"profile {name}: warmup_sessions={warmup} exceeds the "
                f"pre-registered embargo of {embargo}"
            )
        assert embargo > 0, (
            f"profile {name} runs on a dataset with no embargo, so the check "
            "above proved nothing"
        )
        checked += 1
    assert checked == len(cfg["profiles"]) >= 3, checked


def test_a_dataset_with_no_pre_registered_calendar_stands_the_check_down():
    """The NQ adapter has no calendar until `config/calendars/` is written by a
    human. There is no embargo to check against, and inventing one would be
    worse than not checking."""
    import run

    class NoCalendar:
        def calendar(self):
            raise NotImplementedError("config/calendars/nq-trades.yaml is not written")

    run.check_warmup_against_embargo("harness", 999, NoCalendar())


# --------------------------------------------------------------------------- #
# adversarial review, Task 20: reachability, not visibility
# --------------------------------------------------------------------------- #
# The immutability walk above skips underscore-prefixed names. That was the Task
# 13 specification -- "every public attribute and method return" -- and it was
# flagged and accepted at the time. Adversarial review showed what it cost:
# `ctx._engine` and `ctx._dataset` held the live objects, so a validation
# strategy read the split and the seed, called `unlock_holdout`, pulled a holdout
# tick and mutated engine state, through two attributes the test had been told
# not to look at.
#
# This walks everything: private names, the `__self__` of bound methods, and
# `__closure__` cells. Hiding an attribute is not a defence and neither is a test
# that agrees not to look.
FORBIDDEN_TYPES = ("Engine", "SyntheticDataset", "NQDataset", "Blotter",
                   "HoldoutLock")

# `_SealedDataset` is reachable from `SetupContext`, and deliberately.
#
# `setup()` streams the training split lazily -- materialising it is 22 GiB on
# the real adapter -- so something in the context must be able to produce those
# ticks, and in-process that means holding a reference that can. There is no
# arrangement of closures that avoids it: a closure cell holds what it closes
# over and a bound method holds its `__self__`.
#
# So the reference it holds is the sealed one, which has no `unlock_holdout`, no
# `holdout_lock` and no `_lock`, and the lock still refuses any split but the one
# being served. What a strategy gets by reaching it is the training data it was
# already handed. `test_the_reachable_dataset_cannot_open_the_holdout` is the
# assertion that this exemption is safe rather than convenient.
#
# Real isolation is a separate process. This is the honest distance to it.
# The exemption is a *path*, not a type: only the two accessors `setup()` needs.
# Anything else reaching a dataset is a finding.
SETUP_CAPABILITY_PATHS = ("ctx._train_ticks", "ctx._session_bounds")


def reachable_from(root, budget: int = 80_000) -> list[str]:
    """Every path from `root` to an object of a forbidden type."""
    seen: set[int] = set()
    stack = [("ctx", root)]
    hits: list[str] = []
    while stack and len(seen) < budget:
        path, obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if type(obj).__name__ in FORBIDDEN_TYPES:
            hits.append(f"{path} -> {type(obj).__name__}")
            continue
        if isinstance(obj, (str, bytes, int, float, bool, type(None))):
            continue
        for name in dir(obj):
            if name.startswith("__") and name not in ("__self__", "__wrapped__"):
                continue
            try:
                stack.append((f"{path}.{name}", getattr(obj, name)))
            except Exception:
                continue
        for cell in getattr(obj, "__closure__", None) or ():
            try:
                stack.append((f"{path}.<closure>", cell.cell_contents))
            except ValueError:
                pass
        if isinstance(obj, (list, tuple, set, frozenset)):
            for i, item in enumerate(list(obj)[:64]):
                stack.append((f"{path}[{i}]", item))
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:64]:
                stack.append((f"{path}[{key!r}]", value))
    return hits


class _Reacher:
    """Walks each context it is handed, the way the review's reproduction did."""

    def __init__(self):
        self.hits: dict[str, list[str]] = {}
        self.attributes: dict[str, bool] = {}

    def _walk(self, ctx):
        cls = type(ctx).__name__
        if cls in self.hits:
            return
        self.attributes[f"{cls}._engine"] = hasattr(ctx, "_engine")
        self.attributes[f"{cls}._dataset"] = hasattr(ctx, "_dataset")
        self.hits[cls] = reachable_from(ctx)

    def setup(self, ctx):
        ctx.subscribe(INTERVAL)
        self._walk(ctx)

    def on_bars(self, ctx):
        self._walk(ctx)
        if ctx.actionable and not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, stop_offset=1.0, target_offset=1.0, tag="probe")

    def on_fill(self, ctx):
        self._walk(ctx)

    def on_lifecycle(self, ctx):
        self._walk(ctx)


REACHER = _Reacher()
Engine(ds(), REACHER, split=Split.VALIDATION, warmup_sessions=1).run()


def test_the_reachability_walk_actually_ran():
    assert set(REACHER.hits) >= {
        "SetupContext", "BarContext", "FillContext", "LifecycleContext"
    }, sorted(REACHER.hits)


def test_no_context_reaches_the_engine_or_the_dataset_by_any_path():
    found = {}
    for cls, paths in REACHER.hits.items():
        unexpected = [
            path for path in paths
            if not (cls == "SetupContext"
                    and path.startswith(SETUP_CAPABILITY_PATHS))
        ]
        if unexpected:
            found[cls] = unexpected
    assert not found, (
        "strategy code can reach engine or dataset objects:\n  "
        + "\n  ".join(f"{cls}: {paths}" for cls, paths in found.items())
    )


def test_the_only_reachable_dataset_is_the_one_setup_must_stream_from():
    """And it is reachable through those two accessors and nothing else. If a
    third path appears, something was added that did not need the dataset."""
    setup_paths = REACHER.hits.get("SetupContext", [])
    assert all(p.startswith(SETUP_CAPABILITY_PATHS) for p in setup_paths), setup_paths
    for cls in ("BarContext", "FillContext", "LifecycleContext"):
        assert not REACHER.hits.get(cls), (cls, REACHER.hits.get(cls))


def test_the_two_attributes_the_review_used_are_gone():
    """The reproduction itself, kept as a regression test."""
    assert not any(REACHER.attributes.values()), REACHER.attributes


def test_the_reachable_dataset_cannot_open_the_holdout():
    """The exemption above, asserted rather than assumed."""
    engine = Engine(ds(), _Reacher(), split=Split.VALIDATION)
    from src.core.types import HoldoutLocked

    for name in ("unlock_holdout", "holdout_lock", "_lock"):
        with pytest.raises(AttributeError):
            getattr(engine.dataset, name)
    with pytest.raises(HoldoutLocked):
        next(engine.dataset.ticks(Split.HOLDOUT))


def test_the_engine_holds_a_dataset_with_no_holdout_door():
    """Belt and braces: even something inside the engine cannot open the holdout
    by accident, because the method is not on the object it holds."""
    engine = Engine(ds(), _Reacher(), split=Split.VALIDATION)
    for name in ("unlock_holdout", "holdout_lock", "_lock"):
        with pytest.raises(AttributeError, match="deliberate act|not available"):
            getattr(engine.dataset, name)
    assert engine.dataset.name.startswith("synthetic")
