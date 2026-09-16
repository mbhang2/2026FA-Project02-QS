"""B: defeat T+1 by clearing eligible_from_seq on the live Order."""
import sys; sys.path.insert(0,".")
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.types import Split

def run(cheat):
    class S:
        def setup(self, ctx): ctx.subscribe("5m")
        def on_bars(self, ctx):
            if not ctx.actionable: return
            h = ctx.history("5m")
            if len(h) < 1 or ctx.position or ctx.working_order_ids: return
            last = h[-1]
            if last.close == last.open: return
            (ctx.buy if last.close > last.open else ctx.sell)(
                1, stop_offset=0.75, target_offset=0.75, tag="e")
        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx):
            if not cheat: return
            book = ctx._port.working_ids.__self__          # _OrderBook
            for o in book.working.values():
                o.eligible_from_seq = 0                    # T+1 gone
    ds = SyntheticDataset(SyntheticSpec(n_sessions=60, ticks_per_session=390,
                                        vol_per_tick=.15, seed=3, signal_strength=0.0))
    e = Engine.for_test(ds, S(), split=Split.VALIDATION, warmup_sessions=1)
    e.run()
    net = e.blotter.realized_pnl - e.blotter.commission
    return net, len(e.blotter.rows), e.record.blotter_digest[:12]

for cheat in (False, True):
    net, rows, dig = run(cheat)
    print(f"  cheat={cheat!s:5}  net {net:+9.2f}  rows {rows:4d}  digest {dig}")
