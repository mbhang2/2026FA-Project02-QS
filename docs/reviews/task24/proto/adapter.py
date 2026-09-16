"""The engine side of the boundary. A `Strategy` the engine cannot tell apart.

`SubprocessStrategy` implements the four callbacks. Each one serialises the part
of the context the strategy might read, sends one frame, blocks for the reply,
validates the actions against a whitelist and replays them onto the real context.
One round trip per callback: the reference strategy ignores the order ids that
`ctx.buy` returns, so nothing needs a second trip.

**Order ids are the loose end**, and the spike does not hide it. In-process
`ctx.buy()` returns the id at the call, allocated by the intent queue; across the
boundary the child would need either a pre-allocated block per callback or a
round trip per order. A block is easy and deterministic and is what the real
build should do. The reference strategy does not exercise it, so this prototype
returns nothing and says so rather than pretending.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO))

from docs.reviews.task24.proto.protocol import (  # noqa: E402
    ProtocolError, bar_row, recv, send, validate,
)


def sandbox_argv(argv: list[str]) -> list[str]:
    """Wrap the child in the macOS sandbox profile.

    The interpreter has to be allowed to exec by literal path, because
    `sandbox-exec` applies the profile *before* the exec that starts the child.
    Three paths, not one: the venv shim, its realpath, and the framework's
    `Python.app` stub that the framework build re-execs through. Discovering that
    took three attempts and it is the least portable thing in this file -- a real
    build should resolve them at start-up rather than hard-code them, and it is
    part of why the recommendation is Linux.
    """
    import os
    import tempfile

    real = os.path.realpath(argv[0])
    app = ("/Library/Frameworks/Python.framework/Versions/"
           f"{sys.version_info.major}.{sys.version_info.minor}"
           "/Resources/Python.app/Contents/MacOS/Python")
    literals = " ".join(f'(literal "{p}")' for p in dict.fromkeys([argv[0], real, app]))
    profile = (HERE.parent / "strategy.sb").read_text().replace(
        '(allow process-exec (literal "/PYTHON/"))',
        f"(allow process-exec {literals})",
    )
    handle = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False)
    handle.write(profile)
    handle.close()
    return ["sandbox-exec", "-f", handle.name, *argv]


class StrategyCrashed(RuntimeError):
    """The strategy process raised. In the real build this becomes a trial with
    `outcome: crashed` -- which is cleaner than the in-process version, because
    the engine is still standing to write the row."""


class SubprocessStrategy:
    """Speaks the engine's protocol on one side and a pipe on the other."""

    def __init__(self, strategy_path: Path, class_name: str, *,
                 window: int = 1, interval: str = "5m", incremental: bool = False,
                 sandbox: bool = False):
        self.strategy_path = Path(strategy_path)
        self.class_name = class_name
        self.window = window
        self.interval = interval
        # A rolling window is append-only, so re-sending it every callback sends
        # the same bars over and over. In incremental mode the parent sends only
        # bars the child has not seen -- one, almost always -- and the child keeps
        # its own deque of `window` size. The window a strategy can read is
        # unchanged; what changes is that its cost stops scaling with its size.
        self.incremental = incremental
        self.sandbox = sandbox
        self.messages = 0
        self.bytes_out = 0
        self._sent_bars = 0
        self._proc = None
        self._to_child = None
        self._from_child = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        to_child_r, to_child_w = os.pipe()
        to_parent_r, to_parent_w = os.pipe()
        os.set_inheritable(to_child_r, True)
        os.set_inheritable(to_parent_w, True)

        env = dict(os.environ,
                   SPIKE_READ_FD=str(to_child_r), SPIKE_WRITE_FD=str(to_parent_w),
                   PYTHONHASHSEED="0")
        argv = [sys.executable, str(HERE / "child.py")]
        if self.sandbox:
            argv = sandbox_argv(argv)
        self._proc = subprocess.Popen(
            argv, close_fds=False, env=env, cwd=str(REPO),
        )
        os.close(to_child_r)
        os.close(to_parent_w)
        self._to_child, self._from_child = to_child_w, to_parent_r
        self._exchange({"kind": "load", "path": str(self.strategy_path),
                        "class": self.class_name})

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._exchange({"kind": "stop"})
        except (EOFError, OSError):
            pass
        os.close(self._to_child)
        os.close(self._from_child)
        self._proc.wait(timeout=10)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- the wire ---------------------------------------------------------- #
    def _exchange(self, message: dict) -> list[dict]:
        import json

        self.messages += 1
        self.bytes_out += len(json.dumps(message))
        send(self._to_child, message)
        reply = recv(self._from_child)
        if not reply.get("ok", False):
            raise StrategyCrashed(
                f"{reply.get('error')}\n{reply.get('traceback', '')}"
            )
        return validate(reply.get("actions", []))

    # The scalars every run-time context answers, and only the ones that are
    # properties -- `sub_id` reads like one and is a method taking a subscription,
    # `seq` is deliberately absent because it is on the tick, not the context.
    # Discovering both by `AttributeError` and `TypeError` rather than from the
    # generated reference is exactly the kind of surprise the real build has to
    # enumerate rather than assume. The authority is `docs/design/context-api.md`
    # and the real adapter should be generated from it, for the same reason that
    # file is generated.
    # (original note kept:)
    # it is on the tick, not the context, and discovering that by `AttributeError`
    # rather than from the generated reference is exactly the kind of surprise
    # the real build has to enumerate rather than assume. The authority is
    # `docs/design/context-api.md`, and the real adapter should be generated from
    # it for the same reason that file is generated.
    SCALARS = ("ts", "session", "session_start", "session_end", "position",
               "actionable", "last_price", "realized_pnl", "unrealized_pnl",
               "equity", "average_entry_price")

    def _snapshot(self, ctx, kind: str) -> dict:
        """The part of the context that crosses.

        Scalars are cheap and are all sent; the window is the only thing worth
        thinking about, and it is the last `self.window` bars plus the true total
        length so `len()` stays exact on the far side.
        """
        message = {"kind": kind, "default_interval": self.interval, "windows": {}}
        for name in self.SCALARS:
            message[name] = getattr(ctx, name)
        message["working_order_ids"] = list(ctx.working_order_ids)
        message["closed"] = [bar_row(b) for b in ctx.closed]
        if self._subscribed:
            view = ctx.history(self.interval)
            total = len(view)
            if self.incremental:
                first = max(self._sent_bars, total - self.window)
                self._sent_bars = total
                message["windows"][self.interval] = {
                    "bars": [bar_row(view[i]) for i in range(first, total)],
                    "total": total, "append": True, "keep": self.window,
                }
            else:
                first = max(0, total - self.window)
                message["windows"][self.interval] = {
                    "bars": [bar_row(view[i]) for i in range(first, total)],
                    "total": total, "append": False, "keep": self.window,
                }
        return message

    def _apply(self, ctx, actions: list[dict]) -> None:
        for action in actions:
            op = action.pop("op")
            if op == "subscribe":
                ctx.subscribe(action.pop("interval"), **action)
                self._subscribed = True
            elif op in ("buy", "sell"):
                getattr(ctx, op)(**action)
            elif op == "close":
                ctx.close(**action)
            elif op == "cancel_all":
                ctx.cancel_all()
            else:                                    # pragma: no cover
                raise ProtocolError(f"validated but unhandled: {op}")

    # -- the Strategy protocol --------------------------------------------- #
    _subscribed = False

    def setup(self, ctx) -> None:
        self._subscribed = False
        self._apply(ctx, self._exchange({
            "kind": "setup", "ts": 0, "seq": 0, "session": 0, "session_end": 0,
            "position": 0, "working_order_ids": [], "actionable": False,
            "last_price": 0.0, "realized_pnl": 0.0,
            "default_interval": self.interval, "windows": {},
        }))

    def on_bars(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_bars")))

    def on_fill(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_fill")))

    def on_lifecycle(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_lifecycle")))
