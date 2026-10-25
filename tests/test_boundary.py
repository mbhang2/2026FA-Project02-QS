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
import gc
import inspect
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import CodeType, ModuleType

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
    FittedStrategy,
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
                try:
                    self.seen.append((cls, name, member()))
                except ctxmod.ContextError as exc:
                    # **A documented refusal is recorded, not skipped.** This
                    # sweep exists to catch mutable engine state crossing the
                    # boundary, and a method that refuses hands back nothing at
                    # all. `ctx.options()` refuses on a dataset whose
                    # capabilities report has_options=False, which is the whole
                    # point of declaring a capability -- and adding it to
                    # `skipped` would break
                    # `test_the_probe_skipped_only_the_action_methods`, which is
                    # right to insist that an uncalled reader is a deliberate
                    # exclusion rather than a gap.
                    #
                    # The TYPE NAME is stored and not the exception: an
                    # exception carries `__traceback__`, whose frames reach the
                    # engine, so recording the object itself would walk a path
                    # into exactly what this file is here to keep out.
                    self.seen.append((cls, name, f"refused: {type(exc).__name__}"))
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
    Engine.for_test(ds(), probe, split=Split.VALIDATION, warmup_sessions=1).run()
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

    Engine.for_test(ds(), S(), split=Split.VALIDATION).run()
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

    Engine.for_test(ds(), Mutator(), split=Split.VALIDATION).run()
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

    Engine.for_test(ds(), S(), split=Split.VALIDATION).run()
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

    Engine.for_test(ds(), T(), split=Split.VALIDATION).run()
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
    """Everything the three Protocols declare between them.

    Only `fit` is taken from `FittedStrategy`, because only `fit` is called by the
    engine. `save_weights` and `load_weights` are called by the harness -- `run.py`
    and `runlog.store_weights` -- and asserting the engine calls them would be
    false. `test_the_weight_methods_are_used_by_the_harness` covers their staleness
    instead, so nothing declared goes unchecked.

    `FittedStrategy` is also deliberately absent from
    `test_every_declared_callback_takes_exactly_the_context`: its methods are not
    context callbacks, which is the same reason
    `engine.validate_fitted_strategy` is separate from `validate_strategy`.
    """
    out = set()
    for proto in (Strategy, TickStrategy):
        out |= {
            n for n, v in vars(proto).items()
            if not n.startswith("_") and callable(v)
        }
    return out | {"fit"}


def test_the_protocol_declares_exactly_what_the_engine_calls():
    called = callbacks_the_engine_calls()
    declared = protocol_callbacks()
    assert called, "the AST scan found nothing; the engine was restructured"
    assert called == declared, (
        f"the engine calls {sorted(called)} but "
        f"types.Strategy/TickStrategy/FittedStrategy declare {sorted(declared)}. "
        "The protocol is the only statement of the contract in the code; a stale "
        "one is worse than none."
    )


def test_every_declared_callback_takes_exactly_the_context():
    """`FittedStrategy` is excluded on purpose: `fit()` takes nothing and the
    weight methods take a path, so they are not callbacks and are checked by
    `test_the_fitted_protocol_matches_the_arities_the_engine_enforces` instead."""
    for proto in (Strategy, TickStrategy):
        for name, fn in vars(proto).items():
            if name.startswith("_") or not callable(fn):
                continue
            params = [p for p in inspect.signature(fn).parameters if p != "self"]
            assert params == ["ctx"], f"{proto.__name__}.{name}{params}"


WEIGHT_CALLERS = (
    "run.py",
    "src/core/runlog.py",      # save: store_weights, beside store_source
    "src/core/engine/engine.py",   # load: the engine owns the fit-or-load decision
)


def test_the_weight_methods_are_used_by_the_harness():
    """`save_weights` and `load_weights` are declared but are not engine callbacks,
    so the protocol-equality test above cannot see them. A declaration nothing calls
    is exactly the staleness this file exists to catch -- and it caught one: the
    load path was designed, documented as the protection that stops `--stage
    validate` refitting on the validation split, and not implemented.
    """
    harness = "".join((REPO_ROOT / f).read_text() for f in WEIGHT_CALLERS)
    for name in ("save_weights", "load_weights"):
        assert name in harness, (
            f"FittedStrategy declares {name} and nothing in "
            f"{', '.join(WEIGHT_CALLERS)} calls it")


def test_the_fitted_protocol_matches_the_arities_the_engine_enforces():
    """`FittedStrategy` and `engine.FIT_ARITY` are two statements of one contract
    and must not drift. The engine validates against FIT_ARITY; a reader reads the
    Protocol."""
    from src.core.engine.engine import FIT_ARITY

    declared = {n: v for n, v in vars(FittedStrategy).items()
                if not n.startswith("_") and callable(v)}
    assert set(declared) == set(FIT_ARITY), (
        f"FittedStrategy declares {sorted(declared)} but the engine enforces "
        f"{sorted(FIT_ARITY)}")
    for name, fn in declared.items():
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        assert len(params) == FIT_ARITY[name], (
            f"FittedStrategy.{name}{params} against FIT_ARITY[{name!r}]="
            f"{FIT_ARITY[name]}")


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
        Engine.for_test(ds(), Broken(), split=Split.VALIDATION).run()


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
        Engine.for_test(ds(), OldShape(), split=Split.VALIDATION).run()


def test_the_wiring_error_lists_every_problem_at_once():
    """One error naming everything, not one per run: an agent fixing these one
    at a time spends a trial on each."""
    class Bad:
        def setup(self, ctx):
            ctx.subscribe(INTERVAL)

    with pytest.raises(TypeError) as exc:
        Engine.for_test(ds(), Bad(), split=Split.VALIDATION).run()
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

    Engine.for_test(ds(), S(), split=Split.VALIDATION).run()
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
        # **Datasets that carry their own calendar, against datasets that read
        # one from the manifest.** The branch used to test for `"synthetic"`
        # exactly, so `spx-options` -- a `SyntheticDataset` subclass with an
        # in-code calendar and no `symbol` -- fell into the manifest branch and
        # died on `KeyError: 'symbol'`. The real distinction is where the
        # calendar comes from, not what the dataset is called.
        if spec["kind"] in ("synthetic", "spx-options"):
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
#
# Task 21 widened it again. The second adversarial pass reached mutable run state
# through `ctx._port.session_bounds.__closure__[0]` -- a path the Task 20 version
# of this walk did find, and reported as a dataset hit rather than as the
# arbitrary-P&L hole it was. So the walk now also follows `__func__`,
# `__defaults__`, `__kwdefaults__`, the attributes of an object's *class*, and
# `gc.get_referents`, which is the only one of those that cannot be evaded by
# choosing a different container.
FORBIDDEN_TYPES = ("Engine", "SyntheticDataset", "NQDataset", "Blotter",
                   "HoldoutLock")

# The fill log is the score's source of truth, so reaching it is reaching the
# score. `FillRecord` is frozen, but the *list* is not: append one row and the
# reconstruction books a trade that never happened.
FILL_LOG_TYPES = ("FillRecord",)

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

# Task 25 moved the strategy into its own process, and the escape below is now a
# property of the **in-process path only** -- which since Task 25 may run nothing
# but the committed calibration reference, checked by digest in
# `RunTicket.for_calibration`. `tests/test_remote.py` re-runs this walk inside the
# strategy process and finds nothing: there is no dataset in that address space
# to forward, so finding 2 is absent rather than patched.
#
# The entry stays because the path stays. Deleting it would claim the in-process
# context no longer leaks, which is not true; it is that nothing the project
# distrusts runs there any more. `KNOWN_LIVE_ESCAPES` is now a statement about
# calibration, and the docstring below says so.
#
# Widening the walk in Task 21 surfaced one hit that was invisible to the
# narrower version, and it is a *known live finding*, not a new one:
#
#   ctx -> <the _train_ticks closure> -> cell -> _SealedDataset -> SyntheticDataset
#
# `_SealedDataset` forwards by `__getattr__`, so `dir()` never listed the inner
# dataset and the Task 20 walk stopped at the seal. `gc.get_referents` does not
# ask the object how it would like to be presented, and goes straight through.
# This is the second adversarial pass's finding 2, which Task 21 is explicitly
# scoped out of fixing -- the fix is either a forwarding seal that rebinds rather
# than delegates, or a separate process.
#
# It is listed rather than exempted. An exemption disappears into a prefix tuple;
# a list with a test asserting its exact contents fails both ways -- if a second
# escape appears, and if this one is fixed and the entry is left behind.
KNOWN_LIVE_ESCAPES = {
    "SetupContext": [
        "ctx.<referent function>.<referent tuple>.<referent cell>"
        ".<referent _SealedDataset>.<referent SyntheticDataset> -> SyntheticDataset"
    ],
}


DUNDERS_WORTH_FOLLOWING = (
    "__self__", "__func__", "__wrapped__", "__defaults__", "__kwdefaults__",
)

# `gc.get_referents` on a function returns its `__globals__`, which is a module
# dict, which reaches every class in the repo. That is not a finding: a strategy
# can `import src.core.engine` by name and get the same classes. What matters is
# whether it can reach the *live instances* of this run, and those are not in
# module globals. So the walk skips modules and module dicts, and reaches class
# objects the honest way instead -- through `vars(type(obj))`, below.
def module_dicts() -> set[int]:
    """Recomputed per walk rather than snapshotted at import.

    A snapshot taken when this file is imported misses everything imported
    afterwards, and a walk that then wanders into pandas' module dict allocates
    until the kernel kills the process -- which is exactly what happened the
    first time the whole suite ran together, and not when this file ran alone.
    """
    return {id(m.__dict__) for m in list(sys.modules.values())
            if isinstance(m, ModuleType) and hasattr(m, "__dict__")}


def reachable_from(root, forbidden=FORBIDDEN_TYPES, budget: int = 400_000) -> list[str]:
    """Every path from `root` to an object of a forbidden type."""
    _MODULE_DICTS = module_dicts()
    seen: set[int] = set()
    stack = [("ctx", root)]
    hits: list[str] = []
    while stack and len(seen) < budget:
        path, obj = stack.pop()
        if id(obj) in seen or id(obj) in _MODULE_DICTS:
            continue
        seen.add(id(obj))
        if type(obj).__name__ in forbidden:
            hits.append(f"{path} -> {type(obj).__name__}")
            continue
        if isinstance(obj, (str, bytes, int, float, bool, type(None))):
            continue
        if isinstance(obj, (ModuleType, CodeType)):
            continue
        for name in dir(obj):
            if name.startswith("__") and name not in DUNDERS_WORTH_FOLLOWING:
                continue
            try:
                stack.append((f"{path}.{name}", getattr(obj, name)))
            except Exception:
                continue
        # class attributes: a mutable default or a registry hung off the class
        # is reachable from every instance of it and belongs to none of them
        for cls in type(obj).__mro__ if isinstance(type(obj), type) else ():
            if cls is object:
                continue
            for name, value in list(vars(cls).items()):
                if name.startswith("__") and name not in DUNDERS_WORTH_FOLLOWING:
                    continue
                stack.append((f"{path}.<class {cls.__name__}>.{name}", value))
        closure = getattr(obj, "__closure__", None)
        for cell in closure if isinstance(closure, tuple) else ():
            try:
                stack.append((f"{path}.<closure>", cell.cell_contents))
            except (ValueError, AttributeError):
                pass
        if isinstance(obj, (list, tuple, set, frozenset)):
            for i, item in enumerate(list(obj)[:64]):
                stack.append((f"{path}[{i}]", item))
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:64]:
                stack.append((f"{path}[{key!r}]", value))
        # the backstop: whatever the interpreter says this object points at,
        # regardless of how the object chose to present itself
        for ref in gc.get_referents(obj):
            stack.append((f"{path}.<referent {type(ref).__name__}>", ref))
    return hits


class _Reacher:
    """Walks each context it is handed, the way the review's reproduction did."""

    def __init__(self):
        self.hits: dict[str, list[str]] = {}
        self.fill_log_hits: dict[str, list[str]] = {}
        self.attributes: dict[str, bool] = {}

    def _walk(self, ctx):
        cls = type(ctx).__name__
        if cls in self.hits:
            return
        self.attributes[f"{cls}._engine"] = hasattr(ctx, "_engine")
        self.attributes[f"{cls}._dataset"] = hasattr(ctx, "_dataset")
        self.hits[cls] = reachable_from(ctx)
        self.fill_log_hits[cls] = reachable_from(ctx, FILL_LOG_TYPES)

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


class _Trader:
    """Trades, and does not walk. The walking probe is expensive and these tests
    only need fills."""

    def setup(self, ctx):
        ctx.subscribe(INTERVAL)

    def on_bars(self, ctx):
        if ctx.actionable and not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, stop_offset=1.0, target_offset=1.0, tag="probe")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


REACHER = _Reacher()
Engine.for_test(ds(), REACHER, split=Split.VALIDATION, warmup_sessions=1).run()


def test_the_reachability_walk_actually_ran():
    assert set(REACHER.hits) >= {
        "SetupContext", "BarContext", "FillContext", "LifecycleContext"
    }, sorted(REACHER.hits)


def unexpected_escapes() -> dict[str, list[str]]:
    """Hits that are neither the setup capability nor a named live finding."""
    found = {}
    for cls, paths in REACHER.hits.items():
        known = KNOWN_LIVE_ESCAPES.get(cls, [])
        rest = [
            path for path in paths
            if path not in known
            and not (cls == "SetupContext"
                     and path.startswith(SETUP_CAPABILITY_PATHS))
        ]
        if rest:
            found[cls] = rest
    return found


def test_no_context_reaches_the_engine_or_the_dataset_by_any_path():
    found = unexpected_escapes()
    assert not found, (
        "strategy code can reach engine or dataset objects:\n  "
        + "\n  ".join(f"{cls}: {paths}" for cls, paths in found.items())
    )


def test_the_named_live_escape_is_still_exactly_one_path():
    """The other half of the list, and since Task 25 a narrower claim.

    The escape is real on the in-process path and that path now runs only the
    committed reference strategy -- `RunTicket.for_calibration` refuses anything
    whose digest differs. So this asserts what is still true rather than what
    would be reassuring: the leak exists, and nothing the project distrusts is
    on the far side of it. `tests/test_remote.py` is where the claim about the
    strategy process lives.
    """
    for cls, paths in KNOWN_LIVE_ESCAPES.items():
        still_there = [p for p in paths if p in REACHER.hits.get(cls, [])]
        assert still_there == paths, (
            f"{cls}: a known live escape no longer reproduces. If it was fixed, "
            f"delete it from KNOWN_LIVE_ESCAPES:\n  " + "\n  ".join(paths)
        )
    assert sum(len(v) for v in KNOWN_LIVE_ESCAPES.values()) == 1


def test_the_only_reachable_dataset_is_the_one_setup_must_stream_from():
    """And it is reachable through those two accessors and nothing else. If a
    third path appears, something was added that did not need the dataset."""
    setup_paths = [p for p in REACHER.hits.get("SetupContext", [])
                   if p not in KNOWN_LIVE_ESCAPES["SetupContext"]]
    assert all(p.startswith(SETUP_CAPABILITY_PATHS) for p in setup_paths), setup_paths
    for cls in ("BarContext", "FillContext", "LifecycleContext"):
        assert not REACHER.hits.get(cls), (cls, REACHER.hits.get(cls))


def test_no_context_can_reach_the_fill_log(cls_and_paths=None):
    """The Task 21 acceptance criterion.

    The score is now computed from the fill log, so the log is the thing worth
    protecting and this is the assertion that says so. It uses the widened walk
    -- `gc.get_referents` included -- because the log is a plain `list`, and a
    plain list is exactly the sort of object that leaks through a container that
    presents itself politely.

    A hit here is not a subtle problem. One appended `FillRecord` is a trade the
    reconstruction will book, at a price of the strategy's choosing.
    """
    found = {cls: paths for cls, paths in REACHER.fill_log_hits.items() if paths}
    assert not found, (
        "strategy code can reach the fill log:\n  "
        + "\n  ".join(f"{cls}: {paths}" for cls, paths in found.items())
    )


def test_the_fill_log_walk_would_have_found_a_log_if_there_were_one():
    """A guard on the guard above, which passes vacuously if `FILL_LOG_TYPES` is
    misspelled or the walk stops early. Plant a log on the context and require
    the same walk to find it."""
    engine = Engine.for_test(ds(), _Trader(), split=Split.VALIDATION, warmup_sessions=1)
    engine.run()
    assert engine.fill_log, "the run produced no fills, so this proves nothing"

    class _Leaky:
        def __init__(self, log):
            self._log = log

    assert reachable_from(_Leaky(list(engine.fill_log)), FILL_LOG_TYPES)


def test_the_two_attributes_the_review_used_are_gone():
    """The reproduction itself, kept as a regression test."""
    assert not any(REACHER.attributes.values()), REACHER.attributes


def test_the_reachable_dataset_cannot_open_the_holdout():
    """The exemption above, asserted rather than assumed."""
    engine = Engine.for_test(ds(), _Reacher(), split=Split.VALIDATION)
    from src.core.types import HoldoutLocked

    for name in ("unlock_holdout", "holdout_lock", "_lock"):
        with pytest.raises(AttributeError):
            getattr(engine.dataset, name)
    with pytest.raises(HoldoutLocked):
        next(engine.dataset.ticks(Split.HOLDOUT))


def test_the_engine_holds_a_dataset_with_no_holdout_door():
    """Belt and braces: even something inside the engine cannot open the holdout
    by accident, because the method is not on the object it holds."""
    engine = Engine.for_test(ds(), _Reacher(), split=Split.VALIDATION)
    for name in ("unlock_holdout", "holdout_lock", "_lock"):
        with pytest.raises(AttributeError, match="deliberate act|not available"):
            getattr(engine.dataset, name)
    assert engine.dataset.name.startswith("synthetic")


# --------------------------------------------------------------------------- #
# adversarial review, second pass, finding 1: the score was a running total
# --------------------------------------------------------------------------- #
# The reproduction, kept verbatim. A strategy reached `_RunState` through the
# closure cell of a bound port callable and wrote `avg_price` before flattening:
#
#     st = ctx._port.session_bounds.__closure__[0].cell_contents
#     st.avg_price = ctx.last_price - 5.0        # "I bought five lower"
#
# It took Sortino from -0.2547 to +4.2089 and significance from -1.611 to
# +26.619, with a genuine 1,255-row blotter, a stable digest, a replay that
# reproduced it exactly and an untouched scoring fingerprint. Nothing downstream
# could tell.
#
# **The assertion is that the score does not move, not that the write fails.**
# The write still succeeds and `avg_price` is still wrong afterwards; that is
# fine, because the accumulator is no longer what anything is scored on. Trying
# to block the write is the losing game -- there is always another attribute --
# and the whole point of the fill log is that it does not matter which one.
class _AvgPriceCheat:
    def __init__(self, cheat: bool):
        self.cheat = cheat
        self.wrote = False

    def setup(self, ctx):
        ctx.subscribe("5m")

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
        if not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, tag="e")
            return
        if ctx.position > 0:
            if self.cheat:
                st = ctx._port.session_bounds.__closure__[0].cell_contents
                st.avg_price = ctx.last_price - 5.0
                self.wrote = True
            ctx.close()

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def _cheat_run(cheat: bool):
    from src.core.scoring.scorer import score

    strategy = _AvgPriceCheat(cheat)
    engine = Engine.for_test(
        SyntheticDataset(SyntheticSpec(n_sessions=200, ticks_per_session=390,
                                       vol_per_tick=0.15, seed=3)),
        strategy, split=Split.VALIDATION, warmup_sessions=1,
    )
    engine.run()
    blotter = engine.reconstructed()
    return strategy, engine, blotter, score(blotter, period=engine.session_calendar())


CLEAN = _cheat_run(False)
CHEAT = _cheat_run(True)


def test_the_reproduction_still_writes_what_it_wrote():
    """Otherwise the test below passes because the attack stopped running, which
    is the most common way a regression test for an exploit goes quietly dead."""
    assert CHEAT[0].wrote and not CLEAN[0].wrote
    assert len(CLEAN[2].rows) == 1255, "the reproduction no longer trades"


def test_rewriting_the_accumulator_does_not_move_the_score():
    """Finding 1, closed. Same fills, same score, whatever was done to the
    running total in between."""
    clean_score, cheat_score = CLEAN[3], CHEAT[3]
    assert cheat_score.primary == clean_score.primary
    assert cheat_score.significance == clean_score.significance
    assert CHEAT[2].digest() == CLEAN[2].digest()
    assert CHEAT[1].fill_log_digest() == CLEAN[1].fill_log_digest()


def test_the_accumulator_really_was_corrupted():
    """The other half. If the mutation had silently stopped working, the test
    above would be asserting that nothing equals nothing."""
    clean_net = CLEAN[1].blotter.realized_pnl - CLEAN[1].blotter.commission
    cheat_net = CHEAT[1].blotter.realized_pnl - CHEAT[1].blotter.commission
    assert cheat_net > clean_net + 1000, (clean_net, cheat_net)


def test_the_disagreement_check_notices_even_though_it_did_not_have_to():
    """The bonus, working. It is not the protection -- anything that can reach
    the accumulator can reach both sides of it -- but a run where the two
    derivations disagree is a run nobody should read a number off, and the
    harness invalidates it rather than quietly preferring the right one."""
    assert CLEAN[1].reconciliation()["agree"]
    assert not CHEAT[1].reconciliation()["agree"]


def test_the_fill_log_is_append_only_in_one_place():
    """The property that makes the reconstruction worth anything. If a second
    writer appears, the log stops being a record of what the matching engine did
    and becomes another accumulator."""
    source = (Path(__file__).resolve().parents[1]
              / "src" / "core" / "engine" / "engine.py").read_text()
    assert source.count("self._fill_log.append(") == 1, (
        "the fill log is appended to in more than one place"
    )
    for forbidden in ("_fill_log.pop", "_fill_log.clear", "_fill_log[", "_fill_log ="):
        assert source.count(forbidden) <= (1 if forbidden == "_fill_log =" else 0), (
            f"the fill log is modified by {forbidden!r}"
        )


def test_the_fill_log_bound_raises_rather_than_truncating(monkeypatch):
    """A truncated log is a wrong score with no error, which is the whole class
    of defect the log exists to remove. So the bound is loud."""
    from src.core.engine import engine as engmod
    from src.core.engine.reconstruct import FillLogFull

    monkeypatch.setattr(engmod, "MAX_FILL_LOG", 5)

    class Churn:
        def setup(self, ctx):
            ctx.subscribe("1m")

        def on_bars(self, ctx):
            if ctx.actionable and not ctx.working_order_ids:
                ctx.buy(1) if not ctx.position else ctx.close()

        def on_fill(self, ctx):
            pass

        def on_lifecycle(self, ctx):
            pass

    with pytest.raises(FillLogFull, match="not truncated"):
        Engine.for_test(ds(), Churn(), split=Split.VALIDATION, warmup_sessions=1).run()


def test_reconstruction_is_a_pure_function_of_the_fills():
    """No engine, no dataset, no accumulator -- the argument that the score's
    input cannot have been reached is that the computation takes nothing that
    could have been."""
    import inspect

    from src.core.engine import reconstruct as rec

    params = list(inspect.signature(rec.reconstruct).parameters)
    assert params == ["fills", "commission_per_contract"], params

    engine = Engine.for_test(ds(), _Trader(), split=Split.VALIDATION, warmup_sessions=1)
    engine.run()
    fills = list(engine.fill_log)
    assert fills
    # same input, same output, and shuffling the list changes nothing because
    # the function orders by seq rather than trusting append order
    import random

    shuffled = fills[:]
    random.Random(0).shuffle(shuffled)
    assert (rec.reconstruct(fills, commission_per_contract=engine.commission).digest()
            == rec.reconstruct(shuffled, commission_per_contract=engine.commission).digest()
            == engine.reconstructed().digest())


def test_the_fill_log_is_a_snapshot_not_the_list_itself():
    """`engine.fill_log` hands out a tuple. Handing out the list would put the
    thing the score is computed from back within reach of anything holding the
    engine -- which is not a strategy today, and is one refactor away."""
    engine = Engine.for_test(ds(), _Trader(), split=Split.VALIDATION, warmup_sessions=1)
    engine.run()
    log = engine.fill_log
    assert isinstance(log, tuple)
    assert not hasattr(log, "append")
