"""The agent's file. Everything else in the repo is immutable to it.

Right now it holds the dumbest strategy that can express the planted signal: buy
the next interval if the last one closed up, sell if it closed down, with a
bracket so the position is protected from the instant it fills rather than from
the next callback. On pure-noise data it has no edge by construction, which is
what makes the null half of calibration meaningful.

Start with `docs/agent/strategy-api.md`: the callbacks, the orders, the timing
rules, what fails and why, and five worked examples that are run in CI. What you
can subscribe to and what the fields mean is `docs/agent/data.md`, generated from
the manifest. `docs/design/context-api.md` is the generated per-attribute
reference for `ctx`.
"""

from __future__ import annotations


class MomentumStrategy:
    """Follow the last closed bar. A placeholder, and the calibration reference."""

    def __init__(self, *, interval: str = "5m", stop: float = 0.75,
                 target: float = 0.75, size: int = 1):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.position or ctx.working_order_ids:
            return  # one position at a time; the bracket does the exits
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]      # the bar that just closed; history[len] would raise
        if last.close == last.open:
            return
        place = ctx.buy if last.close > last.open else ctx.sell
        place(
            size=self.size,
            stop_offset=self.stop,
            target_offset=self.target,
            tag="entry",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
