"""The strategy process. Receives data; holds no engine, no dataset, no scorer.

Started with two pipe fds and nothing else. It imports the strategy module by
path -- which is execution, and is correct here: this is the process that is
*supposed* to run strategy code, and there is nothing in it worth reaching.
`docs/reviews/task24/` measured that: a `gc.get_referents` walk over the child's
object graph found no engine, dataset, blotter or lock, and the only `src.*`
modules it loads are `src.core.types` and the strategy itself.

`RemoteContext` answers reads from the pushed message and queues actions into the
reply. Where the push does not contain what the strategy asked for, it raises
rather than returning something plausible -- a boundary that silently truncates
is a boundary that changes results.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
import traceback
from pathlib import Path

def _repo_root() -> Path:
    """The marker walk, inline, because this is the bootstrap.

    Every other module under `src/core/` gets this from `src.core._roots`
    (Task 47). This one cannot: it computes the root *in order to put it on*
    `sys.path`, so at this point in the file there is nothing importable by
    package name -- asking for a helper would be circular. `_load_sibling`
    below cannot help either; reaching the file it would load is the same
    problem one directory at a time.

    So the walk is copied rather than imported, and what matters is that it is
    a WALK: it looks upward for a marker and stops at the first one, which
    survives this file being moved. `Path(__file__).resolve().parents[4]` did
    not, and was one of nine such counts at three different depths.
    `tests/test_no_depth_counted_roots.py` asserts this stays a walk.
    """
    here = Path(__file__).resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
        if (d / "run.py").is_file() and (d / "pyproject.toml").is_file():
            return d
    raise RuntimeError(
        f"no repository marker above {here}: looked for .git, and for run.py "
        "beside pyproject.toml")


REPO_ROOT = _repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# By file path, not by package. `from src.core.engine.remote.protocol import ...`
# executes `src/core/engine/__init__.py`, which imports the engine, the contexts,
# the bar builder and the fill models -- so the child's address space contained
# the whole simulator, reachable by name from any strategy that typed
# `sys.modules`. The walk in `tests/test_remote.py` found exactly that, and it is
# the second time in this project an `__init__` re-export quietly widened a
# boundary. Nothing here is heavy enough to need a package.
def _load_sibling(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_remote_{name}", Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_remote_{name}"] = module
    spec.loader.exec_module(module)
    return module


_protocol = _load_sibling("protocol")
_surface = _load_sibling("_surface")
recv, send = _protocol.recv, _protocol.send
from enum import Enum  # noqa: E402

# `src.core.types` and nothing else. `src/core/__init__.py` is empty, so this
# brings in value types and constants -- no engine, no dataset, no scorer. The
# walk asserts the module list stays exactly this short.
from src.core.types import (  # noqa: E402
    Bar, FillEvent, LifecycleEvent, OrderView, QuoteEvent, Side, Subscription,
    Tick, TickEvent,
)

# The fitted protocol, spelled here rather than imported. This process must not
# import the engine -- that separation is the isolation -- so the names are
# duplicated and `tests/test_boundary.py` asserts they still match
# `engine.FIT_ARITY`. A drift between the two would mean the child advertises a
# capability the engine does not enforce, or misses one it does.
FIT_METHODS = ("fit", "save_weights", "load_weights")


class WindowTooShort(RuntimeError):
    """The strategy reached past the window the engine pushed.

    The one semantic difference between the isolated and in-process paths, and
    it is loud on purpose. In process, `ctx.history()` is a lazy view over
    everything the run has seen; across a boundary something has to bound it,
    and the honest bound raises rather than returning a shorter history that
    looks complete.
    """


class RemoteError(RuntimeError):
    """The child could not answer, for a reason that is not the strategy's."""


def _typed_decoder(cls):
    """Rebuild a value type from its field row.

    The child imports the same `src.core.types` the engine encodes from, so the
    field order is one object rather than two lists that have to agree. Enum
    fields are re-typed from their annotation; everything else is JSON already.
    """
    import dataclasses
    import typing

    hints = typing.get_type_hints(cls)
    enums, tuples = {}, set()
    for field in dataclasses.fields(cls):
        hint = hints.get(field.name)
        for candidate in typing.get_args(hint) or (hint,):
            if isinstance(candidate, type) and issubclass(candidate, Enum):
                enums[field.name] = candidate
        if typing.get_origin(hint) is tuple:
            # JSON has no tuples. A field that was a tuple has to come back as
            # one, or a decoded event compares unequal to the event that was
            # sent -- which is how a boundary passes its own tests and fails the
            # golden digests.
            tuples.add(field.name)
    names = [f.name for f in dataclasses.fields(cls)]

    def decode(row):
        kwargs = {}
        for name, value in zip(names, row):
            enum = enums.get(name)
            if enum and value is not None:
                value = enum(value)
            elif name in tuples and value is not None:
                value = tuple(value)
            kwargs[name] = value
        return cls(**kwargs)

    return decode


_decode_bar = _typed_decoder(Bar)
_decode_fill = _typed_decoder(FillEvent)
_decode_lifecycle = _typed_decoder(LifecycleEvent)
_decode_order = _typed_decoder(OrderView)
_decode_tick = _typed_decoder(TickEvent)
_decode_raw_tick = _typed_decoder(Tick)
_decode_quote = _typed_decoder(QuoteEvent)


class Window:
    """The pushed tail of a history, plus the true total length.

    `len()` is exact -- the total is sent alongside the tail -- so a strategy
    that branches on how much history it has behaves as it does in process.
    Reaching past the tail raises. The tail is maintained incrementally: the
    parent sends only bars the child has not seen, because a rolling window is
    append-only and re-sending it every callback was measured at 9x the cost for
    identical semantics.
    """

    __slots__ = ("_items", "_total")

    def __init__(self, items, total: int):
        self._items = items
        self._total = total

    def __len__(self) -> int:
        return self._total

    def _at(self, index: int):
        if index < 0:
            index += self._total
        first = self._total - len(self._items)
        if index < first or index >= self._total:
            raise WindowTooShort(
                f"index {index} is outside the pushed window: the engine sent "
                f"the last {len(self._items)} of {self._total}. Declare a longer "
                f"window, or keep what you need on `self` as it arrives."
            )
        return self._items[index - first]

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self._total)
            return tuple(self._at(i) for i in range(start, stop, step))
        return self._at(key)

    def __iter__(self):
        first = self._total - len(self._items)
        if first:
            raise WindowTooShort(
                f"iterating would visit {self._total} items and the engine sent "
                f"the last {len(self._items)}"
            )
        return iter(self._items)

    def last(self, k: int = 1):
        """The k most recent, oldest first -- the same contract `HistoryView`
        offers in process, which is the one the documentation and the worked
        examples teach.

        **Missing here until Task 36, and the first LLM in the loop found it on
        its second proposal.** `strategy-api.md` documents `view.last(k)` and
        two worked examples use it; `tests/test_examples.py` ran those examples
        through `Engine.for_test`, which is the *in-process* path, so the check
        that was supposed to prove the examples work against the current engine
        never touched the engine the scored path uses. Every strategy that
        followed the documentation raised `AttributeError` on its first bar --
        and since Task 23 that costs a trial.

        Short history returns what exists, like in process: a warmup shortfall
        is a legitimate state. Asking for more than the *pushed window* holds
        is not, and raises, because returning a shorter tail that looks complete
        is the one thing this boundary refuses to do.
        """
        if k <= 0:
            return ()
        if k > len(self._items) and k <= self._total:
            raise WindowTooShort(
                f"last({k}) reaches past the pushed window: the engine sent the "
                f"last {len(self._items)} of {self._total}. Declare a longer "
                "window, or keep what you need on `self` as it arrives."
            )
        return tuple(self._items[-k:])


class _ClosedBars:
    """`ctx.bars`, rebuilt from `ctx.closed` rather than sent.

    A view over state the child already has cannot disagree with that state,
    which a second encoding of it could.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx):
        self._ctx = ctx

    def __getitem__(self, sub):
        sid = self._ctx.sub_id(sub)
        for bar in self._ctx.closed:
            if bar.sub_id == sid:
                return bar
        raise KeyError(
            f"{sid!r} did not close in this callback; test with "
            f"`if {sub!r} in ctx.bars:` first"
        )

    def __contains__(self, sub) -> bool:
        try:
            sid = self._ctx.sub_id(sub)
        except Exception:
            return False
        return any(bar.sub_id == sid for bar in self._ctx.closed)

    def __len__(self) -> int:
        return len(self._ctx.closed)

    def __iter__(self):
        return iter(b.sub_id for b in self._ctx.closed)


class RemoteContext:
    """Every public name on the real context, answered without the engine.

    The set of names is not maintained here: `tests/test_remote.py` asserts this
    class covers `_surface.SURFACE`, which `scripts/gen_remote_surface.py`
    generates from `context.py`. A member added to the context and forgotten here
    fails the build.
    """

    def __init__(self, message, actions, state):
        self._m = message
        self._actions = actions
        self._state = state          # rolling windows and the subscription map

    # -- pushed scalars ----------------------------------------------------- #
    # Installed below from the generated surface, one real property each, rather
    # than served by a `__getattr__` catch-all. A catch-all answers every name
    # including the ones nobody implemented, so `hasattr` cannot tell coverage
    # from luck -- and coverage is the thing this class is tested for.

    @property
    def working_order_ids(self):
        return tuple(self._m["scalars"]["working_order_ids"])

    @property
    def now(self):
        import datetime as dt
        return dt.datetime.fromisoformat(self._m["now"])

    # -- pushed collections ------------------------------------------------ #
    @property
    def closed(self):
        return tuple(_decode_bar(r) for r in self._m["closed"])

    @property
    def fills(self):
        return tuple(_decode_fill(r) for r in self._m["fills"])

    @property
    def working_orders(self):
        return tuple(_decode_order(r) for r in self._m["working_orders"])

    @property
    def bars(self):
        return _ClosedBars(self)

    @property
    def fill(self):
        return _decode_fill(self._m["fill"])

    @property
    def event(self):
        return _decode_lifecycle(self._m["event"])

    @property
    def tick(self):
        return _decode_tick(self._m["tick"])

    @property
    def quote(self):
        return _decode_quote(self._m["quote"])

    # -- reads derived from what was pushed -------------------------------- #
    def closed_ids(self):
        return tuple(b.sub_id for b in self.closed)

    def sub_id(self, sub):
        key = sub.sub_id if isinstance(sub, Subscription) else str(sub)
        ids = self._state["sub_ids"]
        if key in ids:
            return ids[key]
        raise KeyError(
            f"no subscription {key!r}; declare it in setup(). "
            f"Subscribed: {sorted(set(ids.values()))}"
        )

    def find_orders(self, *, tag=None, order_type=None, side=None, parent_id=None):
        out = []
        for order in self.working_orders:
            if tag is not None and order.tag != tag:
                continue
            if order_type is not None and str(order.order_type) != str(order_type):
                continue
            if side is not None and str(order.side) != str(side):
                continue
            if parent_id is not None:
                continue          # parent_id is not on OrderView; see the report
            out.append(order)
        return tuple(out)

    def history(self, sub):
        sid = self.sub_id(sub)
        window = self._state["bars"].get(sid)
        if window is None:
            raise KeyError(f"no history for {sid!r}")
        return Window(window["items"], window["total"])

    def ticks(self):
        window = self._state["ticks"]
        return Window(window["items"], window["total"])

    # -- actions ----------------------------------------------------------- #
    def subscribe(self, interval, *, emit_empty=False):
        self._actions.append({"op": "subscribe", "interval": interval,
                              "emit_empty": emit_empty})
        return interval

    def _order(self, op, size=1, **kw):
        self._actions.append({"op": op, "size": size,
                              **{k: v for k, v in kw.items() if v is not None}})
        return self._state["ids"].take()

    def buy(self, size=1, **kw):
        return self._order("buy", size, **kw)

    def sell(self, size=1, **kw):
        return self._order("sell", size, **kw)

    def close(self, **kw):
        self._actions.append({"op": "close",
                              **{k: v for k, v in kw.items() if v is not None}})
        return self._state["ids"].take()

    def cancel(self, order_id):
        self._actions.append({"op": "cancel", "order_id": order_id})

    def cancel_all(self):
        self._actions.append({"op": "cancel_all"})

    def modify(self, order_id, *, limit=None, trigger=None, size=None):
        self._actions.append({
            "op": "modify", "order_id": order_id,
            **{k: v for k, v in
               (("limit", limit), ("trigger", trigger), ("size", size))
               if v is not None},
        })


def _install_scalars() -> None:
    """One property per pushed scalar, from `_surface`.

    Generated from `context.py` via `scripts/gen_remote_surface.py`, so a scalar
    added to the context appears here without anyone remembering, and a scalar
    the generator cannot classify fails the build instead.
    """
    for name in _surface.BASECONTEXT_SCALARS:
        if name in vars(RemoteContext):
            continue                     # an explicit property wins

        def getter(self, _name=name):
            try:
                return self._m["scalars"][_name]
            except KeyError:
                raise AttributeError(
                    f"ctx.{_name} was not pushed with this callback"
                ) from None

        getter.__name__ = name
        setattr(RemoteContext, name, property(getter))


_install_scalars()


class _IdBlock:
    """The ids `ctx.buy()` may return in this callback.

    In process the intent queue allocates at the call. Across the boundary the
    engine pre-allocates a block per callback and the child hands them out in
    order, so `ctx.buy()` still returns synchronously without a second round
    trip. The block is the size of the action cap, so exhausting it *is* hitting
    the cap rather than a second failure mode with its own message. Ids not used
    in a callback are retired, never reused: the fill log is keyed on order id
    and an id that meant two orders would make it unreadable.
    """

    def __init__(self, ids):
        self._ids = list(ids)
        self._next = 0

    def take(self):
        if self._next >= len(self._ids):
            return None                # the action cap; the engine says so too
        oid = self._ids[self._next]
        self._next += 1
        return oid


class SetupContext:
    """`setup()`'s own surface. Bulk training data is a pull, and it is capped.

    Pulls are safe here and were rejected for callbacks, and the difference is
    arithmetic: `setup()` happens once, so a handful of round trips is free,
    while a callback happens 301,007 times in a full trial and the multiplier is
    chosen by the strategy.
    """

    def __init__(self, message, actions, pull):
        self._m = message
        self._actions = actions
        self._pull = pull

    @property
    def dataset_name(self):
        return self._m["dataset_name"]

    def subscribe(self, interval, *, emit_empty=False):
        self._actions.append({"op": "subscribe", "interval": interval,
                              "emit_empty": emit_empty})
        return interval

    def subscriptions(self):
        return tuple(self._m.get("declared", ()))

    def ticks(self):
        return tuple(_decode_raw_tick(r) for r in self._pull("ticks", {}))

    def train_bars(self, interval):
        key = interval.sub_id if isinstance(interval, Subscription) else str(interval)
        return tuple(_decode_bar(r) for r in self._pull("train_bars",
                                                        {"interval": key}))


def load(path: str, class_name: str):
    """Import the strategy as the module it was, so relative imports resolve.

    Task 22 hit this in `scripts/replay.py`, Task 24 hit it again in the spike:
    the calibration reference says `from ...types import NS_PER_SECOND`, and a
    module executed under a bare name has no package.
    """
    origin = str(path)
    rel = origin
    if rel.startswith(str(REPO_ROOT)):
        rel = str(Path(rel).relative_to(REPO_ROOT))
    package = rel[:-3].replace("/", ".").rsplit(".", 1)[0] if rel.endswith(".py") else ""
    name = f"{package}._remote_strategy" if package else "remote_strategy"

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for attr in (class_name, "STRATEGY", "Strategy", "MomentumStrategy"):
        obj = getattr(module, attr, None)
        if obj is not None:
            return obj() if isinstance(obj, type) else obj
    raise RemoteError(f"{path} defines no {class_name!r}")


def main() -> None:
    read_fd = int(os.environ["REMOTE_READ_FD"])
    write_fd = int(os.environ["REMOTE_WRITE_FD"])
    strategy = None
    state = {"bars": {}, "ticks": {"items": [], "total": 0}, "sub_ids": {},
             "ids": _IdBlock(())}

    def pull(what, args):
        send(write_fd, {"reply": "pull", "what": what, "args": args})
        answer = recv(read_fd)
        if not answer.get("ok", True):
            raise RemoteError(answer.get("error", "the engine refused the pull"))
        return answer["rows"]

    while True:
        try:
            message = recv(read_fd)
        except EOFError:
            return
        kind = message["kind"]

        if kind == "load":
            try:
                strategy = load(message["path"], message["class"])
                # **What the class actually defines, not what the profile
                # claims.** The parent picks a proxy class from the profile's
                # `fit_sessions` before this process exists; it cross-checks the
                # choice against this list. The silent failure it catches is a
                # profile that omits `fit_sessions` for a class that *has*
                # `fit()`: the fitted path would never engage, the model would
                # score with unfitted parameters, and the row would look real.
                caps = [m for m in FIT_METHODS if hasattr(strategy, m)]
                send(write_fd, {"reply": "done", "ok": True, "actions": [],
                                "capabilities": caps})
            except Exception as exc:                          # noqa: BLE001
                send(write_fd, {"reply": "done", "ok": False, "actions": [],
                                "error_kind": "strategy_raised",
                                "error": f"{type(exc).__name__}: {exc}",
                                "traceback": traceback.format_exc()})
            continue
        if kind == "stop":
            send(write_fd, {"reply": "done", "ok": True, "actions": []})
            return
        if kind == "save_weights":
            # Bytes back over the pipe. This process cannot write, so it hands
            # the parameters to the parent and the parent decides where they go.
            try:
                blob = strategy.save_weights()
                if not isinstance(blob, (bytes, bytearray)):
                    raise TypeError(
                        f"save_weights() returned {type(blob).__name__}, not "
                        "bytes. It hands the parameters over; it does not write "
                        "them anywhere.")
                send(write_fd, {"reply": "done", "ok": True, "actions": [],
                                "blob": base64.b64encode(bytes(blob)).decode()})
            except Exception as exc:                          # noqa: BLE001
                send(write_fd, {"reply": "done", "ok": False, "actions": [],
                                "error_kind": "strategy_raised",
                                "error": f"{type(exc).__name__}: {exc}",
                                "traceback": traceback.format_exc()})
            continue
        if kind == "load_weights":
            try:
                strategy.load_weights(base64.b64decode(message["blob"]))
                send(write_fd, {"reply": "done", "ok": True, "actions": []})
            except Exception as exc:                          # noqa: BLE001
                send(write_fd, {"reply": "done", "ok": False, "actions": [],
                                "error_kind": "strategy_raised",
                                "error": f"{type(exc).__name__}: {exc}",
                                "traceback": traceback.format_exc()})
            continue
        if kind == "fit":
            # **The one callback with no context.** `fit()` reads the state the
            # strategy accumulated for itself across the fit window; it is handed
            # no ctx and it may not trade, which is why `FIT_ARITY` maps it to 0
            # and why it is not in the `_snapshot` path with the others. Training
            # happens here, in the sandboxed child, under this process's rlimits.
            #
            # The replay environment, by file path, under a name that is not a
            # `src.*` package. Importing it as `src.core.engine.remote.replay_env`
            # would execute `src/core/engine/__init__.py` and put the engine in
            # this process. A strategy trains by `import harness_replay`.
            if "harness_replay" not in sys.modules:
                sys.modules["harness_replay"] = _load_sibling("replay_env")
            try:
                strategy.fit()
                send(write_fd, {"reply": "done", "ok": True, "actions": []})
            except Exception as exc:                          # noqa: BLE001
                send(write_fd, {"reply": "done", "ok": False, "actions": [],
                                "error_kind": "strategy_raised",
                                "error": f"{type(exc).__name__}: {exc}",
                                "traceback": traceback.format_exc()})
            continue

        # rolling windows, maintained here so the parent sends only what is new
        for sid, window in message.get("windows", {}).items():
            kept = state["bars"].setdefault(sid, {"items": [], "total": 0})
            kept["items"].extend(_decode_bar(r) for r in window["bars"])
            del kept["items"][:max(0, len(kept["items"]) - window["keep"])]
            kept["total"] = window["total"]
        if "tick_window" in message:
            window = message["tick_window"]
            kept = state["ticks"]
            kept["items"].extend(_decode_tick(r) for r in window["ticks"])
            del kept["items"][:max(0, len(kept["items"]) - window["keep"])]
            kept["total"] = window["total"]
        if "sub_ids" in message:
            state["sub_ids"] = message["sub_ids"]
        state["ids"] = _IdBlock(message.get("id_block", ()))

        actions: list[dict] = []
        ctx = (SetupContext(message, actions, pull) if kind == "setup"
               else RemoteContext(message, actions, state))
        try:
            getattr(strategy, kind)(ctx)
        except Exception as exc:                              # noqa: BLE001
            send(write_fd, {"reply": "done", "ok": False, "actions": [],
                            "error_kind": "strategy_raised",
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()})
            continue
        send(write_fd, {"reply": "done", "ok": True, "actions": actions})


if __name__ == "__main__":
    main()
