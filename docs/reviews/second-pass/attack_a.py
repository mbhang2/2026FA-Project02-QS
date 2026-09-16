"""A: the holdout, re-opened through SetupContext._session_bounds.__self__."""
import sys; sys.path.insert(0,".")
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.types import Split

got = {}
class Attack:
    def setup(self, ctx):
        ctx.subscribe("1m")
        d = ctx._session_bounds.__self__          # sealed? no: the raw dataset
        got["type"] = type(d).__name__
        got["has_unlock"] = hasattr(d, "unlock_holdout")
        try:
            d.unlock_holdout("adversarial review, second pass")
            t = next(iter(d.ticks(Split.HOLDOUT)))
            got["holdout_tick"] = (t.session, t.price)
        except Exception as e:
            got["holdout_tick"] = f"{type(e).__name__}: {e}"
        got["calendar"] = str(d.calendar())[:60]
        got["seed"] = d.spec.seed
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass

ds = SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120,
                                    vol_per_tick=.15, seed=7))
Engine.for_test(ds, Attack(), split=Split.VALIDATION, warmup_sessions=1).run()
for k, v in got.items(): print(f"  {k}: {v}")
