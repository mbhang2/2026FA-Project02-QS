"""C: rewrite the entry price the engine settles P&L against."""
import sys; sys.path.insert(0,".")
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.scoring.scorer import score
from src.core.types import Split

def run(cheat):
    class S:
        def setup(self, ctx): ctx.subscribe("5m")
        def on_bars(self, ctx):
            if not ctx.actionable: return
            if not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tag="e"); return
            if ctx.position > 0:
                if cheat:
                    st = ctx._port.session_bounds.__closure__[0].cell_contents
                    st.avg_price = ctx.last_price - 5.0   # "I bought 5 lower"
                ctx.close()
        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass
    ds = SyntheticDataset(SyntheticSpec(n_sessions=200, ticks_per_session=390,
                                        vol_per_tick=.15, seed=3))
    e = Engine(ds, S(), split=Split.VALIDATION, warmup_sessions=1)
    e.run()
    s = score(e.blotter, period=e.session_calendar())
    return (e.blotter.realized_pnl - e.blotter.commission, len(e.blotter.rows),
            s.primary, s.significance)

for cheat in (False, True):
    net, rows, sortino, sig = run(cheat)
    print(f"  cheat={cheat!s:5}  net {net:+10.2f}  rows {rows:4d}  "
          f"sortino {sortino:+8.4f}  significance {sig:+8.3f}")
