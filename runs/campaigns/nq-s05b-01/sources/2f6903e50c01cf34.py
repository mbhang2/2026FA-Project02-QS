"""Short-side opening reversal in NQ futures.

Adapted from the opening-range reversal pattern in Dow futures. The idea:
when the market establishes an early high, sell that high with a symmetric
1:1 stop and target, expecting a reversal driven by hedging pressure.

This implementation:
- Short-only.
- Defines the opening period as the first 30 minutes of the session.
- Uses the first 15 minutes to establish a high. If the price subsequently
  trades below that high, it places a sell stop at the high, expecting the
  reversal to continue lower.
- Symmetric risk/reward: stop and target are equal distances from entry.
"""

from __future__ import annotations


class Strategy:
    """Short-side opening reversal with symmetric 1:1 risk/reward."""

    def __init__(self, *, interval: str = "5m", high_bars: int = 3,
                 entry_bars: int = 6, stop_target: float = 10.0,
                 size: int = 1):
        self.interval = interval
        self.high_bars = high_bars          # bars used to establish the high
        self.entry_bars = entry_bars        # bars within which entry must occur
        self.stop_target = stop_target      # symmetric stop and target distance
        self.size = size

        self._session = None
        self._bars_this_session = 0
        self._opening_high = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None

        self._bars_this_session += 1

        # Only act during the opening period.
        if self._bars_this_session > self.entry_bars:
            return

        # One position at a time.
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.high_bars:
            return

        # Establish the high from the first `high_bars` bars of the session.
        if self._bars_this_session <= self.high_bars:
            session_bars = [b for b in history if b.session == ctx.session]
            if len(session_bars) >= self.high_bars:
                self._opening_high = max(b.high for b in session_bars[-self.high_bars:])
            return

        if self._opening_high is None:
            return

        # After the high is established, sell a stop at the high if price
        # has moved below it (reversal signal).
        if ctx.last_price < self._opening_high:
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=self._opening_high,
                stop_offset=self.stop_target,
                target_offset=self.stop_target,
                tag="opening-reversal-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
