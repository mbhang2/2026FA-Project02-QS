"""The reachability walk, executed as the strategy process. Helper, not a test.

Loads `child.py` by file path exactly as `adapter.py` starts it -- importing
`src.core.engine.remote.child` instead would execute `src/core/engine/__init__.py`
in this process and measure the walker's own imports rather than the child's.
"""

import gc
import importlib.util
import os
import sys

REPO, STRATEGY = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)


def by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


here = os.path.join(REPO, "src", "core", "engine", "remote")
protocol = by_path("_remote_protocol", os.path.join(here, "protocol.py"))
child = by_path("_remote_child", os.path.join(here, "child.py"))

strategy = child.load(STRATEGY, "Strategy")
actions = []
setup = child.SetupContext({"dataset_name": "synthetic", "declared": []},
                           actions, lambda *a: [])
strategy.setup(setup)
ctx = child.RemoteContext(
    {"kind": "on_bars", "scalars": {}, "windows": {}, "closed": [], "fills": [],
     "working_orders": [], "now": "1970-01-01T00:00:00"},
    actions, {"bars": {}, "ticks": {"items": [], "total": 0}, "sub_ids": {},
              "ids": None})

FORBIDDEN = ("Engine", "SyntheticDataset", "NQDataset", "Blotter", "HoldoutLock",
             "_SealedDataset", "_RunState", "_OrderBook", "FillRecord",
             "IntentQueue", "_SubResolver")
seen, stack, hits = set(), [("strategy", strategy), ("ctx", ctx), ("setup", setup)], []
while stack and len(seen) < 400_000:
    path, obj = stack.pop()
    if id(obj) in seen:
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
        stack.append((f"{path}.<ref {type(ref).__name__}>", ref))

protocol.send(int(os.environ["REMOTE_WRITE_FD"]), {
    "reply": "done", "ok": True, "actions": [], "hits": hits, "walked": len(seen),
    "src_modules": sorted(m for m in sys.modules if m.startswith("src.")),
})
