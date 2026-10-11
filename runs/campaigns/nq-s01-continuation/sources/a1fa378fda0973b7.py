"""Does an outsized move inside a session continue, or give it back?

Eighth proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092
- #6 adaptive percentile continuation: -0.0196

The best two (#1, #4) both measure an outsized move against recent range and
trade continuation. #5 and #6 added filters that cut exposure and hurt.

This version returns to the core #1/#4 structure but changes the signal
slightly to reduce false signals without destroying exposure:

1. **Two-bar confirmation.** Instead of acting on a single outsized bar, we
   require the outsized bar to be followed by a bar that closes in the same
   direction. This filters one-bar spikes that immediately reverse.

2. **Range-based entry timing.** We only enter if the outsized move occurs
   in the first half of the session (by bar count), avoiding late-session
   noise where continuation is less reliable.

3. **Fixed bracket with tighter stop.** Stop at 0.75x session range, target
   at 1.5x, keeping the favorable risk/reward from #1 but slightly tighter
   to reduce loss per trade.

The signal remains: a 5m bar whose body is large relative to the session's
realized range so far, entered in the direction of the bar.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with two-bar confirmation."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.90, stop_mult: float = 0.75,
                 target_mult: float = 1.5, min_bars: int = 10,
                 max_session_fraction: float = 0.5):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.max_session_fraction = max_session_fraction
        self._session = None
        self._session_bars: list[Bar] = []
        self._session_ratios: list[float] = []
        self._pending_direction = 0
        self._pending_ratio = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []
            self._pending_direction = 0
            self._pending_ratio = 0.0

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]
        if last.session != ctx.session:
            return
        self._session_bars.append(last)

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bars = self._session_bars
        if len(bars) < self.min_bars:
            return

        current = bars[-1]
        if current.close == current.open:
            return

        prior = bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        move = abs(current.close - current.open)
        ratio = move / rng
        self._session_ratios.append(ratio)

        if len(self._session_ratios) < 5:
            return

        sorted_ratios = sorted(self._session_ratios)
        idx = int(self.percentile * (len(sorted_ratios) - 1))
        threshold = sorted_ratios[idx]

        direction = 1 if current.close > current.open else -1

        # Check if we have a pending outsized bar from the previous bar.
        if self._pending_direction != 0:
            # Confirm the pending signal with this bar's direction.
            if direction == self._pending_direction:
                # Only enter if still within the first half of the session.
                if len(bars) <= int(self.max_session_fraction * 500):
                    stop = rng * self.stop_mult
                    target = rng * self.target_mult
                    if direction > 0:
                        ctx.buy(size=self.size, stop_offset=stop,
                                target_offset=target,
                                tag="confirmed-continuation-long")
                    else:
                        ctx.sell(size=self.size, stop_offset=stop,
                                 target_offset=target,
                                 tag="confirmed-continuation-short")
            self._pending_direction = 0
            self._pending_ratio = 0.0
            return

        # Check if this bar is outsized.
        if ratio >= threshold:
            self._pending_direction = direction
            self._pending_ratio = ratio

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
