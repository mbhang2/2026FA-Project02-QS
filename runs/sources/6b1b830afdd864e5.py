"""SMOKE ONLY -- absorption on 1m delta, to prove the plumbing before N=50.

Not a candidate and not tuned. It exists to answer four questions before fifty
trials are committed to this hypothesis:

  1. does `bar.delta` exist and vary on real NQ bars,
  2. does a delta-conditioned strategy place and fill orders end to end,
  3. does the engine still jump -- i.e. is `ticks_stepped` small, confirming that
     reading delta off bars costs nothing like `ctx.subscribe("tick")`,
  4. what does one trial cost in wall clock, which sets the campaign's budget.

The rule is the crude form of the hypothesis: a bar with large positive delta
whose close fails to make a new high is buying being absorbed, so fade it; the
mirror for negative delta. Thresholds are round numbers, not fitted.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "1m", lookback: int = 20,
                 delta_mult: float = 2.0, stop: float = 8.0,
                 target: float = 8.0, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.delta_mult = delta_mult
        self.stop = stop
        self.target = target
        self.size = size
        # Smoke instrumentation. Not something a real candidate would carry.
        self.seen = 0
        self.with_delta = 0
        self.unknown = 0
        self.signals = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]
        self.seen += 1
        if last.delta != 0:
            self.with_delta += 1
        self.unknown += last.unknown_volume

        if ctx.position or ctx.working_order_ids:
            return
        if len(history) < self.lookback + 1:
            return

        window = history[-(self.lookback + 1):-1]
        mags = [abs(b.delta) for b in window]
        typical = sum(mags) / len(mags)
        if typical <= 0:
            return
        if abs(last.delta) < self.delta_mult * typical:
            return

        prior_high = max(b.high for b in window)
        prior_low = min(b.low for b in window)

        # Absorption: heavy one-sided flow that fails to extend the range.
        if last.delta > 0 and last.high <= prior_high:
            place = ctx.sell
        elif last.delta < 0 and last.low >= prior_low:
            place = ctx.buy
        else:
            return

        self.signals += 1
        place(size=self.size, stop_offset=self.stop,
              target_offset=self.target, tag="absorption")
