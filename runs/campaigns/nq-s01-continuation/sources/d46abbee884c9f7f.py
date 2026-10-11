"""Does an outsized move inside a session continue, or give it back?

Eighteenth proposal. Prior results:
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
- #14 strength floor: +0.0981
- #15 lower percentile: +0.1272
- #16 pullback confirmation: +0.1048

#12 remains the best. #13-#16 all degraded it. The signal is fragile to
filters and time-window changes.

This version returns to #12's exact configuration but changes the
**confirmation window**. In #12, the confirmation bar must be the very
next bar after the outsized bar. Here we allow the confirmation to occur
within the next **two** bars. This gives the market a little more room
to digest the outsized move before confirming continuation, which may
filter out false signals while retaining the true ones.

Changes from #12:
1. Instead of requiring immediate confirmation on the next bar, we allow
   confirmation on either of the next two bars.
2. All other logic identical to #12: same percentile, same sizing,
   same early-session filter, same 0.75/1.5 bracket.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with two-bar confirmation window."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.90, stop_mult: float = 0.75,
                 target_mult: float = 1.5, min_bars: int = 10,
                 max_session_fraction: float = 0.5,
                 max_size: int = 3):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.max_session_fraction = max_session_fraction
        self.max_size = max_size
        self._session = None
        self._session_bars: list[Bar] = []
        self._session_ratios: list[float] = []
        self._pending_direction = 0
        self._pending_ratio = 0.0
        self._pending_bars_waited = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []
            self._pending_direction = 0
            self._pending_ratio = 0.0
            self._pending_bars_waited = 0

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
            self._pending_bars_waited += 1
            if self._pending_bars_waited > 2:
                # Confirmation window expired without a confirming bar.
                self._pending_direction = 0
                self._pending_ratio = 0.0
                self._pending_bars_waited = 0
                return

            if direction == self._pending_direction:
                if len(bars) <= int(self.max_session_fraction * 500):
                    if threshold > 0:
                        strength = min(self._pending_ratio / threshold, 2.0)
                        size = max(1, min(self.max_size,
                                          int(round(strength))))
                    else:
                        size = self.size
                    stop = rng * self.stop_mult
                    target = rng * self.target_mult
                    if direction > 0:
                        ctx.buy(size=size, stop_offset=stop,
                                target_offset=target,
                                tag="two-bar-window-continuation-long")
                    else:
                        ctx.sell(size=size, stop_offset=stop,
                                 target_offset=target,
                                 tag="two-bar-window-continuation-short")
                self._pending_direction = 0
                self._pending_ratio = 0.0
                self._pending_bars_waited = 0
            return

        if ratio >= threshold:
            self._pending_direction = direction
            self._pending_ratio = ratio
            self._pending_bars_waited = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
