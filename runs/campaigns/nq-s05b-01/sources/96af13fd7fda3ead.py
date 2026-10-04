"""Short-side opening reversal strategy for NQ futures, v7.

This version refines the prior positive-scoring approach (trial #31, +0.1244)
by focusing on a cleaner reversal signal within the opening window.

Key changes from v6:
- Uses a 2-minute bar interval to reduce noise while still acting early.
- Defines the opening high as the highest high of the first 10 bars.
- Requires a close below the opening high after a new high, with a bearish
  bar that closes in the lower half of its range.
- Uses a fixed risk per trade based on the opening range, with a minimum
  range filter to avoid quiet sessions.
- Trades only once per session, short-only, with symmetric 1:1 risk/reward.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_bars: int = 10,
        risk_fraction: float = 0.5,
        min_range: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.risk_fraction = risk_fraction
        self.min_range = min_range
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track the opening high over the first N bars.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need enough bars to establish the opening range.
        if self._bars_seen < self.opening_bars:
            return

        # Only act on the bar immediately after the opening window.
        if self._bars_seen > self.opening_bars + 1:
            return

        # Compute opening range from bars in the window.
        window = history.last(self.opening_bars)
        opening_low = min(b.low for b in window)
        opening_range = self._opening_high - opening_low
        if opening_range < self.min_range:
            return

        # Signal: the current bar closes below the opening high, and is bearish.
        if last.close >= self._opening_high:
            return
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        # Close in the lower half of the bar's range.
        if last.close > (last.high + last.low) / 2:
            return

        # Risk is a fraction of the opening range.
        risk = self.risk_fraction * opening_range
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
