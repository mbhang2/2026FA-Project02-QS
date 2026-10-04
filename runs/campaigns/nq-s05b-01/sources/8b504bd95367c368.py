"""Short-side opening reversal strategy for NQ futures, v4.

The prior versions have all produced negative scores. The best so far was
#18 at -0.0311 with 313 trades, using a 30-second interval, a 15-minute
opening window, and a simple high-breakout signal with an ATR-based stop.

This version keeps the structure that worked best but tightens the entry
conditions further to reduce the number of losing trades. The key changes:

1. Use a 15-minute opening window (the best performer in prior trials).
2. Use a 30-second interval for precise timing.
3. Require a pullback after the breakout: after a bar closes above the
   prior lookback high, wait for a bar that closes below the previous
   bar's low before entering short. This avoids chasing the breakout and
   enters on the first sign of reversal.
4. Keep the symmetric 1:1 stop/target with risk based on ATR.
5. Only trade once per session.

The pullback filter is designed to improve the win rate by avoiding
entries at the extreme of the initial push, which is where many prior
trades likely got stopped out.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 15,
        lookback: int = 10,
        atr_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._recent_highs: deque[float] = deque(maxlen=lookback)
        self._recent_ranges: deque[float] = deque(maxlen=lookback)
        self._breakout_detected = False
        self._breakout_high = 0.0
        self._prev_bar_low = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._recent_highs.clear()
            self._recent_ranges.clear()
            self._breakout_detected = False
            self._breakout_high = 0.0
            self._prev_bar_low = None

        if self._traded_this_session:
            return

        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        last = history[-1]
        prev = history[-2]

        # Update ring buffers with the previous bar's data.
        self._recent_highs.append(prev.high)
        self._recent_ranges.append(prev.high - prev.low)

        if not self._breakout_detected:
            # Look for a breakout: close above the prior lookback high.
            if len(self._recent_highs) == self.lookback:
                prior_high = max(self._recent_highs)
                if last.close > prior_high and last.close > last.open:
                    self._breakout_detected = True
                    self._breakout_high = last.high
                    self._prev_bar_low = last.low
            return

        # We have a breakout; wait for a pullback bar that closes below the
        # previous bar's low.
        if self._prev_bar_low is not None and last.close < self._prev_bar_low:
            # Enter short on the pullback.
            avg_range = (
                sum(self._recent_ranges) / len(self._recent_ranges)
                if self._recent_ranges
                else 0.0
            )
            risk = self.atr_mult * avg_range if avg_range > 0 else 1.0
            if risk <= 0:
                risk = 1.0
            ctx.sell(
                size=self.size,
                stop_offset=risk,
                target_offset=risk,
                tag="opening-reversal-short",
            )
            self._traded_this_session = True
            return

        # Update the previous bar low for the next pullback check.
        self._prev_bar_low = last.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
