"""The engine side of the boundary. A `Strategy` the engine cannot tell apart.

`RemoteStrategy` implements the four callbacks. Each one serialises the context
through the generated surface, sends one frame, blocks for the reply, validates
the actions against a whitelist and replays them onto the real context. One round
trip per callback -- order ids come from a pre-allocated block, so `ctx.buy()`
still returns synchronously on the far side.

`engine.py` does not change. The adapter *is* a `Strategy`, which is the property
the Task 24 spike was built to demonstrate and the reason this refactor is small.

**Isolation is per-platform and the run record says which.** On Linux the child
runs under an unprivileged user namespace with a read-only bind mount; on macOS
under `sandbox-exec`, which is deprecated and cannot set a memory ceiling at all.
`isolation_level()` reports what was actually achieved, not what was intended.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...types import Subscription
from . import _surface
from .protocol import (
    ACTIONS, MAX_SETUP_BYTES, ProtocolError, SetupPayloadTooLarge, encode,
    recv, send,
)

HERE = Path(__file__).resolve().parent
from ..._roots import repo_root

# Was `HERE.parents[3]`. A depth is a fact about where this file sits
# today; six of these broke silently when Task 44 moved three trees.
REPO_ROOT = repo_root()
CHILD = HERE / "child.py"


# --------------------------------------------------------------------------- #
# what went wrong, as five distinguishable things
# --------------------------------------------------------------------------- #
# The distinction that matters is the last one. A harness bug that lands in the
# ledger as a crashed trial inflates N, and N is the denominator of every
# corrected p-value the project produces -- so "the strategy broke" and "we
# broke" must never be the same row.
class RemoteFailure(RuntimeError):
    """Base. `kind` is what the run record records."""

    kind = "harness_failed"


class StrategyRaised(RemoteFailure):
    """The strategy raised. The child's traceback is preserved verbatim: an agent
    debugging against a truncated traceback is debugging against fiction."""

    kind = "strategy_raised"

    def __init__(self, message: str, child_traceback: str = ""):
        super().__init__(message)
        self.child_traceback = child_traceback


class StrategyTimedOut(RemoteFailure):
    kind = "strategy_timed_out"


class StrategyExceededResources(RemoteFailure):
    kind = "strategy_exceeded_resources"


class StrategyViolatedProtocol(RemoteFailure):
    kind = "strategy_violated_protocol"


class HarnessFailed(RemoteFailure):
    """Not a trial. The engine, the pipe or the platform failed, and a row that
    called this a crashed trial would be counting our bug as the agent's."""

    kind = "harness_failed"

    def __init__(self, message: str, *, child_exit_status: int | None = None,
                 completed_callbacks: int | None = None):
        super().__init__(message)
        self.child_exit_status = child_exit_status
        self.completed_callbacks = completed_callbacks


# The harness modules the strategy process is allowed to have loaded. Twice now
# an `__init__` re-export has quietly widened the boundary -- Task 24 found the
# child holding the whole simulator, Task 25 found it again after the fix -- and
# both times a graph walk caught it because someone happened to run one. This is
# the version that fails by name on the next import instead of waiting for a
# review pass.
#
# `src.core.types` is the whole of it: frozen value types, enums and constants.
# The child loads its own `protocol` and `_surface` by file path under `_remote_`
# names precisely so they do not appear here as packages that drag `__init__`
# files with them.
CHILD_MODULE_ALLOWLIST = frozenset({
    "src",
    "src.core",
    "src.core.types",
    "src.strategies",
})

# Prefixes that must never appear, listed separately from the allowlist so the
# failure message can say *what* leaked rather than only that something did.
CHILD_MODULE_FORBIDDEN = (
    "src.core.engine.engine",
    "src.core.engine.context",
    "src.core.data",
    "src.core.scoring.scorer",
    "src.core.scoring.metrics",
    "src.core.scoring.calibration",
    "src.core.runlog",
)

# BSD `sysexits.h`: these are launcher/configuration failures, not a strategy
# traceback. A child that exits in this range before returning from any callback
# never executed strategy code, so charging it to a proposal would inflate N.
SYSEXITS = range(64, 79)


@dataclass
class Isolation:
    """What was actually achieved, per platform. Recorded in every run record.

    Each restriction is a `(state, mechanism)` pair rather than prose, so a row
    can be filtered on `isolation.network.active` without parsing English. The
    prose survives as `mechanism`, because "denied" without saying by what is the
    kind of claim that is true until someone changes a config file.
    """

    level: str                    # "isolated" | "best-effort" | "none"
    platform: str
    filesystem: str
    network: str
    memory_ceiling: str
    cpu_ceiling: str
    detail: str = ""
    wall_clock: str = "the engine's own budget, enforced by the parent"

    def _restriction(self, mechanism: str) -> dict:
        inactive = ("unrestricted", "unavailable", "none", "unknown")
        active = not any(mechanism.startswith(word) for word in inactive)
        return {"active": active, "mechanism": mechanism}

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "platform": self.platform,
            "detail": self.detail,
            "filesystem": self._restriction(self.filesystem),
            "network": self._restriction(self.network),
            "address_space": self._restriction(self.memory_ceiling),
            "cpu": self._restriction(self.cpu_ceiling),
            "wall_clock": self._restriction(self.wall_clock),
        }


def linux_mechanism() -> str:
    """Which sandbox tool this machine actually has.

    Probed rather than remembered, and probed by `isolation_for` as well as by
    the adapter: a reporting function that only knows what it was told reports
    `none` on a machine where the mechanism works, which is how a correct system
    ends up with rows that understate it.
    """
    import shutil

    if shutil.which("bwrap"):
        return "bubblewrap: --ro-bind / /, --unshare-net"
    if shutil.which("unshare"):
        return "unshare: user+mount+net namespace, remount ro (weaker)"
    return ""


def isolation_for(system: str | None = None, *, disabled: bool = False,
                  mechanism: str = "") -> Isolation:
    """The honest description, not a single claim meaning two things.

    macOS cannot set `RLIMIT_AS`, `RLIMIT_DATA` or `RLIMIT_RSS` at all -- Task 24
    measured `ValueError: current limit exceeds maximum limit` with either hard
    limit -- and `sandbox-exec` has been documented as deprecated for a decade.
    Both are true and neither is fatal for development; both are why scored
    campaigns should run on Linux.
    """
    system = system or platform.system()
    if disabled:
        return Isolation(
            level="none", platform=system, filesystem="unrestricted",
            network="unrestricted", memory_ceiling="none", cpu_ceiling="none",
            detail="isolation was switched off with HARNESS_NO_SANDBOX; this row "
                   "is not evidence of anything",
        )
    if system == "Linux":
        # Probe only when the caller did not say. `mechanism="none"` is how a
        # caller asks about a machine with no sandbox tool without pretending to
        # be on one.
        if not mechanism:
            mechanism = linux_mechanism() if platform.system() == "Linux" else ""
        if mechanism == "none":
            mechanism = ""
        if mechanism.startswith("bubblewrap"):
            return Isolation(
                level="isolated", platform=system,
                filesystem="read-only bind mount, unprivileged user namespace "
                           "(bubblewrap)",
                network="empty network namespace (bubblewrap --unshare-net)",
                memory_ceiling="RLIMIT_AS", cpu_ceiling="RLIMIT_CPU",
            )
        if mechanism.startswith("unshare"):
            return Isolation(
                level="best-effort", platform=system,
                filesystem="unshare + remount ro (weaker: a shell command, not "
                           "one syscall)",
                network="empty network namespace (unshare --net)",
                memory_ceiling="RLIMIT_AS", cpu_ceiling="RLIMIT_CPU",
                detail="bubblewrap is not installed; see docs/setup/LINUX_RUNNER.md",
            )
        return Isolation(
            level="none", platform=system,
            filesystem="unrestricted: no bubblewrap and no unshare on PATH",
            network="unrestricted", memory_ceiling="RLIMIT_AS",
            cpu_ceiling="RLIMIT_CPU",
            detail="install bubblewrap; see docs/setup/LINUX_RUNNER.md",
        )
    if system == "Darwin":
        return Isolation(
            level="best-effort", platform=system,
            filesystem="sandbox-exec profile (deprecated)",
            network="denied by the same profile",
            memory_ceiling="unavailable: RLIMIT_AS/DATA/RSS cannot be set",
            cpu_ceiling="RLIMIT_CPU",
            detail="scored campaigns should run on Linux; see "
                   "docs/design/architecture.md",
        )
    return Isolation(level="none", platform=system, filesystem="unrestricted",
                     network="unrestricted", memory_ceiling="unknown",
                     cpu_ceiling="unknown",
                     detail="no isolation implementation for this platform")


@dataclass
class RemoteStrategy:
    """Speaks the engine's protocol on one side and a pipe on the other."""

    strategy_path: Path
    class_name: str = "Strategy"
    window: int = 512
    tick_window: int = 512
    action_cap: int = 64
    timeout_s: float | None = 900.0
    cpu_seconds: int | None = 1800
    memory_bytes: int | None = 4 * 1024 * 1024 * 1024
    sandbox: bool = True

    messages: int = field(default=0, init=False)
    bytes_out: int = field(default=0, init=False)
    isolation: Isolation = field(default=None, init=False)  # type: ignore[assignment]

    _proc: object = field(default=None, init=False, repr=False)
    _to_child: int = field(default=-1, init=False, repr=False)
    _from_child: int = field(default=-1, init=False, repr=False)
    _sent: dict = field(default_factory=dict, init=False, repr=False)
    _sent_ticks: int = field(default=0, init=False, repr=False)
    _sub_ids: dict = field(default_factory=dict, init=False, repr=False)
    _next_id: int = field(default=1, init=False, repr=False)
    _setup_ctx: object = field(default=None, init=False, repr=False)
    _setup_bytes: int = field(default=0, init=False, repr=False)
    _mechanism: str = field(default="", init=False, repr=False)
    completed_callbacks: int = field(default=0, init=False)

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        # `HARNESS_NO_SANDBOX` is the control arm, and it is deliberately not a
        # flag on the scored path: a switch a caller can pass is a switch the
        # loop driver can pass by mistake. It exists so the probes in
        # `docs/reviews/task26/` can show the network is reachable *without*
        # isolation -- three refusals prove nothing if the machine is offline --
        # and the run record says `level: none` when it is set, so a row produced
        # this way declares itself.
        if os.environ.get("HARNESS_NO_SANDBOX"):
            self.sandbox = False
        argv = [sys.executable, str(CHILD)]
        if self.sandbox:
            argv = self._wrap(argv)
        self.isolation = isolation_for(disabled=not self.sandbox,
                                       mechanism=self._mechanism)
        to_child_r, to_child_w = os.pipe()
        to_parent_r, to_parent_w = os.pipe()
        os.set_inheritable(to_child_r, True)
        os.set_inheritable(to_parent_w, True)

        env = dict(os.environ, REMOTE_READ_FD=str(to_child_r),
                   REMOTE_WRITE_FD=str(to_parent_w), PYTHONHASHSEED="0")
        try:
            self._proc = subprocess.Popen(
                argv, close_fds=False, env=env, cwd=str(REPO_ROOT),
                preexec_fn=self._limits,
            )
        except OSError as exc:
            raise HarnessFailed(f"could not start the strategy process: {exc}")
        os.close(to_child_r)
        os.close(to_parent_w)
        self._to_child, self._from_child = to_child_w, to_parent_r
        self._exchange({"kind": "load", "path": str(self.strategy_path),
                        "class": self.class_name})

    def _limits(self) -> None:                          # pragma: no cover
        """In the child, after fork, before exec.

        A hang or a balloon becomes a crashed trial rather than a wedged machine,
        which is cleaner than anything available in process. Memory is best-effort
        because macOS refuses the call outright.
        """
        import resource

        if self.cpu_seconds:
            try:
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (self.cpu_seconds, self.cpu_seconds))
            except (ValueError, OSError):
                pass
        if self.memory_bytes:
            for name in ("RLIMIT_AS", "RLIMIT_DATA"):
                limit = getattr(resource, name, None)
                if limit is None:
                    continue
                try:
                    resource.setrlimit(limit, (self.memory_bytes, self.memory_bytes))
                    break
                except (ValueError, OSError):
                    continue

    def _wrap(self, argv: list[str]) -> list[str]:
        if platform.system() == "Linux":
            return self._wrap_linux(argv)
        if platform.system() != "Darwin":
            return argv
        import tempfile

        profile_path = REPO_ROOT / "config" / "strategy.sb"
        if not profile_path.exists():
            return argv
        real = os.path.realpath(argv[0])
        app = ("/Library/Frameworks/Python.framework/Versions/"
               f"{sys.version_info.major}.{sys.version_info.minor}"
               "/Resources/Python.app/Contents/MacOS/Python")
        literals = " ".join(f'(literal "{p}")'
                            for p in dict.fromkeys([argv[0], real, app]))
        text = profile_path.read_text().replace(
            '(allow process-exec (literal "/PYTHON/"))',
            f"(allow process-exec {literals})")
        handle = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False)
        handle.write(text)
        handle.close()
        return ["sandbox-exec", "-f", handle.name, *argv]

    def _wrap_linux(self, argv: list[str]) -> list[str]:
        """Unprivileged user namespace, read-only bind mount, no network.

        `bubblewrap` first: it is the standard unprivileged sandbox on Linux, it
        needs no root, and `--ro-bind / /` plus `--unshare-net` is exactly the
        recommendation the Task 24 spike made. `--die-with-parent` matters more
        than it looks -- a child that outlives a crashed engine is a strategy
        still running with nothing collecting its output.

        `unshare` is the fallback, and it is weaker: it gets the namespaces but
        the remount has to happen inside, so the read-only guarantee depends on a
        shell command rather than on one syscall the kernel either allowed or
        did not. `isolation_for` reports which one was used, because a row that
        says "isolated" without saying by what is a claim nobody can check.
        """
        import shutil

        writable = REPO_ROOT / "runs"
        if shutil.which("bwrap"):
            self._mechanism = "bubblewrap: --ro-bind / /, --unshare-net"
            return [
                "bwrap",
                "--ro-bind", "/", "/",
                "--dev", "/dev",
                "--proc", "/proc",
                # No `--tmpfs /tmp`. A writable scratch is one the child does not
                # need, and mounting one *shadows the real /tmp* -- which cost an
                # afternoon: probe strategies written to a temporary directory
                # vanished inside the sandbox and every probe "passed" with
                # FileNotFoundError. Read-only everywhere is both stricter and
                # simpler to reason about.
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--die-with-parent",
                "--new-session",
                "--chdir", str(REPO_ROOT),
                "--", *argv,
            ]
        if shutil.which("unshare"):
            self._mechanism = ("unshare: user+mount+net namespace, remount ro "
                               "(weaker than bubblewrap)")
            inner = (
                "mount --make-rprivate / 2>/dev/null; "
                f"mount -o remount,ro,bind {REPO_ROOT} {REPO_ROOT} 2>/dev/null; "
                'exec "$@"'
            )
            return ["unshare", "--user", "--map-root-user", "--mount", "--net",
                    "--ipc", "--uts", "--fork",
                    "/bin/sh", "-c", inner, "sh", *argv]
        self._mechanism = ""
        return argv

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._exchange({"kind": "stop"})
        except Exception:                                     # noqa: BLE001
            pass
        for fd in (self._to_child, self._from_child):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._proc.wait(timeout=10)
        except Exception:                                     # noqa: BLE001
            self._proc.kill()
        self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- the wire ---------------------------------------------------------- #
    def _exchange(self, message: dict) -> list[dict]:
        import json

        blob = json.dumps(message)
        self.messages += 1
        self.bytes_out += len(blob)
        deadline = None if self.timeout_s is None else time.monotonic() + self.timeout_s
        try:
            send(self._to_child, message)
            while True:
                reply = recv(self._from_child)
                if reply.get("reply") == "pull":
                    send(self._to_child, self._serve(reply))
                    continue
                break
        except EOFError:
            raise self._died()
        except OSError as exc:
            raise HarnessFailed(f"the pipe failed: {exc}")
        if deadline is not None and time.monotonic() > deadline:
            raise StrategyTimedOut(
                f"the strategy took longer than {self.timeout_s}s in one callback"
            )
        if not reply.get("ok", False):
            raise StrategyRaised(reply.get("error", "the strategy raised"),
                                 reply.get("traceback", ""))
        if message.get("kind") in {"setup", "on_bars", "on_fill",
                                   "on_lifecycle", "on_tick"}:
            self.completed_callbacks += 1
        try:
            return validate_actions(reply.get("actions", []))
        except ProtocolError as exc:
            raise StrategyViolatedProtocol(str(exc))

    def _died(self) -> RemoteFailure:
        """The child closed the pipe. Which of the four it was comes from how."""
        code = self._proc.poll() if self._proc else None
        if code is None:
            return HarnessFailed("the child closed the pipe while still running",
                                 completed_callbacks=self.completed_callbacks)
        if code < 0:
            signal = -code
            if signal == 24:                                  # SIGXCPU
                return StrategyExceededResources(
                    f"the strategy exceeded its CPU limit of {self.cpu_seconds}s"
                )
            if signal == 9:
                return StrategyExceededResources(
                    "the strategy process was killed, most likely out of memory"
                )
            return StrategyExceededResources(f"the child died on signal {signal}")
        if code in SYSEXITS and self.completed_callbacks == 0:
            return HarnessFailed(
                "the child exited before strategy execution: empty traceback, "
                f"sysexits status {code}, completed callbacks 0",
                child_exit_status=code,
                completed_callbacks=self.completed_callbacks,
            )
        return StrategyRaised(f"the child exited with status {code}")

    # -- setup pulls ------------------------------------------------------- #
    def _serve(self, request: dict) -> dict:
        import json

        what, args = request.get("what"), request.get("args", {})
        ctx = self._setup_ctx
        if ctx is None:
            return {"ok": False, "rows": [],
                    "error": f"{what} is only available during setup()"}
        try:
            if what == "ticks":
                rows = [encode(t) for t in ctx.ticks()]
            elif what == "train_bars":
                rows = [encode(b) for b in ctx.train_bars(args["interval"])]
            else:
                return {"ok": False, "rows": [], "error": f"unknown pull {what!r}"}
        except Exception as exc:                              # noqa: BLE001
            return {"ok": False, "rows": [], "error": f"{type(exc).__name__}: {exc}"}

        size = len(json.dumps(rows))
        self._setup_bytes += size
        if self._setup_bytes > MAX_SETUP_BYTES:
            raise SetupPayloadTooLarge(
                f"setup() asked for {self._setup_bytes / 1e6:,.0f} MB and the cap "
                f"is {MAX_SETUP_BYTES / 1e6:,.0f} MB. The 384-session NQ training "
                f"split is 8.0 GB as raw ticks and 12.7 MB as 5m bars: use "
                f"ctx.train_bars(interval), which streams the same data through "
                f"the same aggregator the live walk uses."
            )
        return {"ok": True, "rows": rows}

    # -- snapshots --------------------------------------------------------- #
    def _id_block(self) -> list[int]:
        """The ids this callback's orders may use. Retired, never reused."""
        block = list(range(self._next_id, self._next_id + self.action_cap))
        self._next_id += self.action_cap
        return block

    def _snapshot(self, ctx, kind: str) -> dict:
        message = {"kind": kind, "id_block": self._id_block(),
                   "scalars": {}, "windows": {}}
        for name in _surface.BASECONTEXT_SCALARS:
            message["scalars"][name] = _jsonable(getattr(ctx, name))
        message["now"] = ctx.now.isoformat()
        message["closed"] = [encode(b) for b in ctx.closed]
        message["fills"] = [encode(f) for f in ctx.fills]
        message["working_orders"] = [encode(o) for o in ctx.working_orders]
        if kind == "on_fill":
            message["fill"] = encode(ctx.fill)
        elif kind == "on_lifecycle":
            message["event"] = encode(ctx.event)
        elif kind == "on_tick":
            message["tick"] = encode(ctx.tick)

        if self._sub_ids:
            message["sub_ids"] = self._sub_ids
        for sid in self._bar_subs:
            view = ctx.history(sid)
            total = len(view)
            sent = self._sent.get(sid, 0)
            first = max(sent, total - self.window)
            self._sent[sid] = total
            message["windows"][sid] = {
                "bars": [encode(view[i]) for i in range(first, total)],
                "total": total, "keep": self.window,
            }
        if self._tick_sub:
            view = ctx.ticks()
            total = len(view)
            first = max(self._sent_ticks, total - self.tick_window)
            self._sent_ticks = total
            message["tick_window"] = {
                "ticks": [encode(view[i]) for i in range(first, total)],
                "total": total, "keep": self.tick_window,
            }
        return message

    # -- replay ------------------------------------------------------------ #
    def _apply(self, ctx, actions: list[dict]) -> None:
        for action in actions:
            action = dict(action)
            op = action.pop("op")
            try:
                if op == "subscribe":
                    ctx.subscribe(action.pop("interval"), **action)
                elif op in ("buy", "sell"):
                    getattr(ctx, op)(**action)
                elif op == "close":
                    ctx.close(**action)
                elif op == "cancel":
                    ctx.cancel(action["order_id"])
                elif op == "cancel_all":
                    ctx.cancel_all()
                elif op == "modify":
                    ctx.modify(action.pop("order_id"), **action)
                else:                                        # pragma: no cover
                    raise ProtocolError(f"validated but unhandled: {op}")
            except TypeError as exc:
                raise StrategyViolatedProtocol(f"{op}: {exc}")

    # -- the Strategy protocol --------------------------------------------- #
    _bar_subs: tuple = ()
    _tick_sub: bool = False

    def setup(self, ctx) -> None:
        self._setup_ctx = ctx
        try:
            actions = self._exchange({
                "kind": "setup", "dataset_name": ctx.dataset_name,
                "declared": [], "id_block": [],
            })
            self._apply(ctx, actions)
        finally:
            self._setup_ctx = None
        subs = ctx.subscriptions()
        self._bar_subs = tuple(s.sub_id for s in subs if s.is_bars)
        self._tick_sub = any(not s.is_bars for s in subs)
        # `ctx.sub_id("5m")` on the far side resolves through this map: every
        # spelling the strategy used, plus the canonical id, pointing at the
        # canonical id. The resolver stays on the engine, which is what makes an
        # unsubscribed id an error rather than a silent miss.
        self._sub_ids = {s.sub_id: s.sub_id for s in subs}
        for action in actions:
            if action.get("op") == "subscribe":
                spelling = str(action["interval"])
                match = _resolve(spelling, subs)
                if match:
                    self._sub_ids[spelling] = match

    def on_bars(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_bars")))

    def on_fill(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_fill")))

    def on_lifecycle(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_lifecycle")))

    def on_tick(self, ctx) -> None:
        self._apply(ctx, self._exchange(self._snapshot(ctx, "on_tick")))


def _resolve(spelling: str, subs) -> str:
    from ..context import canonical_sub_id

    try:
        sid = canonical_sub_id(spelling)
    except Exception:                                         # noqa: BLE001
        return ""
    return sid if any(s.sub_id == sid for s in subs) else ""


def _jsonable(value):
    if isinstance(value, tuple):
        return list(value)
    return value


def validate_actions(actions) -> list[dict]:
    if not isinstance(actions, list):
        raise ProtocolError(f"actions must be a list, got {type(actions).__name__}")
    out = []
    for action in actions:
        if not isinstance(action, dict) or "op" not in action:
            raise ProtocolError(f"malformed action {action!r}")
        op = action["op"]
        if op not in ACTIONS:
            raise ProtocolError(f"unknown action {op!r}; allowed: {sorted(ACTIONS)}")
        extra = set(action) - {"op"} - ACTIONS[op]
        if extra:
            raise ProtocolError(f"{op} does not take {sorted(extra)}")
        out.append(action)
    return out
