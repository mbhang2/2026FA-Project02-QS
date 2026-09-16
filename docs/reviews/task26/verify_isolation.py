#!/usr/bin/env python3
"""Verify isolation where it is claimed. Runs on either platform; reports both.

    python docs/reviews/task26/verify_isolation.py

The architecture said scored runs happen on Linux and no run had ever happened on
Linux. This is the list that makes that a measurement: goldens through the
boundary, the escape walk inside the child, filesystem probes, network probes,
and the resource ceilings -- `RLIMIT_AS` in particular, which is the specific gap
macOS cannot close.

Every probe is a real run through `Engine` and `RemoteStrategy`, not a check of
the configuration that was meant to produce it. A configuration test passes on a
machine where the mechanism silently does nothing.
"""

from __future__ import annotations

import json
import pathlib
import platform
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.engine.remote import (  # noqa: E402
    RemoteFailure, RemoteStrategy, StrategyExceededResources, StrategyRaised,
    isolation_for,
)
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.types import Split  # noqa: E402

REFERENCE = REPO / "src" / "core" / "scoring" / "calibration" / "reference.py"
GOLDEN = json.loads((REPO / "tests" / "golden" / "pre_context_refactor.json").read_text())
IRREGULAR = json.loads((REPO / "tests" / "golden" / "irregular_generator.json").read_text())

PASS, FAIL, EXPECTED = "  ok  ", " FAIL ", " n/a  "


def hostile(body: str, **kw):
    """Run a one-off strategy through the real boundary; return the failure.

    The probe lives in the ordinary temp directory, which the child can *read*
    because the sandbox binds the whole filesystem read-only rather than mounting
    a fresh tmpfs over `/tmp`. The tmpfs version cost an afternoon: probes
    written to `/tmp` did not exist inside the child, so every probe "passed"
    with `FileNotFoundError` -- the most dangerous kind of green there is.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "probe.py"
        path.write_text(
            "import socket, urllib.request, pathlib\n"
            "class Strategy:\n"
            "    def setup(self, ctx): ctx.subscribe('5m')\n"
            f"{body}"
            "    def on_fill(self, ctx): pass\n"
            "    def on_lifecycle(self, ctx): pass\n"
        )
        ds = SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120,
                                            vol_per_tick=0.15, seed=1))
        try:
            with RemoteStrategy(path, "Strategy", **kw) as remote:
                Engine.for_test(ds, remote, split=Split.VALIDATION,
                                warmup_sessions=1, seed=1).run()
            return None
        except RemoteFailure as exc:
            return exc
        finally:
            pass


def check(label: str, ok: bool, detail: str = "", *, declared: bool = True) -> bool:
    """`declared` is whether the run record claims this restriction is active.

    A platform with no memory ceiling that *says so* is not a failure; a platform
    that claims one and does not have it is the worst outcome available. So the
    two are separated: `n/a` means the row already told the reader, and the check
    still fails if the mechanism is claimed and absent.
    """
    if not declared:
        print(f"{EXPECTED} {label:<52} not claimed on this platform; {detail}")
        return True
    print(f"{PASS if ok else FAIL} {label:<52} {detail}")
    return ok


def goldens() -> bool:
    ok = True
    for name, cases in (("uniform", GOLDEN["cases"]),
                        ("irregular", IRREGULAR["cases"])):
        for case in cases:
            for window in (1, 512):
                with RemoteStrategy(REPO / "src" / "strategies" / "current.py",
                                    "MomentumStrategy", window=window) as remote:
                    engine = Engine.for_test(
                        SyntheticDataset(SyntheticSpec(**case["spec"])), remote,
                        split=Split.VALIDATION, warmup_sessions=1,
                        fill_model=case["fill_model"], seed=case["spec"]["seed"],
                    )
                    engine.run()
                ok &= engine.blotter.digest() == case["blotter_digest"]
        print(f"       {name}: {len(cases)} cases x 2 windows")
    return ok


def escape_walk() -> tuple[bool, dict]:
    """The graph walk, inside the child, on this platform."""
    from src.core.engine.remote.protocol import recv
    import os
    import subprocess

    walk = (REPO / "docs" / "reviews" / "task26" / "_walk_in_child.py")
    to_child_r, to_child_w = os.pipe()
    to_parent_r, to_parent_w = os.pipe()
    os.set_inheritable(to_child_r, True)
    os.set_inheritable(to_parent_w, True)
    proc = subprocess.Popen(
        [sys.executable, str(walk), str(REPO),
         str(REPO / "src" / "strategies" / "current.py")],
        close_fds=False, cwd=str(REPO),
        env=dict(os.environ, REMOTE_READ_FD=str(to_child_r),
                 REMOTE_WRITE_FD=str(to_parent_w)),
    )
    os.close(to_child_r)
    os.close(to_parent_w)
    reply = recv(to_parent_r)
    os.close(to_child_w)
    os.close(to_parent_r)
    proc.wait(timeout=30)
    return (not reply["hits"] and reply["walked"] > 1000), reply


def main() -> int:
    iso = isolation_for()
    print(f"platform   {platform.system()} {platform.machine()}  "
          f"python {platform.python_version()}")
    print(f"isolation  {iso.level}")
    print(f"           filesystem: {iso.filesystem}")
    print(f"           network:    {iso.network}")
    print(f"           memory:     {iso.memory_ceiling}")
    print()

    ok = True

    # POSITIVE CONTROLS. Task 26's `--tmpfs /tmp` bug made every filesystem probe
    # pass with FileNotFoundError -- all-clear because nothing ran. A check where
    # *everything* fails is indistinguishable from a check that did not execute,
    # so each group below is preceded by something that must SUCCEED.
    print("positive controls -- these must succeed, or nothing below means "
          "anything")
    exc = hostile("    def on_bars(self, ctx):\n        pass\n")
    ok &= check("control: an ordinary strategy runs to completion", exc is None,
                "the harness itself is working" if exc is None
                else f"NOTHING RAN: {exc}")
    exc = hostile("    def on_bars(self, ctx):\n"
                  "        pathlib.Path(ctx.__class__.__name__)\n"
                  "        raise ValueError('control')\n")
    ok &= check("control: a raising strategy is reported as raising",
                isinstance(exc, StrategyRaised) and "control" in str(exc),
                str(exc).splitlines()[0][:56] if exc else "NOT DETECTED")
    exc = hostile("    def on_bars(self, ctx):\n"
                  f"        open({str(REPO / 'pyproject.toml')!r}).read(8)\n")
    ok &= check("control: reading an allowed file still works", exc is None,
                "the sandbox permits reads" if exc is None
                else f"READS ARE BROKEN, not just writes: {exc}")
    print()

    ok &= check("goldens byte-identical through the boundary", goldens())

    walked, reply = escape_walk()
    ok &= check("escape walk inside the child", walked,
                f"{reply['walked']:,} objects, hits: {reply['hits'] or 'none'}")
    ok &= check("module allowlist holds in the child",
                set(reply["src_modules"]) <= {
                    "src", "src.core", "src.core.types", "src.strategies",
                    "src.strategies._remote_strategy"},
                ", ".join(reply["src_modules"]))

    print()
    for target in ("config/run.yaml", "runs/ledger.jsonl",
                   "data/calibration/empirical-null.json",
                   "src/strategies/current.py"):
        exc = hostile(
            "    def on_bars(self, ctx):\n"
            f"        pathlib.Path({str(REPO / target)!r}).write_text('x')\n")
        # Two kernels, two errnos for the same refusal: macOS `sandbox-exec`
        # returns EPERM, a Linux read-only bind mount returns EROFS. Matching on
        # one of them makes the check a platform test.
        message = str(exc)
        blocked = isinstance(exc, StrategyRaised) and (
            "ermission" in message or "Read-only file system" in message)
        ok &= check(f"write to {target}", blocked,
                    message.splitlines()[0][:56] if exc else "THE WRITE SUCCEEDED")

    print()
    for name, probe in (
        ("DNS", "socket.getaddrinfo('example.com', 80)"),
        ("raw TCP to a literal IP", "socket.create_connection(('1.1.1.1',80),timeout=5)"),
        ("HTTP via urllib", "urllib.request.urlopen('http://1.1.1.1/',timeout=5)"),
    ):
        exc = hostile(f"    def on_bars(self, ctx):\n        {probe}\n")
        ok &= check(f"network: {name}", exc is not None,
                    str(exc).splitlines()[0][:56] if exc else "REACHED THE NETWORK")

    print()
    exc = hostile(
        "    def on_bars(self, ctx):\n"
        "        self.b = getattr(self, 'b', [])\n"
        "        self.b.append(bytearray(64 * 1024 * 1024))\n",
        memory_bytes=256 * 1024 * 1024)
    bound = isinstance(exc, (StrategyExceededResources, StrategyRaised))
    ok &= check("RLIMIT_AS binds (allocate without bound)", bound,
                str(exc).splitlines()[0][:56] if exc else "allocated freely",
                declared=iso.as_dict()["address_space"]["active"])

    exc = hostile("    def on_bars(self, ctx):\n        pass\n", cpu_seconds=1)
    ok &= check("RLIMIT_CPU is accepted and the run still completes",
                exc is None or isinstance(exc, StrategyExceededResources),
                str(exc).splitlines()[0][:56] if exc else "completed")

    print()
    print(json.dumps(iso.as_dict(), indent=2, sort_keys=True))
    print()
    print("ALL CHECKS PASSED" if ok else "SOMETHING DID NOT HOLD")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
