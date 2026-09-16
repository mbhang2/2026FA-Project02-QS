"""I: the IntentQueue is reachable. Inject actions past every validation."""
import sys; sys.path.insert(0,".")
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.types import Split, Submit, Side, OrderType, TimeInForce

seen = {}
class S:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        if not ctx.actionable or "done" in seen: return
        q = ctx._queue
        seen["cap_before"] = q.cap
        q.cap = 10_000                                  # the action cap, raised
        # a Submit the context would have refused: size 0 is rejected by _order()
        for _ in range(200):
            q.actions.append((Submit(side=Side.BUY, size=250,
                                     order_type=OrderType.MARKET,
                                     tif=TimeInForce.DAY, tag="injected"),
                              q._alloc()))
        seen["done"] = True
        seen["queued"] = len(q.actions)
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass

ds = SyntheticDataset(SyntheticSpec(n_sessions=200, ticks_per_session=390,
                                    vol_per_tick=.15, seed=3))
e = Engine.for_test(ds, S(), split=Split.VALIDATION, warmup_sessions=1)
r = e.run()
print(f"  action cap was {seen['cap_before']}, raised in place; {seen['queued']} actions queued")
print(f"  cap hits recorded: {r.action_cap_hits}   actions dropped: {r.actions_dropped}")
print(f"  fills {r.fills}   |max position| {max(abs(v) for v in r.end_of_session_positions.values())}")
print(f"  net {e.blotter.realized_pnl - e.blotter.commission:+.2f}")
