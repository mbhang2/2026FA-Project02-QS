"""Enter long on the third bar of every session, with a fixed bracket.

The simplest thing that is still a strategy: no feature, no state beyond
"which bar of this session is this". It exists to show the shape -- subscribe
in setup(), act in on_bars(), let the bracket handle the exit.
"""


class Strategy:
    def __init__(self, interval="5m", entry_bar=3, size=1,
                 stop_offset=0.75, target_offset=0.75):
        self.interval = interval
        self.entry_bar = entry_bar
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self._session = None
        self._bars_this_session = 0

    def setup(self, ctx):
        # Declared once, before the walk. There is no way to subscribe later.
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
        self._bars_this_session += 1

        # Warmup sessions deliver real bars but refuse orders. Checking keeps
        # the count of ignored actions in the run record at zero.
        if not ctx.actionable:
            return
        if self._bars_this_session != self.entry_bar:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # A market buy. The bracket is armed by the engine at the instant the
        # entry fills, not at the next callback -- which is the difference
        # between a protected position and an unprotected one.
        ctx.buy(
            size=self.size,
            stop_offset=self.stop_offset,
            target_offset=self.target_offset,
            tag="entry",
        )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
