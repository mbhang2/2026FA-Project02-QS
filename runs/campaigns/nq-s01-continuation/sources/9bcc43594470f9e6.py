"""Does an outsized move inside a session continue, or give it back?

Twelfth proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092
- #6 adaptive percentile continuation: -0.0196
- #7 two-bar confirmation: +0.4534
- #8 trailing stop with stronger confirmation: -0.1915
- #9 #7 with wider bracket: +0.1324
- #10 breakout confirmation: +0.3609

#7 remains the best. #10 added a breakout requirement to the confirmation bar
and lost a little ground (+0.36 vs +0.45), suggesting the simpler same-direction
confirmation is better.

This version keeps #7's core exactly but adds a **volume filter** to the
confirmation bar. The idea: an outsized move that continues should be
accompanied by strong participation. If the confirmation bar closes in the same
direction but on very low volume, the continuation is likely weak.

Changes from #7:
1. Require the confirmation bar's volume to be at least the median volume of
   the session so far.
2. No other changes: same percentile, same bracket, same early-session filter.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with volume-confirmed follow-through."""

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

        if self._pending_direction != 0:
            # Confirm the pending signal with this bar's direction and volume.
            if direction == self._pending_direction:
                # Volume filter: confirmation bar must have at least median
                # volume of the session so far.
                volumes = sorted(b.volume for b in bars)
                median_volume = volumes[len(volumes) // 2]
                if current.volume >= median_volume:
                    # Only enter if still within the first half of the session.
                    if len(bars) <= int(self.max_session_fraction * 500):
                        stop = rng * self.stop_mult
                        target = rng * self.target_mult
                        if direction > 0:
                            ctx.buy(size=self.size, stop_offset=stop,
                                    target_offset=target,
                                    tag="volume-confirmed-continuation-long")
                        else:
                            ctx.sell(size=self.size, stop_offset=stop,
                                     target_offset=target,
                                     tag="volume-confirmed-continuation-short")
            self._pending_direction = 0
            self._pending_ratio = 0.0
            return

        if ratio >= threshold:
            self._pending_direction = direction
            self._pending_ratio = ratio

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
