#!/usr/bin/env python3
"""Does the sandbox actually hold, in the prototype's exact configuration?

    python docs/reviews/task24/proto_sandbox_probe.py

A strategy that tries to rewrite a null table, open a socket and spawn a process
from `setup()`, run through the same protocol and the same profile as the
acceptance run. Unsandboxed it succeeds and the file is on disk; sandboxed the
attempt raises inside the child, comes back as `StrategyCrashed`, and nothing was
written. Finding 7's prevention half, per platform.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from docs.reviews.task24.proto.adapter import (  # noqa: E402
    StrategyCrashed, SubprocessStrategy,
)
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402

TARGET = pathlib.Path(tempfile.gettempdir()) / "task24-sandbox-probe.txt"

HOSTILE = '''
import pathlib, socket, subprocess

TARGET = pathlib.Path({target!r})


class Hostile:
    def setup(self, ctx):
        ctx.subscribe("5m")
        TARGET.write_text("a strategy wrote this\\n")

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
'''


def attempt(sandbox: bool) -> str:
    TARGET.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "hostile.py"
        path.write_text(HOSTILE.format(target=str(TARGET)))
        ds = SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120,
                                            vol_per_tick=0.15, seed=1))
        try:
            with SubprocessStrategy(path, "Hostile", sandbox=sandbox) as remote:
                Engine.for_test(ds, remote, split=Split.VALIDATION,
                                warmup_sessions=1, seed=1).run()
            outcome = "the run completed"
        except StrategyCrashed as exc:
            outcome = f"StrategyCrashed: {str(exc).splitlines()[0]}"
    return f"{outcome}   file on disk: {TARGET.exists()}"


def main() -> int:
    print(f"target  {TARGET}")
    print(f"  unsandboxed  {attempt(False)}")
    print(f"  sandboxed    {attempt(True)}")
    TARGET.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
