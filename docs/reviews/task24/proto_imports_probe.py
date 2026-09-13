#!/usr/bin/env python3
"""Does anything about the dataset or the engine have to cross the boundary?

    python docs/reviews/task24/proto_imports_probe.py

The child reports every `src.*` module in its own `sys.modules` after a run, and
whether it holds any engine, dataset or file handle. If the answer is "value
types only", finding 2 -- `_SealedDataset` forwarding the raw dataset through
`__getattr__` and `gc.get_referents` -- closes as a side effect of isolation
rather than by patching the path, which is the argument for doing this at all.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from docs.reviews.task24.proto.protocol import recv, send  # noqa: E402

PROBE = r'''
import gc, json, os, sys
from pathlib import Path
sys.path.insert(0, %r)
from docs.reviews.task24.proto.protocol import recv, send
from docs.reviews.task24.proto.child import RemoteContext, load

read_fd, write_fd = int(os.environ["SPIKE_READ_FD"]), int(os.environ["SPIKE_WRITE_FD"])
strategy = load(%r, "ReferenceStrategy")
actions = []
ctx = RemoteContext({"kind": "setup", "default_interval": "5m", "windows": {},
                     "working_order_ids": [], "closed": []}, actions)
strategy.setup(ctx)

FORBIDDEN = ("Engine", "SyntheticDataset", "NQDataset", "Blotter", "HoldoutLock",
             "_SealedDataset", "_RunState", "_OrderBook")
seen, stack, hits = set(), [("strategy", strategy), ("ctx", ctx)], []
while stack:
    path, obj = stack.pop()
    if id(obj) in seen or len(seen) > 200000:
        continue
    seen.add(id(obj))
    if type(obj).__name__ in FORBIDDEN:
        hits.append(f"{path} -> {type(obj).__name__}")
        continue
    if isinstance(obj, (str, bytes, int, float, bool, type(None))):
        continue
    if type(obj).__name__ in ("module", "code"):
        continue
    for ref in gc.get_referents(obj):
        stack.append((path + ".<ref>", ref))

send(write_fd, {"ok": True, "actions": [],
                "src_modules": sorted(m for m in sys.modules if m.startswith("src.")),
                "open_fds": sorted(int(f) for f in os.listdir("/dev/fd")
                                   if f.isdigit()),
                "reachable_forbidden": hits,
                "objects_walked": len(seen)})
'''


def main() -> int:
    reference = REPO / "src" / "core" / "scoring" / "calibration" / "reference.py"
    with tempfile.TemporaryDirectory() as tmp:
        script = pathlib.Path(tmp) / "probe.py"
        script.write_text(PROBE % (str(REPO), str(reference)))

        to_child_r, to_child_w = os.pipe()
        to_parent_r, to_parent_w = os.pipe()
        os.set_inheritable(to_child_r, True)
        os.set_inheritable(to_parent_w, True)
        proc = subprocess.Popen(
            [sys.executable, str(script)], close_fds=False, cwd=str(REPO),
            env=dict(os.environ, SPIKE_READ_FD=str(to_child_r),
                     SPIKE_WRITE_FD=str(to_parent_w)),
        )
        os.close(to_child_r)
        os.close(to_parent_w)
        reply = recv(to_parent_r)
        os.close(to_child_w)
        os.close(to_parent_r)
        proc.wait(timeout=10)

    print(f"objects walked in the child   {reply['objects_walked']:,}")
    print(f"engine/dataset reachable      {reply['reachable_forbidden'] or 'none'}")
    print(f"open file descriptors         {reply['open_fds']}  "
          f"(0,1,2 plus the two pipe ends)")
    print("src.* modules the child loaded:")
    for name in reply["src_modules"]:
        print(f"   {name}")
    (pathlib.Path(__file__).parent / "child-imports.json").write_text(
        json.dumps(reply, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
