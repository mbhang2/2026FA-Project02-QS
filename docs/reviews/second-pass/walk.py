"""Widest possible object-graph walk from each context."""
import sys; sys.path.insert(0,".")
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.types import Split

TARGETS = ("Engine", "SyntheticDataset", "NQDataset", "_SealedDataset", "Blotter",
           "HoldoutLock", "_RunState", "_OrderBook", "IntentQueue", "Order",
           "BarBuilder", "BarHistory")

def walk(root, budget=200_000):
    seen, stack, hits = set(), [("ctx", root)], {}
    while stack and len(seen) < budget:
        path, obj = stack.pop()
        if id(obj) in seen: continue
        seen.add(id(obj))
        tn = type(obj).__name__
        if tn in TARGETS:
            hits.setdefault(tn, path)
            continue
        if isinstance(obj, (str, bytes, int, float, bool, type(None))): continue
        if isinstance(obj, type): continue        # classes, not instances
        for n in dir(obj):
            if n.startswith("__") and n not in (
                "__self__", "__func__", "__wrapped__", "__defaults__",
                "__kwdefaults__", "__dict__", "__closure__"): continue
            try: stack.append((f"{path}.{n}", getattr(obj, n)))
            except Exception: continue
        for c in getattr(obj, "__closure__", None) or ():
            try: stack.append((f"{path}.<cell>", c.cell_contents))
            except ValueError: pass
        if isinstance(obj, (list, tuple, set, frozenset)):
            for i, v in enumerate(list(obj)[:80]): stack.append((f"{path}[{i}]", v))
        if isinstance(obj, dict):
            for k, v in list(obj.items())[:200]: stack.append((f"{path}[{k!r}]", v))
    return hits, len(seen)

out = {}
class W:
    def setup(self, ctx):
        ctx.subscribe("1m"); out["SetupContext"] = walk(ctx)
    def on_bars(self, ctx):
        if "BarContext" not in out: out["BarContext"] = walk(ctx)
        if ctx.actionable and not ctx.position and not ctx.working_order_ids:
            ctx.buy(1, stop_offset=1.0, target_offset=1.0)
    def on_fill(self, ctx):
        if "FillContext" not in out: out["FillContext"] = walk(ctx)
    def on_lifecycle(self, ctx):
        if "LifecycleContext" not in out: out["LifecycleContext"] = walk(ctx)

ds = SyntheticDataset(SyntheticSpec(n_sessions=20, ticks_per_session=120,
                                    vol_per_tick=.15, seed=1, train_frac=.5,
                                    validation_frac=.2))
Engine(ds, W(), split=Split.VALIDATION, warmup_sessions=1).run()
for cls, (hits, n) in out.items():
    print(f"== {cls}  ({n:,} objects visited)")
    for t, path in sorted(hits.items()):
        print(f"     {t:16s} {path}")
