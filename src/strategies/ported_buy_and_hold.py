"""`nalin_l1.strategy.reference_strategies.BuyAndHold`, ported (Task 40).

The original is three lines:

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=price_history.index)

A full long position from the first bar onward, as a fraction of capital.

**What the translation costs, even here.** The source states a *position for
every bar*; this API takes *orders*, and holding a position is the absence of
one. So the port enters once and then does nothing, and "does nothing" has to be
written as a guard rather than being the default -- `if ctx.position: return`
is doing the work `pd.Series(1.0, ...)` did by construction.

**And `1.0` does not survive.** nalin's position is a fraction of capital in
[-1, 1]; this engine trades integer contracts. `size=1` is not "fully
invested", it is one contract, and there is no expression in this API for the
first. That is a real modelling difference and not a porting detail: a
fraction-of-capital strategy is scale-free and a contract-count strategy is not,
which is exactly the invariance `docs/design/metric.md` screens for. The metric
survives it; the strategy's own meaning does not.

Kept as apparatus, not as a proposal: `runs/` records nothing about this file
unless somebody runs it, and the loop does not.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1) -> None:
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # `actionable` is warmup and the embargo, enforced by the harness. The
        # source API has no equivalent because it has no concept of a run that
        # is under way but not yet allowed to trade.
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        ctx.buy(size=self.size, tag="buy-and-hold")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
