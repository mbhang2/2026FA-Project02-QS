"""A fitted strategy: the threshold comes from the fit window, not from me.

Accumulates absolute delta through the fit window the way warmup accumulates,
fits one number at its edge, and trades that number afterwards. The smallest
thing that is genuinely fitted -- no library, no training loop, no
hyperparameter.
"""


class Strategy:
    def __init__(self):
        self.seen = []
        self.threshold = None

    def setup(self, ctx):
        ctx.subscribe("5m")

    def on_bars(self, ctx):
        history = ctx.history("5m")
        if not history:
            return
        bar = history[-1]
        # Accumulate on every bar, fit window included. Orders here are ignored
        # until the window is past, the same as during warmup.
        self.seen.append(abs(bar.delta))

        if self.threshold is None or not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if abs(bar.delta) < self.threshold:
            return
        if bar.delta > 0:
            ctx.buy(size=1, stop_offset=4.0, target_offset=4.0, tag="fitted-long")
        else:
            ctx.sell(size=1, stop_offset=4.0, target_offset=4.0, tag="fitted-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass

    def fit(self):
        """One parameter, from what was accumulated. Called once, by the engine."""
        if not self.seen:
            raise RuntimeError("fit() ran with nothing accumulated")
        ordered = sorted(self.seen)
        self.threshold = float(ordered[int(0.8 * (len(ordered) - 1))])

    def save_weights(self) -> bytes:
        """The parameters, not the accumulation buffer."""
        return repr(self.threshold).encode()

    def load_weights(self, blob: bytes) -> None:
        self.threshold = float(blob)
