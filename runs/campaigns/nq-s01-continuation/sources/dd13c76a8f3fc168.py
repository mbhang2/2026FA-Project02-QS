"""Does an outsized move inside a session continue, or give it back?

Eleventh proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092
- #6 adaptive percentile continuation: -0.0196
- #7 two-bar confirmation: +0.4534
- #8 trailing stop with stronger confirmation: -0.1915
- #9 #7 with wider bracket: +0.1324

#7 remains the best. #9 widened the bracket and lost ground, suggesting the
original 0.75/1.5 bracket was well-tuned.

This version keeps #7's core exactly but improves the signal quality by
requiring the confirmation bar to make a new session high (for longs) or low
(for shorts) beyond the outsized bar's extreme. This is a stronger confirmation
than merely closing in the same direction, which should filter out weak
continuations while preserving the best trades.

Changes from #7:
1. Confirmation requires the second bar to trade beyond the outsized bar's
   high (longs) or low (shorts), not just close in the same direction.
2. No other changes: same percentile, same bracket, same early-session filter.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with breakout confirmation."""

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
        self._pending_extreme = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []
            self._pending_direction = 0
            self._pending_extreme = 0.0

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
            # Confirmation: same direction AND breakout beyond pending extreme.
            if direction == self._pending_direction:
                if direction > 0 and current.high > self._pending_extreme:
                    if len(bars) <= int(self.max_session_fraction * 500):
                        stop = rng * self.stop_mult
                        target = rng * self.target_mult
                        ctx.buy(size=self.size, stop_offset=stop,
                                target_offset=target,
                                tag="confirmed-breakout-long")
                elif direction < 0 and current.low < self._pending_extreme:
                    if len(bars) <= int(self.max_session_fraction * 500):
                        stop = rng * self.stop_mult
                        target = rng * self.target_mult
                        ctx.sell(size=self.size, stop_offset=stop,
                                 target_offset=target,
                                 tag="confirmed-breakout-short")
            self._pending_direction = 0
            self._pending_extreme = 0.0
            return

        if ratio >= threshold:
            self._pending_direction = direction
            self._pending_extreme = current.high if direction > 0 else current.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
