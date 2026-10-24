"""One-decision drift strategy for the next trial.

The prior evidence in this campaign strongly favours low turnover: trial #34
holds a position almost continuously and is the best so far at +0.0703 with 38
trades, while higher-turnover variants were deeply negative after costs.

This strategy goes one step further down the turnover curve. In setup() the
whole training split is available through ctx.train_bars(). If that sample has
a persistent drift, the single most robust expression of that drift is to hold
one contract in the direction of the full-sample move from first open to last
close, and then do nothing.

The implementation is intentionally minimal: it enters once at the first
actionable bar of the walk and then holds through session closes with a GTC
market order. No per-session flips, no brackets, no history-dependent feature
that could trigger WindowTooShort or LookaheadError. This keeps the strategy
inside the exposure floor and keeps commission cost at a single entry fill.
"""

from __future__ import annotations


class Strategy:
    """Hold the fitted full-sample drift direction, entered once."""

    def __init__(self, *, interval: str = "1s", size: int = 1) -> None:
        self.interval = interval
        self.size = size
        self.direction = 1

    def setup(self, ctx) -> None:
        # One-second bars get us in as early as possible while still being a
        # bar subscription, so the single entry captures nearly the whole
        # sampled drift.
        ctx.subscribe(self.interval)

        try:
            bars = tuple(ctx.train_bars(self.interval))
            if len(bars) >= 2:
                self.direction = 1 if bars[-1].close >= bars[0].open else -1
        except Exception:
            # Fall back to long; the only failure mode here is that training
            # bars are unavailable, in which case one position in either
            # direction still keeps us exposed rather than crashed.
            self.direction = 1

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        # One position at a time; once filled, do nothing for the remainder.
        if ctx.position or ctx.working_order_ids:
            return

        if self.direction > 0:
            ctx.buy(size=self.size, tif="GTC", tag="drift-long")
        else:
            ctx.sell(size=self.size, tif="GTC", tag="drift-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
