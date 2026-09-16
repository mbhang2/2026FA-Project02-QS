"""The strategy process. Receives data; holds no engine and no dataset.

Started with two pipe fds and nothing else. It imports the strategy module by
path -- which is execution, and is fine here, because *this* process is the one
that is allowed to run strategy code and has nothing worth reaching.

`RemoteContext` answers reads from the pushed message. Where the pushed window
does not contain what the strategy asked for, it raises rather than returning
something plausible: a boundary that silently truncates history is a boundary
that changes results, which is the failure mode the whole exercise exists to
avoid.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from docs.reviews.task24.proto.protocol import recv, send  # noqa: E402


class Bar:
    __slots__ = ("sub_id", "open_ts", "close_ts", "open", "high", "low", "close",
                 "volume", "tick_count", "session")

    def __init__(self, row):
        (self.sub_id, self.open_ts, self.close_ts, self.open, self.high, self.low,
         self.close, self.volume, self.tick_count, self.session) = row

    @property
    def is_empty(self):
        return self.tick_count == 0


class Window:
    """The pushed tail of the history, plus the true total length.

    `len()` is exact, so a strategy that branches on how much history it has
    behaves the same as in-process. Reaching past the pushed tail raises
    `WindowTooShort` -- the one honest answer, and an error the in-process
    version cannot produce. It is the price of the push shape and it belongs in
    the recommendation rather than in a footnote.
    """

    def __init__(self, bars, total):
        self._bars = [Bar(r) for r in bars]
        self._total = total

    def __len__(self):
        return self._total

    def __getitem__(self, key):
        if isinstance(key, slice):
            raise WindowTooShort("slicing a pushed window is not implemented in "
                                 "the spike")
        index = key if key >= 0 else self._total + key
        first = self._total - len(self._bars)
        if index < first or index >= self._total:
            raise WindowTooShort(
                f"bar {key} is outside the pushed window: the last "
                f"{len(self._bars)} of {self._total} bars were sent"
            )
        return self._bars[index - first]

    def __iter__(self):
        raise WindowTooShort("iterating a pushed window is not implemented in "
                             "the spike")


class WindowTooShort(RuntimeError):
    pass


# The child's own copy of the rolling window, in incremental mode. It lives here
# rather than on the context because a context is one callback long and the whole
# point is that the window outlives it.
ROLLING: dict[str, list] = {}


class RemoteContext:
    """Everything the reference strategy reads, and nothing else.

    Deliberately not a full context: the spike's question is whether the boundary
    can be faithful, and a partial context that raises on anything unimplemented
    answers it more honestly than a stub that returns zeros.
    """

    def __init__(self, message, actions):
        self._m = message
        self._actions = actions

    # -- reads, answered from the message --------------------------------- #
    SCALARS = ("ts", "session", "session_start", "session_end", "position",
               "actionable", "last_price", "realized_pnl", "unrealized_pnl",
               "equity", "average_entry_price")

    @property
    def working_order_ids(self):
        return tuple(self._m["working_order_ids"])

    @property
    def closed(self):
        return tuple(Bar(r) for r in self._m["closed"])

    def history(self, interval=None):
        key = interval or self._m["default_interval"]
        window = self._m["windows"][key]
        if window.get("append"):
            kept = ROLLING.setdefault(key, [])
            kept.extend(window["bars"])
            del kept[:max(0, len(kept) - window["keep"])]
            return Window(kept, window["total"])
        return Window(window["bars"], window["total"])

    # -- actions, queued into the reply ----------------------------------- #
    def subscribe(self, interval, **kw):
        self._actions.append({"op": "subscribe", "interval": interval, **kw})

    def buy(self, size=1, **kw):
        self._actions.append({"op": "buy", "size": size, **kw})

    def sell(self, size=1, **kw):
        self._actions.append({"op": "sell", "size": size, **kw})

    def close(self, **kw):
        self._actions.append({"op": "close", **kw})

    def cancel_all(self):
        self._actions.append({"op": "cancel_all"})

    def __getattr__(self, name):
        if name in RemoteContext.SCALARS and name in self._m:
            return self._m[name]
        raise AttributeError(
            f"the spike's RemoteContext does not implement ctx.{name}; the real "
            "one would push it or serve it"
        )


def load(path: str, class_name: str, origin: str = ""):
    """Import the strategy as the module it was, so relative imports resolve.

    Task 22 hit this in `scripts/replay.py` and it recurs here for the same
    reason: the calibration reference says `from ...types import NS_PER_SECOND`,
    and a module executed under a bare name has no package.

    It is also the spike's first real finding about what has to cross. The child
    needs `src.core.types` importable -- `Side`, `OrderType`, `TimeInForce`,
    `NS_PER_SECOND`, `Bar`. Those are frozen value types and constants: data,
    with no dataset, engine or file handle behind them. Nothing else from the
    harness is needed, which is the answer to "does the dataset have to cross".
    """
    origin = origin or path
    package = ""
    rel = origin
    if rel.startswith(str(Path(__file__).resolve().parents[4])):
        rel = str(Path(rel).relative_to(Path(__file__).resolve().parents[4]))
    if rel.endswith(".py"):
        package = rel[:-3].replace("/", ".").rsplit(".", 1)[0]
    name = f"{package}._remote_strategy" if package else "remote_strategy"

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return getattr(module, class_name)()


def main() -> None:
    read_fd = int(os.environ["SPIKE_READ_FD"])
    write_fd = int(os.environ["SPIKE_WRITE_FD"])
    strategy = None
    while True:
        try:
            message = recv(read_fd)
        except EOFError:
            return
        if message["kind"] == "load":
            strategy = load(message["path"], message["class"])
            send(write_fd, {"ok": True, "actions": []})
            continue
        if message["kind"] == "stop":
            send(write_fd, {"ok": True, "actions": []})
            return

        actions: list[dict] = []
        ctx = RemoteContext(message, actions)
        try:
            getattr(strategy, message["kind"])(ctx)
        except Exception as exc:                              # noqa: BLE001
            send(write_fd, {"ok": False, "actions": [],
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(limit=8)})
            continue
        send(write_fd, {"ok": True, "actions": actions})


if __name__ == "__main__":
    main()
