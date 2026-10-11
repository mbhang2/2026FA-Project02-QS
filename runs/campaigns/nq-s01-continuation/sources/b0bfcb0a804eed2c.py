"""Does an outsized move inside a session continue, or give it back?

Fifteenth proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092
- #6 adaptive percentile continuation: -0.0196
- #7 two-bar confirmation: +0.4534
- #8 trailing stop with stronger confirmation: -0.1915
- #9 #7 with wider bracket: +0.1324
- #10 breakout confirmation: +0.3609
- #11 volume-confirmed continuation: +0.3880
- #12 strength-sized continuation: +0.5136
- #13 wider time window: +0.1290

#12 remains the best. #13 widened the time window and lost nearly all the
gain, suggesting the early-session constraint is important.

This version returns to #12's exact configuration but adds a **minimum
strength threshold** for trading. In #12, all signals above the session
percentile are traded, with size scaled by strength. Here we require the
signal strength to be at least 1.5x the current threshold before trading,
and scale size from that floor up to the cap. The idea is to avoid the
weakest signals that barely clear the percentile and may be noise, while
still scaling size for the strong ones that do trade.

Changes from #12:
1. Added `min_strength` parameter (default 1.5). A signal must have
   ratio >= min_strength * threshold to be traded.
2. Size scaling maps strength from min_strength to 2.0 onto [1, max_size].
3. All other logic identical to #12: same percentile, two-bar confirmation,
   early-session filter, 0.75/1.5 bracket.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with strength floor."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.90, stop_mult: float = 0.75,
                 target_mult: float = 1.5, min_bars: int = 10,
                 max_session_fraction: float = 0.5,
                 max_size: int = 3, min_strength: float = 1.5):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.max_session_fraction = max_session_fraction
        self.max_size = max_size
        self.min_strength = min_strength
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
            if direction == self._pending_direction:
                if len(bars) <= int(self.max_session_fraction * 500):
                    # Only trade if the pending ratio is strong enough.
                    if threshold > 0:
                        strength = self._pending_ratio / threshold
                        if strength >= self.min_strength:
                            # Map strength from min_strength to 2.0 onto [1, max_size].
                            scaled = (strength - self.min_strength) / (2.0 - self.min_strength)
                            scaled = max(0.0, min(1.0, scaled))
                            size = max(1, min(self.max_size,
                                              int(round(1 + (self.max_size - 1) * scaled))))
                        else:
                            size = 0
                    else:
                        size = self.size
                    if size > 0:
                        stop = rng * self.stop_mult
                        target = rng * self.target_mult
                        if direction > 0:
                            ctx.buy(size=size, stop_offset=stop,
                                    target_offset=target,
                                    tag="strength-floor-continuation-long")
                        else:
                            ctx.sell(size=size, stop_offset=stop,
                                     target_offset=target,
                                     tag="strength-floor-continuation-short")
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
